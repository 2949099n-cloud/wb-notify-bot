"""Доставка уведомлений: фото + подпись, fallback на отдельное текстовое
сообщение при подписи >1024 символов (лимит caption у Telegram Bot API).

**Одно фото, а не карусель** — решение пользователя после серии живых тестов:
Telegram упорно рендерил первое фото альбома отдельным крупным блоком, а
остальные — компактным рядом, независимо от аспекта (3:4 и 1:1), разрешения и
наличия подписи. Уменьшение фото до 160px дало размытие без изменения раскладки.
Вывод: раскладкой альбома управляет клиент Telegram, а не мы. Поэтому шлём одно
главное фото карточки в оригинальном качестве. `cards_cache.photos_json`
(первые 3 фото) сохранён на случай возврата к карусели.

Фото передаются в Telegram БАЙТАМИ, а не URL. Проверено вживую: при передаче
голого URL Telegram иногда отвечает "Failed to get http url content" на
конкретном фото, хотя та же самая ссылка прекрасно открывается напрямую
(curl, 200 OK) — сбой на стороне механизма скачивания по URL у самого Telegram,
не у WB CDN и не у нас. Скачиваем фото сами (httpx, с retry) и загружаем как
файл — так на порядок надёжнее. Не скачалось — уходим в текстовый фоллбек.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta

import httpx
from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError
from telegram.request import HTTPXRequest

from wbnotify.counters import now_msk
from wbnotify.db import utcnow
from wbnotify.models import ShopRow
from wbnotify.telegram.formatters import (
    CAPTION_LIMIT,
    PARSE_MODE,
    format_buyout_message,
    format_cancel_message,
    format_order_message,
    format_return_message,
)

logger = logging.getLogger(__name__)


def make_bot(token: str) -> Bot:
    """Bot с увеличенными таймаутами под media group из нескольких фото.

    Дефолты python-telegram-bot (read/write_timeout=5с) на практике оказались
    маловаты для sendMediaGroup из 3 фото — поймали "Timed out" вживую при
    штатной отправке (не единичный сбой, повторный запрос сразу же тоже упал).
    """
    request = HTTPXRequest(connect_timeout=10.0, read_timeout=30.0, write_timeout=30.0, media_write_timeout=60.0)
    return Bot(token=token, request=request)

PHOTO_FETCH_TIMEOUT = 15.0
PHOTO_FETCH_RETRIES = 2


async def _download_photo(url: str) -> bytes | None:
    for attempt in range(1, PHOTO_FETCH_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=PHOTO_FETCH_TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPError as exc:
            logger.warning("Не удалось скачать фото %s (попытка %d/%d): %s", url, attempt, PHOTO_FETCH_RETRIES, exc)
    return None


async def _download_photos(urls: list[str]) -> list[bytes]:
    """Фото отправляются в оригинальном качестве, без пережатия: уменьшение
    (пробовали 480px и 160px) давало размытие и не влияло на раскладку в чате."""
    photos = []
    for url in urls:
        data = await _download_photo(url)
        if data is not None:
            photos.append(data)
    return photos


async def send_notification(bot: Bot, chat_id: int, text: str, photo_urls: list[str]) -> None:
    """`photo_urls` — 0..N фото карусели (см. cards_cache.photos_json, обычно до 3-х)."""
    photos = await _download_photos(photo_urls)

    if not photos:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=PARSE_MODE)
        return

    if len(photos) == 1:
        if len(text) <= CAPTION_LIMIT:
            await bot.send_photo(chat_id=chat_id, photo=photos[0], caption=text, parse_mode=PARSE_MODE)
            return
        await bot.send_photo(chat_id=chat_id, photo=photos[0])
    else:
        if len(text) <= CAPTION_LIMIT:
            media = [
                InputMediaPhoto(media=data, caption=text if i == 0 else None, parse_mode=PARSE_MODE)
                for i, data in enumerate(photos)
            ]
            await bot.send_media_group(chat_id=chat_id, media=media)
            return
        media = [InputMediaPhoto(media=data) for data in photos]
        await bot.send_media_group(chat_id=chat_id, media=media)

    # Фото(-карусель) отдельно, текст следующим сообщением, с пометкой — точный текст из ТЗ.
    note = f"Фото пришло отдельным сообщением — текст не поместился в подпись ({len(text)} из {CAPTION_LIMIT} символов)."
    await bot.send_message(chat_id=chat_id, text=f"{note}\n\n{text}", parse_mode=PARSE_MODE)


def _render(conn: sqlite3.Connection, shop: ShopRow, queue_row: sqlite3.Row) -> tuple[str, list[str]]:
    ref_table = queue_row["ref_table"]
    if ref_table not in ("orders", "sales"):
        raise ValueError(f"неожиданный ref_table={ref_table!r}")

    ref_row = conn.execute(f"SELECT * FROM {ref_table} WHERE id = ?", (queue_row["ref_id"],)).fetchone()
    if ref_row is None:
        raise LookupError(f"{ref_table} id={queue_row['ref_id']} не найден")

    card = conn.execute(
        "SELECT photo_url, photos_json FROM cards_cache WHERE shop_id = ? AND nm_id = ?",
        (shop.id, ref_row["nm_id"]),
    ).fetchone()
    # Только ГЛАВНОЕ фото карточки (решение пользователя, см. docstring модуля).
    # photos_json хранит первые 3 — берём из него первое, чтобы источник был один.
    if card and card["photos_json"]:
        photo_urls = json.loads(card["photos_json"])[:1]
    elif card and card["photo_url"]:
        photo_urls = [card["photo_url"]]
    else:
        photo_urls = []

    event_type = queue_row["event_type"]
    daily_seq = queue_row["daily_seq"]
    if event_type == "order":
        text = format_order_message(conn, shop, ref_row, daily_seq)
    elif event_type == "cancel":
        text = format_cancel_message(conn, shop, ref_row, daily_seq)
    elif event_type == "return":
        text = format_return_message(conn, shop, ref_row, daily_seq)
    elif event_type == "buyout":
        text = format_buyout_message(conn, shop, ref_row, daily_seq, event_type)
    else:
        raise ValueError(f"неизвестный event_type={event_type!r}")
    return text, photo_urls


# Насколько «старым» может быть событие, чтобы его всё-таки отправить. Нужно
# именно окно, а не отсечка по дате включения рассылки: WB иногда отдаёт заказ/
# продажу с опозданием на день-два-три, и такое событие показать НАДО. Всё, что
# старше окна, считаем архивом и не шлём.
MAX_EVENT_AGE_DAYS = 7


async def drain_queue_for_shop(conn: sqlite3.Connection, bot: Bot, shop: ShopRow, limit: int | None = None) -> int:
    """Отправляет pending-уведомления магазина (все, либо первые `limit` по id),
    помечает sent/failed. Возвращает число успешно отправленных.

    `limit` существует в первую очередь для безопасного ручного/CLI-теста на
    реальном чате — без него после большого бэкфилла (шаги 2-3) можно случайно
    отправить пользователю тысячи сообщений разом.
    """
    # notify_from — отсечка бэкфилла: уведомляем только о событиях, попавших в
    # очередь ПОСЛЕ подключения магазина к рассылке. Без неё первый же запуск
    # планировщика отправил бы пользователю десятки тысяч исторических событий.
    # NULL = не уведомлять ни о чём (безопасно по умолчанию).
    notify_from = conn.execute("SELECT notify_from FROM shops WHERE id = ?", (shop.id,)).fetchone()["notify_from"]
    if not notify_from:
        logger.info("shop_id=%s: notify_from не задан — рассылка не начата, очередь не трогаем", shop.id)
        return 0

    # Два разных фильтра, каждый закрывает свой сценарий:
    #
    # 1) created_at >= notify_from — отсекает первичный бэкфилл (десятки тысяч
    #    исторических событий, попавших в очередь ДО включения рассылки).
    #    Опоздавшие события он пропускает, и это правильно: WB может отдать заказ
    #    через день-два, такой заказ попадает в очередь уже ПОСЛЕ notify_from.
    #
    # 2) event_date >= сегодня минус MAX_EVENT_AGE_DAYS — страховка от совсем
    #    древних событий, если они по какой-то причине классифицируются впервые
    #    сейчас. Именно ОКНО, а не «не раньше даты включения»: жёсткая отсечка по
    #    дате включения выбрасывала бы и опоздавшие на день-два заказы, которые
    #    показать как раз надо.
    max_age_cutoff = (now_msk().date() - timedelta(days=MAX_EVENT_AGE_DAYS)).isoformat()

    query = (
        "SELECT * FROM notification_queue WHERE shop_id = ? AND status = 'pending' "
        "AND created_at >= ? AND (event_date IS NULL OR event_date >= ?) ORDER BY id"
    )
    params: list = [shop.id, notify_from, max_age_cutoff]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()

    sent = 0
    for row in rows:
        try:
            text, photo_urls = _render(conn, shop, row)
            await send_notification(bot, shop.telegram_chat_id, text, photo_urls)
        except (LookupError, ValueError, TelegramError) as exc:
            logger.error("notification_queue id=%s: ошибка отправки: %s", row["id"], exc)
            conn.execute(
                "UPDATE notification_queue SET status='failed', error=? WHERE id=?", (str(exc), row["id"])
            )
            conn.commit()
            continue

        conn.execute(
            "UPDATE notification_queue SET status='sent', sent_at=? WHERE id=?", (utcnow(), row["id"])
        )
        conn.commit()
        sent += 1

    return sent
