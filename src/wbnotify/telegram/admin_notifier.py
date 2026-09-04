"""Куда и чем отправлять служебные сообщения владельцу сервиса.

Одно место, решающее «каким ботом»: если задан отдельный служебный бот — им,
иначе основным (прежнее поведение). Иначе выбор пришлось бы дублировать в
планировщике, в основном боте и в тестах, и он бы разъехался.
"""
from __future__ import annotations

from telegram import Bot

from wbnotify.config import Config


def make_admin_bot(config: Config) -> Bot | None:
    """None, если чат владельца не настроен — отправлять всё равно некуда."""
    if config.telegram_admin_chat_id is None:
        return None

    from wbnotify.telegram.sender import make_bot

    return make_bot(config.telegram_admin_bot_token or config.telegram_bot_token)


def uses_separate_bot(config: Config) -> bool:
    return bool(config.telegram_admin_bot_token)
