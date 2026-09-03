"""GET /api/v1/supplier/orders (statistics-api.wildberries.ru).

Поля ответа проверены вживую (2026-08-31): date, lastChangeDate, warehouseName,
warehouseType, countryName, oblastOkrugName, regionName, supplierArticle, nmId,
barcode, category, subject, brand, techSize, incomeID, isSupply, isRealization,
totalPrice, discountPercent, spp, finishedPrice, priceWithDisc, isCancel,
cancelDate, sticker, gNumber, srid. НЕТ поля chrtId — размерный артикул тут не
возвращается, только techSize/barcode (chrtId достаётся через content-карточки).

Лимит на аккаунт продавца (токен категории "Статистика"): 1 запрос/мин, всплеск 10.
"""
from __future__ import annotations

from wbnotify.wb_api.client import STATISTICS_API_BASE, request

ORDERS_URL = f"{STATISTICS_API_BASE}/api/v1/supplier/orders"


async def get_orders(token: str, date_from: str, flag: int = 0) -> list[dict]:
    resp = await request("GET", ORDERS_URL, token, params={"dateFrom": date_from, "flag": flag})
    return resp.json()
