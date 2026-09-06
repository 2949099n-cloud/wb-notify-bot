"""Панель владельца бота: статистика, алерты, обращения в поддержку."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from wbnotify import admin_alerts, members_repo, shops_repo
from wbnotify.config import Config
from wbnotify.db import utcnow
from wbnotify.telegram import admin

OWNER_ID, OWNER_CHAT = 100, 1000
ADMIN_CHAT = 999


def _config(db_path: str, admin_chat: int | None = ADMIN_CHAT) -> Config:
    return Config(
        telegram_bot_token="dummy",
        token_encryption_key="dummy-key",
        telegram_admin_chat_id=admin_chat,
        poll_interval_minutes=5,
        stocks_poll_interval_minutes=30,
        cards_refresh_hour_msk=3,
        tariffs_refresh_hour_msk=3,
        db_path=db_path,
        timezone="Europe/Moscow",
        log_level="INFO",
    )


def _shop(conn, name="NILONIL", token_status="active", is_active=1, notify_from="2026-01-01T00:00:00+00:00"):
    now = utcnow()
    shop_id = conn.execute(
        """
        INSERT INTO shops (owner_user_id, telegram_chat_id, name, wb_api_token_encrypted,
                           token_status, is_active, notify_from, created_at, updated_at)
        VALUES (?, ?, ?, X'00', ?, ?, ?, ?, ?)
        """,
        (OWNER_ID, OWNER_CHAT, name, token_status, is_active, notify_from, now, now),
    ).lastrowid
    conn.commit()
    members_repo.add_owner(conn, shop_id, OWNER_ID, OWNER_CHAT)
    members_repo.upsert_user(conn, OWNER_ID, "Natali", OWNER_CHAT, username="natali")
    return shop_id


def test_only_admin_chat_passes_the_check(tmp_path):
    config = _config(str(tmp_path / "x.db"))
    assert admin.is_admin(config, ADMIN_CHAT) is True
    assert admin.is_admin(config, OWNER_CHAT) is False


def test_admin_disabled_when_chat_not_configured(tmp_path):
    """Без TELEGRAM_ADMIN_CHAT_ID панель не должна открываться никому."""
    config = _config(str(tmp_path / "x.db"), admin_chat=None)
    assert admin.is_admin(config, ADMIN_CHAT) is False


def test_stats_counts_shops_by_state(conn):
    _shop(conn, "Рабочий")
    _shop(conn, "Сбойный", token_status="invalid")
    _shop(conn, "Отключённый", is_active=0)
    _shop(conn, "Молчащий", notify_from=None)

    text = admin.stats_text(conn)
    assert "<b>Кабинетов:</b> 4" in text
    assert "🟢 работают: 2" in text
    assert "🔴 сбой токена: 1" in text
    assert "⚪️ отключены: 1" in text
    assert "🔇 без рассылки: 1" in text
    assert "@natali" in text, "владелец должен быть узнаваем"


def test_support_header_names_user_and_shops(conn):
    _shop(conn)
    header = admin.support_header(conn, OWNER_ID)
    assert "Natali" in header and "@natali" in header
    assert "NILONIL" in header and "owner" in header


def test_support_thread_roundtrip(conn):
    admin.remember_thread(conn, admin_message_id=555, user_id=OWNER_ID, chat_id=OWNER_CHAT)
    thread = admin.thread_by_message(conn, 555)
    assert (thread["user_id"], thread["chat_id"]) == (OWNER_ID, OWNER_CHAT)
    assert admin.thread_by_message(conn, 556) is None


def test_connecting_shop_queues_alert(conn, monkeypatch):
    """Владелец бота должен узнавать о новых кабинетах сам."""
    _shop(conn)
    admin_alerts.queue(conn, "shop_connected", "тест", 1)
    pending = admin_alerts.pending(conn)
    assert pending[-1]["kind"] == "shop_connected"
    assert "Новый кабинет" in admin_alerts.render(pending[-1])


def test_revoking_token_queues_alert_with_reason(conn):
    shop_id = _shop(conn)
    shops_repo.revoke_token(conn, shop_id)

    alert = [a for a in admin_alerts.pending(conn) if a["kind"] == "token_revoked"][-1]
    assert "отозван владельцем" in alert["text"]
    assert "Опрос кабинета остановлен" in alert["text"]


def test_deleting_shop_queues_alert(conn):
    shop_id = _shop(conn)
    shops_repo.deactivate_shop(conn, shop_id)
    assert any(a["kind"] == "shop_deleted" for a in admin_alerts.pending(conn))


def test_deleting_account_queues_alert(conn):
    _shop(conn)
    members_repo.delete_account(conn, OWNER_ID)
    alert = [a for a in admin_alerts.pending(conn) if a["kind"] == "account_deleted"][-1]
    assert "Natali" in alert["text"]


async def test_flush_sends_and_marks_alerts(conn, tmp_path):
    from wbnotify.scheduler.jobs import flush_admin_alerts

    admin_alerts.queue(conn, "shop_connected", "первый")
    admin_alerts.queue(conn, "token_invalid", "второй")
    bot = SimpleNamespace(send_message=AsyncMock())

    sent = await flush_admin_alerts(conn, bot, _config(str(tmp_path / "x.db")))

    assert sent == 2
    assert admin_alerts.pending(conn) == [], "отправленные не должны уйти повторно"


async def test_flush_does_nothing_without_admin_chat(conn, tmp_path):
    """Алерты копятся и дождутся настройки, а не теряются."""
    from wbnotify.scheduler.jobs import flush_admin_alerts

    admin_alerts.queue(conn, "shop_connected", "первый")
    bot = SimpleNamespace(send_message=AsyncMock())

    sent = await flush_admin_alerts(conn, bot, _config(str(tmp_path / "x.db"), admin_chat=None))

    assert sent == 0
    bot.send_message.assert_not_awaited()
    assert len(admin_alerts.pending(conn)) == 1


def test_alerts_go_to_separate_bot_when_configured(tmp_path):
    """Служебный бот настроен — админская лента уходит им, а не основным."""
    from wbnotify.telegram import admin_notifier

    config = _config(str(tmp_path / "x.db"))
    config = Config(**{**config.__dict__, "telegram_admin_bot_token": "222:ADMIN"})

    assert admin_notifier.uses_separate_bot(config) is True
    assert admin_notifier.make_admin_bot(config).token == "222:ADMIN"


def test_alerts_fall_back_to_main_bot(tmp_path):
    from wbnotify.telegram import admin_notifier

    config = _config(str(tmp_path / "x.db"))
    assert admin_notifier.uses_separate_bot(config) is False
    assert admin_notifier.make_admin_bot(config).token == "dummy"


def test_no_admin_bot_without_admin_chat(tmp_path):
    """Некуда слать — не создаём бота вовсе."""
    from wbnotify.telegram import admin_notifier

    config = _config(str(tmp_path / "x.db"), admin_chat=None)
    assert admin_notifier.make_admin_bot(config) is None


def test_shops_page_splits_long_list(conn):
    """Сотня кабинетов не должна вываливаться одним сообщением."""
    from wbnotify.telegram.admin import SHOPS_PER_PAGE

    for index in range(SHOPS_PER_PAGE + 3):
        _shop(conn, f"Кабинет {index}")

    first, page, pages = admin.shops_page(conn, 0)
    assert (page, pages) == (0, 2)
    assert first.count("(id ") == SHOPS_PER_PAGE

    second, page, pages = admin.shops_page(conn, 1)
    assert (page, pages) == (1, 2)
    assert second.count("(id ") == 3

    # Выход за границы не должен ломать экран.
    assert admin.shops_page(conn, 99)[1] == 1


def test_summary_lists_only_problem_shops(conn):
    """В сводке — цифры и проблемные кабинеты, остальные постранично."""
    _shop(conn, "Рабочий")
    _shop(conn, "Сбойный", token_status="invalid")

    text = admin.stats_text(conn)
    assert "Требуют внимания" in text
    assert "Сбойный" in text
    assert "Рабочий" not in text


async def test_support_message_is_not_lost_when_admin_bot_is_down(conn, tmp_path, monkeypatch):
    """Реальный случай: служебный бот падал по кругу из-за кривого токена, и
    обращения пользователей исчезали бесследно — ни у владельца, ни в базе."""
    from telegram.error import TelegramError

    from wbnotify.telegram import admin_notifier, bot as bot_module

    _shop(conn)
    config = _config(str(tmp_path / "x.db"))
    object.__setattr__(config, "db_path", conn.execute("PRAGMA database_list").fetchone()[2])

    class DeadBot:
        async def send_message(self, **kwargs):
            raise TelegramError("Unauthorized")

    monkeypatch.setattr(admin_notifier, "make_admin_bot", lambda cfg: DeadBot())

    replies = []
    update = SimpleNamespace(
        message=SimpleNamespace(text="не приходят уведомления", reply_text=AsyncMock()),
        effective_user=SimpleNamespace(id=OWNER_ID),
        effective_chat=SimpleNamespace(id=OWNER_CHAT),
    )
    context = SimpleNamespace(bot_data={"config": config}, bot=None, user_data={})

    await bot_module._do_support(update, context)

    queued = [a for a in admin_alerts.pending(conn) if a["kind"] == "support"]
    assert len(queued) == 1, "обращение должно лечь в очередь, а не пропасть"
    assert "не приходят уведомления" in queued[0]["text"]
    assert admin_alerts.payload_of(queued[0]) == {"user_id": OWNER_ID, "chat_id": OWNER_CHAT}
    update.message.reply_text.assert_awaited_once()
    assert "принято" in update.message.reply_text.await_args.args[0]


async def test_deferred_support_message_remembers_who_to_answer(conn, tmp_path):
    """После доставки отложенного обращения ответ реплаем должен находить автора."""
    from wbnotify.scheduler.jobs import flush_admin_alerts

    admin_alerts.queue(
        conn, "support", "🆘 Обращение\nтекст", payload={"user_id": OWNER_ID, "chat_id": OWNER_CHAT}
    )

    class Bot:
        async def send_message(self, chat_id, text, parse_mode=None):
            return SimpleNamespace(message_id=777)

    sent = await flush_admin_alerts(conn, Bot(), _config(str(tmp_path / "x.db")))

    assert sent == 1
    thread = admin.thread_by_message(conn, 777)
    assert (thread["user_id"], thread["chat_id"]) == (OWNER_ID, OWNER_CHAT)
