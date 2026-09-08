"""Очередь служебных уведомлений владельцу бота (не путать с уведомлениями о
заказах — те идут участникам кабинета).

Алерты именно КОПЯТСЯ в БД, а не отправляются на месте: события возникают в
репозиториях (`shops_repo`, `members_repo`), у которых нет ни бота, ни асинхронного
контекста. Планировщик разбирает очередь каждым циклом и отправляет их админу.
Отдельная таблица, а не `shop_alerts`: у той жёсткий CHECK на kind и обязательный
shop_id, а часть событий (удаление аккаунта) к конкретному кабинету не привязана.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from wbnotify.db import utcnow

# Заголовки алертов — они же определяют, что видно в первой строке сообщения.
KINDS = {
    "shop_connected": "🆕 Новый кабинет",
    "token_invalid": "🔴 Сбой по кабинету",
    "token_revoked": "🔕 Токен отозван",
    "shop_deleted": "🗑 Кабинет удалён",
    "account_deleted": "🗑 Аккаунт удалён",
    # Обращение, которое не удалось передать сразу (служебный бот был недоступен).
    "support": "⏳ Доставлено с задержкой",
    "sync_degraded": "⚠️ Кабинет работает частично",
}

# Повторяющиеся проблемы (например, урезанный токен клиента) шлём не чаще раза
# в сутки: они держатся неделями, а цикл опроса идёт каждые пять минут.
DEDUP_WINDOW_HOURS = 24


def queue(
    conn: sqlite3.Connection,
    kind: str,
    text: str,
    shop_id: int | None = None,
    payload: dict | None = None,
) -> None:
    """`payload` нужен отложенным обращениям в поддержку: чтобы ответить автору,
    при отправке надо знать, кто писал — а связь «сообщение → автор» создаётся
    только в момент реальной отправки владельцу."""
    conn.execute(
        "INSERT INTO admin_alerts (kind, shop_id, text, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (kind, shop_id, text, json.dumps(payload, ensure_ascii=False) if payload else None, utcnow()),
    )
    conn.commit()


def queue_once_per_day(conn: sqlite3.Connection, kind: str, text: str, shop_id: int | None = None) -> bool:
    """Ставит алерт, если такой же по этому кабинету не ставился за сутки.

    Возвращает True, если поставили. Нужно для постоянных, а не разовых проблем:
    сбойный шаг синка повторяется каждые пять минут, и без этого владелец бота
    получал бы триста одинаковых сообщений в день.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=DEDUP_WINDOW_HOURS)).isoformat()
    already = conn.execute(
        "SELECT 1 FROM admin_alerts WHERE kind = ? AND shop_id IS ? AND created_at >= ? LIMIT 1",
        (kind, shop_id, cutoff),
    ).fetchone()
    if already:
        return False
    queue(conn, kind, text, shop_id)
    return True


def payload_of(alert: sqlite3.Row) -> dict:
    raw = alert["payload_json"] if "payload_json" in alert.keys() else None
    return json.loads(raw) if raw else {}


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
