"""Меню бота: мьюты типов событий и «Остатки подробно»."""
from __future__ import annotations

import json

from wbnotify import shops_repo
from wbnotify.db import utcnow
from wbnotify.telegram.formatters import format_stocks_detail
from wbnotify.telegram.keyboards import build_callback, notification_keyboard, parse_callback
from wbnotify.telegram.sender import drain_queue_for_shop

CHAT_ID = 555


class FakeBot:
    def __init__(self):
        self.sent = 0

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, reply_markup=None):
        self.sent += 1

    async def send_media_group(self, chat_id, media):
        self.sent += 1

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent += 1


def _insert_shop(conn) -> int:
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO shops (owner_user_id, telegram_chat_id, name, wb_api_token_encrypted,
                            token_status, is_active, notify_from, created_at, updated_at)
        VALUES (1, ?, 'test', X'00', 'active', 1, '2026-01-01T00:00:00+00:00', ?, ?)
        """,
        (CHAT_ID, now, now),
    )
    conn.commit()
    return cur.lastrowid


def _queue(conn, shop_id: int, event_type: str) -> None:
    from wbnotify.counters import now_msk

    today = now_msk().date().isoformat()
    table = "orders" if event_type in ("order", "cancel") else "sales"
    if table == "orders":
        conn.execute(
            "INSERT INTO orders (shop_id, srid, date, last_change_date, nm_id, tech_size, raw_json)"
            " VALUES (?, ?, ?, ?, 111, '42', '{}')",
            (shop_id, f"srid-{event_type}", f"{today}T10:00:00", f"{today}T10:00:00"),
        )
    else:
        conn.execute(
            "INSERT INTO sales (shop_id, sale_id, is_return, date, last_change_date, nm_id, tech_size, raw_json)"
            " VALUES (?, ?, ?, ?, ?, 111, '42', '{}')",
            (shop_id, f"S-{event_type}", 1 if event_type == "return" else 0, f"{today}T10:00:00", f"{today}T10:00:00"),
        )
    ref_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO notification_queue (shop_id, event_type, ref_table, ref_id, daily_seq, event_date, status, created_at)"
        " VALUES (?, ?, ?, ?, 1, ?, 'pending', ?)",
        (shop_id, event_type, table, ref_id, f"{today}T10:00:00", utcnow()),
    )
    conn.commit()


def test_callback_roundtrip():
    data = build_callback("st", 7, 12345, "order")
    assert parse_callback(data) == ("st", 7, 12345, "order")
    assert len(data.encode()) <= 64, "Telegram ограничивает callback_data 64 байтами"


def test_notification_keyboard_has_wb_url_button():
    markup = notification_keyboard(1, 12345, "order", "42")
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    assert urls == ["https://www.wildberries.ru/catalog/12345/detail.aspx"]


async def test_muted_event_type_is_not_sent(conn):
    """Замьюченный тип не уходит, остальные продолжают приходить."""
    shop_id = _insert_shop(conn)
    _queue(conn, shop_id, "order")
    _queue(conn, shop_id, "buyout")
    conn.execute(
        "INSERT INTO chat_mutes (chat_id, event_type, shop_id) VALUES (?, 'order', ?)", (CHAT_ID, shop_id)
    )
    conn.commit()

    bot = FakeBot()
    sent = await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id))
    assert sent == 1, "должен уйти только не-замьюченный выкуп"

    left = conn.execute(
        "SELECT event_type FROM notification_queue WHERE status='pending'"
    ).fetchall()
    assert [r["event_type"] for r in left] == ["order"], "замьюченное остаётся в очереди, а не теряется"


async def test_unmute_restores_delivery(conn):
    shop_id = _insert_shop(conn)
    _queue(conn, shop_id, "order")
    conn.execute(
        "INSERT INTO chat_mutes (chat_id, event_type, shop_id) VALUES (?, 'order', ?)", (CHAT_ID, shop_id)
    )
    conn.commit()

    bot = FakeBot()
    assert await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id)) == 0

    conn.execute("DELETE FROM chat_mutes WHERE chat_id = ?", (CHAT_ID,))
    conn.commit()
    assert await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id)) == 1


def test_stocks_detail_zero_size_shown_without_breakdown(conn):
    """Размер без остатка показывается строкой «(0 шт)» без разбивки по складам."""
    shop_id = _insert_shop(conn)
    conn.execute(
        "INSERT INTO cards_cache (shop_id, nm_id, sizes_json, refreshed_at) VALUES (?, 111, ?, ?)",
        (
            shop_id,
            json.dumps(
                [
                    {"chrtID": 1, "techSize": "38", "wbSize": "38", "skus": []},
                    {"chrtID": 2, "techSize": "39", "wbSize": "39", "skus": []},
                ]
            ),
            utcnow(),
        ),
    )
    conn.execute(
        "INSERT INTO stocks_current (shop_id, nm_id, chrt_id, warehouse_id, warehouse_name, quantity, snapshot_at, warehouse_kind)"
        " VALUES (?, 111, 1, -999999, 'Склад WB', 5, ?, 'wb')",
        (shop_id, utcnow()),
    )
    conn.commit()

    text = format_stocks_detail(conn, shop_id, 111)
    assert "🔪 Размер 38 (5 шт" in text
    assert "Склады WB: 5 шт." in text
    assert "🔪 Размер 39 (0 шт)" in text
