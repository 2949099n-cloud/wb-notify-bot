"""Ежедневная закреплённая сводка."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from wbnotify import members_repo, shops_repo
from wbnotify.db import utcnow
from wbnotify.telegram import daily_summary

OWNER_ID, OWNER_CHAT = 100, 1000
MANAGER_ID, MANAGER_CHAT = 200, 2000
DAY = "2026-09-03"


def _shop(conn) -> int:
    now = utcnow()
    shop_id = conn.execute(
        """
        INSERT INTO shops (owner_user_id, telegram_chat_id, name, wb_api_token_encrypted,
                           token_status, is_active, notify_from, created_at, updated_at)
        VALUES (?, ?, 'NILONIL', X'00', 'active', 1, ?, ?, ?)
        """,
        (OWNER_ID, OWNER_CHAT, now, now, now),
    ).lastrowid
    conn.commit()
    members_repo.add_owner(conn, shop_id, OWNER_ID, OWNER_CHAT)
    members_repo.upsert_user(conn, OWNER_ID, "Natali", OWNER_CHAT)
    return shop_id


def _order(conn, shop_id, srid, nm_id=555, price=1000.0, cancelled=False, day=DAY):
    conn.execute(
        """
        INSERT INTO orders (shop_id, srid, date, last_change_date, nm_id, tech_size,
                            price_with_disc, is_cancel, cancel_date, supplier_article, raw_json)
        VALUES (?, ?, ?, ?, ?, '40', ?, ?, ?, 'ART-1', '{}')
        """,
        (shop_id, srid, f"{day}T10:00:00", f"{day}T10:00:00", nm_id, price,
         1 if cancelled else 0, f"{day}T15:00:00" if cancelled else None),
    )
    conn.commit()


def _sale(conn, shop_id, sale_id, nm_id=555, price=900.0, is_return=0, day=DAY):
    conn.execute(
        """
        INSERT INTO sales (shop_id, sale_id, srid, is_return, date, last_change_date,
                           nm_id, tech_size, price_with_disc, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, '40', ?, '{}')
        """,
        (shop_id, sale_id, f"srid-{sale_id}", is_return, f"{day}T12:00:00", f"{day}T12:00:00", nm_id, price),
    )
    conn.commit()


class FakeBot:
    def __init__(self):
        self.messages = []
        self.pinned = []
        self.unpinned = []
        self._next_id = 500

    async def send_message(self, chat_id, text, parse_mode=None):
        self._next_id += 1
        self.messages.append((chat_id, text))
        return SimpleNamespace(message_id=self._next_id)

    async def pin_chat_message(self, chat_id, message_id, disable_notification=None):
        self.pinned.append((chat_id, message_id))

    async def unpin_chat_message(self, chat_id, message_id):
        self.unpinned.append((chat_id, message_id))


def test_summary_counts_orders_sales_cancels_and_returns(conn):
    shop_id = _shop(conn)
    _order(conn, shop_id, "s1", price=1000.0)
    _order(conn, shop_id, "s2", price=2000.0)
    _order(conn, shop_id, "s3", price=1500.0, cancelled=True)
    _sale(conn, shop_id, "S1", price=900.0)
    _sale(conn, shop_id, "S2", price=800.0, is_return=1)

    text = daily_summary.build_summary(conn, shops_repo.get_shop(conn, shop_id), DAY)

    assert "Сводка за 03.09.2026" in text
    assert "📦 Заказы: <b>3 шт. · 4 500 ₽</b>" in text
    assert "✅ Продажи: <b>1 шт. · 900 ₽</b>" in text
    assert "❌ Отмены: <b>1 шт. · 1 500 ₽</b>" in text
    assert "🔄 Возвраты: <b>1 шт. · 800 ₽</b>" in text


def test_summary_ignores_other_days(conn):
    shop_id = _shop(conn)
    _order(conn, shop_id, "s1", price=1000.0)
    _order(conn, shop_id, "other", price=9999.0, day="2026-09-01")

    text = daily_summary.build_summary(conn, shops_repo.get_shop(conn, shop_id), DAY)
    assert "📦 Заказы: <b>1 шт. · 1 000 ₽</b>" in text


def test_summary_ranks_items_by_revenue_and_profit(conn):
    """Топы строятся по деньгам продаж, а не по числу заказов."""
    shop_id = _shop(conn)
    _sale(conn, shop_id, "S1", nm_id=555, price=1000.0)
    _sale(conn, shop_id, "S2", nm_id=777, price=3000.0)

    text = daily_summary.build_summary(conn, shops_repo.get_shop(conn, shop_id), DAY)
    revenue_block = text.split("Топ-5 по выручке")[1]
    assert revenue_block.index("777") < revenue_block.index("555"), "дороже — выше"
    assert "Топ-5 по прибыли" in text
    assert 'href="https://www.wildberries.ru/catalog/777/detail.aspx"' in text


def test_summary_compares_with_previous_day(conn):
    """Под каждой строкой — изменение ко вчера, цвет по смыслу метрики."""
    shop_id = _shop(conn)
    # Отменённый заказ остаётся заказом того дня — WB его из заказов не вычитает,
    # поэтому в счёт строки «Заказы» он тоже попадает.
    _order(conn, shop_id, "y1", price=1000.0, day="2026-09-02")
    _order(conn, shop_id, "t1", price=1000.0)
    _order(conn, shop_id, "t2", price=1000.0)
    _order(conn, shop_id, "t3", price=1000.0)
    # Отмен вчера было две, сегодня одна — падение отмен это хорошо, зелёное.
    _order(conn, shop_id, "yc1", price=500.0, cancelled=True, day="2026-09-02")
    _order(conn, shop_id, "yc2", price=500.0, cancelled=True, day="2026-09-02")
    _order(conn, shop_id, "tc1", price=500.0, cancelled=True)

    text = daily_summary.build_summary(conn, shops_repo.get_shop(conn, shop_id), DAY)
    orders_delta = text.split("📦 Заказы")[1].splitlines()[1]
    cancels_delta = text.split("❌ Отмены")[1].splitlines()[1]

    assert "🟢 +1 шт." in orders_delta, "рост заказов — зелёный"
    assert "🟢 −1 шт." in cancels_delta, "падение отмен — тоже зелёный"


def test_summary_shows_commission_logistics_and_profit(conn):
    shop_id = _shop(conn)
    _sale(conn, shop_id, "S1", price=1000.0)

    text = daily_summary.build_summary(conn, shops_repo.get_shop(conn, shop_id), DAY)
    assert "💰 Комиссия:" in text
    assert "🚚 Логистика:" in text and "прямая" in text and "возвратная" in text
    assert "Операционная прибыль" in text


def test_low_stock_shows_both_articles_and_days(conn):
    """В блоке остатков — артикул WB и артикул продавца, срок в днях (решение
    пользователя): по названию товар не опознать, они почти одинаковые."""
    shop_id = _shop(conn)
    from wbnotify.counters import now_msk

    today = now_msk().date().isoformat()
    _order(conn, shop_id, "fresh", day=today)

    text = daily_summary.build_summary(conn, shops_repo.get_shop(conn, shop_id), today)
    block = text[text.index("Заканчивается"):]
    assert "555" in block and "ART-1" in block, "оба артикула на месте"
    assert "на 0 дней" in block, "нулевой запас — тоже в днях, для единообразия"
    assert "закончился" not in block


def test_days_are_declined_properly():
    """«на 1 дней» в отчёте выглядело бы неряшливо."""
    from wbnotify.telegram.daily_summary import _days

    assert [_days(v) for v in (0, 1, 2, 5, 11, 21, 22)] == [
        "0 дней", "1 день", "2 дня", "5 дней", "11 дней", "21 день", "22 дня",
    ]
    assert _days(6.5) == "6.5 дня"


async def test_summary_goes_to_every_member_and_is_pinned(conn):
    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)
    _order(conn, shop_id, "s1")

    bot = FakeBot()
    sent = await daily_summary.send_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id), DAY)

    assert sent == 2
    assert sorted(chat for chat, _ in bot.messages) == [OWNER_CHAT, MANAGER_CHAT]
    assert len(bot.pinned) == 2, "сводка должна закрепляться в каждом чате"
    assert bot.unpinned == [], "откреплять в первый день нечего"


async def test_yesterdays_summary_is_unpinned(conn):
    """В шапке чата должна висеть ровно одна, свежая сводка."""
    shop_id = _shop(conn)
    _order(conn, shop_id, "s1")
    shop = shops_repo.get_shop(conn, shop_id)

    bot = FakeBot()
    await daily_summary.send_for_shop(conn, bot, shop, "2026-09-02")
    first_id = bot.pinned[0][1]

    await daily_summary.send_for_shop(conn, bot, shop, DAY)

    assert bot.unpinned == [(OWNER_CHAT, first_id)]


async def test_unreachable_chat_does_not_block_others(conn):
    """Менеджер заблокировал бота — владелец сводку всё равно получает."""
    from telegram.error import TelegramError

    shop_id = _shop(conn)
    token, _ = members_repo.create_invite(conn, shop_id, OWNER_ID)
    members_repo.accept_invite(conn, token, MANAGER_ID, MANAGER_CHAT)
    _order(conn, shop_id, "s1")

    bot = FakeBot()
    original = bot.send_message

    async def flaky(chat_id, text, parse_mode=None):
        if chat_id == MANAGER_CHAT:
            raise TelegramError("bot was blocked by the user")
        return await original(chat_id, text, parse_mode=parse_mode)

    bot.send_message = flaky
    sent = await daily_summary.send_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id), DAY)

    assert sent == 1
    assert [chat for chat, _ in bot.messages] == [OWNER_CHAT]


async def test_pin_failure_does_not_lose_the_summary(conn):
    """Нет прав закреплять — сообщение всё равно отправлено."""
    from telegram.error import TelegramError

    shop_id = _shop(conn)
    _order(conn, shop_id, "s1")

    bot = FakeBot()

    async def cannot_pin(chat_id, message_id, disable_notification=None):
        raise TelegramError("not enough rights to pin a message")

    bot.pin_chat_message = cannot_pin
    sent = await daily_summary.send_for_shop(conn, bot, shops_repo.get_shop(conn, shop_id), DAY)

    assert sent == 1
    assert len(bot.messages) == 1


def test_summary_does_not_mention_the_wb_source_gap(conn):
    """Пояснение про разрыв между источниками WB из сводки убрано (решение
    пользователя): сводка — про цифры дня, а не про наши ограничения."""
    shop_id = _shop(conn)
    _order(conn, shop_id, "s1")
    conn.execute(
        """
        INSERT INTO sales_funnel_daily (shop_id, date_msk, nm_id, order_count, order_sum,
                                        buyout_count, buyout_sum, cancel_count, cancel_sum, refreshed_at)
        VALUES (?, ?, 555, 5, 5000, 0, 0, 0, 0, ?)
        """,
        (shop_id, DAY, utcnow()),
    )
    conn.commit()

    text = daily_summary.build_summary(conn, shops_repo.get_shop(conn, shop_id), DAY)
    assert "Поштучно" not in text
    assert "ограничение API" not in text
    assert "📦 Заказы: <b>5 шт." in text, "сами цифры дня считаются по воронке, как в приложении WB"
