"""Синк тарифов (комиссия по категориям + логистика по складам) -> tariffs_cache.

Как и cards, тарифы меняются редко — частота обновления (раз в сутки,
TARIFFS_REFRESH_HOUR_MSK) навешивается планировщиком (шаг 5), здесь только
сама операция. Полностью заменяем кэш при каждом синке (снимок, не история).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date

from wbnotify.db import utcnow
from wbnotify.wb_api.tariffs import get_box_tariffs, get_commission_table

logger = logging.getLogger(__name__)


async def sync_tariffs(conn: sqlite3.Connection, token: str) -> dict:
    commission_rows = await get_commission_table(token)
    box_data = await get_box_tariffs(token, date.today().isoformat())
    warehouse_rows = box_data.get("warehouseList", [])

    refreshed_at = utcnow()
    conn.execute("DELETE FROM tariffs_cache WHERE kind = 'commission'")
    for row in commission_rows:
        conn.execute(
            """
            INSERT INTO tariffs_cache (kind, subject_or_category, warehouse_name, value_json, refreshed_at)
            VALUES ('commission', ?, NULL, ?, ?)
            """,
            (row["subjectName"], json.dumps(row, ensure_ascii=False), refreshed_at),
        )

    conn.execute("DELETE FROM tariffs_cache WHERE kind = 'box'")
    for row in warehouse_rows:
        conn.execute(
            """
            INSERT INTO tariffs_cache (kind, subject_or_category, warehouse_name, value_json, refreshed_at)
            VALUES ('box', NULL, ?, ?, ?)
            """,
            (row["warehouseName"], json.dumps(row, ensure_ascii=False), refreshed_at),
        )
    conn.commit()

    logger.info(
        "тарифы: %d категорий комиссии, %d складов логистики", len(commission_rows), len(warehouse_rows)
    )
    return {"commission": len(commission_rows), "box": len(warehouse_rows)}
