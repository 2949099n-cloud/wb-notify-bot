"""Синк рейтинга/отзывов (item-rating) -> card_analytics_cache, по одному магазину.

wb_buyout_percent НЕ заполняется этим синком — % выкупа мы уже считаем сами из
собственной истории orders/sales (calc/metrics.buyout_rate_with_returns), это
надёжнее и не требует лишнего API-вызова; поле оставлено в схеме на будущее.
"""
from __future__ import annotations

import logging
import sqlite3

from wbnotify.db import utcnow
from wbnotify.wb_api.analytics import get_item_ratings

logger = logging.getLogger(__name__)

BATCH_SIZE = 50  # лимит nmIds за один вызов item-rating


async def sync_shop_analytics(conn: sqlite3.Connection, shop_id: int, token: str) -> int:
    nm_ids = [r["nm_id"] for r in conn.execute("SELECT nm_id FROM cards_cache WHERE shop_id = ?", (shop_id,))]
    if not nm_ids:
        return 0

    refreshed_at = utcnow()
    updated = 0
    for i in range(0, len(nm_ids), BATCH_SIZE):
        batch = nm_ids[i : i + BATCH_SIZE]
        items = await get_item_ratings(token, batch)
        for item in items:
            rating = item.get("feedbackRating", {}).get("current")
            reviews_count = item.get("feedbackCount", {}).get("current")
            conn.execute(
                """
                INSERT INTO card_analytics_cache (shop_id, nm_id, rating, reviews_count, refreshed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(shop_id, nm_id) DO UPDATE SET
                    rating = excluded.rating,
                    reviews_count = excluded.reviews_count,
                    refreshed_at = excluded.refreshed_at
                """,
                (shop_id, item["nmId"], rating, reviews_count, refreshed_at),
            )
            updated += 1
        conn.commit()

    logger.info("shop_id=%s analytics: обновлено карточек=%d", shop_id, updated)
    return updated
