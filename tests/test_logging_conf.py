"""Логи не должны содержать токенов."""
from __future__ import annotations

import logging

from wbnotify.logging_conf import setup_logging


def test_httpx_requests_are_not_logged_at_info():
    """Telegram кладёт токен бота в путь запроса, а httpx на INFO печатает URL
    целиком — так токен и попадал в журнал при каждом опросе."""
    setup_logging("INFO")
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_our_own_loggers_still_talk():
    setup_logging("INFO")
    assert logging.getLogger("wbnotify.scheduler.jobs").isEnabledFor(logging.INFO)
