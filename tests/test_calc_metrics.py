from __future__ import annotations

import json
from datetime import timedelta

from wbnotify.calc.metrics import (
    buyout_rate_with_returns,
    order_velocity,
    rate,
    sales_count,
    stock_days_aggregate,
    stock_days_by_warehouse,
    stock_eta,
    velocity,
    yesterday_today_breakdown,
)
from wbnotify.db import utcnow

SHOP_ID = 1
NM_ID = 111
SIZE = "42"


def _insert_shop(conn):
    conn.execute(
        """
        INSERT INTO shops (id, owner_user_id, telegram_chat_id, name, wb_api_token_encrypted, created_at, updated_at)
        VALUES (?, 1, 1, 'test', X'00', ?, ?)
        """,
        (SHOP_ID, utcnow(), utcnow()),
    )
    conn.commit()


def _insert_order(conn, srid: str, date: str, warehouse_name: str = "Склад А"):
    conn.execute(
        """
        INSERT INTO orders (shop_id, srid, date, last_change_date, nm_id, tech_size, warehouse_name, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
        """,
        (SHOP_ID, srid, date, date, NM_ID, SIZE, warehouse_name),
    )
    conn.commit()


def _insert_sale(conn, sale_id: str, date: str, is_return: int, finished_price: float = 1000.0, srid: str | None = None):
    # price_with_disc == finished_price в фикстурах: суммы «вчера/сегодня» считаются
    # по priceWithDisc (сверено с аналитикой WB), остальным тестам поле безразлично.
    conn.execute(
        """
        INSERT INTO sales (shop_id, sale_id, srid, is_return, date, last_change_date, nm_id, tech_size,
                            finished_price, price_with_disc, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
        """,
        (SHOP_ID, sale_id, srid, is_return, date, date, NM_ID, SIZE, finished_price, finished_price),
    )
    conn.commit()


# --------------------------- чистые функции ---------------------------


def test_rate_zero_denominator_returns_zero():
    assert rate(5, 0) == 0.0


def test_rate_normal_case():
    assert rate(50, 200) == 25.0


def test_velocity_zero_days_returns_zero():
    assert velocity(10, 0) == 0.0


def test_velocity_normal_case():
    assert velocity(14, 7) == 2.0


def test_stock_eta_infinite_on_zero_velocity():
    assert stock_eta(qty=50, items_per_day=0) == "∞"


def test_stock_eta_normal_case():
    assert stock_eta(qty=20, items_per_day=2) == "10"


# --------------------------- обёртки поверх БД ---------------------------


def test_order_velocity_counts_orders_in_window(conn):
    _insert_shop(conn)
    _insert_order(conn, "s1", "2026-08-25T10:00:00")
    _insert_order(conn, "s2", "2026-08-26T10:00:00")
    _insert_order(conn, "s3", "2026-08-20T10:00:00")  # вне 7-дневного окна от 2026-08-31

    v = order_velocity(conn, SHOP_ID, NM_ID, SIZE, window_days=7, asof="2026-08-31")
    assert v == velocity(2, 7)


def test_order_velocity_does_not_leak_into_future_relative_to_asof(conn):
    """Регрессия: окно раньше не имело верхней границы — для исторического asof
    (например, старое бэкфилленное уведомление, отрисованное сильно позже)
    в расчёт незаметно попадали заказы ПОСЛЕ asof, вплоть до реального "сегодня"."""
    _insert_shop(conn)
    _insert_order(conn, "in-window", "2026-08-28T10:00:00")
    _insert_order(conn, "after-asof", "2026-09-15T10:00:00")  # "будущее" относительно asof

    v = order_velocity(conn, SHOP_ID, NM_ID, SIZE, window_days=7, asof="2026-08-31")
    assert v == velocity(1, 7)  # только "in-window", "after-asof" не должен учитываться


def test_buyout_rate_with_returns_subtracts_returns(conn):
    """Когортный расчёт: выкупы/возвраты должны ссылаться (по srid) на заказы
    ИЗ ЭТОЙ ЖЕ когорты, а не просто попадать в то же календарное окно."""
    _insert_shop(conn)
    for i in range(10):
        _insert_order(conn, f"order-{i}", "2026-08-28T10:00:00")
    for i in range(6):
        _insert_sale(conn, f"S{i}", "2026-08-28T10:00:00", is_return=0, srid=f"order-{i}")
    for i in range(6, 8):
        _insert_sale(conn, f"R{i}", "2026-08-28T10:00:00", is_return=1, srid=f"order-{i}")

    pct = buyout_rate_with_returns(conn, SHOP_ID, NM_ID, SIZE, window_days=7, asof="2026-08-31")
    # (6 выкупов - 2 возврата) / 10 заказов когорты * 100 = 40%
    assert pct == 40.0


def test_buyout_rate_never_exceeds_100_percent_from_unrelated_cohorts(conn):
    """Регрессия на реальный баг: заказ в окне + выкупы СТАРЫХ заказов (другой
    srid, вне окна) раньше давали 200%, т.к. числитель/знаменатель были
    независимыми выборками по датам. Теперь выкуп чужого (более раннего) заказа
    не должен засчитываться в когорту этого окна."""
    _insert_shop(conn)
    _insert_order(conn, "recent-order", "2026-08-31T10:00:00")  # единственный заказ в 7-дневном окне
    # эти два выкупа — от заказов, размещённых ДО окна (другой srid, не входящий в когорту)
    _insert_sale(conn, "S-old-1", "2026-08-26T10:00:00", is_return=0, srid="old-order-1")
    _insert_sale(conn, "S-old-2", "2026-08-31T10:00:00", is_return=0, srid="old-order-2")

    pct = buyout_rate_with_returns(conn, SHOP_ID, NM_ID, SIZE, window_days=7, asof="2026-08-31")
    assert pct == 0.0  # "recent-order" сам ещё не выкуплен, чужие выкупы к его когорте не относятся


def test_buyout_rate_counts_buyout_even_if_sale_happens_after_window(conn):
    """Заказ попал в когорту окна, но фактическая продажа случилась ПОЗЖЕ, чем
    заканчивается окно (доставка не мгновенная) — всё равно должна засчитаться."""
    _insert_shop(conn)
    _insert_order(conn, "srid-1", "2026-08-25T10:00:00")  # в 7-дневном окне до 2026-08-31
    _insert_sale(conn, "S1", "2026-09-05T10:00:00", is_return=0, srid="srid-1")  # выкуп ПОСЛЕ окна

    pct = buyout_rate_with_returns(conn, SHOP_ID, NM_ID, SIZE, window_days=7, asof="2026-08-31")
    assert pct == 100.0


def test_sales_count_counts_only_buyouts_not_returns(conn):
    _insert_shop(conn)
    _insert_sale(conn, "S1", "2026-08-28T10:00:00", is_return=0)
    _insert_sale(conn, "S2", "2026-08-28T10:00:00", is_return=0)
    _insert_sale(conn, "R1", "2026-08-28T10:00:00", is_return=1)

    n = sales_count(conn, SHOP_ID, NM_ID, SIZE, window_days=7, asof="2026-08-31")
    assert n == 2


def test_yesterday_today_boundary_is_exact_midnight(conn):
    _insert_shop(conn)
    _insert_sale(conn, "S-late-yesterday", "2026-08-30T23:59:59", is_return=0, finished_price=100)
    _insert_sale(conn, "S-early-today", "2026-08-31T00:00:00", is_return=0, finished_price=200)

    result = yesterday_today_breakdown(conn, SHOP_ID, asof="2026-08-31")
    assert result["yesterday_total"] == {"qty": 1, "amount": 100.0}
    assert result["today_total"] == {"qty": 1, "amount": 200.0}


def test_yesterday_today_basis_depends_on_event_type(conn):
    """Решение пользователя: в уведомлении «Заказ» — источник orders, в «Выкуп» —
    sales(is_return=0), в «Отмена заказа»/«Возврат» — sales(is_return=1)."""
    _insert_shop(conn)
    _insert_order(conn, "o1", "2026-08-31T10:00:00")
    _insert_sale(conn, "S1", "2026-08-31T10:00:00", is_return=0, finished_price=500)
    _insert_sale(conn, "R1", "2026-08-31T10:00:00", is_return=1, finished_price=300)

    orders_basis = yesterday_today_breakdown(conn, SHOP_ID, asof="2026-08-31", event_type="order")
    assert orders_basis["today_total"]["qty"] == 1

    buyout_basis = yesterday_today_breakdown(conn, SHOP_ID, asof="2026-08-31", event_type="buyout")
    assert buyout_basis["today_total"] == {"qty": 1, "amount": 500.0}

    cancel_basis = yesterday_today_breakdown(conn, SHOP_ID, asof="2026-08-31", event_type="cancel")
    assert cancel_basis["today_total"] == {"qty": 1, "amount": 300.0}

    return_basis = yesterday_today_breakdown(conn, SHOP_ID, asof="2026-08-31", event_type="return")
    assert return_basis["today_total"] == {"qty": 1, "amount": 300.0}


def test_stock_days_aggregate_infinite_when_no_orders(conn):
    _insert_shop(conn)
    conn.execute(
        "INSERT INTO cards_cache (shop_id, nm_id, sizes_json, refreshed_at) VALUES (?, ?, ?, ?)",
        (SHOP_ID, NM_ID, json.dumps([{"chrtID": 999, "techSize": SIZE, "wbSize": SIZE, "skus": []}]), utcnow()),
    )
    conn.execute(
        """
        INSERT INTO stocks_current (shop_id, nm_id, chrt_id, warehouse_id, warehouse_name, quantity, snapshot_at)
        VALUES (?, ?, 999, 1, 'Склад А', 25, ?)
        """,
        (SHOP_ID, NM_ID, utcnow()),
    )
    conn.commit()
    # заказов за окно нет -> скорость 0 -> "∞", а не деление на 0
    assert stock_days_aggregate(conn, SHOP_ID, NM_ID, SIZE, window_days=7) == "∞"


def test_stock_days_basis_orders_vs_sales(conn):
    """Решение пользователя: в «Заказ» остаток делим на скорость ЗАКАЗОВ,
    в «Выкуп» — на скорость ПРОДАЖ. При разном темпе это разные числа."""
    _insert_shop(conn)
    conn.execute(
        "INSERT INTO cards_cache (shop_id, nm_id, sizes_json, refreshed_at) VALUES (?, ?, ?, ?)",
        (SHOP_ID, NM_ID, json.dumps([{"chrtID": 999, "techSize": SIZE, "wbSize": SIZE, "skus": []}]), utcnow()),
    )
    conn.execute(
        """
        INSERT INTO stocks_current (shop_id, nm_id, chrt_id, warehouse_id, warehouse_name, quantity, snapshot_at)
        VALUES (?, ?, 999, 1, 'Склад А', 14, ?)
        """,
        (SHOP_ID, NM_ID, utcnow()),
    )
    conn.commit()
    # 7 заказов за неделю (1/день), но только 2 выкупа (0.29/день)
    today = utcnow()[:10]
    for i in range(7):
        _insert_order(conn, f"o{i}", f"{today}T0{i}:00:00")
    for i in range(2):
        _insert_sale(conn, f"S{i}", f"{today}T0{i}:00:00", is_return=0)

    by_orders = stock_days_aggregate(conn, SHOP_ID, NM_ID, SIZE, window_days=7, basis="orders")
    by_sales = stock_days_aggregate(conn, SHOP_ID, NM_ID, SIZE, window_days=7, basis="sales")
    assert by_orders == "14"  # 14 шт / 1.0 в день
    assert by_sales == "49"   # 14 шт / (2/7) в день
    assert by_orders != by_sales


def test_stock_days_by_warehouse_per_warehouse_breakdown(conn):
    _insert_shop(conn)
    conn.execute(
        "INSERT INTO cards_cache (shop_id, nm_id, sizes_json, refreshed_at) VALUES (?, ?, ?, ?)",
        (SHOP_ID, NM_ID, json.dumps([{"chrtID": 999, "techSize": SIZE, "wbSize": SIZE, "skus": []}]), utcnow()),
    )
    conn.execute(
        """
        INSERT INTO stocks_current (shop_id, nm_id, chrt_id, warehouse_id, warehouse_name, quantity, snapshot_at)
        VALUES (?, ?, 999, 1, 'Склад А', 20, ?)
        """,
        (SHOP_ID, NM_ID, utcnow()),
    )
    conn.execute(
        """
        INSERT INTO stocks_current (shop_id, nm_id, chrt_id, warehouse_id, warehouse_name, quantity, snapshot_at)
        VALUES (?, ?, 999, 2, 'Склад Б', 5, ?)
        """,
        (SHOP_ID, NM_ID, utcnow()),
    )
    conn.commit()
    # 2 заказа со Склада А за последние 7 дней, ни одного со Склада Б.
    # Даты — ОТНОСИТЕЛЬНО сегодняшнего дня: окно у stock_days_by_warehouse
    # отсчитывается от «сейчас», и с фиксированными датами тест ломался сам собой,
    # когда календарь доезжал до края окна (поймано 06.09.2026).
    from wbnotify.counters import now_msk

    today = now_msk().date()
    _insert_order(conn, "wa-1", f"{today - timedelta(days=1)}T10:00:00", warehouse_name="Склад А")
    _insert_order(conn, "wa-2", f"{today - timedelta(days=2)}T10:00:00", warehouse_name="Склад А")

    result = stock_days_by_warehouse(conn, SHOP_ID, NM_ID, SIZE, windows=(7,))
    by_name = {r["warehouse_name"]: r for r in result}

    assert by_name["Склад А"]["quantity"] == 20
    assert by_name["Склад А"]["eta_by_window"][7] == stock_eta(20, velocity(2, 7))
    assert by_name["Склад Б"]["quantity"] == 5
    assert by_name["Склад Б"]["eta_by_window"][7] == "∞"  # ни одного заказа с этого склада


def test_order_velocity_counts_whole_article_not_one_size(conn):
    """Скорость заказов считается по артикулу целиком (решение пользователя).

    Раньше фильтровали ещё и по размеру: у артикула их пять-шесть, спрос по
    каждому в отдельности близок к нулю, и цифра выглядела абсурдно заниженной.
    """
    from wbnotify.calc.metrics import order_velocity

    _insert_shop(conn)
    shop_id = SHOP_ID
    for index, size in enumerate(["38", "39", "40", "38", "39", "38", "40"]):
        conn.execute(
            "INSERT INTO orders (shop_id, srid, date, last_change_date, nm_id, tech_size, raw_json)"
            " VALUES (?, ?, '2026-09-02T10:00:00', '2026-09-02T10:00:00', 555, ?, '{}')",
            (shop_id, f"srid-{index}", size),
        )
    conn.commit()

    asof = "2026-09-03T00:00:00"
    # 7 заказов по артикулу за 7 дней = 1.0 шт/день, независимо от размера в кнопке
    assert order_velocity(conn, shop_id, 555, "40", 7, asof) == 1.0
    assert order_velocity(conn, shop_id, 555, "38", 7, asof) == 1.0


def test_sales_and_buyout_rate_count_whole_article(conn):
    """Продажи и % выкупа тоже считаются по артикулу целиком, а не по размеру."""
    from wbnotify.calc.metrics import buyout_rate_with_returns, sales_count

    _insert_shop(conn)
    asof = "2026-09-03T00:00:00"
    # Четыре заказа одного артикула разных размеров, выкуплены два.
    for index, size in enumerate(["38", "39", "40", "41"]):
        conn.execute(
            "INSERT INTO orders (shop_id, srid, date, last_change_date, nm_id, tech_size, raw_json)"
            " VALUES (?, ?, '2026-09-01T10:00:00', '2026-09-01T10:00:00', 555, ?, '{}')",
            (SHOP_ID, f"srid-{index}", size),
        )
    for index, size in enumerate(["38", "40"]):
        conn.execute(
            "INSERT INTO sales (shop_id, sale_id, srid, is_return, date, last_change_date, nm_id, tech_size, raw_json)"
            " VALUES (?, ?, ?, 0, '2026-09-02T10:00:00', '2026-09-02T10:00:00', 555, ?, '{}')",
            (SHOP_ID, f"S-{index}", f"srid-{0 if size == '38' else 2}", size),
        )
    conn.commit()

    # Размер в аргументе больше не влияет: считаем по всему артикулу.
    assert sales_count(conn, SHOP_ID, 555, "41", 7, asof) == 2
    assert sales_count(conn, SHOP_ID, 555, "38", 7, asof) == 2
    assert buyout_rate_with_returns(conn, SHOP_ID, 555, "41", 7, asof) == 50.0
