"""Логи не должны содержать токенов."""
from __future__ import annotations

import logging

from wbnotify.logging_conf import setup_logging


def test_httpx_requests_are_not_logged_at_info():
    """Telegram кладёт токен бота в путь запроса, а httpx на INFO печатает URL
    целиком — так токен и попадал в журнал при каждом опросе."""
    setup_logging("INFO")
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_our_own_loggers_stay_louder_than_httpx():
    """Глушим именно httpx, а не логирование целиком: свои сообщения о синке и
    рассылке в журнале нужны."""
    logging.getLogger().setLevel(logging.INFO)
    setup_logging("INFO")

    httpx_level = logging.getLogger("httpx").getEffectiveLevel()
    ours = logging.getLogger("wbnotify.scheduler.jobs").getEffectiveLevel()
    assert httpx_level > ours
