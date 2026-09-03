"""POST /api/analytics/v2/item-rating (seller-analytics-api.wildberries.ru).

Проверено вживую (2026-09-01): `data.items[].feedbackRating.current` — рейтинг
товара, НЕ зависит от периода запроса (проверено: одинаковое значение 4.33 и для
периода в 1 месяц, и в 365 дней — это накопленный/текущий рейтинг, а не "за период").
`data.items[].feedbackCount.current` — а вот это, наоборот, СЧЁТЧИК ИМЕННО ЗА ПЕРИОД
(0 отзывов за август, 2 отзыва за последние 365 дней у одного и того же товара) —
метод не отдаёт лайфтайм-счётчик отзывов напрямую. Берём максимально широкий
разрешённый период (365 дней, дальше API отвечает 400) как лучшее доступное
приближение к "всего отзывов" — это ограничение самого API, не баг.

`currentPeriod` обязателен даже при желании просто получить "текущий" рейтинг.
"""
from __future__ import annotations

from datetime import date, timedelta

from wbnotify.wb_api.client import request

ITEM_RATING_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v2/item-rating"

MAX_PERIOD_DAYS = 365


SALES_FUNNEL_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
FUNNEL_PAGE_LIMIT = 1000


async def get_sales_funnel(token: str, day: str) -> list[dict]:
    """Воронка продаж за один день (`day` = 'ГГГГ-ММ-ДД').

    Это источник цифр «сводки на главной» в ЛК — проверено вживую: за 02.09
    воронка дала 37 заказов / 266340 ₽, за 01.09 — 38 / 281348 ₽, ровно как в
    сводке пользователя, тогда как Statistics API /orders на те же дни отдаёт
    по 30 заказов. Два разных счёта самой WB, для сводки верен этот.

    ВАЖНО: выкупы/отмены воронка относит к дню, когда товар был ЗАКАЗАН, а не
    когда выкуплен — поэтому для «выкупов за день» она НЕ подходит, там
    используются наши sales (см. calc/metrics._BASIS_BY_EVENT_TYPE).
    """
    body = {"selectedPeriod": {"start": day, "end": day}, "limit": FUNNEL_PAGE_LIMIT, "offset": 0}
    resp = await request("POST", SALES_FUNNEL_URL, token, json=body)
    return resp.json().get("data", {}).get("products", [])


async def get_item_ratings(token: str, nm_ids: list[int]) -> list[dict]:
    """До 50 nmIds за раз (лимит API).

    end = вчера, не сегодня: WB отклоняет currentPeriod.end == сегодня с 400
    "current period end date cannot be today" (проверено вживую) — видимо,
    сегодняшние данные ещё не финализированы.

    start считается от СЕГОДНЯ (не от end): "365 дней назад от end" оказалось
    на день больше допустимого — WB отвечает 400 "start earlier than a year
    ago" (проверено вживую), т.е. лимит считается от текущей даты, а не от
    конца периода.
    """
    today = date.today()
    end = today - timedelta(days=1)
    start = today - timedelta(days=MAX_PERIOD_DAYS)
    body = {
        "currentPeriod": {"start": start.isoformat(), "end": end.isoformat()},
        "nmIds": nm_ids,
        "orderBy": {"field": "feedbackCount", "mode": "desc"},
        "limit": len(nm_ids),
        "offset": 0,
    }
    resp = await request("POST", ITEM_RATING_URL, token, json=body)
    return resp.json().get("data", {}).get("items", [])
