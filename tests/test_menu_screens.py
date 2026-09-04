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
    assert "💼 Igor — Менеджер" in _buttons(markup)


def test_invite_screen_builds_start_link(conn):
    shop_id = _shop(conn)
    text, _ = menu.invite_screen(conn, shop_id, OWNER_ID, "SuperWBbot")
    assert "https://t.me/SuperWBbot?start=inv_" in text
    assert "14 дней" in text


def test_tokens_screen_renders_real_token_data(conn, monkeypatch):
    """Регрессия: экран падал на обращении к полю, которого не было в ShopRow,
    и кнопка «Токены WB» просто не срабатывала."""
    from wbnotify import shops_repo
    from wbnotify.config import Config

    shop_id = _shop(conn)
    mask = sum(1 << bit for bit in (1, 2, 5, 30))
    monkeypatch.setattr(
        shops_repo, "get_decrypted_token", lambda *a, **kw: _fake_token(mask, int(time.time()) + 86400)
    )
    config = Config.__new__(Config)
    object.__setattr__(config, "token_encryption_key", "x")

    text, markup = menu.tokens_screen(conn, shop_id, OWNER_ID, config)
    assert "Подключён:" in text
    assert "Доступ (3 из 13)" in text
    assert "только чтение" in text
    assert _buttons(markup) == ["Заменить", "Отозвать", "‹ Назад"]


def test_add_flow_goes_warning_then_instructions():
    """Подключение кабинета: памятка → инструкция → ожидание токена."""
    warning_text, warning_kb = menu.add_warning_screen("Бот")
    assert "Прежде чем начать" in warning_text
    assert _buttons(warning_kb) == ["✅ Понятно, продолжить", "❌ Отмена"]
    assert warning_kb.inline_keyboard[0][0].callback_data == "m|addtok"

    text, keyboard = menu.add_instructions_screen()
    assert "Добавление WB-токена" in text
    assert "начинается с eyJ" in text
    assert _buttons(keyboard) == ["❌ Отмена"]


def test_manager_button_has_no_face_emoji(conn):
    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)
    members_repo.upsert_user(conn, MANAGER_ID, "Igor", MANAGER_CHAT)

    labels = _buttons(menu.team_screen(conn, shop_id, OWNER_ID)[1])
    assert "💼 Igor — Менеджер" in labels
    assert not any("🧑" in label for label in labels)


def test_subscription_button_is_inactive_while_free(conn):
    """Подписка бесплатная — кнопка ведёт на заглушку, а не на экран тарифов."""
    shop_id = _shop(conn)
    kb = menu.shop_menu(conn, shop_id, OWNER_ID)[1]
    sub = [b for row in kb.inline_keyboard for b in row if "Подписка" in b.text][0]
    assert sub.callback_data == f"m|subfree|{shop_id}"


def test_billing_screen_shows_no_prices(conn):
    """Подписка бесплатная — ни цен, ни тарифов на экране быть не должно."""
    _shop(conn)
    text, markup = menu.billing_screen(conn, OWNER_ID)
    assert "бесплатная" in text
    assert "₽" not in text
    assert _buttons(markup) == ["‹ Назад"]


def test_profile_has_delete_account(conn):
    _shop(conn)
    assert "🗑 Удалить аккаунт" in _buttons(menu.profile_screen(conn, OWNER_ID, OWNER_CHAT)[1])


def test_delete_account_removes_access_and_stops_shop(conn):
    from wbnotify import shops_repo

    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)

    members_repo.delete_account(conn, OWNER_ID)

    assert shops_repo.get_shop(conn, shop_id).is_active is False
    assert members_repo.role_of(conn, shop_id, OWNER_ID) is None
    assert members_repo.role_of(conn, shop_id, MANAGER_ID) is None, "кабинет закрыт — доступа нет ни у кого"


def test_deleting_manager_account_leaves_shop_alive(conn):
    from wbnotify import shops_repo

    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)

    members_repo.delete_account(conn, MANAGER_ID)

    assert shops_repo.get_shop(conn, shop_id).is_active is True
    assert members_repo.role_of(conn, shop_id, OWNER_ID) == "owner"


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
