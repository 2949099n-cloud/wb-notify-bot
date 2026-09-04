"""Очередь служебных уведомлений владельцу бота (не путать с уведомлениями о
заказах — те идут участникам кабинета).

Алерты именно КОПЯТСЯ в БД, а не отправляются на месте: события возникают в
репозиториях (`shops_repo`, `members_repo`), у которых нет ни бота, ни асинхронного
контекста. Планировщик разбирает очередь каждым циклом и отправляет их админу.
Отдельная таблица, а не `shop_alerts`: у той жёсткий CHECK на kind и обязательный
shop_id, а часть событий (удаление аккаунта) к конкретному кабинету не привязана.
"""
from __future__ import annotations

import sqlite3

from wbnotify.db import utcnow

# Заголовки алертов — они же определяют, что видно в первой строке сообщения.
KINDS = {
    "shop_connected": "🆕 Новый кабинет",
    "token_invalid": "🔴 Сбой по кабинету",
    "token_revoked": "🔕 Токен отозван",
    "shop_deleted": "🗑 Кабинет удалён",
    "account_deleted": "🗑 Аккаунт удалён",
}


def queue(conn: sqlite3.Connection, kind: str, text: str, shop_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO admin_alerts (kind, shop_id, text, created_at) VALUES (?, ?, ?, ?)",
        (kind, shop_id, text, utcnow()),
    )
    conn.commit()


def pending(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM admin_alerts WHERE sent_at IS NULL ORDER BY id LIMIT ?", (limit,)
    ).fetchall()


def mark_sent(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute("UPDATE admin_alerts SET sent_at = ? WHERE id = ?", (utcnow(), alert_id))
    conn.commit()


def render(alert: sqlite3.Row) -> str:
    title = KINDS.get(alert["kind"], alert["kind"])
    return f"{title}\n{alert['text']}"
