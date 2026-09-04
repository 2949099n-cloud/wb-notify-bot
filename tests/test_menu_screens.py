"""Экраны меню и разбор WB-токена."""
from __future__ import annotations

import base64
import json
import time

from wbnotify import members_repo
from wbnotify.db import utcnow
from wbnotify.security.jwt_info import parse_token
from wbnotify.telegram import menu

OWNER_ID, OWNER_CHAT = 100, 1000
MANAGER_ID, MANAGER_CHAT = 200, 2000


def _shop(conn) -> int:
    now = utcnow()
    shop_id = conn.execute(
        """
        INSERT INTO shops (owner_user_id, telegram_chat_id, name, wb_api_token_encrypted,
                           token_status, is_active, created_at, updated_at)
        VALUES (?, ?, 'NILONIL', X'00', 'active', 1, ?, ?)
        """,
        (OWNER_ID, OWNER_CHAT, now, now),
    ).lastrowid
    conn.commit()
    members_repo.add_owner(conn, shop_id, OWNER_ID, OWNER_CHAT)
    members_repo.upsert_user(conn, OWNER_ID, "Natali", OWNER_CHAT)
    return shop_id


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _fake_token(mask: int, exp: int, acc: int = 3) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"s": mask, "exp": exp, "acc": acc, "t": False}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature-tail"


def test_main_menu_greets_by_name_and_counts_shops(conn):
    _shop(conn)
    text, markup = menu.main_menu(conn, OWNER_ID)
    assert "Привет, <b>Natali</b>" in text
    assert "Кабинетов: <b>1</b>" in text
    assert _buttons(markup) == [
        "🔔 Настройки кабинета",
        "💳 Подписка и оплата",
        "➕ Подключить ещё кабинет",
        "👤 Профиль",
    ]


def test_manager_does_not_see_owner_only_sections(conn):
    """Менеджеру доступны уведомления и просмотр — управляющих кнопок нет."""
    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)

    owner_buttons = _buttons(menu.shop_menu(conn, shop_id, OWNER_ID)[1])
    manager_buttons = _buttons(menu.shop_menu(conn, shop_id, MANAGER_ID)[1])

    assert "👥 Команда" in owner_buttons and "⚙️ Управление кабинетом" in owner_buttons
    assert manager_buttons == ["🔑 Токены WB", "‹ Назад"]


def test_team_screen_lists_owner_and_manager(conn):
    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)
    members_repo.upsert_user(conn, MANAGER_ID, "Igor", MANAGER_CHAT)

    text, markup = menu.team_screen(conn, shop_id, OWNER_ID)
    assert "Участники (2)" in text
    assert "👑 Natali — Владелец <i>(вы)</i>" in text
    assert "🧑 Igor — Менеджер" in _buttons(markup)


def test_invite_screen_builds_start_link(conn):
    shop_id = _shop(conn)
    text, _ = menu.invite_screen(conn, shop_id, OWNER_ID, "SuperWBbot")
    assert "https://t.me/SuperWBbot?start=inv_" in text
    assert "14 дней" in text


def test_token_texts_use_our_bot_name_not_reference(conn):
    """В шаблонах не должно остаться имени референсного бота."""
    text, _ = menu.token_warning_screen(1, "SuperBot WB")
    assert "SuperBot WB" in text
    assert "Fenix" not in text
    assert "не снимает и не переводит деньги" in text


def test_parse_token_reads_scopes_expiry_and_readonly():
    """Маска настоящего токена магазина: биты 1-7,10,11,13 + 30 (только чтение)."""
    mask = sum(1 << bit for bit in (1, 2, 3, 4, 5, 6, 7, 10, 11, 13, 30))
    info = parse_token(_fake_token(mask, exp=int(time.time()) + 86400))

    assert info.token_type == "Персональный"
    assert info.is_readonly is True
    assert info.is_expired is False
    assert info.scopes == [
        "Контент",
        "Аналитика",
        "Цены и скидки",
        "Маркетплейс",
        "Статистика",
        "Продвижение",
        "Вопросы и отзывы",
        "Поставки",
        "Возвраты покупателями",
        "Финансы",
    ]
    assert info.tail == "tail"


def test_parse_token_marks_expired():
    info = parse_token(_fake_token(2, exp=int(time.time()) - 10))
    assert info.is_expired is True


def test_parse_token_survives_garbage():
    """Нестандартный токен не должен ронять экран — только обеднять его."""
    assert parse_token("не-джвт-вообще") is None
