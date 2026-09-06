"""Проверки .env: понятная ошибка вместо падения процесса в рестарт-цикле."""
from __future__ import annotations

import pytest

from wbnotify.config import ConfigError, _check_token

GOOD = "8407482309:AAE2QDr2yZkLinVXtjkk3MAAi2D_MncOPOQ"


def test_valid_token_passes():
    assert _check_token(GOOD, "TELEGRAM_BOT_TOKEN") == GOOD


def test_token_with_pasted_timestamp_is_rejected():
    """Реальный случай: при копировании из BotFather прилипло время «21:07»,
    и служебный бот падал по кругу с трейсбеком, печатая токен в журнал."""
    with pytest.raises(ConfigError, match="неверный формат"):
        _check_token(GOOD + "21:07", "TELEGRAM_ADMIN_BOT_TOKEN")


@pytest.mark.parametrize("broken", ["", "простотекст", "8407482309", "8407482309:", ":ABC" + "x" * 30])
def test_broken_tokens_are_rejected(broken):
    with pytest.raises(ConfigError):
        _check_token(broken, "TELEGRAM_BOT_TOKEN")


def test_surrounding_spaces_are_trimmed():
    """Пробел на конце строки в .env — частая причина «Unauthorized»."""
    assert _check_token(f"  {GOOD}\t", "TELEGRAM_BOT_TOKEN") == GOOD


def test_error_message_does_not_contain_the_token():
    with pytest.raises(ConfigError) as exc:
        _check_token(GOOD + "21:07", "TELEGRAM_ADMIN_BOT_TOKEN")
    assert "AAE2QDr2yZkLinVXtjkk3MAAi2D_MncOPOQ" not in str(exc.value)
