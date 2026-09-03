"""Дневные счётчики [#N] и МСК-границы дня.

ВАЖНО: даты, которые отдаёт WB API (date, lastChangeDate, cancelDate, ...),
УЖЕ в московском времени как naive-строки (см. докс: "время передаётся в
часовом поясе Москва UTC+3") — конвертация не нужна, достаточно взять
первые 10 символов. Конвертация через pytz нужна только для "текущего
момента" (например, для дневной сводки/боевого поллинга), а не для дат из WB.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytz

MSK = pytz.timezone("Europe/Moscow")


def msk_date_str(wb_date_str: str) -> str:
    """Дата (YYYY-MM-DD) из строки даты WB API — без конвертации, WB уже отдаёт МСК."""
    return wb_date_str[:10]


def now_msk() -> datetime:
    return datetime.now(MSK)


def today_msk_str() -> str:
    return now_msk().strftime("%Y-%m-%d")


def next_daily_seq(conn: sqlite3.Connection, shop_id: int, event_type: str, date_msk: str) -> int:
    """Атомарно увеличивает и возвращает следующий номер [#N] для (shop_id, event_type, date_msk).

    Отдельный счётчик на каждые сутки — сброс происходит естественно, т.к. date_msk
    меняется, а не через отдельную cron-задачу.
    """
    conn.execute(
        """
        INSERT INTO daily_counters (shop_id, event_type, date_msk, last_seq)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(shop_id, event_type, date_msk) DO UPDATE SET last_seq = last_seq + 1
        """,
        (shop_id, event_type, date_msk),
    )
    row = conn.execute(
        "SELECT last_seq FROM daily_counters WHERE shop_id = ? AND event_type = ? AND date_msk = ?",
        (shop_id, event_type, date_msk),
    ).fetchone()
    return row["last_seq"]
