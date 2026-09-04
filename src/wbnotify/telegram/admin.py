"""Панель владельца бота: статистика по кабинетам и переписка с поддержкой.

Доступ только у чата из `TELEGRAM_ADMIN_CHAT_ID` — это не роль внутри кабинета,
а владелец самого бота. Если переменная не задана, панель и поддержка выключены
и честно об этом сообщают, а не молчат.

Поддержка устроена так: сообщение пользователя уходит владельцу с подписью, кто
и по какому кабинету пишет, а `support_threads` запоминает id этого сообщения.
Ответ владельца реплаем на него бот пересылает обратно автору. Личный аккаунт
владельца пользователям при этом не виден.
"""
from __future__ import annotations

import sqlite3

from wbnotify.config import Config
from wbnotify.db import utcnow
from wbnotify.telegram.formatters import _esc

SHOPS_PER_PAGE = 15
# Сколько проблемных кабинетов показать прямо в сводке: остальные — на страницах.
MAX_PROBLEMS_IN_SUMMARY = 10


def is_admin(config: Config, chat_id: int) -> bool:
    return config.telegram_admin_chat_id is not None and chat_id == config.telegram_admin_chat_id


def _counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN is_active = 1 AND token_status = 'active' THEN 1 ELSE 0 END) AS working,
            SUM(CASE WHEN is_active = 1 AND token_status != 'active' THEN 1 ELSE 0 END) AS broken,
            SUM(CASE WHEN is_active = 0 THEN 1 ELSE 0 END) AS disabled,
            SUM(CASE WHEN is_active = 1 AND notify_from IS NULL THEN 1 ELSE 0 END) AS silent
        FROM shops
        """
    ).fetchone()
    return {key: row[key] or 0 for key in ("total", "working", "broken", "disabled", "silent")}


def _shop_line(shop: sqlite3.Row) -> str:
    if not shop["is_active"]:
        mark = "⚪️"
    elif shop["token_status"] != "active":
        mark = "🔴"
    elif not shop["notify_from"]:
        mark = "🔇"
    else:
        mark = "🟢"
    handle = f" @{shop['owner_username']}" if shop["owner_username"] else ""
    return (
        f"{mark} <b>{_esc(shop['name'])}</b> (id {shop['id']})\n"
        f"   {_esc(shop['owner_name'])}{_esc(handle)} · участников: {shop['members']} · "
        f"с {shop['created_at'][:10]}"
    )


_SHOPS_QUERY = """
    SELECT s.id, s.name, s.is_active, s.token_status, s.notify_from, s.created_at,
           COALESCE(u.display_name, '—') AS owner_name, u.username AS owner_username,
           (SELECT COUNT(*) FROM shop_members m WHERE m.shop_id = s.id) AS members
    FROM shops s
    LEFT JOIN bot_users u ON u.telegram_user_id = s.owner_user_id
    {where}
    ORDER BY s.id DESC
    LIMIT ? OFFSET ?
"""


def stats_text(conn: sqlite3.Connection) -> str:
    """Сводка: цифры и только ПРОБЛЕМНЫЕ кабинеты.

    Полный список сюда не выводим сознательно: при сотне кабинетов сообщение
    стало бы нечитаемым и упёрлось бы в лимит длины Telegram. Всё остальное —
    постранично, кнопкой «Все кабинеты».
    """
    counts = _counts(conn)
    users = conn.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
    managers = conn.execute("SELECT COUNT(*) FROM shop_members WHERE role = 'manager'").fetchone()[0]
    sent_today = conn.execute(
        "SELECT COUNT(*) FROM notification_queue WHERE status = 'sent' AND sent_at >= date('now')"
    ).fetchone()[0]
    failed_today = conn.execute(
        "SELECT COUNT(*) FROM notification_queue WHERE status = 'failed' AND created_at >= date('now')"
    ).fetchone()[0]

    lines = [
        "📊 <b>Статистика бота</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"<b>Кабинетов:</b> {counts['total']}",
        f"   🟢 работают: {counts['working']}",
        f"   🔴 сбой токена: {counts['broken']}",
        f"   ⚪️ отключены: {counts['disabled']}",
    ]
    if counts["silent"]:
        lines.append(f"   🔇 без рассылки: {counts['silent']}")
    lines += [
        "",
        f"<b>Людей в боте:</b> {users} (из них менеджеров: {managers})",
        f"<b>Уведомлений за сегодня:</b> отправлено {sent_today}, с ошибкой {failed_today}",
    ]

    problems = conn.execute(
        _SHOPS_QUERY.format(
            where="WHERE s.is_active = 1 AND (s.token_status != 'active' OR s.notify_from IS NULL)"
        ),
        (MAX_PROBLEMS_IN_SUMMARY, 0),
    ).fetchall()
    if problems:
        lines += ["", "<b>Требуют внимания:</b>"]
        lines += [_shop_line(shop) for shop in problems]
    else:
        lines += ["", "Проблемных кабинетов нет."]
    return "\n".join(lines)


def shops_page(conn: sqlite3.Connection, page: int = 0) -> tuple[str, int, int]:
    """Страница списка кабинетов. Возвращает (текст, номер страницы, всего страниц)."""
    total = _counts(conn)["total"]
    pages = max(1, (total + SHOPS_PER_PAGE - 1) // SHOPS_PER_PAGE)
    page = max(0, min(page, pages - 1))

    shops = conn.execute(
        _SHOPS_QUERY.format(where=""), (SHOPS_PER_PAGE, page * SHOPS_PER_PAGE)
    ).fetchall()
    lines = [f"🏢 <b>Кабинеты</b> — страница {page + 1} из {pages} (всего {total})", "━━━━━━━━━━━━━━━━━━━"]
    lines += [_shop_line(shop) for shop in shops] or ["Пусто."]
    return "\n".join(lines), page, pages


def support_header(conn: sqlite3.Connection, user_id: int) -> str:
    """Подпись к обращению: кто пишет и по каким кабинетам, чтобы владелец бота
    не выяснял это вручную."""
    from wbnotify import members_repo

    shops = members_repo.list_user_shops(conn, user_id)
    if shops:
        where = ", ".join(f"{shop['name']} (id {shop['id']}, {shop['role']})" for shop in shops)
    else:
        where = "кабинетов нет"
    return f"🆘 <b>Обращение</b>\nОт: {_esc(members_repo.describe_user(conn, user_id))}\nКабинеты: {_esc(where)}"


def remember_thread(conn: sqlite3.Connection, admin_message_id: int, user_id: int, chat_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO support_threads (admin_message_id, user_id, chat_id, created_at)"
        " VALUES (?, ?, ?, ?)",
        (admin_message_id, user_id, chat_id, utcnow()),
    )
    conn.commit()


def thread_by_message(conn: sqlite3.Connection, admin_message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM support_threads WHERE admin_message_id = ?", (admin_message_id,)
    ).fetchone()
