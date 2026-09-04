"""Настройка логирования для всего проекта."""
from __future__ import annotations

import logging
import sys


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
