"""GET /api/v1/tariffs/commission, GET /api/v1/tariffs/box (common-api.wildberries.ru).

Проверено вживую (2026-09-01):
- commission: `report[]` с полями kgvpBooking/kgvpMarketplace/kgvpPickup/kgvpSupplier/
  kgvpSupplierExpress/paidStorageKgvp/parentID/parentName/subjectID/subjectName.
  subjectName совпадает 1:1 со значением поля `subject` в /orders и /sales (проверено
  на реальных категориях: Ботинки, Кроссовки, Босоножки, Сапоги, Мокасины, Лоферы).
  kgvpSupplier — комиссия для модели "поставщик" (товар лежит на складе WB, у заказа
  warehouseType == "Склад WB"); kgvpMarketplace — для модели FBS/DBS (склад продавца).
- box: `response.data.warehouseList[]` с boxDeliveryBase/Liter/CoefExpr (тариф для
  FBW) и boxDeliveryMarketplaceBase/Liter/CoefExpr (для FBS). Значения — СТРОКИ с
  запятой как десятичным разделителем (например "78,2"), "-" = не применимо для
  этого склада.
  ВАЖНО (найдено вживую): warehouseList не содержит конкретные названия складов,
  которые встречаются в /orders (например "Коледино", "Воронеж") — там более
  крупные логистические группировки (страны/города + пара "Свой склад..." записей).
  Прямого совпадения по имени склада часто не будет — это ограничение самого API,
  а не баг в коде (см. calc-слой: при отсутствии совпадения — явный fallback,
  не выдумываем цифры).
"""
from __future__ import annotations

from wbnotify.wb_api.client import COMMON_API_BASE, request

COMMISSION_URL = f"{COMMON_API_BASE}/api/v1/tariffs/commission"
BOX_TARIFFS_URL = f"{COMMON_API_BASE}/api/v1/tariffs/box"


async def get_commission_table(token: str) -> list[dict]:
    resp = await request("GET", COMMISSION_URL, token, params={"locale": "ru"})
    return resp.json().get("report", [])


async def get_box_tariffs(token: str, date: str) -> dict:
    """`date` — 'ГГГГ-ММ-ДД'. Возвращает весь response.data (currency, warehouseList и т.д.)."""
    resp = await request("GET", BOX_TARIFFS_URL, token, params={"date": date})
    return resp.json().get("response", {}).get("data", {})
