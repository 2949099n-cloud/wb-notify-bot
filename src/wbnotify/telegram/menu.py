"""Меню бота: кабинеты, токены, команда, подписка, профиль.

Структура экранов и тексты заданы пользователем (по образцу референсного бота).
Название бота НЕ зашито: подставляется живое имя из getMe — иначе в текстах
стояло бы чужое имя из референса.

Навигация целиком на инлайн-кнопках, каждое нажатие ПЕРЕРИСОВЫВАЕТ то же
сообщение (edit_message_text), а не плодит новые — иначе после десятка нажатий
чат забивается копиями меню.

callback_data: "m|<действие>|<аргументы>". Разделитель "|" намеренно другой,
чем ":" у кнопок под уведомлениями (keyboards.py) — так один обработчик
однозначно понимает, чьё нажатие пришло, и старые кнопки не ломаются.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from wbnotify import members_repo, shops_repo
from wbnotify.config import Config
from wbnotify.db import db_session
from wbnotify.security.jwt_info import SCOPE_BITS, parse_token
from wbnotify.telegram.formatters import PARSE_MODE, _esc

logger = logging.getLogger(__name__)

PREFIX = "m"
# Флаг «чего ждём от пользователя» в user_data (см. telegram/bot.py). Меню его
# ставит, обработчик текста разбирает. Любой другой переход по меню его снимает —
# поэтому «зависнуть» в ожидании ввода нельзя.
AWAIT_KEY = "awaiting_input"
RULE = "━━━━━━━━━━━━━━━━━━━"

# Подписка сейчас бесплатная (решение пользователя): ни цен, ни тарифов в
# интерфейсе нет. Когда платежи понадобятся, добавлять их надо здесь и в
# billing_screen, а не возвращать прежний экран тарифов.
SUBSCRIPTION_IS_FREE = True


def cb(action: str, *args) -> str:
    return "|".join([PREFIX, action, *(str(a) for a in args)])


def parse(data: str) -> tuple[str, list[str]]:
    parts = data.split("|")
    return parts[1], parts[2:]


def is_menu_callback(data: str) -> bool:
    return data.startswith(f"{PREFIX}|")


def _date(value: str | None) -> str:
    """ISO-дата из БД -> ДД.ММ.ГГГГ. Пусто/мусор -> «—», меню не должно падать
    из-за одной кривой отметки времени."""
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return "—"


def _bot_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.bot_data.get("bot_name") or "бот"


# ── Главное меню ──────────────────────────────────────────────────────────────


def main_menu(conn: sqlite3.Connection, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    shops = members_repo.list_user_shops(conn, user_id)
    text = (
        f"🏠 <b>Меню</b>\n{RULE}\n"
        f"Привет, <b>{_esc(members_repo.display_name(conn, user_id))}</b>.\n"
        f"Кабинетов: <b>{len(shops)}</b>."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔔 Настройки кабинета", callback_data=cb("shops"))],
            [InlineKeyboardButton("💳 Подписка и оплата", callback_data=cb("billing"))],
            [InlineKeyboardButton("➕ Подключить ещё кабинет", callback_data=cb("addshop"))],
            [InlineKeyboardButton("👤 Профиль", callback_data=cb("profile"))],
        ]
    )
    return text, keyboard


def _shop_picker(conn: sqlite3.Connection, user_id: int, action: str) -> tuple[str, InlineKeyboardMarkup]:
    shops = members_repo.list_user_shops(conn, user_id)
    rows = [
        [InlineKeyboardButton(f"🏢 {shop['name']}", callback_data=cb(action, shop["id"]))] for shop in shops
    ]
    rows.append([InlineKeyboardButton("‹ Назад", callback_data=cb("main"))])
    return f"Выберите кабинет:\n{RULE}", InlineKeyboardMarkup(rows)


# ── Кабинет ───────────────────────────────────────────────────────────────────


def shop_menu(conn: sqlite3.Connection, shop_id: int, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    shop = shops_repo.get_shop(conn, shop_id)
    role = members_repo.role_of(conn, shop_id, user_id)
    text = (
        f"🏢 <b>{_esc(shop.name)}</b>\n{RULE}\n"
        f"Выберите раздел кабинета:\n"
        f"Ваша роль: <b>{members_repo.ROLE_TITLES.get(role, role)}</b>"
    )
    rows = [[InlineKeyboardButton("🔑 Токены WB", callback_data=cb("tok", shop_id))]]
    # Менеджеру доступны только уведомления и просмотр (решение пользователя),
    # поэтому управляющие разделы ему не показываем вовсе — не рисуем кнопки,
    # которые всё равно откажут.
    if role == "owner":
        rows += [
            [InlineKeyboardButton("👥 Команда", callback_data=cb("team", shop_id))],
            [InlineKeyboardButton("💳 Подписка", callback_data=cb("subfree", shop_id))],
            [InlineKeyboardButton("⚙️ Управление кабинетом", callback_data=cb("manage", shop_id))],
        ]
    rows.append([InlineKeyboardButton("‹ Назад", callback_data=cb("main"))])
    return text, InlineKeyboardMarkup(rows)


# ── Токены ────────────────────────────────────────────────────────────────────


def tokens_screen(conn: sqlite3.Connection, shop_id: int, user_id: int, config: Config):
    shop = shops_repo.get_shop(conn, shop_id)
    role = members_repo.role_of(conn, shop_id, user_id)

    lines = [
        f"🔑 <b>{_esc(shop.name)}</b>",
        RULE,
        f"<i>Подключён:</i> <b>{_date(shop.created_at)}</b>",
        f"<i>Ваша роль:</i> <b>{members_repo.ROLE_TITLES.get(role, role)}</b>",
        "",
    ]

    if shop.token_status != "active":
        lines.append("🔴 Токен помечен недействительным — опрос WB остановлен,")
        lines.append("уведомления по этому кабинету не приходят.")
        lines.append("")
        lines.append("Если токен на самом деле рабочий (например, его отозвали по ошибке),")
        lines.append("нажмите «Возобновить опрос» — я проверю его живым запросом к WB.")
    else:
        info = parse_token(shops_repo.get_decrypted_token(conn, shop_id, config.token_encryption_key))
        if info is None:
            lines.append("🟢 Токен подключён.")
        else:
            mark = "🔴" if info.is_expired else "🟢"
            lines.append(f"{mark} Токен …{_esc(info.tail)}")
            if info.expires_at:
                word = "истёк" if info.is_expired else "действует до"
                lines.append(f"{word} <b>{info.expires_at.strftime('%d.%m.%Y')}</b>")
            extra = " · только чтение" if info.is_readonly else ""
            lines.append(f"Тип: {info.token_type}{extra}")
            lines.append("")
            lines.append(f"<i>Доступ ({len(info.scopes)} из {len(SCOPE_BITS)}):</i>")
            lines.append(_esc(", ".join(info.scopes)) if info.scopes else "—")

    rows = []
    if role == "owner":
        if shop.token_status == "active":
            rows.append(
                [
                    InlineKeyboardButton("Заменить", callback_data=cb("tokrep", shop_id)),
                    InlineKeyboardButton("Отозвать", callback_data=cb("tokrev", shop_id)),
                ]
            )
        else:
            rows.append([InlineKeyboardButton("▶️ Возобновить опрос", callback_data=cb("tokon", shop_id))])
            rows.append([InlineKeyboardButton("Заменить", callback_data=cb("tokrep", shop_id))])
    rows.append([InlineKeyboardButton("‹ Назад", callback_data=cb("shop", shop_id))])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# Шаги создания токена в личном кабинете WB — одни и те же при подключении
# кабинета и при замене токена, поэтому вынесены, чтобы тексты не разъезжались.
_TOKEN_STEPS = (
    "<b>1.</b> Откройте «Доступ к API» в кабинете WB. Создавать токены может только владелец.\n\n"
    "<b>2.</b> Нажмите «Создать токен» и заполните:\n"
    "• Имя — на ваш выбор\n"
    "• Тип — <b>Персональный</b>\n"
    "• Только чтение — <b>включить</b>\n"
    "• Срок — <b>180 дней</b>\n\n"
    "<b>Категории доступа.</b> Обязательны эти три:\n"
    "✅ <b>Статистика</b> — продажи, заказы, остатки\n"
    "✅ <b>Контент</b> — названия и характеристики товаров\n"
    "✅ <b>Аналитика</b> — финрезультаты и комиссии\n\n"
    "Остальные категории отметьте для полной картины (финансы, реклама, отзывы, цены, "
    "поставки и др.). Не отмечайте только ❌ <b>Пользователи</b> — она боту не нужна.\n\n"
    "⚠️ Токен покажут <b>один раз</b> — копируйте сразу.\n\n"
    "<b>3.</b> Пришлите его сюда одним сообщением. Длинная строка, начинается с eyJ.\n"
    "Сообщение с токеном я удалю из чата сразу после обработки."
)


def _token_warning_text(bot_name: str) -> str:
    return (
        f"🔐 <b>Прежде чем начать</b>\n{RULE}\n\n"
        "<b>Зачем нужен токен?</b>\n"
        f"Токен — это ключ доступа к вашему кабинету WB. Он нужен, чтобы {_esc(bot_name)} мог "
        "читать ваши данные: продажи, заказы, остатки, финрезультаты, комиссии.\n\n"
        "<b>Что мы с ним делаем?</b>\n"
        "Только читаем. Чем полнее доступ вы дадите, тем точнее аналитика — "
        f"{_esc(bot_name)} просто показывает правду о ваших данных.\n\n"
        "<b>Что мы НЕ делаем?</b>\n"
        f"{_esc(bot_name)} только читает и никогда ничего не меняет в вашем кабинете:\n"
        "❌ не снимает и не переводит деньги\n"
        "❌ не меняет цены и скидки\n"
        "❌ не создаёт поставки и заказы\n"
        "❌ не трогает карточки товаров"
    )


def token_warning_screen(shop_id: int, bot_name: str):
    """Памятка перед ЗАМЕНОЙ токена уже подключённого кабинета."""
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Понятно, продолжить", callback_data=cb("tokrep2", shop_id))],
            [InlineKeyboardButton("❌ Отмена", callback_data=cb("tok", shop_id))],
        ]
    )
    return _token_warning_text(bot_name), keyboard


def add_warning_screen(bot_name: str):
    """Та же памятка, но ведёт в ПОДКЛЮЧЕНИЕ нового кабинета."""
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Понятно, продолжить", callback_data=cb("addtok"))],
            [InlineKeyboardButton("❌ Отмена", callback_data=cb("main"))],
        ]
    )
    return _token_warning_text(bot_name), keyboard


def add_instructions_screen():
    text = (
        f"🔑 <b>Добавление WB-токена</b>\n{RULE}\n"
        "Токен — это <b>не пароль</b>. Он только читает данные кабинета, ничего не меняет.\n\n"
        f"{_TOKEN_STEPS}"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=cb("main"))]])
    return text, keyboard


def rename_screen(conn: sqlite3.Connection, user_id: int):
    text = (
        f"✏️ <b>Смена имени</b>\n{RULE}\n"
        f"<i>Сейчас:</i> <b>{_esc(members_repo.display_name(conn, user_id))}</b>\n\n"
        "Пришлите новое имя одним сообщением."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=cb("profile"))]])
    return text, keyboard


def delete_account_screen():
    text = (
        f"⚠️ <b>Удаление аккаунта</b>\n{RULE}\n"
        "Будут удалены ваше имя, доступ ко всем кабинетам и подключённые вами кабинеты — "
        "опрос WB по ним прекратится, уведомления перестанут приходить, команда потеряет доступ.\n\n"
        "История заказов и продаж останется в базе.\n"
        "<i>Действие необратимо. Подтвердите или отмените.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❌ Отмена", callback_data=cb("profile")),
                InlineKeyboardButton("⚠️ Подтвердить", callback_data=cb("delaccok")),
            ]
        ]
    )
    return text, keyboard


def token_instructions_screen(shop_id: int):
    text = (
        f"🔁 <b>Замена WB-токена</b>\n{RULE}\n"
        "Старый токен будет отозван автоматически после успешной проверки нового.\n\n"
        f"{_TOKEN_STEPS}"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=cb("tok", shop_id))]])
    return text, keyboard


def token_revoke_screen(conn: sqlite3.Connection, shop_id: int, config: Config):
    info = None
    if shops_repo.get_shop(conn, shop_id).token_status == "active":
        info = parse_token(shops_repo.get_decrypted_token(conn, shop_id, config.token_encryption_key))
    target = f"…{info.tail}" if info else "текущий токен"
    text = (
        f"⚠️ <b>Отозвать токен</b>\n{RULE}\n"
        f"<i>Цель:</i> <b>{_esc(target)}</b>\n\n"
        "Кабинет перестанет опрашиваться, уведомления прекратятся.\n"
        "<i>Действие необратимо. Подтвердите или отмените.</i>\n\n"
        "Сам токен останется живым в личном кабинете WB — удалить его там нужно "
        "вручную, в разделе «Доступ к API»."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❌ Отмена", callback_data=cb("tok", shop_id)),
                InlineKeyboardButton("⚠️ Подтвердить", callback_data=cb("tokrevok", shop_id)),
            ]
        ]
    )
    return text, keyboard


# ── Команда ───────────────────────────────────────────────────────────────────


def team_screen(conn: sqlite3.Connection, shop_id: int, user_id: int):
    shop = shops_repo.get_shop(conn, shop_id)
    members = members_repo.list_members(conn, shop_id)
    lines = [f"👥 <b>Команда · {_esc(shop.name)}</b>", RULE, f"<i>Участники ({len(members)}):</i>"]
    rows = []
    for member in members:
        title = members_repo.ROLE_TITLES.get(member["role"], member["role"])
        if member["role"] == "owner":
            you = " <i>(вы)</i>" if member["telegram_user_id"] == user_id else ""
            lines.append(f"👑 {_esc(member['name'])} — {title}{you}")
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"💼 {member['name']} — {title}",
                        callback_data=cb("mem", shop_id, member["telegram_user_id"]),
                    )
                ]
            )
    rows.append([InlineKeyboardButton("➕ Пригласить нового", callback_data=cb("inv", shop_id))])
    rows.append([InlineKeyboardButton("‹ Назад", callback_data=cb("shop", shop_id))])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def member_screen(conn: sqlite3.Connection, shop_id: int, member_id: int):
    name = members_repo.display_name(conn, member_id)
    text = f"💼 <b>{_esc(name)}</b>\n{RULE}\nРоль: <b>Менеджер</b>\nПолучает уведомления по этому кабинету."
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 Удалить из кабинета", callback_data=cb("memdel", shop_id, member_id))],
            [InlineKeyboardButton("‹ Назад", callback_data=cb("team", shop_id))],
        ]
    )
    return text, keyboard


def invite_screen(conn: sqlite3.Connection, shop_id: int, user_id: int, bot_username: str):
    token, expires_at = members_repo.create_invite(conn, shop_id, user_id)
    link = f"https://t.me/{bot_username}?start=inv_{token}"
    text = (
        f"✉️ <b>Приглашение готово</b>\n{RULE}\n"
        f"<i>Срок действия:</i> {members_repo.INVITE_TTL_DAYS} дней "
        f"(до {expires_at.strftime('%d.%m.%Y')})\n"
        "<i>Канал:</i> Telegram (этот бот)\n\n"
        f"🔗 <i>Ссылка для получателя:</i>\n{_esc(link)}\n\n"
        "⚠️ Эта ссылка работает только при открытии в Telegram и только один раз."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("‹ Назад", callback_data=cb("team", shop_id))]])
    return text, keyboard


# ── Подписка, управление, профиль ─────────────────────────────────────────────


def manage_screen(conn: sqlite3.Connection, shop_id: int):
    shop = shops_repo.get_shop(conn, shop_id)
    text = (
        f"⚙️ <b>Управление кабинетом</b>\n{RULE}\n"
        f"<i>Кабинет:</i> <b>{_esc(shop.name)}</b>\n\n"
        "<i>Сменить поставщика</i> — удалит этот кабинет и предложит подключить новый."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Сменить поставщика", callback_data=cb("chgsup", shop_id))],
            [InlineKeyboardButton("🗑 Удалить кабинет", callback_data=cb("del", shop_id))],
            [InlineKeyboardButton("‹ Назад", callback_data=cb("shop", shop_id))],
        ]
    )
    return text, keyboard


def delete_confirm_screen(conn: sqlite3.Connection, shop_id: int, then_add: bool):
    shop = shops_repo.get_shop(conn, shop_id)
    tail = "\n\nПосле удаления пришлите /addshop, чтобы подключить нового поставщика." if then_add else ""
    text = (
        f"⚠️ <b>Удаление кабинета</b>\n{RULE}\n"
        f"<i>Кабинет:</i> <b>{_esc(shop.name)}</b>\n\n"
        "Опрос WB прекратится, уведомления перестанут приходить, команда потеряет доступ.\n"
        "История заказов и продаж сохранится в базе.\n"
        f"<i>Действие необратимо. Подтвердите или отмените.</i>{tail}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❌ Отмена", callback_data=cb("manage", shop_id)),
                InlineKeyboardButton("⚠️ Подтвердить", callback_data=cb("delok", shop_id)),
            ]
        ]
    )
    return text, keyboard


def profile_screen(conn: sqlite3.Connection, user_id: int, chat_id: int):
    text = (
        f"👤 <b>Профиль</b>\n{RULE}\n"
        f"<i>Имя:</i> <b>{_esc(members_repo.display_name(conn, user_id))}</b>\n\n"
        f"<i>Каналы:</i>\n✅ Telegram ({chat_id})\n\n"
        f"<i>Кабинетов:</i> {len(members_repo.list_user_shops(conn, user_id))}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Сменить имя", callback_data=cb("rename"))],
            [InlineKeyboardButton("🗑 Удалить аккаунт", callback_data=cb("delacc"))],
            [InlineKeyboardButton("‹ Назад", callback_data=cb("main"))],
        ]
    )
    return text, keyboard


def billing_screen(conn: sqlite3.Connection, user_id: int):
    shops = members_repo.list_user_shops(conn, user_id)
    lines = ["💳 <b>Подписка и оплата</b>", RULE, "<b>Подписка сейчас бесплатная</b> — платить ничего не нужно.", ""]
    if not shops:
        lines.append("Кабинет ещё не подключён.")
    else:
        lines.append("<i>Ваши кабинеты:</i>")
        for shop in shops:
            lines.append(f"🏢 {_esc(shop['name'])} — доступ открыт")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("‹ Назад", callback_data=cb("main"))]])
    return "\n".join(lines), keyboard


# ── Роутер ────────────────────────────────────────────────────────────────────


class _Ack:
    """Ответ на нажатие кнопки — ровно один раз.

    Telegram принимает answerCallbackQuery на один query только однократно, а
    ветки роутера то показывают всплывающее предупреждение, то просто
    перерисовывают экран. Без этой обёртки ветка с предупреждением отвечала бы
    вторым вызовом и падала с BadRequest.
    """

    def __init__(self, query):
        self._query = query
        self._done = False

    async def __call__(self, text: str | None = None, alert: bool = False) -> None:
        if self._done:
            return
        self._done = True
        await self._query.answer(text, show_alert=alert)


async def _show(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(text, parse_mode=PARSE_MODE, reply_markup=keyboard)
    except BadRequest as exc:
        # «Message is not modified» — пользователь нажал ту же кнопку повторно.
        # Не ошибка, показывать её незачем.
        if "not modified" not in str(exc).lower():
            raise


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    ack = _Ack(query)
    action, args = parse(query.data)
    config: Config = context.bot_data["config"]
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    shop_id = int(args[0]) if args and args[0].lstrip("-").isdigit() else None

    # Переход по меню отменяет ожидание ввода: пользователь передумал присылать
    # токен или имя. Ветки, которые ввод как раз запрашивают, ставят флаг заново.
    context.user_data.pop(AWAIT_KEY, None)

    with db_session(config.db_path) as conn:
        # Изоляция: к кабинету допускаются только его участники. Проверяем ДО
        # любого действия, а не по факту — иначе чужой shop_id в callback_data
        # открыл бы данные другого продавца.
        if shop_id:
            role = members_repo.role_of(conn, shop_id, user_id)
            if role is None:
                await ack("У вас нет доступа к этому кабинету.", alert=True)
                return
            owner_only = action not in ("shop", "tok")
            if owner_only and role != "owner":
                await ack("Это может сделать только владелец кабинета.", alert=True)
                return

        if action == "main":
            await _show(query, *main_menu(conn, user_id))

        elif action == "shops":
            shops = members_repo.list_user_shops(conn, user_id)
            if not shops:
                await ack("Кабинет ещё не подключён — пришлите /addshop.", alert=True)
            elif len(shops) == 1:
                await _show(query, *shop_menu(conn, shops[0]["id"], user_id))
            else:
                await _show(query, *_shop_picker(conn, user_id, "shop"))

        elif action == "shop":
            await _show(query, *shop_menu(conn, shop_id, user_id))

        elif action == "tok":
            await _show(query, *tokens_screen(conn, shop_id, user_id, config))

        elif action == "tokrep":
            await _show(query, *token_warning_screen(shop_id, _bot_name(context)))

        elif action == "tokrev":
            await _show(query, *token_revoke_screen(conn, shop_id, config))

        elif action == "tokon":
            try:
                await shops_repo.resume_token(conn, shop_id, config.token_encryption_key)
            except shops_repo.InvalidTokenError as exc:
                await ack(f"Не вышло: {exc}", alert=True)
            else:
                await ack("Опрос возобновлён, уведомления снова пойдут.", alert=True)
            await _show(query, *tokens_screen(conn, shop_id, user_id, config))

        elif action == "tokrevok":
            shops_repo.revoke_token(conn, shop_id)
            await _show(query, *tokens_screen(conn, shop_id, user_id, config))
            await ack("Токен отозван, опрос кабинета остановлен.", alert=True)

        elif action == "team":
            await _show(query, *team_screen(conn, shop_id, user_id))

        elif action == "mem":
            await _show(query, *member_screen(conn, shop_id, int(args[1])))

        elif action == "memdel":
            members_repo.remove_member(conn, shop_id, int(args[1]))
            await _show(query, *team_screen(conn, shop_id, user_id))
            await ack("Участник удалён из кабинета.")

        elif action == "inv":
            await _show(query, *invite_screen(conn, shop_id, user_id, context.bot_data["bot_username"]))

        elif action == "addtok":
            context.user_data[AWAIT_KEY] = ("addshop", None)
            await _show(query, *add_instructions_screen())

        elif action == "tokrep2":
            context.user_data[AWAIT_KEY] = ("replace_token", shop_id)
            await _show(query, *token_instructions_screen(shop_id))

        elif action == "rename":
            context.user_data[AWAIT_KEY] = ("rename", None)
            await _show(query, *rename_screen(conn, user_id))

        elif action == "subfree":
            await ack("Подписка сейчас бесплатная — платить ничего не нужно.", alert=True)

        elif action == "addshop":
            await _show(query, *add_warning_screen(_bot_name(context)))

        elif action == "delacc":
            await _show(query, *delete_account_screen())

        elif action == "delaccok":
            members_repo.delete_account(conn, user_id)
            await _show(query, *main_menu(conn, user_id))
            await ack("Аккаунт удалён.", alert=True)

        elif action == "billing":
            await _show(query, *billing_screen(conn, user_id))

        elif action in ("sub", "pay"):
            # Кнопки со старых экранов тарифов — они ещё висят в чате.
            await ack("Подписка сейчас бесплатная — платить ничего не нужно.", alert=True)

        elif action == "manage":
            await _show(query, *manage_screen(conn, shop_id))

        elif action in ("del", "chgsup"):
            await _show(query, *delete_confirm_screen(conn, shop_id, then_add=action == "chgsup"))

        elif action == "delok":
            shops_repo.deactivate_shop(conn, shop_id)
            await _show(query, *main_menu(conn, user_id))
            await ack("Кабинет удалён.", alert=True)

        elif action == "profile":
            await _show(query, *profile_screen(conn, user_id, chat_id))

        else:
            logger.warning("Неизвестное действие меню: %s", query.data)
            await ack("Кнопка устарела, откройте меню заново: /menu", alert=True)

    await ack()
