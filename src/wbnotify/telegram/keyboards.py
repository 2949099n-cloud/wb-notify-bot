"""Инлайн-кнопки под уведомлениями и разбор их callback_data.

callback_data у Telegram ограничен 64 байтами, поэтому кладём только то, чего
не восстановить: действие, магазин, товар и размер. Всё остальное берётся из БД
в момент нажатия — так кнопка не «протухает» вместе с сообщением.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

WB_CARD_URL = "https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"

ACTION_STOCKS = "st"
ACTION_MUTE = "mu"
ACTION_APPEARANCE = "ap"
SEPARATOR = ":"


def build_callback(action: str, shop_id: int, nm_id: int, extra: str = "") -> str:
    return SEPARATOR.join([action, str(shop_id), str(nm_id), extra])


def parse_callback(data: str) -> tuple[str, int, int, str]:
    action, shop_id, nm_id, extra = (data.split(SEPARATOR, 3) + ["", "", "", ""])[:4]
    return action, int(shop_id), int(nm_id), extra


def notification_keyboard(shop_id: int, nm_id: int, event_type: str, tech_size: str | None) -> InlineKeyboardMarkup:
    """Две кнопки под уведомлением (решение пользователя): «На WB» — обычная
    URL-кнопка, серверная логика ей не нужна; «Остатки подробно» приходит
    колбэком в процесс бота.

    ACTION_MUTE/ACTION_APPEARANCE отсюда убраны, но обработчики в bot.py
    оставлены: под уже отправленными уведомлениями эти кнопки ещё висят и
    должны продолжать работать.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒 На WB", url=WB_CARD_URL.format(nm_id=nm_id))],
            [InlineKeyboardButton("📦 Остатки подробно", callback_data=build_callback(ACTION_STOCKS, shop_id, nm_id))],
        ]
    )
