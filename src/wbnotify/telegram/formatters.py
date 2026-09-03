"""Точные шаблоны уведомлений (Заказ/Отмена/Выкуп + блок аналитики карточки).

ВАЖНО (см. CLAUDE.md, правило 3): текст/эмодзи/порядок полей менять нельзя без
явного запроса пользователя — формат утверждён и должен совпадать с
референсным ботом FenixWB дословно.

Комиссия WB — точная, из tariffs_cache (kgvpSupplier/kgvpMarketplace по
subject+warehouseType заказа). Логистика WB/Тариф/ИЛ — тариф на доставку тоже
берётся из tariffs_cache, но конкретной официально задокументированной формулы
"тариф -> итоговая сумма логистики" WB не публикует; используется общепринятая
оценка (база + доп.литры × коэффициент), см. _estimate_logistics_sum — это
ЛУЧШЕЕ ДОСТУПНОЕ ПРИБЛИЖЕНИЕ, а не гарантированно точная цифра, в отличие от
% комиссии. "Обратная логистика WB" (в «Отмена») и Оценка/Отзывы, для которых
нет карточки в card_analytics_cache, — по-прежнему "—" (см. CLAUDE.md).
"""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime

from wbnotify.calc.metrics import (
    buyout_rate_with_returns,
    order_velocity,
    sales_count,
    stock_days_aggregate,
    yesterday_today_breakdown,
)
from wbnotify.calc.fbs_shipping import shipping_adjustment, shipping_hours_for_order
from wbnotify.calc.tariffs import get_box_tariff, get_commission_pct, get_return_logistics_sum, is_fbw
from wbnotify.models import ShopRow

PLACEHOLDER = "—"
CAPTION_LIMIT = 1024

# Сообщения уходят с parse_mode=HTML (нужен жирный «FBS» в строке склада),
# поэтому ЛЮБОЙ подставляемый из данных текст обязан проходить через _esc:
# в названиях товаров и складов реально встречаются & < >, без экранирования
# Telegram отвергнет сообщение целиком.
PARSE_MODE = "HTML"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=False)

# ИЛ (индекс локализации) временно отменён Wildberries. Не удаляем строку и
# логику — показываем 0, чтобы при возврате ИЛ достаточно было заменить
# источник значения (решение пользователя, см. CLAUDE.md).
_IL_DISABLED_VALUE = "0"

# Окна для оценки «на сколько хватит» в «Остатках подробно» — три сразу
# (решение пользователя), а не одно дефолтное, как в блоке уведомления.
STOCK_WINDOWS = (7, 30, 60)


def _fmt_money(value: float | None) -> str:
    if value is None:
        return PLACEHOLDER
    return f"{int(value)}" if value == int(value) else f"{value:.2f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return PLACEHOLDER
    return f"{int(value)}" if value == int(value) else f"{value:.1f}"


def _fmt_dt(iso_str: str | None) -> str:
    if not iso_str:
        return PLACEHOLDER
    dt = datetime.fromisoformat(iso_str)
    return dt.strftime("%d.%m.%Y %H:%M")


def _days_between(later_iso: str, earlier_iso: str) -> int:
    """Разница в сутках, никогда не отрицательная.

    WB отдаёт cancelDate только с точностью до даты (время всегда T00:00:00),
    тогда как order.date — с точностью до секунды. При отмене день в день это
    даёт формально "отрицательную" разницу (cancelDate 00:00 раньше, чем order
    16:17 того же дня) — проверено на реальных данных. 0 суток — корректный
    ответ для "отменили в тот же день", а не отрицательное число.
    """
    later = datetime.fromisoformat(later_iso)
    earlier = datetime.fromisoformat(earlier_iso)
    return max(0, (later - earlier).days)


def _card(conn: sqlite3.Connection, shop_id: int, nm_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cards_cache WHERE shop_id=? AND nm_id=?", (shop_id, nm_id)).fetchone()


def _product_name(card: sqlite3.Row | None) -> str:
    return _esc(card["name"]) if card and card["name"] else PLACEHOLDER


def _shop_line(shop: ShopRow, card: sqlite3.Row | None) -> str:
    """«👑 <бренд> (<продавец>)». Бренд берётся из карточки товара, а не из магазина:
    у одного продавца их несколько (проверено на реальных данных: NILONIL, 0NILONIL,
    0NIL0), и он разный от товара к товару. В скобках — название продавца."""
    brand = (card["brand"] if card and card["brand"] else None) or shop.brand_code or PLACEHOLDER
    return f"👑 {_esc(brand)} ({_esc(shop.name)})"


def _route_line(warehouse_type: str | None, warehouse_name: str | None, region_name: str | None) -> str:
    """Для FBS склад отправки — собственный склад продавца, и подставлять сюда
    название склада WB некорректно (WB отдаёт в этом поле «Склад WB РФ» даже для
    FBS-заказов — проверено на реальных данных). «FBS» выделяем жирным."""
    origin = (
        "<b>FBS</b> склад продавца" if not is_fbw(warehouse_type) else _esc(warehouse_name or PLACEHOLDER)
    )
    return f"🚚 {origin} → {_esc(region_name or PLACEHOLDER)}"


def _header_id_line(
    nm_id: int, supplier_article: str | None, tech_size: str | None, barcode: str | None, card: sqlite3.Row | None
) -> str:
    """"Артикул размера" в спецификации — это артикул ПРОДАВЦА (WB: supplierArticle),
    а не внутренний системный chrtID WB (проверено сверкой с реальными данными и
    референсным ботом — chrtID показывать не нужно, это перепутанное поле).
    Берём supplier_article из самой строки заказа/продажи; если его почему-то нет
    (старые строки до миграции) — фоллбек на vendor_code карточки."""
    article = supplier_article or (card["vendor_code"] if card else None) or PLACEHOLDER
    return (
        f"🆔 {nm_id} / {_esc(article)} / ({_esc(tech_size or PLACEHOLDER)})"
        f"\n🎹 {_esc(barcode or PLACEHOLDER)}"
    )


# В момент ЗАКАЗА скидка/штраф за скорость отгрузки FBS ещё не определены —
# товар физически не отгружен, поставка не закрыта. Корректировка становится
# известна только к выкупу/возврату (решение пользователя) — поэтому применяем
# её только для этих типов событий, а в «Заказ» показываем базовую комиссию.
_SHIPPING_ADJUSTED_EVENTS = ("buyout", "return")


def _commission_block(
    conn: sqlite3.Connection,
    shop_id: int,
    subject: str | None,
    warehouse_type: str | None,
    base_price: float | None,
    srid: str | None,
    event_type: str,
) -> str:
    """Строка «Комиссия WB» + при наличии просрочки — строка «Штраф за отгрузку».

    Для FBS комиссия корректируется скидкой за скорость отгрузки (−5 п.п. до 13ч,
    −3,5 п.п. до 42ч), а просрочка свыше 48ч с 31.08.2026 берётся отдельным
    штрафом в рублях, а не надбавкой к комиссии — поэтому это две разные строки.
    Время отгрузки берётся из Marketplace API (см. calc/fbs_shipping.py); если
    его нет (поставка ещё не закрыта) — показываем базовую комиссию.
    """
    pct = get_commission_pct(conn, subject, warehouse_type)
    if pct is None:
        return f"💳 Комиссия WB: {PLACEHOLDER}"

    penalty_line = ""
    if not is_fbw(warehouse_type) and event_type in _SHIPPING_ADJUSTED_EVENTS:
        hours = shipping_hours_for_order(conn, shop_id, srid)
        adjustment = shipping_adjustment(hours, base_price)
        if adjustment is not None:
            pct = max(0.0, pct - adjustment.discount_pp)
            if adjustment.penalty_amount > 0:
                penalty_line = (
                    f"\n⚠️ Штраф за отгрузку: {_fmt_money(adjustment.penalty_amount)}₽"
                    f" ({adjustment.overdue_hours:.0f} ч просрочки × {adjustment.penalty_rate_pp_per_hour} п.п.)"
                )

    amount = base_price * pct / 100 if base_price is not None else None
    return f"💳 Комиссия WB: {_fmt_pct(pct)}% ({_fmt_money(amount)}₽){penalty_line}"


def _estimate_logistics_sum(box: dict, volume_l: float | None) -> float | None:
    """(база + доп.литры × ставка/л.) × коэффициент склада.

    ВАЖНО: `coef` здесь — это КОЭФФИЦИЕНТ ДОСТАВКИ СКЛАДА (boxDeliveryCoefExpr),
    а НЕ индекс локализации (ИЛ). Это разные вещи: ИЛ временно отменён WB и
    показывается отдельной строкой как 0 (см. _logistics_lines), а коэффициент
    склада продолжает участвовать в расчёте суммы — сумма подтверждена
    пользователем как верная.
    """
    base, liter, coef = box.get("base"), box.get("liter"), box.get("coef")
    if base is None:
        return None
    extra_liters = max(0.0, (volume_l or 1.0) - 1.0)
    sum_before_coef = base + extra_liters * (liter or 0)
    return sum_before_coef * (coef / 100) if coef is not None else sum_before_coef


def _logistics_lines(conn: sqlite3.Connection, order: sqlite3.Row, card: sqlite3.Row | None, label: str) -> str:
    box = get_box_tariff(conn, order["warehouse_name"], order["warehouse_type"])
    volume_l = card["volume_l"] if card else None
    dims = (
        f"{card['dims_l']:.0f}x{card['dims_w']:.0f}x{card['dims_h']:.0f} см. ({volume_l:.2f}л.)"
        if card and card["dims_l"] and card["dims_w"] and card["dims_h"]
        else PLACEHOLDER
    )
    # ИЛ (индекс локализации) временно отменён WB — показываем 0, но строку
    # оставляем и логику не удаляем: если WB вернёт ИЛ, менять нужно будет
    # только источник этого значения (решение пользователя, см. CLAUDE.md).
    il = _IL_DISABLED_VALUE
    if box is None:
        return f"💳 {label}: {PLACEHOLDER}\n   Габариты: {dims}\n   Тариф: {PLACEHOLDER}\n   ИЛ: {il}"

    logistics_sum = _estimate_logistics_sum(box, volume_l)
    tariff = f"{_fmt_money(box['base'])}₽/1л. + {_fmt_money(box['liter'])}₽/доп.л." if box["base"] is not None else PLACEHOLDER
    return f"💳 {label}: {_fmt_money(logistics_sum)}₽\n   Габариты: {dims}\n   Тариф: {tariff}\n   ИЛ: {il}"


def _rating_line(conn: sqlite3.Connection, shop_id: int, nm_id: int) -> tuple[str, str]:
    row = conn.execute(
        "SELECT rating, reviews_count FROM card_analytics_cache WHERE shop_id=? AND nm_id=?", (shop_id, nm_id)
    ).fetchone()
    if row is None:
        return PLACEHOLDER, PLACEHOLDER
    rating = f"{row['rating']:.1f}" if row["rating"] is not None else PLACEHOLDER
    reviews = str(row["reviews_count"]) if row["reviews_count"] is not None else PLACEHOLDER
    return rating, reviews


# На сколько дней хватит остатка: в «Заказ»/«Отмена» считаем по заказам,
# в «Выкуп»/«Возврат» — по продажам (решение пользователя).
_STOCK_BASIS_BY_EVENT_TYPE = {
    "order": ("orders", "по заказам"),
    "cancel": ("orders", "по заказам"),
    "buyout": ("sales", "по продажам"),
    "return": ("sales", "по продажам"),
}


def _stock_lines(
    conn: sqlite3.Connection, shop_id: int, nm_id: int, card: sqlite3.Row | None, event_type: str
) -> str:
    """"Остаток:" — по КАЖДОМУ размеру карточки (не только по размеру текущего
    заказа). В скобках два слагаемых: остаток на складах WB + остаток на своих
    складах продавца (два независимых источника, см. stocks_current.warehouse_kind);
    оценка "на N дн." считается от их суммы."""
    if card is None or not card["sizes_json"]:
        return f"   {PLACEHOLDER}"

    basis, basis_label = _STOCK_BASIS_BY_EVENT_TYPE.get(event_type, _STOCK_BASIS_BY_EVENT_TYPE["order"])
    lines = []
    for size in json.loads(card["sizes_json"]):
        tech_size = size.get("techSize")
        chrt_id = size.get("chrtID")
        wb_qty = seller_qty = 0
        if chrt_id is not None:
            for row in conn.execute(
                """
                SELECT warehouse_kind, COALESCE(SUM(quantity),0) AS qty FROM stocks_current
                WHERE shop_id=? AND nm_id=? AND chrt_id=? GROUP BY warehouse_kind
                """,
                (shop_id, nm_id, chrt_id),
            ):
                if row["warehouse_kind"] == "seller":
                    seller_qty = row["qty"]
                else:
                    wb_qty = row["qty"]
        eta = stock_days_aggregate(conn, shop_id, nm_id, tech_size, window_days=7, basis=basis)
        lines.append(f"   {tech_size} ({wb_qty}шт+{seller_qty}шт) ≈ на {eta} дн. ({basis_label})")
    return "\n".join(lines)


def format_stocks_detail(conn: sqlite3.Connection, shop_id: int, nm_id: int) -> str:
    """«Остатки подробно» — по каждому размеру, с оценкой на 7/30/60 дней.

    ВАЖНО про разбивку по складам. В исходном ТЗ предполагалось расписать остаток
    по каждому складу WB, но проверено вживую на всех трёх методах WB
    (stocks-report/wb-warehouses, /seller-warehouses, /api/v2/stocks-report/offices):
    WB отдаёт остатки складов WB ОДНИМ агрегатом («Склад WB», warehouseId=-999999),
    поле offices во всех ответах пустое. Детализация есть только по складам
    продавца — она приходит из Marketplace API. Поэтому строки ниже — это
    «склады WB одной суммой» + каждый склад продавца отдельно; расписать WB
    подробнее нечем, это ограничение API, а не упрощение с нашей стороны.
    """
    card = _card(conn, shop_id, nm_id)
    if card is None or not card["sizes_json"]:
        return "📦 Остатки на складах:\n\nНет данных по карточке товара."

    lines = ["📦 Остатки на складах:", ""]
    for size in json.loads(card["sizes_json"]):
        tech_size = size.get("techSize")
        chrt_id = size.get("chrtID")
        rows = conn.execute(
            """
            SELECT warehouse_kind, warehouse_name, SUM(quantity) AS qty
            FROM stocks_current
            WHERE shop_id=? AND nm_id=? AND chrt_id=? AND quantity > 0
            GROUP BY warehouse_kind, warehouse_name
            ORDER BY qty DESC
            """,
            (shop_id, nm_id, chrt_id),
        ).fetchall()
        total = sum(r["qty"] for r in rows)

        if not total:
            lines.append(f"🔪 Размер {_esc(tech_size)} (0 шт)")
            lines.append("")
            continue

        etas = "/".join(
            stock_days_aggregate(conn, shop_id, nm_id, tech_size, window_days=w) for w in STOCK_WINDOWS
        )
        lines.append(f"🔪 Размер {_esc(tech_size)} ({total} шт ≈ {etas} дн. за {'/'.join(map(str, STOCK_WINDOWS))} дн.):")
        for row in rows:
            name = "Склады WB" if row["warehouse_kind"] == "wb" else _esc(row["warehouse_name"] or "склад продавца")
            lines.append(f"   • {name}: {row['qty']} шт.")
        lines.append("")

    return "\n".join(lines).rstrip()


def _card_analytics_block(
    conn: sqlite3.Connection, shop_id: int, nm_id: int, tech_size: str | None, asof: str, event_type: str
) -> str:
    """`asof` — дата/время САМОГО СОБЫТИЯ в уведомлении (order.date / cancel_date /
    sale.date), а не момент рендера. Все оконные метрики (% выкупа, скорость,
    продажи, вчера/сегодня) считаются "как если бы сейчас был asof" — иначе для
    исторических/бэкфилленных событий получаются бессмысленные (и совпадающие
    между разными уведомлениями) числа, отвязанные от даты самого события —
    репорт пользователя, см. CLAUDE.md.

    `event_type` выбирает источник "Вчера/Сегодня" (решение пользователя): в
    «Заказ» — заказы, в «Выкуп» — выкупы, в «Отмена заказа»/«Возврат» — возвраты.
    """
    card = _card(conn, shop_id, nm_id)

    buyout_7 = buyout_rate_with_returns(conn, shop_id, nm_id, tech_size, 7, asof)
    buyout_14 = buyout_rate_with_returns(conn, shop_id, nm_id, tech_size, 14, asof)
    buyout_30 = buyout_rate_with_returns(conn, shop_id, nm_id, tech_size, 30, asof)

    vel_7 = order_velocity(conn, shop_id, nm_id, tech_size, 7, asof)
    vel_14 = order_velocity(conn, shop_id, nm_id, tech_size, 14, asof)
    vel_30 = order_velocity(conn, shop_id, nm_id, tech_size, 30, asof)

    sales_7 = sales_count(conn, shop_id, nm_id, tech_size, 7, asof)
    sales_14 = sales_count(conn, shop_id, nm_id, tech_size, 14, asof)
    sales_30 = sales_count(conn, shop_id, nm_id, tech_size, 30, asof)
    sales_60 = sales_count(conn, shop_id, nm_id, tech_size, 60, asof)
    sales_90 = sales_count(conn, shop_id, nm_id, tech_size, 90, asof)

    yt = yesterday_today_breakdown(conn, shop_id, nm_id, tech_size, asof, event_type)
    rating, reviews = _rating_line(conn, shop_id, nm_id)

    # Состав блока зависит от события (решение пользователя): в «Возврате» оценка,
    # отзывы и % выкупа не показываются, в «Продаже» — нет только % выкупа.
    head = ""
    if event_type != "return":
        head += f"⭐ Оценка: {rating}\n💬 Отзывы: {reviews}\n"
    if event_type not in ("return", "buyout"):
        head += (
            f"⚖️ Выкуп/с учётом возврата (7/14/30): "
            f"{_fmt_pct(buyout_7)}% / {_fmt_pct(buyout_14)}% / {_fmt_pct(buyout_30)}%\n"
        )

    # Суммы «вчера/сегодня» для «Отмены» и «Возврата» берутся из одного источника
    # (возвраты, где WB хранит суммы отрицательными), но показываются по-разному:
    # в «Возврате» знак минус сохраняем (деньги уходят), в «Отмене заказа» —
    # показываем просто сумму (решение пользователя).
    def money(value: float | None) -> str:
        if value is not None and event_type == "cancel":
            value = abs(value)
        return _fmt_money(value)

    return (
        f"{head}"
        f"💎 Скорость заказов за 7/14/30 дней: {vel_7:.1f} | {vel_14:.1f} | {vel_30:.1f} шт. в день\n"
        f"📖 Продаж за 7/14/30/60/90 дней: {sales_7} | {sales_14} | {sales_30} | {sales_60} | {sales_90} шт.\n"
        f"📦 Остаток:\n"
        f"{_stock_lines(conn, shop_id, nm_id, card, event_type)}\n\n"
        f"💶 Вчера таких: {yt['yesterday_item']['qty']} на {money(yt['yesterday_item']['amount'])}₽\n"
        f"💶 Вчера всего: {yt['yesterday_total']['qty']} на {money(yt['yesterday_total']['amount'])}₽\n"
        f"💵 Сегодня таких: {yt['today_item']['qty']} на {money(yt['today_item']['amount'])}₽\n"
        f"💵 Сегодня всего: {yt['today_total']['qty']} на {money(yt['today_total']['amount'])}₽"
    )


def format_order_message(conn: sqlite3.Connection, shop: ShopRow, order: sqlite3.Row, daily_seq: int) -> str:
    card = _card(conn, shop.id, order["nm_id"])
    commission = _commission_block(
        conn, shop.id, order["subject"], order["warehouse_type"], order["price_with_disc"], order["srid"], "order"
    )
    header = (
        f"🧾 Заказ [#{daily_seq}] {_fmt_dt(order['date'])}\n\n"
        f"{_shop_line(shop, card)}\n"
        f"📝 Название: {_product_name(card)}\n"
        f"{_header_id_line(order['nm_id'], order['supplier_article'], order['tech_size'], order['barcode'], card)}\n"
        f"{_route_line(order['warehouse_type'], order['warehouse_name'], order['region_name'])}\n"
        f"💰 Цена заказа: {_fmt_money(order['price_with_disc'])}₽\n"
        f"🎁 Изначальная цена: {_fmt_money(order['total_price'])}₽\n"
        f"{commission}\n"
        f"🛍 СПП: {_fmt_pct(order['spp'])}% (Цена для покупателя: {_fmt_money(order['finished_price'])}₽ без учёта WB Кошелька)\n"
        f"{_logistics_lines(conn, order, card, 'Логистика WB')}"
    )
    return f"{header}\n\n{_card_analytics_block(conn, shop.id, order['nm_id'], order['tech_size'], order['date'], 'order')}"


def format_cancel_message(conn: sqlite3.Connection, shop: ShopRow, order: sqlite3.Row, daily_seq: int) -> str:
    card = _card(conn, shop.id, order["nm_id"])
    days = _days_between(order["cancel_date"], order["date"]) if order["cancel_date"] else 0
    header = (
        f"❌ Отмена заказа [#{daily_seq}] {_fmt_dt(order['cancel_date'])}\n\n"
        f"{_shop_line(shop, card)}\n"
        f"📝 Название: {_product_name(card)}\n"
        f"{_header_id_line(order['nm_id'], order['supplier_article'], order['tech_size'], order['barcode'], card)}\n"
        f"📅 Дата заказа: {_fmt_dt(order['date'])}\n"
        f"📅 Дата отмены: {_fmt_dt(order['cancel_date'])}\n"
        f"⏰ {days} суток с даты заказа\n"
        f"{_route_line(order['warehouse_type'], order['warehouse_name'], order['region_name'])}\n"
        f"💰 Цена заказа: {_fmt_money(order['price_with_disc'])}₽\n"
        f"🎁 Изначальная цена: {_fmt_money(order['total_price'])}₽\n"
        f"💳 Обратная логистика WB: {_fmt_money(get_return_logistics_sum(card['volume_l'] if card else None))}₽"
    )
    return f"{header}\n\n{_card_analytics_block(conn, shop.id, order['nm_id'], order['tech_size'], order['cancel_date'], 'cancel')}"


def _find_order_date_by_srid(conn: sqlite3.Connection, shop_id: int, srid: str | None) -> str | None:
    if not srid:
        return None
    row = conn.execute("SELECT date FROM orders WHERE shop_id=? AND srid=?", (shop_id, srid)).fetchone()
    return row["date"] if row else None


def _find_sale_date_by_srid(conn: sqlite3.Connection, shop_id: int, srid: str | None) -> str | None:
    """Дата ПРОДАЖИ по тому же srid — нужна для «Возврата»: возвращают то, что
    ранее выкупили, и в шаблоне показывается срок именно от даты продажи."""
    if not srid:
        return None
    row = conn.execute(
        "SELECT date FROM sales WHERE shop_id=? AND srid=? AND is_return=0 ORDER BY date LIMIT 1",
        (shop_id, srid),
    ).fetchone()
    return row["date"] if row else None


def format_return_message(conn: sqlite3.Connection, shop: ShopRow, sale: sqlite3.Row, daily_seq: int) -> str:
    """«Возврат» — собственный шаблон, не переиспользует «Выкуп» (решение
    пользователя): возврат и отмена заказа — разные события, и у возврата свои
    поля («К возврату», «Дата возврата», срок от даты ПРОДАЖИ, а не от заказа).
    Суммы возврата WB отдаёт отрицательными — так и показываем, знак не убираем.
    """
    card = _card(conn, shop.id, sale["nm_id"])
    sale_date = _find_sale_date_by_srid(conn, shop.id, sale["srid"])
    days = _days_between(sale["date"], sale_date) if sale_date else 0
    commission = _commission_block(
        conn, shop.id, sale["subject"], sale["warehouse_type"], sale["price_with_disc"], sale["srid"], "return"
    )
    header = (
        f"🔄 Возврат [#{daily_seq}] {_fmt_dt(sale['date'])}\n\n"
        f"{_shop_line(shop, card)}\n"
        f"📝 Название: {_product_name(card)}\n"
        f"{_header_id_line(sale['nm_id'], sale['supplier_article'], sale['tech_size'], sale['barcode'], card)}\n"
        f"📅 Дата продажи: {_fmt_dt(sale_date)}\n"
        f"⏰ {days} суток с даты продажи\n"
        f"📅 Дата возврата: {_fmt_dt(sale['date'])}\n"
        f"{_route_line(sale['warehouse_type'], sale['warehouse_name'], sale['region_name'])}\n"
        f"💰 К возврату: {_fmt_money(sale['price_with_disc'])}₽\n"
        f"{commission}\n"
        f"💰 К выплате: {_fmt_money(sale['for_pay'])}₽\n"
        f"💳 Обратная логистика WB: {_fmt_money(get_return_logistics_sum(card['volume_l'] if card else None))}₽"
    )
    return f"{header}\n\n{_card_analytics_block(conn, shop.id, sale['nm_id'], sale['tech_size'], sale['date'], 'return')}"


def format_buyout_message(
    conn: sqlite3.Connection, shop: ShopRow, sale: sqlite3.Row, daily_seq: int, event_type: str = "buyout"
) -> str:
    card = _card(conn, shop.id, sale["nm_id"])
    order_date = _find_order_date_by_srid(conn, shop.id, sale["srid"])
    days = _days_between(sale["date"], order_date) if order_date else 0
    commission = _commission_block(
        conn, shop.id, sale["subject"], sale["warehouse_type"], sale["price_with_disc"], sale["srid"], event_type
    )
    header = (
        f"💵 Продажа [#{daily_seq}] {_fmt_dt(sale['date'])}\n\n"
        f"{_shop_line(shop, card)}\n"
        f"📝 Название: {_product_name(card)}\n"
        f"{_header_id_line(sale['nm_id'], sale['supplier_article'], sale['tech_size'], sale['barcode'], card)}\n"
        f"📅 Дата заказа: {_fmt_dt(order_date)}\n"
        f"📅 Дата продажи: {_fmt_dt(sale['date'])}\n"
        f"⏰ {days} суток с даты заказа\n"
        f"{_route_line(sale['warehouse_type'], sale['warehouse_name'], sale['region_name'])}\n"
        f"💰 Цена продажи: {_fmt_money(sale['price_with_disc'])}₽\n"
        f"🛍 СПП: {_fmt_pct(sale['spp'])}% (Цена для покупателя: {_fmt_money(sale['finished_price'])}₽ без учёта WB Кошелька)\n"
        f"{commission}\n"
        f"💰 К выплате: {_fmt_money(sale['for_pay'])}₽"
    )
    return f"{header}\n\n{_card_analytics_block(conn, shop.id, sale['nm_id'], sale['tech_size'], sale['date'], event_type)}"
