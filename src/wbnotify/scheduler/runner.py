"""Точка входа фонового процесса: планировщик опроса WB и рассылки уведомлений.

Запуск:
    python -m wbnotify.scheduler.runner

Пока процесс не запущен, бот не опрашивает WB и ничего не присылает — это и было
причиной «протухших» цифр при ручных проверках. На локальной машине процесс живёт
только пока она включена; для постоянной работы нужен VPS/всегда включённый сервер.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from wbnotify.config import Config, load_config
from wbnotify.db import init_db
from wbnotify.logging_conf import setup_logging
from wbnotify.scheduler.jobs import daily_refresh, full_sync_once, poll_and_notify

logger = logging.getLogger(__name__)


def build_scheduler(config: Config) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=config.timezone)

    # coalesce + max_instances=1: если предыдущий прогон затянулся (429 от WB с
    # ожиданием, медленная сеть), новый не запускается параллельно, а пропущенные
    # срабатывания схлопываются в одно — иначе задания начнут наслаиваться.
    scheduler.add_job(
        poll_and_notify,
        trigger=IntervalTrigger(minutes=config.poll_interval_minutes),
        args=[config],
        id="poll_and_notify",
        name="Опрос WB + рассылка уведомлений",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        daily_refresh,
        trigger=CronTrigger(hour=config.cards_refresh_hour_msk, minute=0),
        args=[config],
        id="daily_refresh",
        name="Суточное обновление карточек/рейтингов/тарифов",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    return scheduler


async def _main() -> None:
    config = load_config()
    setup_logging(config.log_level)
    init_db(config.db_path)

    scheduler = build_scheduler(config)
    scheduler.start()
    for job in scheduler.get_jobs():
        logger.info("Задание «%s»: следующий запуск %s", job.name, job.next_run_time)

    logger.info("Стартовый синк...")
    await full_sync_once(config)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler — там остановка по Ctrl+C
            # прилетит как KeyboardInterrupt и будет поймана в run().
            pass

    logger.info("Планировщик запущен. Ctrl+C — остановка.")
    await stop.wait()
    scheduler.shutdown(wait=True)
    logger.info("Планировщик остановлен.")


def run() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем.")


if __name__ == "__main__":
    run()
