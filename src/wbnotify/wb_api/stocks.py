"""POST /api/analytics/v1/stocks-report/wb-warehouses (seller-analytics-api.wildberries.ru).

Заменяет отключаемый /api/v1/supplier/stocks. Поля ответа проверены вживую
(2026-08-31): nmId, chrtId, warehouseId, warehouseName, regionName, quantity,
inWayToClient, inWayFromClient. НЕТ techSize/barcode — они связываются через
content-карточки по chrtId (см. wb_api/content.py, cards_cache.sizes_json).

Лимит: 3 запроса/20с, всплеск 1 (токен категории "Аналитика").
"""
from __future__ import annotations

from wbnotify.wb_api.client import request

STOCKS_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses"

PAGE_LIMIT = 100_000


async def get_stocks(token: str) -> list[dict]:
    """Пагинация по offset до тех пор, пока не вернётся страница короче лимита."""
    items: list[dict] = []
    offset = 0
    while True:
        resp = await request(
            "POST", STOCKS_URL, token, json={"limit": PAGE_LIMIT, "offset": offset}
        )
        if resp.status_code == 204:
            break
        page = resp.json().get("data", {}).get("items", [])
        items.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return items
