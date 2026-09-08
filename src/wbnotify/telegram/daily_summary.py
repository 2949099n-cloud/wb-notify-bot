"""Ежедневная сводка по кабинету — одно закреплённое сообщение в чате.

Отправляется утром и подводит итог ПРОШЕДШЕГО дня: за сегодня цифры ещё
неполные, а закреплённая сводка должна быть окончательной.

Каждое утро сообщение отправляется заново и закрепляется вместо вчерашнего:
редактировать старое нельзя — оно уедет вверх ленты за ночными уведомлениями,
и его никто не увидит. Прошлое открепляем, чтобы в шапке чата всегда висела
ровно одна, свежая сводка.

Источники цифр те же, что в блоке аналитики уведомлений (`calc/metrics.py`),
поэтому сводка и уведомления не расходятся между собой: заказы — из воронки
продаж (совпадает со «сводкой на главной» в ЛК), выкупы и возвраты — из наших
`sales`, отмены — из `orders.cancel_date`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from telegram import Bot
from telegram.error import TelegramError

from wbnotify import members_repo
from wbnotify.calc import day_economics
from wbnotify.calc.metrics import order_velocity, stock_eta, yesterday_today_breakdown
from wbnotify.counters import now_msk
from wbnotify.db import utcnow
from wbnotify.models import ShopRow
from wbnotify.telegram.formatters import PARSE_MODE, _esc
from wbnotify.telegram.keyboards import WB_CARD_URL

TOP_ITEMS = 5
RULE = "━━━━━━━━━━━━━━━━━━━"
# Типографский минус: обычный дефис в столбце цифр читается как перенос.
MINUS = "−"
# Ниже какого запаса товар попадает в блок «заканчивается».
LOW_STOCK_DAYS = 7
LOW_STOCK_ITEMS = 5

logger = logging.getLogger(__name__)


def _rub(value: float) -> str:
    """Суммы в сводке — целыми рублями и с пробелом между тысячами: это итог дня,
    его читают глазами, а копейки и слитные «125951» только мешают. В самих
    уведомлениях формат прежний (`_fmt_money`), его менять нельзя."""
    return f"{round(value):,}".replace(",", " ") + " ₽"


def _qty_sum(stats: dict) -> str:
    """Ключ суммы — `amount`: так его называет calc.metrics, оттуда цифры и берутся."""
    return f"{stats['qty']} шт на {_rub(stats['amount'])}"


def _cancels(conn: sqlite3.Connection, shop_id: int, date_str: str) -> dict:
    """Отмены берём по `orders.cancel_date`, а не по возвратам в `sales`.

    В уведомлении «Отмена заказа» строка «Вчера/Сегодня» намеренно считается по
    возвратам (решение пользователя), но для итога дня нужны именно отменённые
    заказы — иначе сводка не сойдётся с тем, что человек видит в кабинете.
    """
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(price_with_disc), 0) FROM orders "
        "WHERE shop_id=? AND is_cancel=1 AND cancel_date >= ? AND cancel_date < ?",
        (shop_id, date_str, date_str + "T23:59:59.999999"),
    ).fetchone()
    return {"qty": row[0], "amount": row[1] or 0}


def _stock_eta_for_article(conn: sqlite3.Connection, shop_id: int, nm_id: int) -> tuple[int, str]:
    """На сколько дней хватит остатка ПО АРТИКУЛУ целиком.

    Не переиспользуем `stock_days_aggregate`: она считает по конкретному размеру
    (ей нужен tech_size), а в сводке речь про товар целиком — как и скорость
    заказов, которая тоже считается по артикулу.
    """
    qty = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM stocks_current WHERE shop_id = ? AND nm_id = ?",
        (shop_id, nm_id),
    ).fetchone()[0]
    return qty, stock_eta(qty, order_velocity(conn, shop_id, nm_id, "", 7))


def _low_stock(conn: sqlite3.Connection, shop_id: int) -> list[tuple[sqlite3.Row, int, float]]:
    """Товары, которые скоро кончатся. Смотрим только те, что заказывали за
    последние 7 дней: у остальных запас «на ∞ дней» и предупреждать не о чем."""
    recent = conn.execute(
        """
        SELECT DISTINCT o.nm_id, COALESCE(c.name, '') AS name,
               COALESCE(o.supplier_article, '') AS article
        FROM orders o
        LEFT JOIN cards_cache c ON c.shop_id = o.shop_id AND c.nm_id = o.nm_id
        WHERE o.shop_id = ? AND o.date >= ?
        """,
        (shop_id, (now_msk() - timedelta(days=7)).strftime("%Y-%m-%d")),
    ).fetchall()

    low = []
    for row in recent:
        qty, eta = _stock_eta_for_article(conn, shop_id, row["nm_id"])
        if eta == "∞":
            continue
        try:
            days = float(eta)
        except (TypeError, ValueError):
            continue
        if days <= LOW_STOCK_DAYS:
            low.append((row, qty, days))

    low.sort(key=lambda item: item[2])
    return [(row, qty, days) for row, qty, days in low[:LOW_STOCK_ITEMS]]


def _delta_line(current: dict, previous: dict, higher_is_better: bool) -> str:
    """Строка сравнения с предыдущим днём: «🟢 +5 шт. · 🟢 +20 172 ₽ (+9.2%)».

    Цвет — по смыслу, а не по знаку: рост отмен и возвратов красный, рост
    заказов и продаж зелёный.
    """
    d_qty = current["qty"] - previous["qty"]
    d_amount = current["amount"] - previous["amount"]
    pct = (d_amount / previous["amount"] * 100) if previous["amount"] else None

    def mark(value: float) -> str:
        if value == 0:
            return "⚪️"
        good = value > 0 if higher_is_better else value < 0
        return "🟢" if good else "🔴"

    pct_text = f" ({_signed_pct(pct)})" if pct is not None else ""
    return f"   {mark(d_qty)} {_signed_qty(d_qty)} · {mark(d_amount)} {_signed_rub(d_amount)}{pct_text}"


def _signed_qty(value: float) -> str:
    return f"{'+' if value > 0 else MINUS if value < 0 else ''}{abs(round(value))} шт."


def _signed_rub(value: float) -> str:
    return f"{'+' if value > 0 else MINUS if value < 0 else ''}{_rub(abs(value))}"


def _signed_pct(value: float) -> str:
    return f"{'+' if value > 0 else MINUS if value < 0 else ''}{abs(value):.1f}%"


def _days(value: float) -> str:
    """«1 день», «2 дня», «5 дней», «2.5 дня» — по правилам русского языка.
    Без этого в колонке остатков стояло бы «на 1 дней»."""
    if value != int(value):
        return f"{value:g} дня"
    number = int(value)
    if 11 <= number % 100 <= 14:
        return f"{number} дней"
    last = number % 10
    if last == 1:
        return f"{number} день"
    if 2 <= last <= 4:
        return f"{number} дня"
    return f"{number} дней"


def _wb_link(nm_id: int) -> str:
    return f'<a href="{WB_CARD_URL.format(nm_id=nm_id)}">{nm_id}</a>'


def _top_lines(title: str, items: list, value_of) -> list[str]:
    ranked = sorted(items, key=value_of, reverse=True)[:TOP_ITEMS]
    if not ranked:
        return []
    lines = [title]
    for place, item in enumerate(ranked, start=1):
        article = f" · {_esc(item.article)}" if item.article else ""
        lines.append(f"{place}) {_wb_link(item.nm_id)}{article}: {_rub(value_of(item))}")
    return lines


def build_summary(conn: sqlite3.Connection, shop: ShopRow, date_str: str) -> str:
    """`date_str` — день в МСК (ГГГГ-ММ-ДД), за который подводится итог."""
    day = datetime.strptime(date_str, "%Y-%m-%d")
    # yesterday_total у breakdown — это день ПЕРЕД asof, поэтому за нужный день
    # спрашиваем «завтра», а за предыдущий — сам день.
    asof_day = (day + timedelta(days=1)).strftime("%Y-%m-%dT12:00:00")
    asof_prev = day.strftime("%Y-%m-%dT12:00:00")
    prev_date = (day - timedelta(days=1)).strftime("%Y-%m-%d")

    def totals(event_type: str, asof: str) -> dict:
        return yesterday_today_breakdown(conn, shop.id, asof=asof, event_type=event_type)["yesterday_total"]

    blocks = [
        ("📦 Заказы", totals("order", asof_day), totals("order", asof_prev), True),
        ("❌ Отмены", _cancels(conn, shop.id, date_str), _cancels(conn, shop.id, prev_date), False),
        ("✅ Продажи", totals("buyout", asof_day), totals("buyout", asof_prev), True),
        ("🔄 Возвраты", totals("return", asof_day), totals("return", asof_prev), False),
    ]

    pretty_date = day.strftime("%d.%m.%Y")
    lines = [f"📊 <b>Сводка за {pretty_date}</b>", RULE]
    for title, current, previous, higher_is_better in blocks:
        lines.append(f"{title}: <b>{current['qty']} шт. · {_rub(current['amount'])}</b>")
        lines.append(_delta_line(current, previous, higher_is_better))

    # ПРО РАЗРЫВ МЕЖДУ ИСТОЧНИКАМИ WB В СВОДКЕ НЕ ПИШЕМ (решение пользователя).
    # Факт остаётся фактом: воронка (её же цифры показывает приложение WB) даёт
    # заказов больше, чем поштучная выдача statistics/orders, из которой только и
    # можно собрать уведомление. Проверено на устоявшихся днях: 46 против 35,
    # 48 против 37 — это не задержка. Пояснение в сводке пробовали, пользователь
    # попросил убрать: сводка — про цифры дня, а не про наши ограничения.
    # Разрыв по-прежнему смотрится командой sync_cli.py compare.

    economics = day_economics.for_day(conn, shop.id, date_str)
    previous_economics = day_economics.for_day(conn, shop.id, prev_date)
    pct = economics.commission_pct
    lines += [
        "",
        f"💰 Комиссия: {_rub(economics.commission)}" + (f" ({pct:.1f}%)" if pct is not None else ""),
        f"🚚 Логистика: {_rub(economics.logistics)} "
        f"(прямая {_rub(economics.logistics_direct)} + возвратная {_rub(economics.logistics_return)})",
        "",
        f"💵 <b>Операционная прибыль: {_rub(economics.profit)}</b>",
        _delta_line(
            {"qty": 0, "amount": economics.profit},
            {"qty": 0, "amount": previous_economics.profit},
            higher_is_better=True,
        ).replace("⚪️ 0 шт. · ", ""),
    ]

    for title, value_of in (
        ("🔝 <b>Топ-5 по выручке:</b>", lambda i: i.revenue),
        ("🏆 <b>Топ-5 по прибыли:</b>", lambda i: i.profit),
    ):
        top = _top_lines(title, economics.items, value_of)
        if top:  # в день без продаж пустой заголовок оставил бы дыру в тексте
            lines += [""] + top

    low = _low_stock(conn, shop.id)
    if low:
        lines += ["", f"📦 <b>Заканчивается</b> (меньше {LOW_STOCK_DAYS} дн.):"]
        for item, qty, days in low:
            # Артикул WB и артикул продавца, как в топах: по названию товар не
            # опознать — у соседних карточек они почти одинаковые (решение
            # пользователя). Нулевой запас пишем «на 0 дней», а не словом
            # «закончился», чтобы вся колонка читалась единообразно.
            article = f" · {_esc(item['article'])}" if item["article"] else ""
            lines.append(f"• {_wb_link(item['nm_id'])}{article} — на {_days(days)}")

    return "\n".join(lines)


async def send_for_shop(conn: sqlite3.Connection, bot: Bot, shop: ShopRow, date_str: str) -> int:
    """Отправляет и закрепляет сводку во всех чатах кабинета. Возвращает число
    чатов, куда сводка ушла."""
    text = build_summary(conn, shop, date_str)
    chats = members_repo.recipient_chats(conn, shop.id) or [shop.telegram_chat_id]

    sent = 0
    for chat_id in chats:
        try:
            message = await bot.send_message(chat_id=chat_id, text=text, parse_mode=PARSE_MODE)
        except TelegramError as exc:
            # Недоступный чат одного участника не должен лишать сводки остальных.
            logger.error("shop_id=%s: сводка в чат %s не ушла: %s", shop.id, chat_id, exc)
            continue

        await _repin(conn, bot, chat_id, message.message_id, date_str, text)
        sent += 1
    return sent


async def _repin(
    conn: sqlite3.Connection, bot: Bot, chat_id: int, message_id: int, date_str: str, text: str
) -> None:
    """Закрепляет свежую сводку и снимает вчерашнюю."""
    previous = conn.execute(
        "SELECT message_id FROM daily_summary WHERE chat_id = ? ORDER BY date_msk DESC LIMIT 1",
        (chat_id,),
    ).fetchone()

    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except TelegramError as exc:
        # В группе бот может быть без права закреплять — сводку это не отменяет,
        # она уже отправлена обычным сообщением.
        logger.warning("Не удалось закрепить сводку в чате %s: %s", chat_id, exc)

    if previous and previous["message_id"] and previous["message_id"] != message_id:
        try:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=previous["message_id"])
        except TelegramError as exc:
            logger.info("Прошлая сводка в чате %s не откреплена: %s", chat_id, exc)

    conn.execute(
        """
        INSERT INTO daily_summary (chat_id, date_msk, message_id, stats_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, date_msk) DO UPDATE SET
            message_id = excluded.message_id,
            stats_json = excluded.stats_json,
            updated_at = excluded.updated_at
        """,
        (chat_id, date_str, message_id, json.dumps({"text": text}, ensure_ascii=False), utcnow()),
    )
    conn.commit()
