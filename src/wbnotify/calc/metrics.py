"""Расчётные функции: % выкупа, скорость заказов, дней остатка.

Денежная/расчётная логика — намеренно разбита на чистые функции (тривиально
тестируемые, без БД) и обёртки поверх БД, которые эти чистые функции вызывают.
Деление на 0 нигде не должно кидать исключение — только "∞"/0.0.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from wbnotify.counters import today_msk_str

# ---------------------------------------------------------------------------
# Чистые функции — без БД, без побочных эффектов.
# ---------------------------------------------------------------------------


def rate(numerator: float, denominator: float) -> float:
    """Процент (0..100). 0.0 при делении на 0 — не ошибка, а "нет данных за период"."""
    if not denominator:
        return 0.0
    return numerator / denominator * 100


def velocity(count: float, days: float) -> float:
    """Штук в день. 0.0 при days == 0."""
    if not days:
        return 0.0
    return count / days


def stock_eta(qty: float, items_per_day: float) -> str:
    """"≈ на N дн." — только число, единицу измерения добавляет вызывающий код.
    "∞" если скорость нулевая (за период не было ни одного заказа/продажи) —
    обязательный edge case, не деление на 0 через исключение.
    """
    if not items_per_day:
        return "∞"
    return str(round(qty / items_per_day))


# ---------------------------------------------------------------------------
# Обёртки поверх БД.
# ---------------------------------------------------------------------------


def _window_bounds(window_days: int, asof: str | None) -> tuple[str, str]:
    """(cutoff, upper_exclusive) — окно [cutoff; upper) СТРОГО вокруг asof.

    Верхняя граница обязательна: без неё окно "утекает" в реальное будущее
    относительно asof — для исторических/бэкфилленных событий (например,
    уведомление о заказе с датой в марте, отрисованное в сентябре) это
    незаметно тянуло в расчёт данные вплоть до сегодняшнего дня вместо
    "N дней ДО этого события" (найдено и исправлено по репорту пользователя —
    см. CLAUDE.md).
    """
    asof_date_str = (asof or today_msk_str())[:10]
    asof_date = datetime.strptime(asof_date_str, "%Y-%m-%d")
    cutoff = (asof_date - timedelta(days=window_days)).strftime("%Y-%m-%d")
    upper = (asof_date + timedelta(days=1)).strftime("%Y-%m-%d")  # исключая, т.е. по конец asof_date включительно
    return cutoff, upper


def order_velocity(
    conn: sqlite3.Connection, shop_id: int, nm_id: int, tech_size: str, window_days: int, asof: str | None = None
) -> float:
    """Скорость заказов (шт/день) за window_days ДО asof (не считая будущего
    относительно asof). Считаем ВСЕ заказы, включая впоследствии отменённые —
    это сигнал спроса, а не фактических отгрузок."""
    cutoff, upper = _window_bounds(window_days, asof)
    count = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE shop_id=? AND nm_id=? AND tech_size=? AND date >= ? AND date < ?",
        (shop_id, nm_id, tech_size, cutoff, upper),
    ).fetchone()[0]
    return velocity(count, window_days)


def sales_count(
    conn: sqlite3.Connection, shop_id: int, nm_id: int, tech_size: str, window_days: int, asof: str | None = None
) -> int:
    """Кол-во ВЫКУПОВ (не заказов) за period — is_return=0 в sales."""
    cutoff, upper = _window_bounds(window_days, asof)
    return conn.execute(
        "SELECT COUNT(*) FROM sales WHERE shop_id=? AND nm_id=? AND tech_size=? AND is_return=0 AND date >= ? AND date < ?",
        (shop_id, nm_id, tech_size, cutoff, upper),
    ).fetchone()[0]


def buyout_rate_with_returns(
    conn: sqlite3.Connection, shop_id: int, nm_id: int, tech_size: str, window_days: int, asof: str | None = None
) -> float:
    """(выкупы - возвраты) / заказы * 100 — КОГОРТНЫЙ расчёт, не независимые окна.

    Раньше числитель (выкупы/возвраты) и знаменатель (заказы) фильтровались
    каждый по СВОЕЙ дате независимо — это давало >100% (например, 200%),
    когда в окно попадали выкупы старых заказов, размещённых до этого окна
    (найдено на реальных данных — см. CLAUDE.md). Правильно: берём заказы,
    СДЕЛАННЫЕ в окне [cutoff; asof], и смотрим, у скольких ИЗ ЭТИХ конкретных
    заказов (по srid) в итоге есть продажа — независимо от того, когда сама
    продажа произошла (выкуп может случиться и позже окна заказа).
    """
    cutoff, upper = _window_bounds(window_days, asof)
    cohort_srids = [
        r["srid"]
        for r in conn.execute(
            "SELECT srid FROM orders WHERE shop_id=? AND nm_id=? AND tech_size=? AND date >= ? AND date < ?",
            (shop_id, nm_id, tech_size, cutoff, upper),
        )
    ]
    if not cohort_srids:
        return 0.0

    placeholders = ",".join("?" * len(cohort_srids))
    buyouts = conn.execute(
        f"SELECT COUNT(*) FROM sales WHERE shop_id=? AND is_return=0 AND srid IN ({placeholders})",
        (shop_id, *cohort_srids),
    ).fetchone()[0]
    returns = conn.execute(
        f"SELECT COUNT(*) FROM sales WHERE shop_id=? AND is_return=1 AND srid IN ({placeholders})",
        (shop_id, *cohort_srids),
    ).fetchone()[0]
    return rate(buyouts - returns, len(cohort_srids))


# Источник для "Вчера/Сегодня" зависит от ТИПА уведомления, в котором показывается
# блок (решение пользователя): в уведомлении «Заказ» — считаем заказы, в «Выкуп» —
# выкупы, в «Отмена заказа» — возвраты. (table, price_column, sales_is_return_filter)
# Поле суммы — priceWithDisc, а НЕ finishedPrice: сверено с аналитикой WB
# пользователя на реальном дне — выкупы за 01.09 дали 8 шт / 67248 ₽, что
# совпало с priceWithDisc до рубля (finishedPrice давал 39459 ₽ — мимо).
_BASIS_BY_EVENT_TYPE = {
    "order": ("orders", "price_with_disc", None),
    "buyout": ("sales", "price_with_disc", 0),
    "cancel": ("sales", "price_with_disc", 1),
    "return": ("sales", "price_with_disc", 1),
}


def _funnel_breakdown(
    conn: sqlite3.Connection, shop_id: int, nm_id: int | None, today: str, yesterday: str
) -> dict:
    """«Вчера/Сегодня» по ЗАКАЗАМ — из воронки продаж (sales_funnel_daily).

    Statistics API (/orders) и воронка считают заказы по-разному: на реальных днях
    у /orders 30 и 30, у воронки 37 и 38 — и со «сводкой на главной» в ЛК совпадает
    воронка (проверено до рубля). Раз пользователь сверяется со сводкой, берём её же
    источник. Гранулярность воронки — nmId, размеров в ней нет, поэтому «таких»
    здесь = «по этому артикулу» (а не по артикулу+размеру, как в остальных блоках).
    """

    def _agg(date_str: str, item_only: bool) -> dict:
        query = "SELECT COALESCE(SUM(order_count),0), COALESCE(SUM(order_sum),0) FROM sales_funnel_daily WHERE shop_id=? AND date_msk=?"
        params: list = [shop_id, date_str]
        if item_only:
            query += " AND nm_id=?"
            params.append(nm_id)
        qty, amount = conn.execute(query, params).fetchone()
        return {"qty": qty, "amount": amount}

    result = {
        "yesterday_total": _agg(yesterday, item_only=False),
        "today_total": _agg(today, item_only=False),
    }
    if nm_id is not None:
        result["yesterday_item"] = _agg(yesterday, item_only=True)
        result["today_item"] = _agg(today, item_only=True)
    return result


def yesterday_today_breakdown(
    conn: sqlite3.Connection,
    shop_id: int,
    nm_id: int | None = None,
    tech_size: str | None = None,
    asof: str | None = None,
    event_type: str = "buyout",
) -> dict:
    """Вчера/сегодня, отдельно "по этому товару" (если переданы nm_id/tech_size) и
    "по всем товарам магазина". `event_type` выбирает источник данных — см.
    _BASIS_BY_EVENT_TYPE (решение пользователя, не единая логика для всех событий)."""
    today_str = (asof or today_msk_str())[:10]
    yesterday_str = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    if event_type == "order":
        funnel = _funnel_breakdown(conn, shop_id, nm_id, today_str, yesterday_str)
        # Воронка синкается только за сегодня/вчера. Для исторических уведомлений
        # (бэкфилл) её данных нет — тогда честно падаем на orders, иначе показали бы нули.
        if funnel["today_total"]["qty"] or funnel["yesterday_total"]["qty"]:
            return funnel

    table, price_column, is_return_filter = _BASIS_BY_EVENT_TYPE.get(event_type, _BASIS_BY_EVENT_TYPE["buyout"])
    today, yesterday = today_str, yesterday_str

    def _agg(date_str: str, item_only: bool) -> dict:
        query = f"SELECT COUNT(*), COALESCE(SUM({price_column}), 0) FROM {table} WHERE shop_id=? AND date >= ? AND date < ?"
        params: list = [shop_id, date_str, date_str + "T23:59:59.999999"]
        if is_return_filter is not None:
            query += " AND is_return=?"
            params.append(is_return_filter)
        if item_only:
            query += " AND nm_id=? AND tech_size=?"
            params += [nm_id, tech_size]
        qty, amount = conn.execute(query, params).fetchone()
        return {"qty": qty, "amount": amount}

    result = {
        "yesterday_total": _agg(yesterday, item_only=False),
        "today_total": _agg(today, item_only=False),
    }
    if nm_id is not None and tech_size is not None:
        result["yesterday_item"] = _agg(yesterday, item_only=True)
        result["today_item"] = _agg(today, item_only=True)
    return result


def _chrt_ids_for_size(conn: sqlite3.Connection, shop_id: int, nm_id: int, tech_size: str) -> list[int]:
    """chrtId(ы) для (nm_id, tech_size) через cards_cache.sizes_json — это
    единственное место, где WB связывает размерный артикул с techSize (нужно
    для join с stocks_current, который ключуется по chrt_id, а не techSize)."""
    row = conn.execute(
        "SELECT sizes_json FROM cards_cache WHERE shop_id=? AND nm_id=?", (shop_id, nm_id)
    ).fetchone()
    if row is None or not row["sizes_json"]:
        return []
    sizes = json.loads(row["sizes_json"])
    return [s["chrtID"] for s in sizes if s.get("techSize") == tech_size]


def sales_velocity(
    conn: sqlite3.Connection, shop_id: int, nm_id: int, tech_size: str, window_days: int, asof: str | None = None
) -> float:
    """Скорость ПРОДАЖ (выкупов) в день — аналог order_velocity, но по sales."""
    return velocity(sales_count(conn, shop_id, nm_id, tech_size, window_days, asof), window_days)


def stock_days_aggregate(
    conn: sqlite3.Connection, shop_id: int, nm_id: int, tech_size: str, window_days: int = 7, basis: str = "orders"
) -> str:
    """Остаток по размеру, СУММАРНО по всем складам, делённый на скорость —
    для общего блока аналитики карточки в уведомлении (не путать с "Остатки подробно",
    где разбивка по каждому складу отдельно — см. stock_days_by_warehouse).

    `basis` — от чего считать, на сколько дней хватит остатка (решение пользователя):
    в уведомлении «Заказ»/«Отмена» — по заказам, в «Выкуп»/«Возврат» — по продажам.

    Скорость здесь намеренно считается "на сейчас" (без asof), даже если сама
    функция вызвана при рендере старого/бэкфилленного уведомления: stocks_current —
    это ТЕКУЩИЙ снимок остатков (истории по датам не храним), так что делить его на
    историческую скорость "по состоянию на дату события" было бы менее осмысленно,
    чем "на текущий остаток — текущая скорость". Это осознанный выбор, не пропущенный
    asof (см. buyout_rate_with_returns/order_velocity/sales_count, где asof, наоборот,
    обязателен)."""
    chrt_ids = _chrt_ids_for_size(conn, shop_id, nm_id, tech_size)
    if not chrt_ids:
        qty = 0
    else:
        placeholders = ",".join("?" * len(chrt_ids))
        qty = conn.execute(
            f"SELECT COALESCE(SUM(quantity), 0) FROM stocks_current WHERE shop_id=? AND nm_id=? AND chrt_id IN ({placeholders})",
            (shop_id, nm_id, *chrt_ids),
        ).fetchone()[0]
    v = (
        sales_velocity(conn, shop_id, nm_id, tech_size, window_days)
        if basis == "sales"
        else order_velocity(conn, shop_id, nm_id, tech_size, window_days)
    )
    return stock_eta(qty, v)


def stock_days_by_warehouse(
    conn: sqlite3.Connection, shop_id: int, nm_id: int, tech_size: str, windows: tuple[int, ...] = (7, 30, 60)
) -> list[dict]:
    """Остаток и "≈ N дн." ПО КАЖДОМУ СКЛАДУ ОТДЕЛЬНО, для 7/30/60-дневных окон
    (подтверждено пользователем — не одно дефолтное окно). Скорость считается по
    заказам ИМЕННО С ЭТОГО склада (join stocks_current.warehouse_name <->
    orders.warehouse_name по строковому совпадению — /orders не отдаёт warehouseId,
    поэтому другого ключа для связки нет; если WB когда-нибудь разойдётся в написании
    названий складов между эндпоинтами, эту связку придётся пересмотреть).
    """
    chrt_ids = _chrt_ids_for_size(conn, shop_id, nm_id, tech_size)
    if not chrt_ids:
        return []
    placeholders = ",".join("?" * len(chrt_ids))
    warehouse_rows = conn.execute(
        f"""
        SELECT warehouse_name, SUM(quantity) AS qty
        FROM stocks_current
        WHERE shop_id=? AND nm_id=? AND chrt_id IN ({placeholders})
        GROUP BY warehouse_name
        """,
        (shop_id, nm_id, *chrt_ids),
    ).fetchall()

    result = []
    for row in warehouse_rows:
        warehouse_name = row["warehouse_name"]
        qty = row["qty"]
        eta_by_window = {}
        for window_days in windows:
            cutoff, upper = _window_bounds(window_days, None)
            count = conn.execute(
                """
                SELECT COUNT(*) FROM orders
                WHERE shop_id=? AND nm_id=? AND tech_size=? AND warehouse_name=? AND date >= ? AND date < ?
                """,
                (shop_id, nm_id, tech_size, warehouse_name, cutoff, upper),
            ).fetchone()[0]
            eta_by_window[window_days] = stock_eta(qty, velocity(count, window_days))
        result.append({"warehouse_name": warehouse_name, "quantity": qty, "eta_by_window": eta_by_window})
    return result
