"""Единая точка входа для подключения магазина: используется и CLI (шаг 2),
и будущей Telegram-командой /addshop (шаг 4) — оба вызывают register_shop().
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from wbnotify.db import utcnow as _now
from wbnotify.models import ShopRow
from wbnotify.security.tokens import decrypt_token, encrypt_token
from wbnotify.wb_api.client import WBAuthError, WBError, ping, seller_info


class InvalidTokenError(RuntimeError):
    """Токен не прошёл проверку WB API при регистрации — показать пользователю как есть."""


async def register_shop(
    conn: sqlite3.Connection,
    owner_user_id: int,
    telegram_chat_id: int,
    raw_token: str,
    enc_key: str,
) -> ShopRow:
    """Валидирует токен живым запросом к WB, шифрует и сохраняет магазин.

    Не пишет ничего в БД при невалидном токене.
    """
    try:
        await ping(raw_token)
    except WBAuthError as exc:
        raise InvalidTokenError("Токен недействителен (WB API вернул 401 Unauthorized)") from exc
    except WBError as exc:
        raise InvalidTokenError(f"Не удалось проверить токен через WB API: {exc}") from exc

    name = f"Магазин {owner_user_id}"
    brand_code: str | None = None
    try:
        info = await seller_info(raw_token)
        name = info.get("tradeMark") or info.get("name") or name
        brand_code = info.get("tradeMark") or None
    except WBError:
        # seller-info недоступен/лимит — не блокируем онбординг, имя можно поправить позже
        pass

    now = _now()
    cursor = conn.execute(
        """
        INSERT INTO shops
            (owner_user_id, telegram_chat_id, name, brand_code,
             wb_api_token_encrypted, token_status, token_checked_at,
             is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, 1, ?, ?)
        """,
        (
            owner_user_id,
            telegram_chat_id,
            name,
            brand_code,
            encrypt_token(raw_token, enc_key),
            now,
            now,
            now,
        ),
    )
    conn.commit()
    shop_id = cursor.lastrowid
    return get_shop(conn, shop_id)


def get_shop(conn: sqlite3.Connection, shop_id: int) -> ShopRow:
    row = conn.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
    if row is None:
        raise LookupError(f"Магазин id={shop_id} не найден")
    return ShopRow.from_row(row)


def is_shop_active(shop: ShopRow) -> bool:
    """Единственное место, определяющее, опрашивать ли магазин: ручное отключение,
    статус токена и статус подписки — три независимые оси, все должны быть 'зелёные'.
    Поллинг (sync/*.py) и любой будущий gate на уведомления должны звать именно эту
    функцию, а не проверять поля ShopRow напрямую по отдельности.
    """
    if not shop.is_active:
        return False
    if shop.token_status != "active":
        return False
    if shop.subscription_status != "active":
        return False
    if shop.subscription_expires_at:
        try:
            expires_at = datetime.fromisoformat(shop.subscription_expires_at)
        except ValueError:
            return False
        if expires_at <= datetime.now(timezone.utc):
            return False
    return True


def list_active_shops(conn: sqlite3.Connection) -> list[ShopRow]:
    rows = conn.execute("SELECT * FROM shops WHERE is_active = 1").fetchall()
    shops = [ShopRow.from_row(r) for r in rows]
    return [s for s in shops if is_shop_active(s)]


def list_shops_by_owner(conn: sqlite3.Connection, owner_user_id: int) -> list[ShopRow]:
    rows = conn.execute(
        "SELECT * FROM shops WHERE owner_user_id = ? AND is_active = 1", (owner_user_id,)
    ).fetchall()
    return [ShopRow.from_row(r) for r in rows]


def get_decrypted_token(conn: sqlite3.Connection, shop_id: int, enc_key: str) -> str:
    row = conn.execute(
        "SELECT wb_api_token_encrypted FROM shops WHERE id = ?", (shop_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Магазин id={shop_id} не найден")
    return decrypt_token(row["wb_api_token_encrypted"], enc_key)


def mark_token_invalid(conn: sqlite3.Connection, shop_id: int) -> None:
    now = _now()
    conn.execute(
        "UPDATE shops SET token_status = 'invalid', token_checked_at = ?, updated_at = ? WHERE id = ?",
        (now, now, shop_id),
    )
    conn.execute(
        """
        INSERT INTO shop_alerts (shop_id, kind, message, created_at)
        VALUES (?, 'reconnect_required', ?, ?)
        """,
        (shop_id, "Токен магазина недействителен — требуется переподключение через /addshop", now),
    )
    conn.commit()
