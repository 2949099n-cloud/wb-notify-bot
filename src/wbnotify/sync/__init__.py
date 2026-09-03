"""Оркестрация синка одного магазина и всех активных магазинов параллельно.

Частота вызова orders/sales/stocks (каждые ~30 мин) и cards (раз в сутки) —
забота планировщика (шаг 5). Здесь — только сама операция и изоляция ошибок:
сбой одного магазина (в т.ч. WBAuthError) не должен прерывать синк остальных.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from wbnotify import shops_repo
from wbnotify.models import ShopRow
from wbnotify.sync.analytics_sync import sync_shop_analytics
from wbnotify.sync.cards_sync import sync_shop_cards
from wbnotify.sync.fbs_sync import sync_shop_fbs
from wbnotify.sync.funnel_sync import sync_shop_funnel
from wbnotify.sync.seller_stocks_sync import sync_shop_seller_stocks
from wbnotify.sync.orders_sync import sync_shop_orders
from wbnotify.sync.sales_sync import sync_shop_sales
from wbnotify.sync.stocks_sync import sync_shop_stocks
from wbnotify.sync.tariffs_sync import sync_tariffs
from wbnotify.wb_api.client import WBAuthError, WBError

logger = logging.getLogger(__name__)


async def sync_shop_all(conn: sqlite3.Connection, shop: ShopRow, enc_key: str) -> dict:
    """Полный синк одного магазина (частые шаги + суточные) — для ручного прогона
    через sync_cli. Планировщик вместо этого зовёт sync_shop_frequent/sync_shop_daily
    по своим расписаниям."""
    return await _run_shop_sync(conn, shop, enc_key, steps=FREQUENT_STEPS + DAILY_STEPS)


# Что гоняем каждые POLL_INTERVAL_MINUTES, а что раз в сутки. Разделение нужно
# для планировщика: карточки (2 запроса на 176 позиций) и рейтинги (4 батча,
# регулярно упирающиеся в 429 с ожиданием ~15с) меняются редко и не стоят того,
# чтобы дёргать их каждые полчаса.
FREQUENT_STEPS = ("orders", "sales", "stocks", "fbs", "seller_stocks", "funnel")
DAILY_STEPS = ("cards", "analytics")

# Имя шага -> имя функции в этом модуле. Именно ИМЯ, а не сама функция: иначе
# словарь захватит объекты при импорте, и подмена атрибута модуля (в тестах,
# monkeypatch) перестанет действовать.
_STEP_FUNC_NAMES = {
    "orders": "sync_shop_orders",
    "sales": "sync_shop_sales",
    "stocks": "sync_shop_stocks",
    "cards": "sync_shop_cards",
    "analytics": "sync_shop_analytics",
    "fbs": "sync_shop_fbs",
    "seller_stocks": "sync_shop_seller_stocks",
    "funnel": "sync_shop_funnel",
}


async def sync_shop_frequent(conn: sqlite3.Connection, shop: ShopRow, enc_key: str) -> dict:
    """Частый синк (каждые ~30 мин): заказы, продажи, остатки, FBS, воронка."""
    return await _run_shop_sync(conn, shop, enc_key, steps=FREQUENT_STEPS)


async def sync_shop_daily(conn: sqlite3.Connection, shop: ShopRow, enc_key: str) -> dict:
    """Суточный синк: карточки и рейтинги/отзывы."""
    return await _run_shop_sync(conn, shop, enc_key, steps=DAILY_STEPS)


async def _run_shop_sync(conn: sqlite3.Connection, shop: ShopRow, enc_key: str, steps: tuple[str, ...]) -> dict:
    """При WBAuthError (401) — помечает магазин invalid и создаёт алерт, не бросая
    исключение наружу. Любая другая ошибка логируется с shop_id и тоже не
    пробрасывается — чтобы падение одного магазина не рвало asyncio.gather.

    ВАЖНО: все sync_shop_* используют ОДНО общее sqlite3-соединение (conn),
    вызываемое из нескольких корутин через asyncio.gather. Это безопасно только
    потому, что каждая из них пишет в БД синхронно (execute+commit) БЕЗ await
    между операциями записи — транзакция всегда закрыта коммитом до следующего
    await (сетевой вызов). Если при правке sync_shop_*.py между execute() и
    commit() появится await — это может привести к interleaved-записи на общем
    соединении. Не нарушайте этот инвариант.
    """
    token = shops_repo.get_decrypted_token(conn, shop.id, enc_key)
    result: dict = {"shop_id": shop.id, "error": None}
    result.update({step: 0 for step in steps})
    try:
        for step in steps:
            step_func = globals()[_STEP_FUNC_NAMES[step]]
            result[step] = await step_func(conn, shop.id, token)
    except WBAuthError:
        logger.warning("shop_id=%s: токен невалиден (401), помечаю invalid", shop.id)
        shops_repo.mark_token_invalid(conn, shop.id)
        result["error"] = "invalid_token"
    except WBError as exc:
        logger.error("shop_id=%s: ошибка WB API: %s", shop.id, exc)
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — синк одного магазина не должен ронять остальные
        logger.exception("shop_id=%s: неожиданная ошибка синка", shop.id)
        result["error"] = str(exc)
    return result


async def sync_all_active_shops(conn: sqlite3.Connection, enc_key: str) -> list[dict]:
    shops = shops_repo.list_active_shops(conn)

    # Тарифы (комиссия/логистика) — публичные данные WB, одинаковые для всех
    # продавцов (tariffs_cache без shop_id), поэтому синкаются ОДИН раз за цикл
    # любым валидным токеном, а не за каждый магазин отдельно.
    if shops:
        try:
            token = shops_repo.get_decrypted_token(conn, shops[0].id, enc_key)
            await sync_tariffs(conn, token)
        except WBError as exc:
            logger.error("Не удалось синкнуть тарифы: %s", exc)

    results = await asyncio.gather(*(sync_shop_all(conn, shop, enc_key) for shop in shops))
    return list(results)
