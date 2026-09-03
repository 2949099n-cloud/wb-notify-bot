"""Детект событий Заказ/Отмена/Выкуп/Возврат из orders/sales -> notification_queue.

Дедуп по конструкции: каждая функция ищет строки по notified_*_at IS NULL и
сразу проставляет метку — при повторном вызове (следующий опрос) эта же строка
уже не попадётся, независимо от того, сколько раз /orders или /sales её вернут.
"""
from __future__ import annotations

import logging
import sqlite3

from wbnotify.counters import msk_date_str, next_daily_seq
from wbnotify.db import utcnow

logger = logging.getLogger(__name__)


def _enqueue(
    conn: sqlite3.Connection,
    shop_id: int,
    event_type: str,
    ref_table: str,
    ref_id: int,
    daily_seq: int,
    event_date: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO notification_queue (
            shop_id, event_type, ref_table, ref_id, daily_seq, event_date, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (shop_id, event_type, ref_table, ref_id, daily_seq, event_date, utcnow()),
    )
    return cur.lastrowid


def classify_new_orders(conn: sqlite3.Connection, shop_id: int) -> list[int]:
    """«Заказ»: строки orders, ещё не уведомлённые (notified_order_at IS NULL).

    Уведомляем независимо от текущего is_cancel — даже если заказ отменили в том же
    цикле опроса, продавец всё равно должен узнать, что заказ БЫЛ; «Отмена» придёт
    отдельным событием следом (см. classify_cancellations).
    """
    rows = conn.execute(
        "SELECT id, date FROM orders WHERE shop_id = ? AND notified_order_at IS NULL ORDER BY date", (shop_id,)
    ).fetchall()
    queue_ids = []
    now = utcnow()
    for row in rows:
        seq = next_daily_seq(conn, shop_id, "order", msk_date_str(row["date"]))
        queue_ids.append(_enqueue(conn, shop_id, "order", "orders", row["id"], seq, row["date"]))
        conn.execute("UPDATE orders SET notified_order_at = ? WHERE id = ?", (now, row["id"]))
    conn.commit()
    if queue_ids:
        logger.info("shop_id=%s: новых событий 'order' = %d", shop_id, len(queue_ids))
    return queue_ids


def classify_cancellations(conn: sqlite3.Connection, shop_id: int) -> list[int]:
    """«Отмена»: переход is_cancel 0->1, детектится как notified_cancel_at IS NULL
    у строки с is_cancel=1 — срабатывает ровно один раз на заказ: после того как
    notified_cancel_at проставлен, повторный опрос эту же строку уже не найдёт,
    даже если WB продолжит отдавать её в ответе (idempotent re-fetch).
    """
    rows = conn.execute(
        """
        SELECT id, cancel_date FROM orders
        WHERE shop_id = ? AND is_cancel = 1 AND notified_cancel_at IS NULL
        ORDER BY cancel_date
        """,
        (shop_id,),
    ).fetchall()
    queue_ids = []
    now = utcnow()
    for row in rows:
        date_bucket = msk_date_str(row["cancel_date"] or now)
        seq = next_daily_seq(conn, shop_id, "cancel", date_bucket)
        queue_ids.append(_enqueue(conn, shop_id, "cancel", "orders", row["id"], seq, row["cancel_date"]))
        conn.execute("UPDATE orders SET notified_cancel_at = ? WHERE id = ?", (now, row["id"]))
    conn.commit()
    if queue_ids:
        logger.info("shop_id=%s: новых событий 'cancel' = %d", shop_id, len(queue_ids))
    return queue_ids


def classify_sales(conn: sqlite3.Connection, shop_id: int) -> list[int]:
    """«Выкуп»/«Возврат»: строки sales, ещё не уведомлённые (notified_at IS NULL).

    is_return уже вычислен при синке по префиксу saleID ('S' -> продажа, 'R' -> возврат) —
    здесь просто читаем готовый флаг, повторной классификации по строке не делаем.
    """
    rows = conn.execute(
        "SELECT id, date, is_return FROM sales WHERE shop_id = ? AND notified_at IS NULL ORDER BY date", (shop_id,)
    ).fetchall()
    queue_ids = []
    now = utcnow()
    for row in rows:
        event_type = "return" if row["is_return"] else "buyout"
        seq = next_daily_seq(conn, shop_id, event_type, msk_date_str(row["date"]))
        queue_ids.append(_enqueue(conn, shop_id, event_type, "sales", row["id"], seq, row["date"]))
        conn.execute("UPDATE sales SET notified_at = ? WHERE id = ?", (now, row["id"]))
    conn.commit()
    if queue_ids:
        logger.info("shop_id=%s: новых событий 'buyout'/'return' = %d", shop_id, len(queue_ids))
    return queue_ids


def classify_shop_events(conn: sqlite3.Connection, shop_id: int) -> dict[str, list[int]]:
    """Полный проход детекции для одного магазина. Порядок важен: сначала заказы
    (чтобы notified_order_at был проставлен раньше отмены по тому же заказу)."""
    return {
        "order": classify_new_orders(conn, shop_id),
        "cancel": classify_cancellations(conn, shop_id),
        "sales": classify_sales(conn, shop_id),
    }
