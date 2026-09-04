"""Юнит-тест хендлера /addshop без реального Telegram: конструируем
duck-typed Update/Context и мокаем shops_repo.register_shop, чтобы не ходить
в сеть. Проверяем главное требование ТЗ: сообщение с токеном удаляется
ВСЕГДА — и при успехе, и при ошибке валидации."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wbnotify import shops_repo
from wbnotify.config import Config
from wbnotify.models import ShopRow
from wbnotify.telegram.bot import addshop_receive_token


def _make_update_context(db_path: str, text: str):
    config = Config(
        telegram_bot_token="dummy",
        token_encryption_key="dummy-key",
        telegram_admin_chat_id=None,
        poll_interval_minutes=30,
        stocks_poll_interval_minutes=30,
        cards_refresh_hour_msk=3,
        tariffs_refresh_hour_msk=3,
        db_path=db_path,
        timezone="Europe/Moscow",
        log_level="INFO",
    )
    message = SimpleNamespace(text=text, message_id=42)
    update = SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=555),
        effective_user=SimpleNamespace(id=777),
    )
    bot = SimpleNamespace(delete_message=AsyncMock(), send_message=AsyncMock())
    context = SimpleNamespace(bot_data={"config": config}, bot=bot)
    return update, context


@pytest.mark.parametrize("outcome", ["success", "invalid_token"])
async def test_token_message_always_deleted(tmp_path, monkeypatch, outcome):
    from wbnotify.db import init_db

    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    update, context = _make_update_context(db_path, text="fake-token")

    if outcome == "success":
        fake_shop = ShopRow(
            id=1, owner_user_id=777, telegram_chat_id=555, name="Тест", brand_code=None,
            token_status="active", subscription_status="active", subscription_expires_at=None, is_active=True,
            created_at="2026-05-08T12:00:00+00:00",
        )

        async def fake_register_shop(conn, owner_user_id, chat_id, raw_token, enc_key):
            return fake_shop

        monkeypatch.setattr(shops_repo, "register_shop", fake_register_shop)
    else:
        async def fake_register_shop_fail(conn, owner_user_id, chat_id, raw_token, enc_key):
            raise shops_repo.InvalidTokenError("токен недействителен")

        monkeypatch.setattr(shops_repo, "register_shop", fake_register_shop_fail)

    await addshop_receive_token(update, context)

    context.bot.delete_message.assert_awaited_once_with(chat_id=555, message_id=42)
    context.bot.send_message.assert_awaited_once()
    reply_text = context.bot.send_message.await_args.kwargs["text"]
    if outcome == "success":
        assert "Тест" in reply_text
    else:
        assert "Не удалось" in reply_text
