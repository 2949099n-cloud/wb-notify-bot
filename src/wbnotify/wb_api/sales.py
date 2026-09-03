"""GET /api/v1/supplier/sales (statistics-api.wildberries.ru).

Поля ответа проверены вживую (2026-08-31): те же, что у orders, плюс
paymentSaleAmount, forPay, saleID ("S..." — продажа/выкуп, "R..." — возврат).
НЕТ поля chrtId (как и у orders).

Лимит на аккаунт продавца (токен категории "Статистика"): 1 запрос/мин, без всплеска.
"""
from __future__ import annotations

from wbnotify.wb_api.client import STATISTICS_API_BASE, request

SALES_URL = f"{STATISTICS_API_BASE}/api/v1/supplier/sales"


async def get_sales(token: str, date_from: str, flag: int = 0) -> list[dict]:
    resp = await request("GET", SALES_URL, token, params={"dateFrom": date_from, "flag": flag})
    return resp.json()
