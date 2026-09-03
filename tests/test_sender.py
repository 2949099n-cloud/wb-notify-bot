from __future__ import annotations

from wbnotify.telegram import sender
from wbnotify.telegram.formatters import CAPTION_LIMIT
from wbnotify.telegram.sender import send_notification


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None):
        self.calls.append(("send_photo", photo, caption))

    async def send_media_group(self, chat_id, media):
        # InputMediaPhoto(media=bytes) заворачивает байты в InputFile — достаём обратно.
        self.calls.append(("send_media_group", [(m.media.input_file_content, m.caption) for m in media]))

    async def send_message(self, chat_id, text, parse_mode=None):
        self.calls.append(("send_message", text))


def _mock_downloads(monkeypatch, url_to_bytes: dict[str, bytes | None]):
    async def fake_download_photos(urls):
        return [url_to_bytes[u] for u in urls if url_to_bytes.get(u) is not None]

    monkeypatch.setattr(sender, "_download_photos", fake_download_photos)


async def test_single_short_caption_sent_directly_with_photo(monkeypatch):
    _mock_downloads(monkeypatch, {"http://example.com/p.jpg": b"photo-bytes"})
    bot = FakeBot()
    await send_notification(bot, 123, "short caption", photo_urls=["http://example.com/p.jpg"])
    assert bot.calls == [("send_photo", b"photo-bytes", "short caption")]


async def test_carousel_short_caption_sent_as_media_group_on_first_photo(monkeypatch):
    urls = ["http://example.com/1.jpg", "http://example.com/2.jpg", "http://example.com/3.jpg"]
    _mock_downloads(monkeypatch, {u: f"bytes-{i}".encode() for i, u in enumerate(urls)})
    bot = FakeBot()
    await send_notification(bot, 123, "caption text", photo_urls=urls)

    assert len(bot.calls) == 1
    kind, media = bot.calls[0]
    assert kind == "send_media_group"
    assert media == [(b"bytes-0", "caption text"), (b"bytes-1", None), (b"bytes-2", None)]


async def test_carousel_long_caption_falls_back_to_media_group_then_text_with_note(monkeypatch):
    urls = ["http://example.com/1.jpg", "http://example.com/2.jpg", "http://example.com/3.jpg"]
    _mock_downloads(monkeypatch, {u: f"bytes-{i}".encode() for i, u in enumerate(urls)})
    bot = FakeBot()
    long_text = "X" * (CAPTION_LIMIT + 500)
    await send_notification(bot, 123, long_text, photo_urls=urls)

    assert bot.calls[0][0] == "send_media_group"
    assert all(caption is None for _, caption in bot.calls[0][1])

    kind, message = bot.calls[1]
    assert kind == "send_message"
    assert f"({len(long_text)} из {CAPTION_LIMIT} символов)" in message
    assert message.endswith(long_text)


async def test_single_photo_long_caption_falls_back_to_photo_then_text_with_note(monkeypatch):
    _mock_downloads(monkeypatch, {"http://example.com/p.jpg": b"photo-bytes"})
    bot = FakeBot()
    long_text = "X" * (CAPTION_LIMIT + 500)
    await send_notification(bot, 123, long_text, photo_urls=["http://example.com/p.jpg"])

    assert bot.calls[0] == ("send_photo", b"photo-bytes", None)
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
    """Ровно ситуация, пойманная на реальном чате: одно фото из трёх не скачалось
    (Telegram/сеть вернули ошибку на конкретный URL) — остальные два всё равно
    должны уйти каруселью, а не завалить всё уведомление."""
    urls = ["http://example.com/1.jpg", "http://example.com/2.jpg", "http://example.com/3.jpg"]
    _mock_downloads(monkeypatch, {urls[0]: b"bytes-0", urls[1]: None, urls[2]: b"bytes-2"})
    bot = FakeBot()
    await send_notification(bot, 123, "caption", photo_urls=urls)

    kind, media = bot.calls[0]
    assert kind == "send_media_group"
    assert media == [(b"bytes-0", "caption"), (b"bytes-2", None)]


async def test_all_photos_broken_falls_back_to_text_only(monkeypatch):
    urls = ["http://example.com/1.jpg", "http://example.com/2.jpg"]
    _mock_downloads(monkeypatch, {u: None for u in urls})
    bot = FakeBot()
    await send_notification(bot, 123, "caption text", photo_urls=urls)
    assert bot.calls == [("send_message", "caption text")]
