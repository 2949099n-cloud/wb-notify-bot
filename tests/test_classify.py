from __future__ import annotations

from wbnotify.counters import msk_date_str, next_daily_seq
from wbnotify.db import utcnow
from wbnotify.events.classify import (
    classify_cancellations,
    classify_new_orders,
    classify_sales,
    classify_shop_events,
)

SHOP_ID = 1


def _insert_shop(conn):
    conn.execute(
        """
        INSERT INTO shops (id, owner_user_id, telegram_chat_id, name, wb_api_token_encrypted, created_at, updated_at)
        VALUES (?, 1, 1, 'test', X'00', ?, ?)
        """,
        (SHOP_ID, utcnow(), utcnow()),
    )
    conn.commit()


def _insert_order(conn, srid: str, date: str, is_cancel: int = 0, cancel_date: str | None = None):
    conn.execute(
        """
        INSERT INTO orders (shop_id, srid, date, last_change_date, nm_id, tech_size, is_cancel, cancel_date, raw_json)
        VALUES (?, ?, ?, ?, 111, '42', ?, ?, '{}')
        """,
        (SHOP_ID, srid, date, date, is_cancel, cancel_date),
    )
    conn.commit()


def _insert_sale(conn, sale_id: str, date: str, is_return: int):
    conn.execute(
        """
        INSERT INTO sales (shop_id, sale_id, is_return, date, last_change_date, nm_id, tech_size, raw_json)
        VALUES (?, ?, ?, ?, ?, 111, '42', '{}')
        """,
        (SHOP_ID, sale_id, is_return, date, date),
    )
    conn.commit()


def test_new_order_notified_exactly_once(conn):
    _insert_shop(conn)
    _insert_order(conn, "srid-1", "2026-08-30T10:00:00")

    first = classify_new_orders(conn, SHOP_ID)
    assert len(first) == 1

    second = classify_new_orders(conn, SHOP_ID)
    assert second == []


def test_cancellation_transition_fires_exactly_once_across_repeated_polls(conn):
    """Заказ, отменённый в тот же цикл опроса: 'Заказ' и 'Отмена' оба уведомляются
    один раз каждое; повторные вызовы classify_cancellations (имитация следующих
    опросов той же уже-обработанной отменённой строки) больше ничего не находят."""
    _insert_shop(conn)
    _insert_order(conn, "srid-cancel", "2026-08-30T10:00:00", is_cancel=1, cancel_date="2026-08-30T11:00:00")

    order_events = classify_new_orders(conn, SHOP_ID)
    assert len(order_events) == 1

    cancel_events_1 = classify_cancellations(conn, SHOP_ID)
    assert len(cancel_events_1) == 1

    # Имитация следующих N опросов: WB продолжает отдавать эту же (уже отменённую) строку.
    cancel_events_2 = classify_cancellations(conn, SHOP_ID)
    cancel_events_3 = classify_cancellations(conn, SHOP_ID)
    assert cancel_events_2 == []
    assert cancel_events_3 == []

    row = conn.execute("SELECT notified_cancel_at FROM orders WHERE srid='srid-cancel'").fetchone()
    assert row["notified_cancel_at"] is not None


def test_sale_id_prefix_routing_to_buyout_vs_return(conn):
    _insert_shop(conn)
    _insert_sale(conn, "S123", "2026-08-30T10:00:00", is_return=0)
    _insert_sale(conn, "R456", "2026-08-30T10:05:00", is_return=1)

    classify_sales(conn, SHOP_ID)

    events = conn.execute(
        "SELECT event_type, ref_id FROM notification_queue WHERE shop_id=? ORDER BY id", (SHOP_ID,)
    ).fetchall()
    types = {e["event_type"] for e in events}
    assert types == {"buyout", "return"}


def test_daily_seq_resets_per_msk_date_bucket(conn):
    _insert_shop(conn)
    seq1 = next_daily_seq(conn, SHOP_ID, "order", "2026-08-31")
    seq2 = next_daily_seq(conn, SHOP_ID, "order", "2026-08-31")
    seq3 = next_daily_seq(conn, SHOP_ID, "order", "2026-08-31")
    assert (seq1, seq2, seq3) == (1, 2, 3)

    # новые сутки (полночь МСК) -> отдельный счётчик, не продолжение 3
    seq_next_day = next_daily_seq(conn, SHOP_ID, "order", "2026-09-01")
    assert seq_next_day == 1


def test_msk_date_str_no_timezone_math_needed():
    # WB отдаёт даты УЖЕ в московском времени -> просто берём первые 10 символов
    assert msk_date_str("2026-08-31T23:59:59") == "2026-08-31"
    assert msk_date_str("2026-01-05T00:00:00.12345") == "2026-01-05"


def test_classify_shop_events_full_pass_assigns_correct_daily_seq(conn):
    _insert_shop(conn)
    _insert_order(conn, "srid-a", "2026-08-31T09:00:00")
    _insert_order(conn, "srid-b", "2026-08-31T10:00:00")
    _insert_order(conn, "srid-c", "2026-09-01T09:00:00")  # следующие сутки

    classify_shop_events(conn, SHOP_ID)

    rows = conn.execute(
        "SELECT ref_id, daily_seq FROM notification_queue WHERE shop_id=? AND event_type='order' ORDER BY ref_id",
        (SHOP_ID,),
    ).fetchall()
    # первые два заказа — 31 августа (#1, #2), третий — 1 сентября (#1, новый день)
    assert [r["daily_seq"] for r in rows] == [1, 2, 1]
