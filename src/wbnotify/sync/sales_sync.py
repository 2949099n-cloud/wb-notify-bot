"""Синк GET /api/v1/supplier/sales -> таблица sales, по одному магазину."""
from __future__ import annotations

import json
import logging
import sqlite3

from wbnotify.db import get_cursor, set_cursor, shift_cursor_back
from wbnotify.wb_api.sales import get_sales

logger = logging.getLogger(__name__)

DEFAULT_DATE_FROM = "2020-01-01"
PAGE_ADVANCE_THRESHOLD = 75_000
MAX_PAGES = 20


def _upsert_sale(conn: sqlite3.Connection, shop_id: int, row: dict) -> None:
    sale_id = row["saleID"]
    conn.execute(
        """
        INSERT INTO sales (
            shop_id, sale_id, srid, is_return, date, last_change_date, nm_id, tech_size,
            barcode, supplier_article, subject, warehouse_type, warehouse_name, region_name,
            oblast_okrug_name, price_with_disc, finished_price, for_pay, spp, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(shop_id, sale_id) DO UPDATE SET
            last_change_date = excluded.last_change_date,
            for_pay = excluded.for_pay,
            price_with_disc = excluded.price_with_disc,
            finished_price = excluded.finished_price,
            supplier_article = excluded.supplier_article,
            subject = excluded.subject,
            warehouse_type = excluded.warehouse_type,
            raw_json = excluded.raw_json
        """,
        (
            shop_id,
            sale_id,
            row.get("srid"),
            1 if sale_id.startswith("R") else 0,
            row["date"],
            row["lastChangeDate"],
            row["nmId"],
            row.get("techSize"),
            row.get("barcode"),
            row.get("supplierArticle"),
            row.get("subject"),
            row.get("warehouseType"),
            row.get("warehouseName"),
            row.get("regionName"),
            row.get("oblastOkrugName"),
            row.get("priceWithDisc"),
            row.get("finishedPrice"),
            row.get("forPay"),
            row.get("spp"),
            json.dumps(row, ensure_ascii=False),
        ),
    )


async def sync_shop_sales(
    conn: sqlite3.Connection, shop_id: int, token: str, date_from_override: str | None = None
) -> int:
    """Как и заказы, запрашиваем с перекрытием назад: WB отдаёт часть продаж с
    опозданием, сохраняя исходный lastChangeDate (см. db.SYNC_LOOKBACK_HOURS)."""
    cursor = get_cursor(conn, shop_id, "sales")
    date_from = date_from_override or shift_cursor_back(cursor) or DEFAULT_DATE_FROM
    max_change_date = cursor or DEFAULT_DATE_FROM
    processed = 0

    for _ in range(MAX_PAGES):
        rows = await get_sales(token, date_from)
        if not rows:
            break
        processed += len(rows)
        for row in rows:
            _upsert_sale(conn, shop_id, row)
            if row["lastChangeDate"] > max_change_date:
                max_change_date = row["lastChangeDate"]
        conn.commit()
        if len(rows) < PAGE_ADVANCE_THRESHOLD:
            break
        date_from = max_change_date

    if processed:
        set_cursor(conn, shop_id, "sales", max_change_date)
        conn.commit()

    logger.info("shop_id=%s sales: обработано строк=%d, курсор=%s", shop_id, processed, max_change_date)
    return processed
