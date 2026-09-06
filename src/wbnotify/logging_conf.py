"""Настройка логирования для всего проекта."""
from __future__ import annotations

import logging
import re
import sys

# Токен бота Telegram: <цифры>:<35+ символов>. Он утекает в логи не только через
# URL запроса (это лечится уровнем httpx ниже), но и через текст исключений самой
# библиотеки — реальный случай: `InvalidToken: The token '123:ABC...' was rejected`
# в журнале сервера. Поэтому режем по факту записи, а не по источнику: любой новый
# способ утечки закрыт заранее.
_TOKEN_PATTERN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}")
TOKEN_PLACEHOLDER = "<токен скрыт>"


class TokenRedactingFilter(logging.Filter):
    """Заменяет токены ботов в тексте записи на заглушку.

    Фильтр висит на обработчике, а не на конкретном логгере: сообщение может
    прийти от любой библиотеки, и заранее знать, от какой именно, нельзя.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _TOKEN_PATTERN.sub(TOKEN_PLACEHOLDER, str(record.msg))
        if record.args:
            record.args = tuple(
                _TOKEN_PATTERN.sub(TOKEN_PLACEHOLDER, str(arg)) if isinstance(arg, str) else arg
                for arg in record.args
            )
        if record.exc_info:
            # Трейсбек форматируется позже, поэтому подменяем текст исключения
            # прямо сейчас — иначе токен уедет в журнал внутри traceback.
            exc_type, exc_value, exc_tb = record.exc_info
            if exc_value is not None and exc_value.args:
                exc_value.args = tuple(
                    _TOKEN_PATTERN.sub(TOKEN_PLACEHOLDER, str(arg)) if isinstance(arg, str) else arg
                    for arg in exc_value.args
                )
        return True


def setup_logging(level: str = "INFO") -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # httpx на INFO печатает URL каждого запроса, а Telegram кладёт токен бота
    # прямо в путь: api.telegram.org/bot<ТОКЕН>/getUpdates. При опросе раз в
    # 10 секунд токен попадал в журнал сотни раз в сутки — а журнал читают через
    # консоль, копируют в переписку и присылают в поддержку. Оставляем только
    # предупреждения и ошибки; запросы к WB мы и так логируем сами, осмысленно.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    redactor = TokenRedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
