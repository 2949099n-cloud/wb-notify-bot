"""Экономика дня: комиссия, логистика и операционная прибыль по кабинету.

Считается ПО КАЖДОЙ продаже и возврату отдельно, а не «средней ставкой по
магазину»: комиссия зависит от категории товара и схемы (FBW/FBS), логистика —
от габаритов и склада. Формулы те же, что в уведомлениях (`calc/tariffs.py`,
`calc/fbs_shipping.py`) — иначе сводка расходилась бы с карточками, из которых
она складывается.

Ограничение, о котором надо помнить: это НАШ расчёт по справочным тарифам WB, а
не фактические удержания из финансового отчёта — тот публикуется с задержкой в
дни и в момент события недоступен (см. CLAUDE.md).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from wbnotify.calc.fbs_shipping import shipping_adjustment, shipping_hours_for_order
from wbnotify.calc.tariffs import get_box_tariff, get_commission_pct, get_return_logistics_sum, is_fbw


@dataclass(frozen=True)
class ItemEconomics:
    nm_id: int
    article: str
    revenue: float
    commission: float
    logistics: float

    @property
    def profit(self) -> float:
        return self.revenue - self.commission - self.logistics


@dataclass(frozen=True)
class DayEconomics:
    revenue: float
    commission: float
    logistics_direct: float
    logistics_return: float
    items: list[ItemEconomics]

    @property
    def logistics(self) -> float:
        return self.logistics_direct + self.logistics_return

    @property
    def profit(self) -> float:
        return self.revenue - self.commission - self.logistics

    @property
    def commission_pct(self) -> float | None:
        return self.commission / self.revenue * 100 if self.revenue else None


def _card(conn: sqlite3.Connection, shop_id: int, nm_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT volume_l FROM cards_cache WHERE shop_id = ? AND nm_id = ?", (shop_id, nm_id)
    ).fetchone()


def _direct_logistics(conn: sqlite3.Connection, shop_id: int, sale: sqlite3.Row) -> float:
    """Прямая логистика — та же оценка, что в уведомлении: (база + доп. литры ×
    ставка) × коэффициент склада."""
    box = get_box_tariff(conn, sale["warehouse_name"], sale["warehouse_type"])
    if box is None or box.get("base") is None:
        return 0.0
    card = _card(conn, shop_id, sale["nm_id"])
    volume_l = card["volume_l"] if card else None
    extra_liters = max(0.0, (volume_l or 1.0) - 1.0)
    total = box["base"] + extra_liters * (box.get("liter") or 0)
    coef = box.get("coef")
    return total * (coef / 100) if coef is not None else total


def _commission(conn: sqlite3.Connection, shop_id: int, sale: sqlite3.Row) -> float:
    pct = get_commission_pct(conn, sale["subject"], sale["warehouse_type"])
    if pct is None:
        return 0.0
    price = sale["price_with_disc"] or 0.0
    # Скидка за скорость отгрузки — только FBS и только по факту отгрузки; в
    # сводке речь про уже состоявшиеся продажи, поэтому применяем, как в
    # уведомлении «Продажа».
    if not is_fbw(sale["warehouse_type"]):
        adjustment = shipping_adjustment(shipping_hours_for_order(conn, shop_id, sale["srid"]), price)
        if adjustment is not None:
            pct = max(0.0, pct - adjustment.discount_pp)
    return price * pct / 100


def for_day(conn: sqlite3.Connection, shop_id: int, date_str: str) -> DayEconomics:
    day_start, day_end = date_str, date_str + "T23:59:59.999999"

    sales = conn.execute(
        """
        SELECT s.nm_id, s.srid, s.subject, s.warehouse_type, s.warehouse_name,
               s.price_with_disc, COALESCE(s.supplier_article, '') AS article
        FROM sales s
        WHERE s.shop_id = ? AND s.is_return = 0 AND s.date >= ? AND s.date < ?
        """,
        (shop_id, day_start, day_end),
    ).fetchall()

    by_item: dict[int, dict] = {}
    revenue = commission = logistics_direct = 0.0
    for sale in sales:
        price = sale["price_with_disc"] or 0.0
        item_commission = _commission(conn, shop_id, sale)
        item_logistics = _direct_logistics(conn, shop_id, sale)

        revenue += price
        commission += item_commission
        logistics_direct += item_logistics

        bucket = by_item.setdefault(
            sale["nm_id"], {"article": sale["article"], "revenue": 0.0, "commission": 0.0, "logistics": 0.0}
        )
        bucket["revenue"] += price
        bucket["commission"] += item_commission
        bucket["logistics"] += item_logistics
        if not bucket["article"]:
            bucket["article"] = sale["article"]

    # Обратная логистика считается только от объёма товара — коэффициент склада
    # и ИЛ в ней не участвуют (официальная методика WB, см. CLAUDE.md).
    returns = conn.execute(
        "SELECT nm_id FROM sales WHERE shop_id = ? AND is_return = 1 AND date >= ? AND date < ?",
        (shop_id, day_start, day_end),
    ).fetchall()
    logistics_return = 0.0
    for row in returns:
        card = _card(conn, shop_id, row["nm_id"])
        logistics_return += get_return_logistics_sum(card["volume_l"] if card else None) or 0.0

    items = [
        ItemEconomics(nm_id, data["article"], data["revenue"], data["commission"], data["logistics"])
        for nm_id, data in by_item.items()
    ]
    return DayEconomics(revenue, commission, logistics_direct, logistics_return, items)
