"""Команда кабинета: приглашения, роли, изоляция, рассылка нескольким получателям."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wbnotify import members_repo, shops_repo
from wbnotify.db import utcnow
from wbnotify.telegram.sender import drain_queue_for_shop

OWNER_ID, OWNER_CHAT = 100, 1000
MANAGER_ID, MANAGER_CHAT = 200, 2000


def _shop(conn, name: str = "NILONIL") -> int:
    now = utcnow()
    shop_id = conn.execute(
        """
        INSERT INTO shops (owner_user_id, telegram_chat_id, name, wb_api_token_encrypted,
                           token_status, is_active, notify_from, created_at, updated_at)
        VALUES (?, ?, ?, X'00', 'active', 1, '2026-01-01T00:00:00+00:00', ?, ?)
        """,
        (OWNER_ID, OWNER_CHAT, name, now, now),
    ).lastrowid
    conn.commit()
    members_repo.add_owner(conn, shop_id, OWNER_ID, OWNER_CHAT)
    members_repo.upsert_user(conn, OWNER_ID, "Natali", OWNER_CHAT)
    return shop_id


def _queue_order(conn, shop_id: int) -> None:
    from wbnotify.counters import now_msk

    today = now_msk().date().isoformat()
    conn.execute(
        "INSERT INTO orders (shop_id, srid, date, last_change_date, nm_id, tech_size, raw_json)"
        " VALUES (?, 'srid-1', ?, ?, 111, '42', '{}')",
        (shop_id, f"{today}T10:00:00", f"{today}T10:00:00"),
    )
    ref_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO notification_queue (shop_id, event_type, ref_table, ref_id, daily_seq,"
        " event_date, status, created_at) VALUES (?, 'order', 'orders', ?, 1, ?, 'pending', ?)",
        (shop_id, ref_id, f"{today}T10:00:00", utcnow()),
    )
    conn.commit()


class FakeBot:
    def __init__(self):
        self.chats = []

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, reply_markup=None):
        self.chats.append(chat_id)

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.chats.append(chat_id)


def test_invite_accepted_makes_manager(conn):
    shop_id = _shop(conn)
    token, expires_at = members_repo.create_invite(conn, shop_id, OWNER_ID)
    assert expires_at > datetime.now(timezone.utc) + timedelta(days=13)

    shop = members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)
    assert shop["name"] == "NILONIL"
    assert members_repo.role_of(conn, shop_id, MANAGER_ID) == "manager"


def test_invite_is_single_use(conn):
    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)

    with pytest.raises(members_repo.InviteError, match="уже воспользовались"):
        members_repo.accept_invite(conn, token, 300, 3000)


def test_expired_invite_is_rejected(conn):
    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    conn.execute(
        "UPDATE shop_invites SET expires_at = ? WHERE token = ?",
        ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), token),
    )
    conn.commit()

    with pytest.raises(members_repo.InviteError, match="истёк"):
        members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)


def test_owner_cannot_be_removed(conn):
    """Иначе кабинет остался бы без хозяина и без получателя уведомлений."""
    shop_id = _shop(conn)
    members_repo.remove_member(conn, shop_id, OWNER_ID)
    assert members_repo.role_of(conn, shop_id, OWNER_ID) == "owner"


def test_manager_loses_access_after_removal(conn):
    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)

    members_repo.remove_member(conn, shop_id, MANAGER_ID)
    assert members_repo.role_of(conn, shop_id, MANAGER_ID) is None
    assert MANAGER_CHAT not in members_repo.recipient_chats(conn, shop_id)


def test_outsider_has_no_role_in_shop(conn):
    """Изоляция: посторонний не участник, даже зная shop_id."""
    shop_id = _shop(conn)
    assert members_repo.role_of(conn, shop_id, 999) is None


def test_user_sees_only_own_shops(conn):
    mine = _shop(conn, "Моя")
    other_owner_shop = conn.execute(
        """
        INSERT INTO shops (owner_user_id, telegram_chat_id, name, wb_api_token_encrypted,
                           token_status, is_active, created_at, updated_at)
        VALUES (777, 7000, 'Чужая', X'00', 'active', 1, ?, ?)
        """,
        (utcnow(), utcnow()),
    ).lastrowid
    conn.commit()
    members_repo.add_owner(conn, other_owner_shop, 777, 7000)

    assert [row["id"] for row in members_repo.list_user_shops(conn, OWNER_ID)] == [mine]


async def test_notification_goes_to_owner_and_manager(conn):
    """Приглашённый менеджер получает те же уведомления в свой чат."""
    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)
    _queue_order(conn, shop_id)

    bot = FakeBot()
    sent = await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id))

    assert sent == 1, "событие в очереди одно — считается один раз, независимо от числа получателей"
    assert sorted(bot.chats) == [OWNER_CHAT, MANAGER_CHAT]


async def test_removed_manager_stops_receiving(conn):
    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)
    members_repo.remove_member(conn, shop_id, MANAGER_ID)
    _queue_order(conn, shop_id)

    bot = FakeBot()
    await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id))
    assert bot.chats == [OWNER_CHAT]


async def test_mute_of_one_member_does_not_affect_another(conn):
    """Мьюты у участников разные, а строка в очереди одна на событие."""
    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)
    conn.execute(
        "INSERT INTO chat_mutes (chat_id, event_type, shop_id) VALUES (?, 'order', ?)",
        (MANAGER_CHAT, shop_id),
    )
    conn.commit()
    _queue_order(conn, shop_id)

    bot = FakeBot()
    await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id))
    assert bot.chats == [OWNER_CHAT]
