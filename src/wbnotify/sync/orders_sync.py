"""Синк GET /api/v1/supplier/orders -> таблица orders, по одному магазину."""
from __future__ import annotations

import json
import logging
import sqlite3

from wbnotify.db import get_cursor, set_cursor, shift_cursor_back
from wbnotify.wb_api.orders import get_orders

logger = logging.getLogger(__name__)

DEFAULT_DATE_FROM = "2020-01-01"  # orders хранятся у WB не больше 90 дней, дата заведомо в прошлом
PAGE_ADVANCE_THRESHOLD = 75_000  # см. док WB: ~80000 строк на ответ, при таком объёме дозапрашиваем
MAX_PAGES = 20  # защита от зацикливания


def _upsert_order(conn: sqlite3.Connection, shop_id: int, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO orders (
            shop_id, srid, g_number, date, last_change_date, nm_id, tech_size, barcode,
            supplier_article, subject, warehouse_type, warehouse_name, region_name,
            oblast_okrug_name, total_price, discount_percent, spp, price_with_disc,
            finished_price, is_cancel, cancel_date, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(shop_id, srid) DO UPDATE SET
            last_change_date = excluded.last_change_date,
            is_cancel = excluded.is_cancel,
            cancel_date = excluded.cancel_date,
            finished_price = excluded.finished_price,
            price_with_disc = excluded.price_with_disc,
            supplier_article = excluded.supplier_article,
            subject = excluded.subject,
            warehouse_type = excluded.warehouse_type,
            raw_json = excluded.raw_json
        """,
        (
            shop_id,
            row["srid"],
            row.get("gNumber"),
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
            row.get("totalPrice"),
            row.get("discountPercent"),
            row.get("spp"),
            row.get("priceWithDisc"),
            row.get("finishedPrice"),
            1 if row.get("isCancel") else 0,
            row.get("cancelDate") if row.get("isCancel") else None,
            json.dumps(row, ensure_ascii=False),
        ),
    )


async def sync_shop_orders(
    conn: sqlite3.Connection, shop_id: int, token: str, date_from_override: str | None = None
) -> int:
    """Возвращает число новых+обновлённых строк, обработанных за этот вызов.

    Запрашиваем не «строго с курсора», а с перекрытием назад (см.
    db.SYNC_LOOKBACK_HOURS): WB отдаёт часть заказов с опозданием, сохраняя им
    исходный lastChangeDate, и строгий курсор терял их навсегда.
    `date_from_override` — для ручного глубокого перезабора через sync_cli.
    """
    cursor = get_cursor(conn, shop_id, "orders")
    date_from = date_from_override or shift_cursor_back(cursor) or DEFAULT_DATE_FROM
    # Курсор двигаем от РЕАЛЬНОГО значения, а не от отмотанного, иначе он поехал
    # бы назад на сутки при каждом синке.
    max_change_date = cursor or DEFAULT_DATE_FROM
    processed = 0

    for _ in range(MAX_PAGES):
        rows = await get_orders(token, date_from)
        if not rows:
            break
        processed += len(rows)
        for row in rows:
            _upsert_order(conn, shop_id, row)
            if row["lastChangeDate"] > max_change_date:
                max_change_date = row["lastChangeDate"]
        conn.commit()
        if len(rows) < PAGE_ADVANCE_THRESHOLD:
            break
        date_from = max_change_date

    if processed:
        set_cursor(conn, shop_id, "orders", max_change_date)
        conn.commit()

    logger.info("shop_id=%s orders: обработано строк=%d, курсор=%s", shop_id, processed, max_change_date)
    return processed
