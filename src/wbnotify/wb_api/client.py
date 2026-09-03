"""Асинхронный клиент WB API. Общий для всех эндпоинтов (orders/sales/stocks/...).

Асинхронность нужна, чтобы опрашивать магазины нескольких пользователей
параллельно через asyncio.gather, а не последовательно один за другим.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

COMMON_API_BASE = "https://common-api.wildberries.ru"
STATISTICS_API_BASE = "https://statistics-api.wildberries.ru"
CONTENT_API_BASE = "https://content-api.wildberries.ru"

MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 2.0


class WBError(RuntimeError):
    """Общая ошибка при обращении к WB API (после исчерпания retry)."""


class WBAuthError(WBError):
    """WB API вернул 401 — токен магазина невалиден/отозван. Retry не имеет смысла."""


async def request(
    method: str,
    url: str,
    token: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """Выполняет запрос с retry/backoff на 429/5xx. 401 -> WBAuthError сразу, без retry."""
    headers = {"Authorization": f"Bearer {token}"}
    attempt = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            attempt += 1
            try:
                resp = await client.request(method, url, headers=headers, params=params, json=json)
            except httpx.HTTPError as exc:
                if attempt >= MAX_RETRIES:
                    raise WBError(f"Сетевая ошибка после {attempt} попыток: {exc}") from exc
                await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue

            if resp.status_code == 401:
                raise WBAuthError(f"401 Unauthorized: {url}")

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= MAX_RETRIES:
                    raise WBError(f"HTTP {resp.status_code} после {attempt} попыток: {resp.text[:200]}")
                # WB отдаёт свой собственный заголовок x-ratelimit-retry (секунды до сброса
                # окна лимита), а не стандартный Retry-After — проверено вживую на 429 от
                # /api/v1/supplier/sales (лимит 1 запрос/мин). Проверяем оба на всякий случай.
                retry_after = resp.headers.get("x-ratelimit-retry") or resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning("WB API %s -> HTTP %s, retry через %.1fс (попытка %d/%d)",
                                url, resp.status_code, delay, attempt, MAX_RETRIES)
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 400:
                raise WBError(f"HTTP {resp.status_code} для {url}: {resp.text[:300]}")
            return resp


async def ping(token: str) -> dict:
    resp = await request("GET", f"{COMMON_API_BASE}/ping", token)
    return resp.json()


async def seller_info(token: str) -> dict:
    """Возвращает {'name': ..., 'sid': ..., 'tradeMark': ...} для авто-заполнения имени магазина."""
    resp = await request("GET", f"{COMMON_API_BASE}/api/v1/seller-info", token)
    return resp.json()
