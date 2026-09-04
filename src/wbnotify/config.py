"""Загрузка конфигурации сервиса из .env. Магазины больше не читаются из .env —
они хранятся в БД (таблица shops) и добавляются пользователями через /addshop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Ошибка конфигурации — некорректный или неполный .env."""


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    token_encryption_key: str  # Fernet key для шифрования WB-токенов магазинов в БД
    telegram_admin_chat_id: int | None  # опционально: чат владельца бота для панели и алертов
    poll_interval_minutes: int
    stocks_poll_interval_minutes: int
    cards_refresh_hour_msk: int
    tariffs_refresh_hour_msk: int
    db_path: str
    timezone: str
    log_level: str
    # Отдельный бот для админской ленты: статистика, служебные алерты и
    # обращения в поддержку. Нужен, чтобы всё это не смешивалось с лентой
    # заказов в основном боте (при сотне кабинетов она станет нечитаемой).
    # Не задан — админская часть работает в основном боте, как раньше.
    telegram_admin_bot_token: str | None = None
    # Час утренней рассылки сводки за прошедший день (МСК).
    daily_summary_hour_msk: int = 9


def _require(env: dict, key: str) -> str:
    value = env.get(key)
    if not value:
        raise ConfigError(f"Отсутствует обязательная переменная окружения: {key}")
    return value


def load_config(env_file: str | None = None) -> Config:
    load_dotenv(dotenv_path=env_file)
    env = os.environ

    admin_chat_id_raw = env.get("TELEGRAM_ADMIN_CHAT_ID")

    return Config(
        telegram_bot_token=_require(env, "TELEGRAM_BOT_TOKEN"),
        token_encryption_key=_require(env, "TOKEN_ENCRYPTION_KEY"),
        telegram_admin_chat_id=int(admin_chat_id_raw) if admin_chat_id_raw else None,
        telegram_admin_bot_token=env.get("TELEGRAM_ADMIN_BOT_TOKEN") or None,
        # 5 минут: уведомления должны приходить постепенно, а не пачкой раз в
        # полчаса. Лимиты WB это позволяют — /orders и /sales по 1 запросу/мин.
        poll_interval_minutes=int(env.get("POLL_INTERVAL_MINUTES", "5")),
        stocks_poll_interval_minutes=int(env.get("STOCKS_POLL_INTERVAL_MINUTES", "5")),
        cards_refresh_hour_msk=int(env.get("CARDS_REFRESH_HOUR_MSK", "3")),
        tariffs_refresh_hour_msk=int(env.get("TARIFFS_REFRESH_HOUR_MSK", "3")),
        # Утро: сводка подводит итог ПРОШЕДШЕГО дня, к 9:00 данные за него уже
        # досинкались (последний ночной цикл опроса проходит в 8:5x).
        daily_summary_hour_msk=int(env.get("DAILY_SUMMARY_HOUR_MSK", "9")),
        db_path=env.get("DB_PATH", "./data/wbnotify.db"),
        timezone=env.get("TIMEZONE", "Europe/Moscow"),
        log_level=env.get("LOG_LEVEL", "INFO"),
    )
