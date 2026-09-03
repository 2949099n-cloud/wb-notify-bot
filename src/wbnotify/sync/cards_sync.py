"""Синк content/v2/get/cards/list -> cards_cache, по одному магазину.

Кэш карточек — источник chrtId<->techSize<->barcode (sizes_json) и габаритов
для блока аналитики/уведомлений. Обновляется раз в сутки (see CLAUDE.md,
CARDS_REFRESH_HOUR_MSK) — расписание навешивается в шаге 5 (scheduler),
здесь только сама операция синка.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from wbnotify.db import utcnow
from wbnotify.wb_api.content import get_all_cards

logger = logging.getLogger(__name__)


CAROUSEL_PHOTOS = 3


def _photo_urls(card: dict) -> list[str]:
    photos = card.get("photos") or []
    urls = [p.get("big") or p.get("c516x688") for p in photos[:CAROUSEL_PHOTOS]]
    return [u for u in urls if u]


def _first_photo_url(card: dict) -> str | None:
    urls = _photo_urls(card)
    return urls[0] if urls else None


async def sync_shop_cards(conn: sqlite3.Connection, shop_id: int, token: str) -> int:
    cards = await get_all_cards(token)
    refreshed_at = utcnow()

    for card in cards:
        dims = card.get("dimensions") or {}
        length = dims.get("length")
        width = dims.get("width")
        height = dims.get("height")
        volume_l = (length * width * height / 1000) if all(
            v is not None for v in (length, width, height)
        ) else None

        conn.execute(
            """
            INSERT INTO cards_cache (
                shop_id, nm_id, brand, name, vendor_code, photo_url, photos_json,
                dims_l, dims_w, dims_h, volume_l, sizes_json, refreshed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(shop_id, nm_id) DO UPDATE SET
                brand = excluded.brand,
                name = excluded.name,
                vendor_code = excluded.vendor_code,
                photo_url = excluded.photo_url,
                photos_json = excluded.photos_json,
                dims_l = excluded.dims_l,
                dims_w = excluded.dims_w,
                dims_h = excluded.dims_h,
                volume_l = excluded.volume_l,
                sizes_json = excluded.sizes_json,
                refreshed_at = excluded.refreshed_at
            """,
            (
                shop_id,
                card["nmID"],
                card.get("brand"),
                card.get("title"),
                card.get("vendorCode"),
                _first_photo_url(card),
                json.dumps(_photo_urls(card), ensure_ascii=False),
                length,
                width,
                height,
                volume_l,
                json.dumps(card.get("sizes") or [], ensure_ascii=False),
                refreshed_at,
            ),
        )
    conn.commit()

    logger.info("shop_id=%s cards: обновлено карточек=%d", shop_id, len(cards))
    return len(cards)
