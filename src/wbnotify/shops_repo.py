"""Единая точка входа для подключения магазина: используется и CLI (шаг 2),
и будущей Telegram-командой /addshop (шаг 4) — оба вызывают register_shop().
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from wbnotify.db import utcnow as _now
from wbnotify import admin_alerts, members_repo
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
    # Владелец сразу становится участником кабинета: рассылка ходит по
    # shop_members, а не по shops.telegram_chat_id (см. members_repo).
    members_repo.add_owner(conn, shop_id, owner_user_id, telegram_chat_id)
    admin_alerts.queue(
        conn,
        "shop_connected",
        f"«{name}» подключил {members_repo.describe_user(conn, owner_user_id)}",
        shop_id,
    )
    return get_shop(conn, shop_id)


async def replace_token(conn: sqlite3.Connection, shop_id: int, raw_token: str, enc_key: str) -> ShopRow:
    """Меняет WB-токен уже подключённого кабинета.

    Старый токен затирается только ПОСЛЕ успешной живой проверки нового — иначе
    неудачная замена оставила бы кабинет вообще без рабочего токена. Заодно
    снимается token_status='invalid', если кабинет был отключён из-за 401.
    """
    try:
        await ping(raw_token)
    except WBAuthError as exc:
        raise InvalidTokenError("Токен недействителен (WB API вернул 401 Unauthorized)") from exc
    except WBError as exc:
        raise InvalidTokenError(f"Не удалось проверить токен через WB API: {exc}") from exc

    now = _now()
    conn.execute(
        """
        UPDATE shops SET wb_api_token_encrypted = ?, token_status = 'active',
                         token_checked_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (encrypt_token(raw_token, enc_key), now, now, shop_id),
    )
    conn.commit()
    return get_shop(conn, shop_id)


async def resume_token(conn: sqlite3.Connection, shop_id: int, enc_key: str) -> ShopRow:
    """Возвращает кабинет в опрос, если сохранённый токен всё ещё рабочий.

    Нужна после «Отозвать» (пользователь передумал) и после автоматической
    пометки invalid по 401 — например, когда WB временно отвечал ошибкой.
    Проверяем живым ping: если токен и правда мёртв, статус остаётся invalid.
    """
    raw_token = get_decrypted_token(conn, shop_id, enc_key)
    try:
        await ping(raw_token)
    except WBAuthError as exc:
        raise InvalidTokenError("Сохранённый токен недействителен — нужна замена") from exc
    except WBError as exc:
        raise InvalidTokenError(f"Не удалось проверить токен через WB API: {exc}") from exc

    now = _now()
    conn.execute(
        "UPDATE shops SET token_status = 'active', token_checked_at = ?, updated_at = ? WHERE id = ?",
        (now, now, shop_id),
    )
    conn.commit()
    return get_shop(conn, shop_id)


def revoke_token(conn: sqlite3.Connection, shop_id: int) -> None:
    """Помечает токен недействительным на нашей стороне: опрос кабинета
    останавливается (is_shop_active), уведомления перестают приходить.

    Сам токен в личном кабинете WB этим НЕ отзывается — у WB API нет метода
    отзыва чужого токена, это делается руками в разделе «Доступ к API».
    """
    mark_token_invalid(
        conn, shop_id, reason="Токен отозван владельцем кабинета", kind="token_revoked"
    )


def deactivate_shop(conn: sqlite3.Connection, shop_id: int) -> None:
    """Мягкое удаление кабинета: опрос прекращается, история заказов остаётся.

    Физически строки не удаляем — на них ссылаются orders/sales/очередь, а
    shops.id по правилу проекта не переиспользуется, так что «воскресить»
    кабинет удалением флага нельзя и не нужно: подключение заводит новый id.
    """
    now = _now()
    shop = conn.execute("SELECT name, owner_user_id FROM shops WHERE id = ?", (shop_id,)).fetchone()
    conn.execute("UPDATE shops SET is_active = 0, updated_at = ? WHERE id = ?", (now, shop_id))
    conn.execute("DELETE FROM shop_members WHERE shop_id = ?", (shop_id,))
    conn.commit()
    if shop is not None:
        admin_alerts.queue(
            conn,
            "shop_deleted",
            f"«{shop['name']}» (id {shop_id}) удалил {members_repo.describe_user(conn, shop['owner_user_id'])}",
            shop_id,
        )


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


def mark_token_invalid(
    conn: sqlite3.Connection,
    shop_id: int,
    reason: str = "WB API вернул 401",
    kind: str = "token_invalid",
) -> None:
    """`reason` и `kind` попадают в алерт владельцу бота: 401 от WB и ручной
    отзыв владельцем кабинета — разные события, путать их в статистике не нужно."""
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
    shop = conn.execute("SELECT name, owner_user_id FROM shops WHERE id = ?", (shop_id,)).fetchone()
    if shop is not None:
        admin_alerts.queue(
            conn,
            kind,
            f"«{shop['name']}» (id {shop_id}, {members_repo.describe_user(conn, shop['owner_user_id'])})\n"
            f"{reason}. Опрос кабинета остановлен.",
            shop_id,
        )
