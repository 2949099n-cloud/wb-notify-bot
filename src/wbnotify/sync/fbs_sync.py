"""Синк сборочных заданий FBS и их поставок -> fbs_assembly_tasks / fbs_supplies.

Поставки запрашиваются по одной на уникальный supply_id и кэшируются: у одной
поставки десятки заданий (в живом тесте 30 заданий = 5 поставок). Уже закрытые
поставки (`done=1`, `closed_at` заполнен) повторно не запрашиваются — closedAt
больше не изменится.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from wbnotify.db import utcnow
from wbnotify.wb_api.marketplace import get_assembly_tasks, get_supply, get_task_statuses

logger = logging.getLogger(__name__)

MAX_PAGES = 50
STATUS_BATCH = 1000


def _upsert_task(conn: sqlite3.Connection, shop_id: int, task: dict) -> None:
    conn.execute(
        """
        INSERT INTO fbs_assembly_tasks (
            shop_id, task_id, rid, supply_id, nm_id, created_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(shop_id, task_id) DO UPDATE SET
            supply_id = excluded.supply_id,
            raw_json = excluded.raw_json
        """,
        (
            shop_id,
            task["id"],
            task["rid"],
            # WB отдаёт пустую строку вместо отсутствия поставки — нормализуем в NULL,
            # иначе URL /supplies/{id} собирается без id и упирается в 301 (поймано вживую).
            task.get("supplyId") or None,
            task.get("nmId"),
            task["createdAt"],
            json.dumps(task, ensure_ascii=False),
        ),
    )


async def sync_shop_fbs(conn: sqlite3.Connection, shop_id: int, token: str) -> int:
    """Возвращает число обработанных сборочных заданий."""
    processed = 0
    cursor = 0
    for _ in range(MAX_PAGES):
        tasks, cursor = await get_assembly_tasks(token, next_cursor=cursor)
        if not tasks:
            break
        for task in tasks:
            _upsert_task(conn, shop_id, task)
        conn.commit()
        processed += len(tasks)
        if not cursor:
            break

    await _sync_statuses(conn, shop_id, token)
    supplies = await _sync_supplies(conn, shop_id, token)

    logger.info("shop_id=%s fbs: заданий=%d, поставок обновлено=%d", shop_id, processed, supplies)
    return processed


async def _sync_statuses(conn: sqlite3.Connection, shop_id: int, token: str) -> None:
    """Статусы обновляем только у заданий, ещё не доехавших до финального статуса —
    у отгруженных/отменённых он уже не изменится."""
    rows = conn.execute(
        """
        SELECT task_id FROM fbs_assembly_tasks
        WHERE shop_id = ? AND (supplier_status IS NULL OR supplier_status NOT IN ('complete','cancel','cancel_carrier'))
        """,
        (shop_id,),
    ).fetchall()
    task_ids = [r["task_id"] for r in rows]
    now = utcnow()
    for i in range(0, len(task_ids), STATUS_BATCH):
        for status in await get_task_statuses(token, task_ids[i : i + STATUS_BATCH]):
            conn.execute(
                """
                UPDATE fbs_assembly_tasks
                SET supplier_status = ?, wb_status = ?, status_checked_at = ?
                WHERE shop_id = ? AND task_id = ?
                """,
                (status.get("supplierStatus"), status.get("wbStatus"), now, shop_id, status["id"]),
            )
        conn.commit()


async def _sync_supplies(conn: sqlite3.Connection, shop_id: int, token: str) -> int:
    """Запрашиваем только поставки, которые ещё не закрыты у нас в кэше."""
    rows = conn.execute(
        """
        SELECT DISTINCT t.supply_id FROM fbs_assembly_tasks t
        LEFT JOIN fbs_supplies s ON s.shop_id = t.shop_id AND s.supply_id = t.supply_id
        WHERE t.shop_id = ? AND t.supply_id IS NOT NULL AND t.supply_id != ''
              AND (s.supply_id IS NULL OR s.closed_at IS NULL)
        """,
        (shop_id,),
    ).fetchall()

    updated = 0
    now = utcnow()
    for row in rows:
        supply = await get_supply(token, row["supply_id"])
        conn.execute(
            """
            INSERT INTO fbs_supplies (shop_id, supply_id, created_at, closed_at, done, refreshed_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(shop_id, supply_id) DO UPDATE SET
                closed_at = excluded.closed_at,
                done = excluded.done,
                refreshed_at = excluded.refreshed_at,
                raw_json = excluded.raw_json
            """,
            (
                shop_id,
                supply["id"],
                supply.get("createdAt"),
                supply.get("closedAt"),
                1 if supply.get("done") else 0,
                now,
                json.dumps(supply, ensure_ascii=False),
            ),
        )
        updated += 1
        conn.commit()
    return updated
