"""Telegram-бот: команды, меню и обработка нажатий.

Процесс отдельный от планировщика (`scheduler/runner.py`): тот только опрашивает
WB и рассылает уведомления, нажатия кнопок к нему не приходят.

Онбординг и замена токена идут через `shops_repo` — те же функции, что проверены
через `scripts/sync_cli.py`. Сообщение с сырым токеном удаляется из чата сразу
после обработки, успешной или нет (правило проекта).
"""
from __future__ import annotations

import logging

from telegram import BotCommand, Update
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

from wbnotify import members_repo, shops_repo
from wbnotify.config import Config
from wbnotify.db import db_session
from wbnotify.telegram import menu
from wbnotify.telegram.formatters import PARSE_MODE, format_stocks_detail
from wbnotify.telegram.keyboards import ACTION_APPEARANCE, ACTION_MUTE, ACTION_STOCKS, parse_callback

logger = logging.getLogger(__name__)

AWAITING_TOKEN = 1
AWAITING_NEW_TOKEN = 2
AWAITING_NAME = 3

INVITE_PREFIX = "inv_"


def _remember_user(context: ContextTypes.DEFAULT_TYPE, update: Update) -> None:
    """Держим имя и личный чат пользователя: имя нужно «Профилю» и списку
    команды, чат — рассылке уведомлений приглашённому менеджеру."""
    user = update.effective_user
    config: Config = context.bot_data["config"]
    with db_session(config.db_path) as conn:
        members_repo.upsert_user(
            conn, user.id, user.full_name or user.username or str(user.id), update.effective_chat.id
        )


# ── Команды ───────────────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_user(context, update)
    config: Config = context.bot_data["config"]

    # /start inv_<token> — переход по ссылке-приглашению в чужой кабинет.
    arg = context.args[0] if context.args else ""
    if arg.startswith(INVITE_PREFIX):
        with db_session(config.db_path) as conn:
            try:
                shop = members_repo.accept_invite(
                    conn, arg[len(INVITE_PREFIX):], update.effective_user.id, update.effective_chat.id
                )
            except members_repo.InviteError as exc:
                await update.message.reply_text(f"⚠️ {exc}")
                return
        await update.message.reply_text(
            f"✅ Вы подключены к кабинету «{shop['name']}» как менеджер.\n"
            "Уведомления по заказам и продажам будут приходить сюда. Меню — /menu"
        )
        return

    await update.message.reply_text(
        "Привет! Я слежу за заказами на Wildberries и присылаю уведомления.\n"
        "Подключить кабинет — /addshop\n"
        "Меню — /menu"
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_user(context, update)
    config: Config = context.bot_data["config"]
    with db_session(config.db_path) as conn:
        text, keyboard = menu.main_menu(conn, update.effective_user.id)
    await update.message.reply_text(text, parse_mode=PARSE_MODE, reply_markup=keyboard)


# ── Подключение кабинета ──────────────────────────────────────────────────────


async def addshop_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Экран с инструкцией и ожидание токена.

    Из меню сюда ведёт кнопка «Понятно, продолжить» на памятке (`m|addtok`),
    из команды /addshop — сразу, памятку в этом случае показывать негде.
    """
    _remember_user(context, update)
    text, keyboard = menu.add_instructions_screen()

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode=PARSE_MODE, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode=PARSE_MODE, reply_markup=keyboard)
    return AWAITING_TOKEN


async def _consume_token_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Забирает текст токена и удаляет сообщение с ним из чата."""
    raw_token = update.message.text.strip()
    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id, message_id=update.message.message_id
        )
    except TelegramError as exc:
        logger.warning("Не удалось удалить сообщение с токеном (chat_id=%s): %s", update.effective_chat.id, exc)
    return raw_token


async def addshop_receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config: Config = context.bot_data["config"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    raw_token = await _consume_token_message(update, context)

    with db_session(config.db_path) as conn:
        try:
            shop = await shops_repo.register_shop(conn, user_id, chat_id, raw_token, config.token_encryption_key)
        except shops_repo.InvalidTokenError as exc:
            reply_text = f"Не удалось подключить кабинет: {exc}\nПопробуйте /addshop ещё раз."
        else:
            reply_text = f"✅ Кабинет «{shop.name}» подключён. Уведомления по заказам будут приходить сюда."

    await context.bot.send_message(chat_id=chat_id, text=reply_text)
    return ConversationHandler.END


# ── Замена токена ─────────────────────────────────────────────────────────────


async def token_replace_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    _, args = menu.parse(query.data)
    shop_id = int(args[0])

    config: Config = context.bot_data["config"]
    with db_session(config.db_path) as conn:
        # Ответ на нажатие даём один раз: либо предупреждением, либо пустым.
        if members_repo.role_of(conn, shop_id, update.effective_user.id) != "owner":
            await query.answer("Это может сделать только владелец кабинета.", show_alert=True)
            return ConversationHandler.END
        await query.answer()
        text, keyboard = menu.token_instructions_screen(shop_id)

    context.user_data["replace_shop_id"] = shop_id
    await query.edit_message_text(text, parse_mode=PARSE_MODE, reply_markup=keyboard)
    return AWAITING_NEW_TOKEN


async def token_replace_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config: Config = context.bot_data["config"]
    chat_id = update.effective_chat.id
    shop_id = context.user_data.pop("replace_shop_id", None)
    raw_token = await _consume_token_message(update, context)

    if shop_id is None:
        await context.bot.send_message(chat_id=chat_id, text="Кабинет не выбран. Откройте /menu заново.")
        return ConversationHandler.END

    with db_session(config.db_path) as conn:
        if members_repo.role_of(conn, shop_id, update.effective_user.id) != "owner":
            await context.bot.send_message(chat_id=chat_id, text="Это может сделать только владелец кабинета.")
            return ConversationHandler.END
        try:
            shop = await shops_repo.replace_token(conn, shop_id, raw_token, config.token_encryption_key)
        except shops_repo.InvalidTokenError as exc:
            reply_text = f"Токен не подошёл: {exc}\nСтарый токен оставлен без изменений."
        else:
            reply_text = f"✅ Токен кабинета «{shop.name}» заменён. Опрос продолжается с новым токеном."

    await context.bot.send_message(chat_id=chat_id, text=reply_text)
    return ConversationHandler.END


# ── Переименование в профиле ──────────────────────────────────────────────────


async def rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Как вас называть? Пришлите новое имя одним сообщением. /cancel — отмена.")
    return AWAITING_NAME


async def rename_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    config: Config = context.bot_data["config"]
    new_name = update.message.text.strip()[:64]
    with db_session(config.db_path) as conn:
        members_repo.rename_user(conn, update.effective_user.id, new_name)
    await update.message.reply_text(f"Готово, теперь вы — {new_name}. Меню — /menu")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ── Кнопки под уведомлениями ──────────────────────────────────────────────────


EVENT_TITLES = {
    "order": "заказы",
    "cancel": "отмены заказов",
    "buyout": "продажи",
    "return": "возвраты",
}


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатия инлайн-кнопок: и меню, и кнопки под уведомлением.

    Кнопка живёт дольше сообщения: всё, кроме действия и товара, подтягивается
    из БД в момент нажатия, поэтому старое уведомление тоже отработает корректно.
    """
    query = update.callback_query

    # Меню отвечает на нажатие само: часть его веток показывает всплывающее
    # предупреждение, а ответить на один query можно только раз.
    if menu.is_menu_callback(query.data):
        await menu.handle(update, context)
        return

    await query.answer()  # убрать «часики» у кнопки

    config: Config = context.bot_data["config"]
    action, shop_id, nm_id, extra = parse_callback(query.data)
    chat_id = update.effective_chat.id

    with db_session(config.db_path) as conn:
        # Изоляция: кнопка действует только у участника этого кабинета.
        if members_repo.role_of(conn, shop_id, update.effective_user.id) is None:
            await query.message.reply_text("У вас нет доступа к этому кабинету.")
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
                f"🔕 Больше не присылаю «{EVENT_TITLES.get(extra, extra)}» по этому кабинету.\n"
                f"Вернуть — /unmute"
            )

        elif action == ACTION_APPEARANCE:
            await query.message.reply_text(_settings_text(conn, chat_id), parse_mode=PARSE_MODE)


def _settings_text(conn, chat_id: int) -> str:
    muted = conn.execute(
        "SELECT event_type, shop_id FROM chat_mutes WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    if not muted:
        return "⚙️ Настройки\n\nСейчас приходят все типы уведомлений."
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


# ── Сборка приложения ─────────────────────────────────────────────────────────


BOT_COMMANDS = [
    BotCommand("menu", "🧭 Меню кабинета"),
    BotCommand("addshop", "➕ Подключить кабинет"),
    BotCommand("start", "Приветствие"),
]


async def _post_init(app: Application) -> None:
    """Имя и @username бота берём у самого Telegram, а не константой: они
    подставляются в тексты меню и в ссылку-приглашение."""
    me = await app.bot.get_me()
    app.bot_data["bot_name"] = me.first_name
    app.bot_data["bot_username"] = me.username
    # Кнопка «Меню» слева от поля ввода — это и есть список команд бота.
    await app.bot.set_my_commands(BOT_COMMANDS)


def build_application(config: Config) -> Application:
    # HTTPXRequest с увеличенными таймаутами — дефолтные 5с оказались маловаты
    # для отправки фото (см. telegram/sender.py:make_bot).
    request = HTTPXRequest(connect_timeout=10.0, read_timeout=30.0, write_timeout=30.0, media_write_timeout=60.0)
    app = Application.builder().token(config.telegram_bot_token).request(request).post_init(_post_init).build()
    app.bot_data["config"] = config

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("unmute", unmute_command))

    # Диалоги регистрируются ДО общего CallbackQueryHandler: внутри одной группы
    # обработчиков побеждает первый подходящий, и иначе их точки входа
    # (нажатия кнопок) перехватил бы общий обработчик.
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("addshop", addshop_start),
                # Памятку «Прежде чем начать» рисует меню, сюда ведёт её кнопка
                # «Понятно, продолжить».
                CallbackQueryHandler(addshop_start, pattern=r"^m\|addtok$"),
            ],
            states={AWAITING_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, addshop_receive_token)]},
            fallbacks=[
                CommandHandler("cancel", cancel),
                # «Отмена» возвращает в главное меню — диалог обязан завершиться,
                # иначе следующее сообщение уйдёт в него как токен.
                CallbackQueryHandler(on_button, pattern=r"^m\|main$"),
            ],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(token_replace_start, pattern=r"^m\|tokrep2\|")],
            states={AWAITING_NEW_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, token_replace_receive)]},
            fallbacks=[
                CommandHandler("cancel", cancel),
                # Кнопка «Отмена» на экране инструкции ведёт обратно к токенам —
                # диалог при этом обязан завершиться, иначе следующее сообщение
                # пользователя уйдёт в него как токен.
                CallbackQueryHandler(on_button, pattern=r"^m\|tok\|"),
            ],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(rename_start, pattern=r"^m\|rename$")],
            states={AWAITING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_receive)]},
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )

    app.add_handler(CallbackQueryHandler(on_button))
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
