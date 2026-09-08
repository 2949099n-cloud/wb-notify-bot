"""Планировщик и защита от рассылки исторического бэкфилла.

Самый опасный сценарий проекта: на момент включения планировщика в очереди
лежало 28 279 событий первичного бэкфилла. Без отсечки первый же цикл отправил
бы их все пользователю в чат.
"""
from __future__ import annotations

from datetime import timedelta

from wbnotify import shops_repo
from wbnotify.config import Config
from wbnotify.db import utcnow
from wbnotify.scheduler.runner import build_scheduler
from wbnotify.telegram.sender import drain_queue_for_shop

SHOP_ID = 1


class FakeBot:
    def __init__(self):
        self.sent = 0

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, reply_markup=None):
        self.sent += 1

    async def send_media_group(self, chat_id, media):
        self.sent += 1

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent += 1


def _make_config(db_path: str = ":memory:") -> Config:
    return Config(
        telegram_bot_token="dummy",
        token_encryption_key="dummy",
        telegram_admin_chat_id=None,
        poll_interval_minutes=30,
        stocks_poll_interval_minutes=30,
        cards_refresh_hour_msk=3,
        tariffs_refresh_hour_msk=3,
        db_path=db_path,
        timezone="Europe/Moscow",
        log_level="INFO",
    )


def _insert_shop(conn, notify_from: str | None) -> int:
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO shops (owner_user_id, telegram_chat_id, name, wb_api_token_encrypted,
                            token_status, is_active, notify_from, created_at, updated_at)
        VALUES (1, 1, 'test', X'00', 'active', 1, ?, ?, ?)
        """,
        (notify_from, now, now),
    )
    conn.commit()
    return cur.lastrowid


def _queue_event(conn, shop_id: int, created_at: str, event_date: str = "2026-09-03T10:00:00") -> None:
    conn.execute(
        """
        INSERT INTO orders (shop_id, srid, date, last_change_date, nm_id, tech_size, raw_json)
        VALUES (?, ?, ?, ?, 111, '42', '{}')
        """,
        (shop_id, f"srid-{created_at}-{event_date}", event_date, event_date),
    )
    order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO notification_queue (
            shop_id, event_type, ref_table, ref_id, daily_seq, event_date, status, created_at
        ) VALUES (?, 'order', 'orders', ?, 1, ?, 'pending', ?)
        """,
        (shop_id, order_id, event_date, created_at),
    )
    conn.commit()


async def test_backfill_is_not_sent_when_notify_from_unset(conn):
    """notify_from не выставлен -> не отправляем НИЧЕГО, очередь не трогаем."""
    shop_id = _insert_shop(conn, notify_from=None)
    for i in range(5):
        _queue_event(conn, shop_id, f"2026-08-31T10:0{i}:00+00:00")

    from wbnotify import shops_repo

    bot = FakeBot()
    sent = await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id))
    assert sent == 0
    assert bot.sent == 0
    still_pending = conn.execute(
        "SELECT COUNT(*) FROM notification_queue WHERE status='pending'"
    ).fetchone()[0]
    assert still_pending == 5


async def test_only_events_after_notify_from_are_sent(conn):
    """События ДО отсечки остаются в очереди навсегда, отправляются только новые."""
    cutoff = "2026-09-01T12:00:00+00:00"
    shop_id = _insert_shop(conn, notify_from=cutoff)
    _queue_event(conn, shop_id, "2026-08-31T10:00:00+00:00")  # бэкфилл
    _queue_event(conn, shop_id, "2026-09-01T11:59:59+00:00")  # за секунду до отсечки
    _queue_event(conn, shop_id, "2026-09-01T12:00:01+00:00")  # после отсечки

    from wbnotify import shops_repo

    bot = FakeBot()
    sent = await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id))
    assert sent == 1
    assert bot.sent == 1
    assert conn.execute("SELECT COUNT(*) FROM notification_queue WHERE status='pending'").fetchone()[0] == 2


async def test_late_delivered_event_is_still_sent(conn):
    """WB может отдать заказ с опозданием на день-два-три. Такое событие попадает
    в очередь уже после включения рассылки, и его НАДО показать — иначе оно
    потеряется навсегда."""
    from wbnotify import shops_repo
    from wbnotify.counters import now_msk

    shop_id = _insert_shop(conn, notify_from="2026-01-01T00:00:00+00:00")
    today = now_msk().date()
    for days_late in (0, 1, 3):
        _queue_event(
            conn,
            shop_id,
            created_at=utcnow(),
            event_date=f"{today - timedelta(days=days_late)}T10:00:00",
        )

    bot = FakeBot()
    sent = await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id))
    assert sent == 3, "опоздавшие на 1 и 3 дня события должны уйти наравне со свежим"


async def test_ancient_event_classified_late_is_not_sent(conn):
    """Но совсем древнее событие (старше окна) не шлём, даже если оно впервые
    классифицировано только сейчас — по created_at оно выглядело бы свежим."""
    from wbnotify import shops_repo
    from wbnotify.counters import now_msk
    from wbnotify.telegram.sender import MAX_EVENT_AGE_DAYS

    shop_id = _insert_shop(conn, notify_from="2026-01-01T00:00:00+00:00")
    today = now_msk().date()
    too_old = today - timedelta(days=MAX_EVENT_AGE_DAYS + 1)
    _queue_event(conn, shop_id, created_at=utcnow(), event_date=f"{too_old}T10:00:00")
    _queue_event(conn, shop_id, created_at=utcnow(), event_date=f"{today}T10:00:00")

    bot = FakeBot()
    sent = await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id))
    assert sent == 1, "старьё за пределами окна не шлём, сегодняшнее — шлём"


async def test_backlog_after_outage_is_sent_oldest_first(conn):
    """После простоя: сначала досылаем непереданное, потом текущее — очередь
    разбирается по возрастанию id, а лимит за цикл не даёт вывалить всё разом."""
    from wbnotify import shops_repo

    cutoff = "2026-09-03T08:00:00+00:00"
    shop_id = _insert_shop(conn, notify_from=cutoff)
    for i in range(5):
        _queue_event(conn, shop_id, created_at=f"2026-09-03T09:0{i}:00+00:00", event_date=f"2026-09-03T09:0{i}:00")

    ids_before = [r["id"] for r in conn.execute("SELECT id FROM notification_queue ORDER BY id")]

    bot = FakeBot()
    sent = await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id), limit=2)
    assert sent == 2, "за цикл уходит не больше лимита"

    sent_ids = [r["id"] for r in conn.execute("SELECT id FROM notification_queue WHERE status='sent' ORDER BY id")]
    assert sent_ids == ids_before[:2], "досылаем самое старое из непереданного, а не последнее"

    sent = await drain_queue_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id), limit=10)
    assert sent == 3, "остаток догоняется следующими циклами"


def test_scheduler_jobs_are_configured():
    """Все расписания заведены и защищены от наслаивания прогонов."""
    scheduler = build_scheduler(_make_config())
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"poll_and_notify", "daily_refresh", "daily_summary"}
    for job in jobs.values():
        assert job.max_instances == 1, "параллельные прогоны одного задания недопустимы"
        assert job.coalesce is True, "пропущенные срабатывания должны схлопываться"


async def test_new_shop_starts_notifying_after_first_sync(conn, monkeypatch, tmp_path):
    """Свежеподключённый кабинет включает рассылку сам.

    Раньше notify_from приходилось выставлять руками через CLI, и на живом
    сервере это стоило суток молчания: кабинет исправно синкался, а уведомления
    не шли вообще.
    """
    from wbnotify.scheduler import jobs

    shop_id = _insert_shop(conn, notify_from=None)
    assert conn.execute("SELECT notify_from FROM shops WHERE id=?", (shop_id,)).fetchone()[0] is None

    jobs._start_notifying(conn, shop_id)

    assert conn.execute("SELECT notify_from FROM shops WHERE id=?", (shop_id,)).fetchone()[0] is not None


def test_start_notifying_does_not_move_existing_mark(conn):
    """Повторный вызов не сдвигает отсечку: иначе события, накопившиеся между
    циклами, потерялись бы."""
    from wbnotify.scheduler import jobs

    shop_id = _insert_shop(conn, notify_from="2026-01-01T00:00:00+00:00")
    jobs._start_notifying(conn, shop_id)

    assert conn.execute("SELECT notify_from FROM shops WHERE id=?", (shop_id,)).fetchone()[0] == "2026-01-01T00:00:00+00:00"


async def test_already_synced_shop_without_mark_starts_notifying(conn, monkeypatch):
    """Кабинет, который давно синкается, но отсечку так и не получил, тоже
    должен заговорить: включение рассылки НЕ привязано к первому запуску.

    Поймано вживую: shop_id=2 синкался часами и молчал, потому что включение
    висело на признаке «карточек ещё нет».
    """
    from wbnotify.scheduler import jobs

    shop_id = _insert_shop(conn, notify_from=None)
    # У кабинета уже есть карточки — первым запуском он не считается.
    conn.execute(
        "INSERT INTO cards_cache (shop_id, nm_id, refreshed_at) VALUES (?, 1, ?)", (shop_id, utcnow())
    )
    conn.commit()
    assert jobs._has_cards(conn, shop_id)

    jobs._start_notifying(conn, shop_id)
    assert conn.execute("SELECT notify_from FROM shops WHERE id=?", (shop_id,)).fetchone()[0] is not None


async def test_failed_step_does_not_abort_the_rest(conn, monkeypatch):
    """Сбой одного шага синка не должен обрывать остальные и глушить кабинет.

    Реальный случай: у кабинета с урезанным токеном «stocks» отдавал 403, цикл
    обрывался на нём, классификация пропускалась — и десятки заказов висели в БД
    со статусом «нет в очереди», уведомления по ним не приходили никогда.
    """
    from wbnotify import sync as sync_pkg
    from wbnotify.wb_api.client import WBError

    shop_id = _insert_shop(conn, notify_from=None)
    shop = shops_repo.get_shop(conn, shop_id)
    called = []

    async def ok(conn_, shop_id_, token):
        called.append("ok")
        return 1

    async def forbidden(conn_, shop_id_, token):
        raise WBError("HTTP 403 Forbidden")

    monkeypatch.setattr(sync_pkg.shops_repo, "get_decrypted_token", lambda *a, **kw: "t")
    for step, func in (("orders", ok), ("sales", ok), ("stocks", forbidden), ("fbs", ok),
                       ("seller_stocks", ok), ("funnel", ok)):
        monkeypatch.setattr(sync_pkg, sync_pkg._STEP_FUNC_NAMES[step], func)

    result = await sync_pkg.sync_shop_frequent(conn, shop, "key")

    assert result["failed_steps"] == {"stocks": "HTTP 403 Forbidden"}
    assert result["error"] is None, "сбой остатков не повод молчать по заказам"
    assert len(called) == 5, "остальные шаги должны отработать"


async def test_failed_orders_step_does_stop_notifications(conn, monkeypatch):
    """А вот если не доехали сами заказы — уведомлять не по чему."""
    from wbnotify import sync as sync_pkg
    from wbnotify.wb_api.client import WBError

    shop_id = _insert_shop(conn, notify_from=None)
    shop = shops_repo.get_shop(conn, shop_id)

    async def ok(conn_, shop_id_, token):
        return 1

    async def broken(conn_, shop_id_, token):
        raise WBError("HTTP 500")

    monkeypatch.setattr(sync_pkg.shops_repo, "get_decrypted_token", lambda *a, **kw: "t")
    for step in sync_pkg.FREQUENT_STEPS:
        monkeypatch.setattr(sync_pkg, sync_pkg._STEP_FUNC_NAMES[step], broken if step == "orders" else ok)

    result = await sync_pkg.sync_shop_frequent(conn, shop, "key")
    assert "orders" in result["error"]


def test_cursor_is_shifted_back_for_late_rows():
    """WB отдаёт часть заказов задним числом, с исходным lastChangeDate.

    Строгий курсор такие строки терял навсегда: фильтр dateFrom их отсекал.
    Поймано вживую — заказы за 12:29 и 15:19 были в отчёте WB и не появились
    в боте, при этом соседние по времени пришли.
    """
    from wbnotify.db import SYNC_LOOKBACK_HOURS, shift_cursor_back

    assert shift_cursor_back("2026-09-08T16:00:00") == "2026-09-07T16:00:00"
    assert SYNC_LOOKBACK_HOURS == 24


def test_shift_cursor_back_survives_empty_and_broken_values():
    """Пустой курсор — это «синк с нуля», его отматывать нельзя."""
    from wbnotify.db import shift_cursor_back

    assert shift_cursor_back(None) is None
    assert shift_cursor_back("") is None
    assert shift_cursor_back("не-дата") == "не-дата"


async def test_orders_sync_requests_with_overlap_but_keeps_cursor_forward(conn, monkeypatch):
    """Запрашиваем с перекрытием назад, а курсор двигаем только вперёд —
    иначе он уезжал бы на сутки назад при каждом синке."""
    from wbnotify.db import set_cursor, get_cursor
    from wbnotify.sync import orders_sync

    shop_id = _insert_shop(conn, notify_from=None)
    set_cursor(conn, shop_id, "orders", "2026-09-08T16:00:00")
    conn.commit()
    asked = []

    async def fake_get_orders(token, date_from, flag=0):
        asked.append(date_from)
        return [{
            "srid": "late-1", "date": "2026-09-08T12:29:00", "lastChangeDate": "2026-09-08T12:29:00",
            "nmId": 1, "techSize": "37", "supplierArticle": "1/3020",
        }]

    monkeypatch.setattr(orders_sync, "get_orders", fake_get_orders)
    await orders_sync.sync_shop_orders(conn, shop_id, "token")

    assert asked == ["2026-09-07T16:00:00"], "запрос уходит с перекрытием"
    assert get_cursor(conn, shop_id, "orders") == "2026-09-08T16:00:00", "курсор не откатился"
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE srid='late-1'").fetchone()[0] == 1
