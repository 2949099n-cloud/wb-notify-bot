from __future__ import annotations

from wbnotify.telegram import sender
from wbnotify.telegram.formatters import CAPTION_LIMIT
from wbnotify.telegram.sender import send_notification


class FakeBot:
    def __init__(self):
        self.calls = []
        self.keyboards = []

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, reply_markup=None):
        self.calls.append(("send_photo", photo, caption))
        if reply_markup is not None:
            self.keyboards.append(reply_markup)


    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.calls.append(("send_message", text))
        if reply_markup is not None:
            self.keyboards.append(reply_markup)


def _size(data: bytes) -> tuple[int, int]:
    import io

    from PIL import Image

    return Image.open(io.BytesIO(data)).size


def _mock_downloads(monkeypatch, url_to_bytes: dict[str, bytes | None]):
    async def fake_download_photos(urls):
        return [url_to_bytes[u] for u in urls if url_to_bytes.get(u) is not None]

    monkeypatch.setattr(sender, "_download_photos", fake_download_photos)


async def test_single_short_caption_sent_directly_with_photo(monkeypatch):
    _mock_downloads(monkeypatch, {"http://example.com/p.jpg": _jpeg((900, 1200))})
    bot = FakeBot()
    await send_notification(bot, 123, "short caption", photo_urls=["http://example.com/p.jpg"])
    kind, photo, caption = bot.calls[0]
    assert (kind, caption) == ("send_photo", "short caption")
    assert _size(photo) == (300, 400)


async def test_three_photos_are_glued_into_one_image_with_caption(monkeypatch):
    """Три фото уходят ОДНИМ сообщением-картинкой, а не альбомом — иначе Telegram
    не даёт прикрепить кнопки."""
    urls = [f"http://example.com/{i}.jpg" for i in range(3)]
    _mock_downloads(monkeypatch, {u: _jpeg((900, 1200)) for u in urls})
    bot = FakeBot()
    await send_notification(bot, 123, "caption text", photo_urls=urls, keyboard="KB")

    assert len(bot.calls) == 1, "должно быть ровно одно сообщение"
    kind, photo, caption = bot.calls[0]
    assert (kind, caption) == ("send_photo", "caption text")
    assert _size(photo) == (900, 400), "три плитки 300x400 в ряд"
    assert bot.keyboards == ["KB"], "кнопки должны уйти вместе с картинкой"


async def test_two_photos_make_narrower_collage(monkeypatch):
    urls = [f"http://example.com/{i}.jpg" for i in range(2)]
    _mock_downloads(monkeypatch, {u: _jpeg((900, 1200)) for u in urls})
    bot = FakeBot()
    await send_notification(bot, 123, "caption", photo_urls=urls)
    assert _size(bot.calls[0][1]) == (600, 400)


async def test_carousel_long_caption_sends_image_then_text_with_note(monkeypatch):
    urls = [f"http://example.com/{i}.jpg" for i in range(3)]
    _mock_downloads(monkeypatch, {u: _jpeg((900, 1200)) for u in urls})
    bot = FakeBot()
    long_text = "x" * (CAPTION_LIMIT + 12)
    await send_notification(bot, 123, long_text, photo_urls=urls, keyboard="KB")

    assert bot.calls[0][0] == "send_photo"
    assert bot.calls[0][2] is None, "подпись не влезла — картинка уходит без неё"
    kind, text = bot.calls[1]
    assert kind == "send_message"
    assert text.startswith("Фото пришло отдельным сообщением")
    assert bot.keyboards[-1] == "KB", "кнопки — под текстом, который читает пользователь"


async def test_single_photo_long_caption_falls_back_to_photo_then_text_with_note(monkeypatch):
    _mock_downloads(monkeypatch, {"http://example.com/p.jpg": _jpeg((900, 1200))})
    bot = FakeBot()
    long_text = "X" * (CAPTION_LIMIT + 500)
    await send_notification(bot, 123, long_text, photo_urls=["http://example.com/p.jpg"])

    assert (bot.calls[0][0], bot.calls[0][2]) == ("send_photo", None)
    kind, message = bot.calls[1]
    assert kind == "send_message"
    assert f"({len(long_text)} из {CAPTION_LIMIT} символов)" in message
    assert message.endswith(long_text)


async def test_no_photos_sends_plain_message(monkeypatch):
    _mock_downloads(monkeypatch, {})
    bot = FakeBot()
    await send_notification(bot, 123, "no photo here", photo_urls=[])
    assert bot.calls == [("send_message", "no photo here")]


async def test_one_broken_photo_in_carousel_is_skipped_not_fatal(monkeypatch):
    """Не скачалось одно фото из трёх — коллаж собирается из оставшихся двух."""
    urls = [f"http://example.com/{i}.jpg" for i in range(3)]
    _mock_downloads(
        monkeypatch,
        {urls[0]: _jpeg((900, 1200)), urls[1]: None, urls[2]: _jpeg((900, 1200))},
    )
    bot = FakeBot()
    await send_notification(bot, 123, "caption", photo_urls=urls)
    assert _size(bot.calls[0][1]) == (600, 400)


async def test_corrupted_photo_bytes_are_skipped(monkeypatch):
    """Скачалось, но файл битый — плитка пропускается, уведомление уходит."""
    urls = [f"http://example.com/{i}.jpg" for i in range(2)]
    _mock_downloads(monkeypatch, {urls[0]: b"not-an-image", urls[1]: _jpeg((900, 1200))})
    bot = FakeBot()
    await send_notification(bot, 123, "caption", photo_urls=urls)
    assert _size(bot.calls[0][1]) == (300, 400)


async def test_all_photos_broken_falls_back_to_text_only(monkeypatch):
    urls = ["http://example.com/1.jpg", "http://example.com/2.jpg"]
    _mock_downloads(monkeypatch, {u: None for u in urls})
    bot = FakeBot()
    await send_notification(bot, 123, "caption text", photo_urls=urls)
    assert bot.calls == [("send_message", "caption text")]


def _jpeg(size: tuple[int, int]) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="JPEG")
    return buf.getvalue()


def test_render_takes_three_photos_from_card(conn):
    """В карусель уходят первые 3 фото карточки, а не одно."""
    import json

    from wbnotify import shops_repo
    from wbnotify.db import utcnow
    from wbnotify.counters import now_msk

    now = utcnow()
    today = now_msk().date().isoformat()
    shop_id = conn.execute(
        "INSERT INTO shops (owner_user_id, telegram_chat_id, name, wb_api_token_encrypted,"
        " token_status, is_active, created_at, updated_at)"
        " VALUES (1, 555, 'test', X'00', 'active', 1, ?, ?)",
        (now, now),
    ).lastrowid
    conn.execute(
        "INSERT INTO cards_cache (shop_id, nm_id, photo_url, photos_json, refreshed_at)"
        " VALUES (?, 111, 'http://x/main.jpg', ?, ?)",
        (shop_id, json.dumps(["http://x/1.jpg", "http://x/2.jpg", "http://x/3.jpg"]), now),
    )
    conn.execute(
        "INSERT INTO orders (shop_id, srid, date, last_change_date, nm_id, tech_size, raw_json)"
        " VALUES (?, 'srid-1', ?, ?, 111, '42', '{}')",
        (shop_id, f"{today}T10:00:00", f"{today}T10:00:00"),
    )
    ref_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO notification_queue (shop_id, event_type, ref_table, ref_id, daily_seq,"
        " event_date, status, created_at) VALUES (?, 'order', 'orders', ?, 1, ?, 'pending', ?)",
        (shop_id, ref_id, f"{today}T10:00:00", now),
    )
    conn.commit()

    queue_row = conn.execute("SELECT * FROM notification_queue").fetchone()
    _, photo_urls = sender._render(conn, shops_repo.get_shop(conn, shop_id), queue_row)
    assert photo_urls == ["http://x/1.jpg", "http://x/2.jpg", "http://x/3.jpg"]
