"""Скидка/штраф за скорость отгрузки FBS.

Пороги ДЕЙСТВУЮТ ДО 31 ЯНВАРЯ 2027 (ЛК → Меры поддержки → FBS, «Снизили
комиссию за быструю отгрузку», введено 7 августа 2026; WB предупреждает, что
пороговые значения могут меняться — при изменении править только таблицы ниже):

    до 13 часов   -> комиссия −5 п.п.
    13–42 часа    -> комиссия −3,5 п.п.
    42–48 часов   -> базовая комиссия
    48–54 часа    -> +0,30 п.п. за каждый час, начиная с 48-го
    54–60 часов   -> +0,35 п.п. за каждый час, начиная с 48-го
    свыше 60 ч    -> +0,45 п.п. за каждый час, начиная с 48-го

С 31 августа 2026 надбавка за просрочку взимается не как повышенная комиссия,
а как ОТДЕЛЬНЫЙ ШТРАФ в рублях (сумма та же, меняется только формат начисления):

    Штраф = Рц × (То × Ко),  где То — часы просрочки (сверх 48), Ко — п.п./час.

Проверка формулы на примере WB (со старыми порогами, механика та же):
товар 500₽, просрочка 2 ч, Ко=0,3 -> 2 × 0,3 = 0,6% -> 500 × 0,6% = 3₽.

Часы считаются от момента заказа до момента передачи в доставку (Тф) —
источник Тф см. `wb_api/marketplace.py` (`supplies[].closedAt`).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

# (верхняя граница часов включительно | None = без предела, скидка в п.п.)
DISCOUNT_TIERS = [
    (13.0, 5.0),
    (42.0, 3.5),
    (48.0, 0.0),
]

# Порог, с которого начинается просрочка, и ставки штрафа по брекетам:
# (верхняя граница часов | None = без предела, п.п. за каждый час просрочки)
PENALTY_THRESHOLD_HOURS = 48.0
PENALTY_TIERS = [
    (54.0, 0.30),
    (60.0, 0.35),
    (None, 0.45),
]


@dataclass(frozen=True)
class ShippingAdjustment:
    """discount_pp — насколько уменьшается комиссия (п.п.);
    penalty_rate_pp_per_hour / overdue_hours / penalty_amount — штраф за просрочку."""

    hours: float
    discount_pp: float
    overdue_hours: float
    penalty_rate_pp_per_hour: float
    penalty_amount: float


def shipping_adjustment(hours: float | None, retail_price: float | None) -> ShippingAdjustment | None:
    """None — если время отгрузки неизвестно (нет закрытой поставки): тогда в
    уведомлении показывается базовая комиссия без скидки/штрафа, а не выдуманная."""
    if hours is None or hours < 0:
        return None

    discount_pp = 0.0
    for max_hours, pp in DISCOUNT_TIERS:
        if hours <= max_hours:
            discount_pp = pp
            break

    overdue_hours = max(0.0, hours - PENALTY_THRESHOLD_HOURS)
    rate = 0.0
    if overdue_hours > 0:
        for max_hours, tier_rate in PENALTY_TIERS:
            if max_hours is None or hours <= max_hours:
                rate = tier_rate
                break

    penalty_amount = 0.0
    if overdue_hours > 0 and retail_price:
        penalty_amount = retail_price * (overdue_hours * rate) / 100

    return ShippingAdjustment(
        hours=hours,
        discount_pp=discount_pp,
        overdue_hours=overdue_hours,
        penalty_rate_pp_per_hour=rate,
        penalty_amount=penalty_amount,
    )


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def shipping_hours_for_order(conn: sqlite3.Connection, shop_id: int, srid: str | None) -> float | None:
    """Часы от заказа до передачи в доставку по данным Marketplace API.

    Связка: fbs_assembly_tasks.rid == orders.srid (проверено вживую). Обе метки
    времени берём из Marketplace API (обе в UTC) — сравнивать `createdAt` задания
    с `closedAt` поставки корректно, а вот смешивать с `orders.date` (МСК) нельзя.
    None — если задания нет (не FBS/вне окна) или поставка ещё не закрыта.
    """
    if not srid:
        return None
    row = conn.execute(
        """
        SELECT t.created_at, s.closed_at
        FROM fbs_assembly_tasks t
        LEFT JOIN fbs_supplies s ON s.shop_id = t.shop_id AND s.supply_id = t.supply_id
        WHERE t.shop_id = ? AND t.rid = ?
        """,
        (shop_id, srid),
    ).fetchone()
    if row is None:
        return None

    ordered_at = _parse_utc(row["created_at"])
    shipped_at = _parse_utc(row["closed_at"])
    if ordered_at is None or shipped_at is None:
        return None
    return (shipped_at - ordered_at).total_seconds() / 3600
