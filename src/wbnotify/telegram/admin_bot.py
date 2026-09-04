"""Отдельный бот владельца сервиса: статистика, служебные алерты, поддержка.

Зачем отдельный бот, а не команды в основном: у владельца в основном боте идёт
собственная лента заказов, и админская переписка вперемешку с ней нечитаема —
особенно когда кабинетов десятки. Здесь же ничего, кроме служебного.

Оба процесса знают оба токена: этот бот отвечает пользователю через ОСНОВНОЙ
бот (человек писал туда и ждёт ответа там), а основной бот отправляет обращение
сюда. Токен, которым отправлять, выбирается один раз в `admin_notifier`.

Если `TELEGRAM_ADMIN_BOT_TOKEN` не задан, этот процесс не запускается, а вся
админская часть остаётся в основном боте — прежнее поведение.
"""
from __future__ import annotations

import logging

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from wbnotify.config import Config
from wbnotify.db import db_session
from wbnotify.telegram import admin
from wbnotify.telegram.formatters import PARSE_MODE

logger = logging.getLogger(__name__)

PREFIX = "a"


def _cb(action: str, *args) -> str:
    return "|".join([PREFIX, action, *(str(a) for a in args)])


def _summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏢 Все кабинеты", callback_data=_cb("shops", 0))]])


def _page_keyboard(page: int, pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Назад", callback_data=_cb("shops", page - 1)))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Дальше ›", callback_data=_cb("shops", page + 1)))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton("📊 К сводке", callback_data=_cb("stats"))])
    return InlineKeyboardMarkup(rows)


def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Пускаем только чат владельца. Постороннему не отвечаем вовсе — админский
    бот не должен подтверждать даже факт своего назначения."""
    config: Config = context.bot_data["config"]
    return admin.is_admin(config, update.effective_chat.id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update, context):
        return
    await update.message.reply_text(
        "Служебный бот: статистика, оповещения о кабинетах и обращения в поддержку.\n"
        "Сводка — /admin\n\n"
        "На обращение отвечайте реплаем на сообщение с ним — ответ уйдёт человеку "
        "от имени основного бота."
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update, context):
        return
    config: Config = context.bot_data["config"]
    with db_session(config.db_path) as conn:
        await update.message.reply_text(
            admin.stats_text(conn), parse_mode=PARSE_MODE, reply_markup=_summary_keyboard()
        )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _guard(update, context):
        return

    config: Config = context.bot_data["config"]
    parts = query.data.split("|")
    action = parts[1]

    with db_session(config.db_path) as conn:
        if action == "shops":
            text, page, pages = admin.shops_page(conn, int(parts[2]))
            keyboard = _page_keyboard(page, pages)
        else:
            text, keyboard = admin.stats_text(conn), _summary_keyboard()

    try:
        await query.edit_message_text(text, parse_mode=PARSE_MODE, reply_markup=keyboard)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def on_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ владельца реплаем на обращение — пересылаем автору ОСНОВНЫМ ботом."""
    if not _guard(update, context):
        return
    reply_to = update.message.reply_to_message
    if reply_to is None:
        await update.message.reply_text("Чтобы ответить, сделайте реплай на сообщение с обращением.")
        return

    config: Config = context.bot_data["config"]
    with db_session(config.db_path) as conn:
        thread = admin.thread_by_message(conn, reply_to.message_id)
    if thread is None:
        await update.message.reply_text("Не нашёл обращение, на которое это ответ.")
        return

    from wbnotify.telegram.sender import make_bot

    user_bot = make_bot(config.telegram_bot_token)
    await user_bot.send_message(
        chat_id=thread["chat_id"],
        text=f"💬 <b>Ответ поддержки</b>\n\n{update.message.text}",
        parse_mode=PARSE_MODE,
    )
    await update.message.reply_text("Отправлено.")


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand("admin", "📊 Сводка по кабинетам")])


def build_application(config: Config) -> Application:
    if not config.telegram_admin_bot_token:
        raise RuntimeError("TELEGRAM_ADMIN_BOT_TOKEN не задан — служебный бот запускать нечем")

    request = HTTPXRequest(connect_timeout=10.0, read_timeout=30.0, write_timeout=30.0)
    app = (
        Application.builder()
        .token(config.telegram_admin_bot_token)
        .request(request)
        .post_init(_post_init)
        .build()
    )
    app.bot_data["config"] = config

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", stats_command))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_reply))
    return app


def run() -> None:
    from wbnotify.config import load_config
    from wbnotify.db import init_db
    from wbnotify.logging_conf import setup_logging

    config = load_config()
    setup_logging(config.log_level)
    init_db(config.db_path)
    app = build_application(config)
    logger.info("Служебный бот запущен, поллинг Telegram...")
    app.run_polling()


if __name__ == "__main__":
    run()
