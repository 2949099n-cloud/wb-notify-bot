"""Синк остатков на СВОИХ складах продавца (FBS, Marketplace API).

Пишет в ту же таблицу stocks_current, что и остатки складов WB, но с
warehouse_kind='seller' — так блок «Остаток» в уведомлении может показать оба
источника, а каждый синк заменяет снимок только своего вида складов.

Marketplace API отдаёт остатки по sku (баркоду), без nmId/techSize — связка
идёт через cards_cache.sizes_json (там у каждого размера есть skus[]), тот же
приём, что и для остатков WB.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from wbnotify.db import utcnow
from wbnotify.wb_api.marketplace import SKU_BATCH, get_seller_stocks, get_seller_warehouses

logger = logging.getLogger(__name__)


def _sku_index(conn: sqlite3.Connection, shop_id: int) -> dict[str, tuple[int, int]]:
    """{barcode: (nm_id, chrt_id)} по кэшу карточек."""
    index: dict[str, tuple[int, int]] = {}
    for row in conn.execute("SELECT nm_id, sizes_json FROM cards_cache WHERE shop_id = ?", (shop_id,)):
        if not row["sizes_json"]:
            continue
        for size in json.loads(row["sizes_json"]):
            for sku in size.get("skus") or []:
                index[str(sku)] = (row["nm_id"], size["chrtID"])
    return index


async def sync_shop_seller_stocks(conn: sqlite3.Connection, shop_id: int, token: str) -> int:
    index = _sku_index(conn, shop_id)
    if not index:
        return 0

    warehouses = await get_seller_warehouses(token)
    skus = list(index)
    snapshot_at = utcnow()

    conn.execute("DELETE FROM stocks_current WHERE shop_id = ? AND warehouse_kind = 'seller'", (shop_id,))
    total = 0
    for warehouse in warehouses:
        for i in range(0, len(skus), SKU_BATCH):
            for item in await get_seller_stocks(token, warehouse["id"], skus[i : i + SKU_BATCH]):
                # Нулевые остатки не храним: таблица — снимок наличия, а размеры
                # без остатка и так отрисовываются как "0 шт." по данным карточки.
                if not item.get("amount"):
                    continue
                mapped = index.get(str(item["sku"]))
                if mapped is None:
                    continue
                nm_id, chrt_id = mapped
                conn.execute(
                    """
                    INSERT INTO stocks_current (
                        shop_id, nm_id, chrt_id, warehouse_id, warehouse_name, region_name,
                        quantity, in_way_to_client, in_way_from_client, snapshot_at, warehouse_kind
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, 0, 0, ?, 'seller')
                    ON CONFLICT(shop_id, nm_id, chrt_id, warehouse_id) DO UPDATE SET
                        quantity = excluded.quantity,
                        snapshot_at = excluded.snapshot_at
                    """,
                    (shop_id, nm_id, chrt_id, warehouse["id"], warehouse.get("name"), item["amount"], snapshot_at),
                )
                total += 1
        conn.commit()

    logger.info("shop_id=%s seller stocks: складов=%d, позиций с остатком=%d", shop_id, len(warehouses), total)
    return total
