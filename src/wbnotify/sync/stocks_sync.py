"""Синк остатков -> stocks_current, по одному магазину.

stocks_current — снимок ТЕКУЩЕГО состояния (не история событий), поэтому
при каждом синке старые строки этого магазина полностью заменяются свежими,
а не апсертятся построчно — это проще и корректно отражает то, что WB
возвращает: "текущие остатки" целиком, без диффов.
"""
from __future__ import annotations

import logging
import sqlite3

from wbnotify.db import utcnow
from wbnotify.wb_api.stocks import get_stocks

logger = logging.getLogger(__name__)


async def sync_shop_stocks(conn: sqlite3.Connection, shop_id: int, token: str) -> int:
    items = await get_stocks(token)
    snapshot_at = utcnow()

    # Только склады WB: остатки своих складов продавца живут в этой же таблице
    # с warehouse_kind='seller' и синкаются отдельно (см. sync/seller_stocks_sync.py).
    conn.execute("DELETE FROM stocks_current WHERE shop_id = ? AND warehouse_kind = 'wb'", (shop_id,))
    for item in items:
        conn.execute(
            """
            INSERT INTO stocks_current (
                shop_id, nm_id, chrt_id, warehouse_id, warehouse_name, region_name,
                quantity, in_way_to_client, in_way_from_client, snapshot_at, warehouse_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'wb')
            """,
            (
                shop_id,
                item["nmId"],
                item["chrtId"],
                item["warehouseId"],
                item.get("warehouseName"),
                item.get("regionName"),
                item.get("quantity", 0),
                item.get("inWayToClient", 0),
                item.get("inWayFromClient", 0),
                snapshot_at,
            ),
        )
    conn.commit()

    logger.info("shop_id=%s stocks: снимок из %d строк", shop_id, len(items))
    return len(items)
