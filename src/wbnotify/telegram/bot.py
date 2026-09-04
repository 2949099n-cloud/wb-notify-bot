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
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from wbnotify import members_repo, shops_repo
from wbnotify.config import Config
from wbnotify.db import db_session
from wbnotify.telegram import admin, admin_notifier, menu
from wbnotify.telegram.formatters import PARSE_MODE, format_stocks_detail
from wbnotify.telegram.keyboards import ACTION_APPEARANCE, ACTION_MUTE, ACTION_STOCKS, parse_callback

logger = logging.getLogger(__name__)

INVITE_PREFIX = "inv_"


def _remember_user(context: ContextTypes.DEFAULT_TYPE, update: Update) -> None:
    """Держим имя и личный чат пользователя: имя нужно «Профилю» и списку
    команды, чат — рассылке уведомлений приглашённому менеджеру."""
    user = update.effective_user
    config: Config = context.bot_data["config"]
    with db_session(config.db_path) as conn:
        members_repo.upsert_user(
            conn,
            user.id,
            user.full_name or user.username or str(user.id),
            update.effective_chat.id,
            user.username,
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


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Статистика по всем кабинетам — только владельцу бота."""
    config: Config = context.bot_data["config"]
    if not admin.is_admin(config, update.effective_chat.id):
        if config.telegram_admin_chat_id is None:
            await update.message.reply_text(
                "Панель выключена: в .env не задан TELEGRAM_ADMIN_CHAT_ID."
            )
        return  # чужому пользователю не отвечаем вовсе — команды как будто нет

    if admin_notifier.uses_separate_bot(config):
        await update.message.reply_text("Статистика переехала в служебный бот — откройте его и наберите /admin.")
        return

    with db_session(config.db_path) as conn:
        await update.message.reply_text(admin.stats_text(conn), parse_mode=PARSE_MODE)


# ── Ввод текста: подключение кабинета, замена токена, переименование ──────────
#
# Раньше эти три сценария были на ConversationHandler, и пользователь получал
# «Кнопка устарела» на кнопках «Сменить имя» и «Понятно, продолжить». Причина:
# незавершённый диалог остаётся активным навсегда (таймаута не было), а пока он
# активен, ConversationHandler проверяет только обработчики ТЕКУЩЕГО состояния —
# и собственную точку входа больше не видит. Нажатие проваливалось в общий
# обработчик меню, который такого действия не знает.
#
# Вместо диалогов — явный флаг «чего ждём от пользователя» в user_data. Он
# сбрасывается при любом переходе по меню, поэтому «зависнуть» не может, а
# логика видна в одном месте.

AWAIT_KEY = "awaiting_input"


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


async def addshop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addshop — сразу инструкция и ожидание токена (памятку показывает меню)."""
    _remember_user(context, update)
    context.user_data[AWAIT_KEY] = ("addshop", None)
    text, keyboard = menu.add_instructions_screen()
    await update.message.reply_text(text, parse_mode=PARSE_MODE, reply_markup=keyboard)


async def _do_addshop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    raw_token = await _consume_token_message(update, context)

    with db_session(config.db_path) as conn:
        try:
            shop = await shops_repo.register_shop(conn, user_id, chat_id, raw_token, config.token_encryption_key)
        except shops_repo.InvalidTokenError as exc:
            reply_text = f"Не удалось подключить кабинет: {exc}\nПопробуйте ещё раз: /addshop"
        else:
            reply_text = f"✅ Кабинет «{shop.name}» подключён. Уведомления по заказам будут приходить сюда."

    await context.bot.send_message(chat_id=chat_id, text=reply_text)


async def _do_replace_token(update: Update, context: ContextTypes.DEFAULT_TYPE, shop_id: int) -> None:
    config: Config = context.bot_data["config"]
    chat_id = update.effective_chat.id
    raw_token = await _consume_token_message(update, context)

    with db_session(config.db_path) as conn:
        if members_repo.role_of(conn, shop_id, update.effective_user.id) != "owner":
            await context.bot.send_message(chat_id=chat_id, text="Это может сделать только владелец кабинета.")
            return
        try:
            shop = await shops_repo.replace_token(conn, shop_id, raw_token, config.token_encryption_key)
        except shops_repo.InvalidTokenError as exc:
            reply_text = f"Токен не подошёл: {exc}\nСтарый токен оставлен без изменений."
        else:
            reply_text = f"✅ Токен кабинета «{shop.name}» заменён. Опрос продолжается с новым токеном."

    await context.bot.send_message(chat_id=chat_id, text=reply_text)


async def _do_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    new_name = update.message.text.strip()[:64]
    with db_session(config.db_path) as conn:
        members_repo.rename_user(conn, update.effective_user.id, new_name)
    await update.message.reply_text(f"Готово, теперь вы — {new_name}. Меню — /menu")


async def _do_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пересылает обращение владельцу бота и запоминает, кому отвечать."""
    config: Config = context.bot_data["config"]
    if config.telegram_admin_chat_id is None:
        await update.message.reply_text("Поддержка сейчас недоступна. Попробуйте позже.")
        return

    user_id = update.effective_user.id
    with db_session(config.db_path) as conn:
        header = admin.support_header(conn, user_id)

    # Обращение уходит служебным ботом, если он настроен: у владельца в этом
    # боте своя лента заказов, и поддержка вперемешку с ней нечитаема.
    admin_bot = admin_notifier.make_admin_bot(config) or context.bot
    sent = await admin_bot.send_message(
        chat_id=config.telegram_admin_chat_id,
        text=f"{header}\n\n{update.message.text}",
        parse_mode=PARSE_MODE,
    )
    with db_session(config.db_path) as conn:
        admin.remember_thread(conn, sent.message_id, user_id, update.effective_chat.id)

    await update.message.reply_text(
        "✅ Обращение отправлено. Ответ придёт сюда же, в этот чат."
    )


async def _relay_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ответ владельца реплаем на обращение -> обратно автору. True, если это
    действительно был ответ на обращение и его обработали.

    Нужен только когда служебного бота нет: с ним ответы приходят туда, и их
    разбирает telegram/admin_bot.py.
    """
    config: Config = context.bot_data["config"]
    if admin_notifier.uses_separate_bot(config):
        return False
    if not admin.is_admin(config, update.effective_chat.id):
        return False
    reply_to = update.message.reply_to_message
    if reply_to is None:
        return False

    with db_session(config.db_path) as conn:
        thread = admin.thread_by_message(conn, reply_to.message_id)
    if thread is None:
        return False

    await context.bot.send_message(
        chat_id=thread["chat_id"], text=f"💬 <b>Ответ поддержки</b>\n\n{update.message.text}",
        parse_mode=PARSE_MODE,
    )
    await update.message.reply_text("Отправлено.")
    return True


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Единственный обработчик текста: смотрит, чего мы ждём от пользователя."""
    if await _relay_admin_reply(update, context):
        return

    awaiting = context.user_data.pop(AWAIT_KEY, None)
    if awaiting is None:
        await update.message.reply_text("Не понимаю. Откройте меню — /menu")
        return

    kind, arg = awaiting
    if kind == "addshop":
        await _do_addshop(update, context)
    elif kind == "replace_token":
        await _do_replace_token(update, context, int(arg))
    elif kind == "rename":
        await _do_rename(update, context)
    elif kind == "support":
        await _do_support(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(AWAIT_KEY, None)
    await update.message.reply_text("Отменено. Меню — /menu")


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
    BotCommand("cancel", "Отменить ввод"),
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
    app.add_handler(CommandHandler("addshop", addshop_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("unmute", unmute_command))

    app.add_handler(CallbackQueryHandler(on_button))
    # Текст обрабатываем последним: команды и нажатия кнопок уже разобраны выше,
    # сюда доходит только то, что пользователь набрал руками.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

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
