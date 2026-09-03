"""Синк воронки продаж -> sales_funnel_daily (источник «сводки на главной» ЛК).

Синкаются только сегодня и вчера — ровно те дни, что показываются в блоке
«Вчера/Сегодня» уведомления. Лимит метода 3 запроса/20с, поэтому два дня = два
запроса, укладываемся с запасом.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from wbnotify.counters import now_msk
from wbnotify.db import utcnow
from wbnotify.wb_api.analytics import get_sales_funnel

logger = logging.getLogger(__name__)


def _days_to_sync() -> list[str]:
    today = now_msk().date()
    return [(today - timedelta(days=1)).isoformat(), today.isoformat()]


async def sync_shop_funnel(conn: sqlite3.Connection, shop_id: int, token: str) -> int:
    refreshed_at = utcnow()
    total = 0
    for day in _days_to_sync():
        products = await get_sales_funnel(token, day)
        # Полная замена дня: воронка отдаёт снимок за день целиком, диффов нет.
        conn.execute("DELETE FROM sales_funnel_daily WHERE shop_id = ? AND date_msk = ?", (shop_id, day))
        for product in products:
            stat = product.get("statistic", {}).get("selected", {})
            conn.execute(
                """
                INSERT INTO sales_funnel_daily (
                    shop_id, date_msk, nm_id, order_count, order_sum,
                    buyout_count, buyout_sum, cancel_count, cancel_sum, refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop_id,
                    day,
                    product["product"]["nmId"],
                    stat.get("orderCount", 0),
                    stat.get("orderSum", 0),
                    stat.get("buyoutCount", 0),
                    stat.get("buyoutSum", 0),
                    stat.get("cancelCount", 0),
                    stat.get("cancelSum", 0),
                    refreshed_at,
                ),
            )
            total += 1
        conn.commit()

    logger.info("shop_id=%s воронка: строк за 2 дня=%d", shop_id, total)
    return total
