"""Задания планировщика: что именно делается по расписанию.

Каждое задание само открывает и закрывает соединение с БД — APScheduler может
запустить задания в разных потоках/циклах, и переиспользовать одно соединение
между ними нельзя (в отличие от asyncio.gather внутри одного задания, где общее
соединение допустимо, см. sync/__init__.py).

Любое задание обязано ловить свои ошибки: необработанное исключение в одном
задании не должно останавливать планировщик и остальные расписания.
"""
from __future__ import annotations

import logging

from telegram.error import TelegramError

from wbnotify import shops_repo
from wbnotify.config import Config
from wbnotify.db import db_session
from wbnotify.events.classify import classify_shop_events
from wbnotify.sync import sync_all_active_shops, sync_shop_daily, sync_shop_frequent
from wbnotify.sync.tariffs_sync import sync_tariffs
from wbnotify.telegram.sender import drain_queue_for_shop, make_bot
from wbnotify.wb_api.client import WBError

logger = logging.getLogger(__name__)

# Потолок на одну отправку: даже если очередь по какой-то причине разрослась,
# планировщик не должен вывалить всё разом в чат пользователя.
DRAIN_LIMIT_PER_CYCLE = 30


def _has_cards(conn, shop_id: int) -> bool:
    return bool(
        conn.execute("SELECT 1 FROM cards_cache WHERE shop_id = ? LIMIT 1", (shop_id,)).fetchone()
    )


async def poll_and_notify(config: Config) -> None:
    """Основной цикл: свежие данные -> детект событий -> отправка уведомлений."""
    bot = make_bot(config.telegram_bot_token)
    with db_session(config.db_path) as conn:
        shops = shops_repo.list_active_shops(conn)
        if not shops:
            logger.info("Активных магазинов нет — цикл опроса пропущен")
            return

        for shop in shops:
            try:
                # Только что подключённый магазин (через /addshop или CLI) ещё не
                # имеет карточек: они синкаются раз в сутки. Без них уведомление
                # уйдёт без названия товара, фото, артикула и остатков — поэтому
                # первый раз догоняем суточные шаги сразу, не дожидаясь ночи.
                if not _has_cards(conn, shop.id):
                    logger.info("shop_id=%s: карточек нет — первичный суточный синк", shop.id)
                    await sync_shop_daily(conn, shop, config.token_encryption_key)

                result = await sync_shop_frequent(conn, shop, config.token_encryption_key)
                if result["error"]:
                    logger.warning("shop_id=%s: синк с ошибкой (%s), уведомления пропускаю", shop.id, result["error"])
                    continue

                events = classify_shop_events(conn, shop.id)
                new_events = sum(len(v) for v in events.values())
                sent = await drain_queue_for_shop(conn, bot, shop, limit=DRAIN_LIMIT_PER_CYCLE)
                logger.info("shop_id=%s: новых событий=%d, отправлено=%d", shop.id, new_events, sent)
            except TelegramError as exc:
                logger.error("shop_id=%s: ошибка Telegram при рассылке: %s", shop.id, exc)
            except Exception:  # noqa: BLE001 — один магазин не должен ронять цикл
                logger.exception("shop_id=%s: неожиданная ошибка в цикле опроса", shop.id)


async def daily_refresh(config: Config) -> None:
    """Раз в сутки: карточки, рейтинги/отзывы и тарифы (тарифы — общие для всех
    магазинов, поэтому синкаются один раз любым валидным токеном)."""
    with db_session(config.db_path) as conn:
        shops = shops_repo.list_active_shops(conn)
        if not shops:
            return

        try:
            token = shops_repo.get_decrypted_token(conn, shops[0].id, config.token_encryption_key)
            await sync_tariffs(conn, token)
        except WBError as exc:
            logger.error("Суточный синк тарифов не удался: %s", exc)

        for shop in shops:
            try:
                result = await sync_shop_daily(conn, shop, config.token_encryption_key)
                logger.info("shop_id=%s: суточный синк — %s", shop.id, result)
            except Exception:  # noqa: BLE001
                logger.exception("shop_id=%s: неожиданная ошибка суточного синка", shop.id)


async def full_sync_once(config: Config) -> None:
    """Разовый полный синк — выполняется на старте, чтобы не ждать первого
    срабатывания расписания (и чтобы сразу увидеть в логах, что токены живы)."""
    with db_session(config.db_path) as conn:
        results = await sync_all_active_shops(conn, config.token_encryption_key)
        for r in results:
            logger.info("стартовый синк: %s", r)
