"""Задания планировщика: что именно делается по расписанию.

Каждое задание само открывает и закрывает соединение с БД — APScheduler может
запустить задания в разных потоках/циклах, и переиспользовать одно соединение
между ними нельзя (в отличие от asyncio.gather внутри одного задания, где общее
соединение допустимо, см. sync/__init__.py).

Любое задание обязано ловить свои ошибки: необработанное исключение в одном
задании не должно останавливать планировщик и остальные расписания.
"""
from __future__ import annotations

import asyncio
import logging

from telegram.error import TelegramError

from datetime import timedelta

from wbnotify import admin_alerts, shops_repo
from wbnotify.config import Config
from wbnotify.counters import now_msk
from wbnotify.db import db_session, utcnow
from wbnotify.telegram import admin_notifier
from wbnotify.events.classify import classify_shop_events
from wbnotify.sync import sync_all_active_shops, sync_shop_daily, sync_shop_frequent
from wbnotify.sync.tariffs_sync import sync_tariffs
from wbnotify.telegram.formatters import PARSE_MODE
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
        # Алерты владельцу шлём до опроса и независимо от него: если магазинов
        # не осталось совсем, сообщение об этом всё равно должно уйти. Отправляем
        # служебным ботом, когда он настроен — админская лента не должна
        # попадать в чат с заказами.
        await flush_admin_alerts(conn, admin_notifier.make_admin_bot(config) or bot, config)

        shops = shops_repo.list_active_shops(conn)
        if not shops:
            logger.info("Активных магазинов нет — цикл опроса пропущен")
            return

        # Магазины обрабатываются ПАРАЛЛЕЛЬНО (правило проекта №5). Раньше это был
        # последовательный цикл, и один медленный кабинет задерживал всех: на живом
        # сервере цикл вместо пяти минут занимал три часа, а пропущенные запуски
        # схлопывались (coalesce) — уведомления приходили раз в три часа пачкой.
        await asyncio.gather(
            *(_process_shop(conn, bot, shop, config) for shop in shops), return_exceptions=True
        )


# Сколько ждём один магазин, прежде чем бросить его до следующего цикла. Меньше
# интервала опроса: зависший кабинет не должен съедать чужое время.
SHOP_CYCLE_TIMEOUT_SECONDS = 240
# Первичный синк нового кабинета тянет карточки и рейтинги, регулярно упирается
# в 429 с ожиданием — ему нужен запас побольше.
BOOTSTRAP_TIMEOUT_SECONDS = 900


async def _process_shop(conn, bot, shop, config: Config) -> None:
    try:
        # Только что подключённый магазин (через /addshop или CLI) ещё не имеет
        # карточек: они синкаются раз в сутки. Без них уведомление уйдёт без
        # названия товара, фото, артикула и остатков — поэтому первый раз
        # догоняем суточные шаги сразу, не дожидаясь ночи.
        if not _has_cards(conn, shop.id):
            logger.info("shop_id=%s: карточек нет — первичный суточный синк", shop.id)
            await asyncio.wait_for(
                sync_shop_daily(conn, shop, config.token_encryption_key), BOOTSTRAP_TIMEOUT_SECONDS
            )

        result = await asyncio.wait_for(
            sync_shop_frequent(conn, shop, config.token_encryption_key), SHOP_CYCLE_TIMEOUT_SECONDS
        )
        if result["failed_steps"]:
            logger.warning(
                "shop_id=%s: шаги с ошибкой (%s) — аналитика будет неполной, уведомления шлём",
                shop.id,
                ", ".join(result["failed_steps"]),
            )
        if result["error"]:
            logger.warning("shop_id=%s: синк с ошибкой (%s), уведомления пропускаю", shop.id, result["error"])
            return

        events = classify_shop_events(conn, shop.id)
        new_events = sum(len(v) for v in events.values())
        # Не привязано к first_run: кабинет мог синкаться часами и всё
        # это время молчать, если отсечку так и не выставили руками
        # (поймано вживую на shop_id=2). Вызов идемпотентен.
        _start_notifying(conn, shop.id)
        sent = await drain_queue_for_shop(conn, bot, shop, limit=DRAIN_LIMIT_PER_CYCLE)
        logger.info("shop_id=%s: новых событий=%d, отправлено=%d", shop.id, new_events, sent)
    except asyncio.TimeoutError:
        logger.error("shop_id=%s: цикл не уложился в отведённое время, продолжим в следующем", shop.id)
    except TelegramError as exc:
        logger.error("shop_id=%s: ошибка Telegram при рассылке: %s", shop.id, exc)
    except Exception:  # noqa: BLE001 — один магазин не должен ронять цикл
        logger.exception("shop_id=%s: неожиданная ошибка в цикле опроса", shop.id)


async def flush_admin_alerts(conn, bot, config: Config) -> int:
    """Отправляет владельцу бота накопившиеся служебные алерты.

    Алерты копятся в БД, потому что возникают в репозиториях, где нет ни бота,
    ни асинхронного контекста (см. admin_alerts.py). Если админ-чат не настроен,
    просто ничего не делаем — алерты останутся в базе и уйдут, когда настроят.

    `bot` выбирает вызывающий: служебный, если он настроен, иначе основной.
    """
    if config.telegram_admin_chat_id is None:
        return 0

    from wbnotify.telegram import admin

    sent = 0
    for alert in admin_alerts.pending(conn):
        try:
            message = await bot.send_message(
                chat_id=config.telegram_admin_chat_id,
                text=admin_alerts.render(alert),
                parse_mode=PARSE_MODE,
            )
        except TelegramError as exc:
            logger.error("Не удалось отправить алерт админу (id=%s): %s", alert["id"], exc)
            break  # чат недоступен — остальные тоже не уйдут, попробуем в следующем цикле

        # Отложенное обращение: связь «сообщение владельцу → автор» создаётся
        # только сейчас, при реальной отправке — иначе ответ реплаем будет некому
        # доставить.
        payload = admin_alerts.payload_of(alert)
        if alert["kind"] == "support" and payload:
            admin.remember_thread(conn, message.message_id, payload["user_id"], payload["chat_id"])

        admin_alerts.mark_sent(conn, alert["id"])
        sent += 1
    return sent


def _start_notifying(conn, shop_id: int) -> None:
    """Включает рассылку для только что подключённого кабинета.

    Вызывается ПОСЛЕ первичного синка и классификации: события исторического
    бэкфилла (за 90 дней их тысячи) уже лежат в очереди с более ранним
    created_at, поэтому отсечка их не пропустит, а всё новое пойдёт как обычно.
    Без этого свежеподключённый кабинет молчал бы навсегда — notify_from
    приходилось выставлять руками через scripts/notify_cli.py.
    """
    row = conn.execute("SELECT notify_from FROM shops WHERE id = ?", (shop_id,)).fetchone()
    if row is None or row["notify_from"]:
        return
    now = utcnow()
    conn.execute("UPDATE shops SET notify_from = ?, updated_at = ? WHERE id = ?", (now, now, shop_id))
    conn.commit()
    logger.info("shop_id=%s: первичный синк завершён — рассылка включена с %s", shop_id, now)


async def daily_summary_job(config: Config) -> None:
    """Утренняя сводка за прошедший день — по одному закреплённому сообщению
    в каждом чате каждого кабинета."""
    from wbnotify.telegram import daily_summary

    yesterday = (now_msk().date() - timedelta(days=1)).isoformat()
    bot = make_bot(config.telegram_bot_token)
    with db_session(config.db_path) as conn:
        for shop in shops_repo.list_active_shops(conn):
            try:
                sent = await daily_summary.send_for_shop(conn, bot, shop, yesterday)
                logger.info("shop_id=%s: сводка за %s отправлена в %d чат(ов)", shop.id, yesterday, sent)
            except Exception:  # noqa: BLE001 — один магазин не должен ронять рассылку остальных
                logger.exception("shop_id=%s: не удалось отправить сводку", shop.id)


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
