"""Логи не должны содержать токенов."""
from __future__ import annotations

import logging

from wbnotify.logging_conf import TOKEN_PLACEHOLDER, TokenRedactingFilter, setup_logging


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


def test_bot_token_is_redacted_from_log_records(caplog):
    """Токен утекал в журнал не только через URL запроса, но и через текст
    исключения библиотеки — поймано на живом сервере."""
    import logging as std_logging

    setup_logging("INFO")
    record = std_logging.LogRecord(
        name="telegram.ext", level=std_logging.ERROR, pathname=__file__, lineno=1,
        msg="The token `8407482309:AAE2QDr2yZkLinVXtjkk3MAAi2D_MncOPOQ` was rejected",
        args=(), exc_info=None,
    )
    TokenRedactingFilter().filter(record)

    assert "AAE2QDr2yZkLinVXtjkk3MAAi2D_MncOPOQ" not in record.msg
    assert TOKEN_PLACEHOLDER in record.msg


def test_redaction_covers_exception_text():
    import logging as std_logging

    exc = ValueError("The token `8407482309:AAE2QDr2yZkLinVXtjkk3MAAi2D_MncOPOQ` was rejected")
    record = std_logging.LogRecord(
        name="telegram.ext", level=std_logging.ERROR, pathname=__file__, lineno=1,
        msg="упало", args=(), exc_info=(ValueError, exc, None),
    )
    TokenRedactingFilter().filter(record)

    assert "AAE2QDr2yZkLinVXtjkk3MAAi2D_MncOPOQ" not in str(exc)


def test_ordinary_text_survives_redaction():
    """Фильтр не должен портить обычные сообщения с двоеточиями и числами."""
    import logging as std_logging

    record = std_logging.LogRecord(
        name="wbnotify", level=std_logging.INFO, pathname=__file__, lineno=1,
        msg="shop_id=2: новых событий=0, отправлено=0", args=(), exc_info=None,
    )
    TokenRedactingFilter().filter(record)
    assert record.msg == "shop_id=2: новых событий=0, отправлено=0"
