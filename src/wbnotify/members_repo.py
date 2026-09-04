"""Участники кабинета и приглашения.

Владелец подключает кабинет через /addshop и автоматически становится
участником с ролью owner. Менеджера он зовёт одноразовой ссылкой; менеджер
получает те же уведомления в свой чат и может смотреть остатки, но не может
трогать токен, команду, подписку и сам кабинет (решение пользователя).

Изоляция: любой запрос по кабинету обязан идти через `role_of` — доступ есть
только у участников, а не у всех, кто как-то узнал shop_id.
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from wbnotify.db import utcnow

INVITE_TTL_DAYS = 14
ROLE_TITLES = {"owner": "Владелец", "manager": "Менеджер"}


def upsert_user(conn: sqlite3.Connection, user_id: int, display_name: str, chat_id: int) -> None:
    """Имя обновляем только при первом появлении: пользователь мог переименовать
    себя в «Профиле», и подставлять поверх имя из Telegram было бы неверно."""
    conn.execute(
        """
        INSERT INTO bot_users (telegram_user_id, display_name, chat_id, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET chat_id = excluded.chat_id
        """,
        (user_id, display_name, chat_id, utcnow()),
    )
    conn.commit()


def display_name(conn: sqlite3.Connection, user_id: int) -> str:
    row = conn.execute(
        "SELECT display_name FROM bot_users WHERE telegram_user_id = ?", (user_id,)
    ).fetchone()
    return row["display_name"] if row else "—"


def rename_user(conn: sqlite3.Connection, user_id: int, new_name: str) -> None:
    conn.execute(
        "UPDATE bot_users SET display_name = ? WHERE telegram_user_id = ?", (new_name, user_id)
    )
    conn.commit()


def add_owner(conn: sqlite3.Connection, shop_id: int, user_id: int, chat_id: int) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO shop_members (shop_id, telegram_user_id, telegram_chat_id, role, added_at)
        VALUES (?, ?, ?, 'owner', ?)
        """,
        (shop_id, user_id, chat_id, utcnow()),
    )
    conn.commit()


def role_of(conn: sqlite3.Connection, shop_id: int, user_id: int) -> str | None:
    row = conn.execute(
        "SELECT role FROM shop_members WHERE shop_id = ? AND telegram_user_id = ?", (shop_id, user_id)
    ).fetchone()
    return row["role"] if row else None


def list_members(conn: sqlite3.Connection, shop_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.telegram_user_id, m.role, COALESCE(u.display_name, '—') AS name
        FROM shop_members m
        LEFT JOIN bot_users u ON u.telegram_user_id = m.telegram_user_id
        WHERE m.shop_id = ?
        ORDER BY CASE m.role WHEN 'owner' THEN 0 ELSE 1 END, m.id
        """,
        (shop_id,),
    ).fetchall()


def list_user_shops(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.id, s.name, m.role
        FROM shop_members m JOIN shops s ON s.id = m.shop_id
        WHERE m.telegram_user_id = ? AND s.is_active = 1
        ORDER BY s.id
        """,
        (user_id,),
    ).fetchall()


def recipient_chats(conn: sqlite3.Connection, shop_id: int) -> list[int]:
    """Чаты, куда уходят уведомления по кабинету — владелец и все менеджеры.

    Дубли схлопываем: владелец и менеджер могут сидеть в одном чате (например,
    менеджер открыл ссылку в том же чате), тогда сообщение уйдёт один раз.
    """
    rows = conn.execute(
        "SELECT DISTINCT telegram_chat_id FROM shop_members WHERE shop_id = ?", (shop_id,)
    ).fetchall()
    return [row["telegram_chat_id"] for row in rows]


def remove_member(conn: sqlite3.Connection, shop_id: int, user_id: int) -> None:
    """Владельца удалить нельзя — иначе кабинет останется без хозяина."""
    conn.execute(
        "DELETE FROM shop_members WHERE shop_id = ? AND telegram_user_id = ? AND role != 'owner'",
        (shop_id, user_id),
    )
    conn.commit()


def delete_account(conn: sqlite3.Connection, user_id: int) -> None:
    """Удаляет пользователя: его кабинеты отключаются, доступы снимаются.

    Кабинеты, где пользователь был владельцем, деактивируются — иначе они
    остались бы в опросе WB без единого получателя уведомлений. Кабинеты, куда
    его лишь приглашали, продолжают работать у владельца.

    Строки `shops` не удаляем физически: на них ссылаются orders/sales/очередь.
    Здесь же поднимается флаг is_active — тот же механизм, что и в
    shops_repo.deactivate_shop (импортировать его нельзя: shops_repo сам зависит
    от этого модуля).
    """
    owned = conn.execute(
        "SELECT shop_id FROM shop_members WHERE telegram_user_id = ? AND role = 'owner'", (user_id,)
    ).fetchall()
    for row in owned:
        conn.execute(
            "UPDATE shops SET is_active = 0, updated_at = ? WHERE id = ?", (utcnow(), row["shop_id"])
        )
        conn.execute("DELETE FROM shop_members WHERE shop_id = ?", (row["shop_id"],))

    conn.execute("DELETE FROM shop_members WHERE telegram_user_id = ?", (user_id,))
    conn.execute("DELETE FROM bot_users WHERE telegram_user_id = ?", (user_id,))
    conn.commit()


def create_invite(conn: sqlite3.Connection, shop_id: int, created_by: int) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS)
    conn.execute(
        """
        INSERT INTO shop_invites (shop_id, token, role, created_by, created_at, expires_at)
        VALUES (?, ?, 'manager', ?, ?, ?)
        """,
        (shop_id, token, created_by, utcnow(), expires_at.isoformat()),
    )
    conn.commit()
    return token, expires_at


class InviteError(Exception):
    """Приглашение не сработало — текст предназначен для показа пользователю."""


def accept_invite(conn: sqlite3.Connection, token: str, user_id: int, chat_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM shop_invites WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise InviteError("Приглашение не найдено. Попросите владельца прислать новую ссылку.")
    if row["used_at"]:
        raise InviteError("Этой ссылкой уже воспользовались. Попросите владельца прислать новую.")
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        raise InviteError("Срок действия ссылки истёк. Попросите владельца прислать новую.")

    shop = conn.execute("SELECT id, name FROM shops WHERE id = ?", (row["shop_id"],)).fetchone()
    if shop is None:
        raise InviteError("Кабинет, в который вас приглашали, больше не существует.")

    conn.execute(
        """
        INSERT INTO shop_members (shop_id, telegram_user_id, telegram_chat_id, role, added_at)
        VALUES (?, ?, ?, 'manager', ?)
        ON CONFLICT(shop_id, telegram_user_id) DO UPDATE SET telegram_chat_id = excluded.telegram_chat_id
        """,
        (row["shop_id"], user_id, chat_id, utcnow()),
    )
    conn.execute(
        "UPDATE shop_invites SET used_at = ?, used_by = ? WHERE id = ?", (utcnow(), user_id, row["id"])
    )
    conn.commit()
    return shop
