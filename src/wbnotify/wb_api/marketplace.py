"""Marketplace API (marketplace-api.wildberries.ru) — сборочные задания FBS.

Зачем отдельный источник: Statistics API (/api/v1/supplier/orders, /sales) НЕ
содержит момента передачи заказа в доставку, а он нужен для расчёта скидки/штрафа
за скорость отгрузки FBS. Проверено вживую (2026-09-01):

- Права: текущий токен магазина УЖЕ имеет доступ к Marketplace API
  (`/ping` -> 200, `/api/v3/orders/new` -> реальные задания). Отдельный токен не нужен.
- Связка со Statistics API: `orders[].rid` == `orders.srid` (совпало 85 из 100;
  остальные — вне 90-дневного окна Statistics API).
- Время: `createdAt` в UTC, наш `orders.date` — МСК (+3ч), совпадают секунда в секунду.
- Метки времени перехода в статус «В доставке» в /api/v3/orders/status НЕТ —
  только текущий статус. НО поставка (`/api/v3/supplies/{id}`) отдаёт `closedAt`
  — момент закрытия поставки, т.е. ровно момент передачи в доставку. Это даёт
  Тф ретроспективно, без необходимости самим ловить переход поллингом.
- Лимиты: 300 запросов/мин (всплеск 20) на все методы сборочных заданий —
  на порядок мягче, чем у Statistics API (1 запрос/мин).
"""
from __future__ import annotations

from wbnotify.wb_api.client import request

MARKETPLACE_BASE = "https://marketplace-api.wildberries.ru"
ORDERS_URL = f"{MARKETPLACE_BASE}/api/v3/orders"
ORDERS_STATUS_URL = f"{MARKETPLACE_BASE}/api/v3/orders/status"
SUPPLY_URL = f"{MARKETPLACE_BASE}/api/v3/supplies/{{supply_id}}"

PAGE_LIMIT = 1000  # максимум по документации
STATUS_BATCH = 1000  # максимум ID в одном запросе статусов


async def get_assembly_tasks(token: str, next_cursor: int = 0, limit: int = PAGE_LIMIT) -> tuple[list[dict], int]:
    """Страница сборочных заданий (созданных не более 3 месяцев назад).
    Возвращает (задания, следующий курсор). Курсор 0 — начало выборки."""
    resp = await request("GET", ORDERS_URL, token, params={"limit": limit, "next": next_cursor})
    data = resp.json()
    return data.get("orders", []), data.get("next", 0)


async def get_task_statuses(token: str, task_ids: list[int]) -> list[dict]:
    """Текущие supplierStatus/wbStatus по ID заданий (до 1000 за раз)."""
    if not task_ids:
        return []
    resp = await request("POST", ORDERS_STATUS_URL, token, json={"orders": task_ids})
    return resp.json().get("orders", [])


WAREHOUSES_URL = f"{MARKETPLACE_BASE}/api/v3/warehouses"
SELLER_STOCKS_URL = f"{MARKETPLACE_BASE}/api/v3/stocks/{{warehouse_id}}"
SKU_BATCH = 1000


async def get_seller_warehouses(token: str) -> list[dict]:
    """Склады продавца (FBS). Проверено вживую: два склада — «ФФ Москва», «ФФ ФБС Спб»."""
    resp = await request("GET", WAREHOUSES_URL, token)
    return resp.json()


async def get_seller_stocks(token: str, warehouse_id: int, skus: list[str]) -> list[dict]:
    """Остатки на складе продавца по баркодам (sku). Ответ: [{sku, chrtId, amount}]."""
    if not skus:
        return []
    resp = await request("POST", SELLER_STOCKS_URL.format(warehouse_id=warehouse_id), token, json={"skus": skus})
    return resp.json().get("stocks", [])


async def get_supply(token: str, supply_id: str) -> dict:
    """Поставка целиком; ключевое поле — `closedAt` (момент передачи в доставку)."""
    resp = await request("GET", SUPPLY_URL.format(supply_id=supply_id), token)
    return resp.json()
