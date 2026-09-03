"""Минимальный Telegram-бот: /start, /addshop (онбординг магазина), /cancel.

/addshop использует ровно shops_repo.register_shop — ту же функцию, что уже
проверена вживую через scripts/sync_cli.py register. Сообщение с сырым токеном
удаляется из чата сразу после обработки (успешной или нет) — правило из CLAUDE.md.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from wbnotify import shops_repo
from wbnotify.config import Config
from wbnotify.db import db_session
from wbnotify.telegram.formatters import PARSE_MODE, format_stocks_detail
from wbnotify.telegram.keyboards import ACTION_APPEARANCE, ACTION_MUTE, ACTION_STOCKS, parse_callback

logger = logging.getLogger(__name__)

AWAITING_TOKEN = 1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я слежу за заказами на Wildberries и присылаю уведомления.\n"
        "Чтобы подключить магазин — /addshop"
    )


async def addshop_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Пришлите ваш WB API-токен (личный кабинет WB: Профиль → Настройки → Доступ к API, "
        "нужен как минимум доступ к категориям «Статистика» и «Аналитика»).\n\n"
        "Сообщение с токеном будет удалено сразу после обработки — не переживайте, что он "
        "останется в переписке. /cancel — чтобы отменить."
    )
    return AWAITING_TOKEN


async def addshop_receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config: Config = context.bot_data["config"]
    raw_token = update.message.text.strip()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message_id = update.message.message_id

    reply_text: str
    with db_session(config.db_path) as conn:
        try:
            shop = await shops_repo.register_shop(conn, user_id, chat_id, raw_token, config.token_encryption_key)
        except shops_repo.InvalidTokenError as exc:
            reply_text = f"Не удалось подключить магазин: {exc}\nПопробуйте /addshop ещё раз."
        else:
            reply_text = f"✅ Магазин «{shop.name}» подключён. Уведомления по заказам будут приходить сюда."

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as exc:
        logger.warning("Не удалось удалить сообщение с токеном (chat_id=%s): %s", chat_id, exc)

    # sendMessage напрямую, а не update.message.reply_text — сообщение с токеном уже
    # удалено выше, отвечать "в ответ" на несуществующее сообщение бессмысленно.
    await context.bot.send_message(chat_id=chat_id, text=reply_text)
    return ConversationHandler.END


async def addshop_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


EVENT_TITLES = {
    "order": "заказы",
    "cancel": "отмены заказов",
    "buyout": "продажи",
    "return": "возвраты",
}


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатия инлайн-кнопок под уведомлением.

    Кнопка живёт дольше сообщения: всё, кроме действия и товара, подтягивается
    из БД в момент нажатия, поэтому старое уведомление тоже отработает корректно.
    """
    query = update.callback_query
    await query.answer()  # убрать «часики» у кнопки

    config: Config = context.bot_data["config"]
    action, shop_id, nm_id, extra = parse_callback(query.data)
    chat_id = update.effective_chat.id

    with db_session(config.db_path) as conn:
        shop = conn.execute("SELECT telegram_chat_id FROM shops WHERE id = ?", (shop_id,)).fetchone()
        # Изоляция: кнопка действует только в том чате, которому принадлежит магазин.
        if shop is None or shop["telegram_chat_id"] != chat_id:
            await query.message.reply_text("Этот магазин не привязан к текущему чату.")
            return

        if action == ACTION_STOCKS:
            await query.message.reply_text(format_stocks_detail(conn, shop_id, nm_id), parse_mode=PARSE_MODE)

        elif action == ACTION_MUTE:
            conn.execute(
                "INSERT OR IGNORE INTO chat_mutes (chat_id, event_type, shop_id) VALUES (?, ?, ?)",
                (chat_id, extra, shop_id),
            )
            conn.commit()
            await query.message.reply_text(
                f"🔕 Больше не присылаю «{EVENT_TITLES.get(extra, extra)}» по этому магазину.\n"
                f"Вернуть — /settings"
            )

        elif action == ACTION_APPEARANCE:
            await query.message.reply_text(_settings_text(conn, chat_id), parse_mode=PARSE_MODE)


def _settings_text(conn, chat_id: int) -> str:
    muted = conn.execute(
        "SELECT event_type, shop_id FROM chat_mutes WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    if not muted:
        return "⚙️ Настройки\n\nСейчас приходят все типы уведомлений.\nОтключить тип — кнопка «Не показывать такие» под уведомлением."
    lines = ["⚙️ Настройки", "", "Отключены:"]
    for row in muted:
        lines.append(f"   • {EVENT_TITLES.get(row['event_type'], row['event_type'])}")
    lines.append("")
    lines.append("Включить обратно всё — /unmute")
    return "\n".join(lines)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    with db_session(config.db_path) as conn:
        await update.message.reply_text(_settings_text(conn, update.effective_chat.id), parse_mode=PARSE_MODE)


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    with db_session(config.db_path) as conn:
        conn.execute("DELETE FROM chat_mutes WHERE chat_id = ?", (update.effective_chat.id,))
        conn.commit()
    await update.message.reply_text("🔔 Все типы уведомлений включены обратно.")


def build_application(config: Config) -> Application:
    # HTTPXRequest с увеличенными таймаутами — дефолтные 5с оказались маловаты
    # для sendMediaGroup из нескольких фото (см. telegram/sender.py:make_bot).
    request = HTTPXRequest(connect_timeout=10.0, read_timeout=30.0, write_timeout=30.0, media_write_timeout=60.0)
    app = Application.builder().token(config.telegram_bot_token).request(request).build()
    app.bot_data["config"] = config

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("unmute", unmute_command))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("addshop", addshop_start)],
            states={AWAITING_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, addshop_receive_token)]},
            fallbacks=[CommandHandler("cancel", addshop_cancel)],
        )
    )
    return app


def run() -> None:
    from wbnotify.config import load_config
    from wbnotify.db import init_db
    from wbnotify.logging_conf import setup_logging

    config = load_config()
    setup_logging(config.log_level)
    init_db(config.db_path)
    app = build_application(config)
    logger.info("Бот запущен, поллинг Telegram...")
    app.run_polling()


if __name__ == "__main__":
    run()
