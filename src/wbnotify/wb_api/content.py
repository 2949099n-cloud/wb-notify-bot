"""POST /content/v2/get/cards/list (content-api.wildberries.ru).

Поля ответа проверены вживую (2026-08-31): nmID, imtID, subjectID, subjectName,
vendorCode, brand, title, photos[], dimensions{width,height,length,weightBrutto},
sizes[]{chrtID, techSize, wbSize, skus[]} — единственное место, где связаны
chrtId <-> techSize <-> barcode. Обновление кэша — раз в сутки (см. sync/cards_sync.py).
"""
from __future__ import annotations

from wbnotify.wb_api.client import CONTENT_API_BASE, request

CARDS_LIST_URL = f"{CONTENT_API_BASE}/content/v2/get/cards/list"

PAGE_LIMIT = 100


async def get_all_cards(token: str) -> list[dict]:
    """Пагинация курсором (updatedAt/nmID последней карточки предыдущей страницы)."""
    cards: list[dict] = []
    cursor: dict = {"limit": PAGE_LIMIT}
    while True:
        resp = await request(
            "POST",
            CARDS_LIST_URL,
            token,
            json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}},
        )
        data = resp.json()
        page = data.get("cards", [])
        cards.extend(page)
        page_cursor = data.get("cursor", {})
        total = page_cursor.get("total", len(page))
        if total < PAGE_LIMIT or not page:
            break
        cursor = {
            "limit": PAGE_LIMIT,
            "updatedAt": page_cursor.get("updatedAt"),
            "nmID": page_cursor.get("nmID"),
        }
    return cards
