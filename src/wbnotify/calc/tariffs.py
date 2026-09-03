"""Подстановка комиссии/логистики из tariffs_cache в уведомления.

tariffs_cache — БЕЗ shop_id (см. db.py): комиссии по категориям и логистика по
складам — публичные тарифы WB, одинаковые для всех продавцов, не привязаны к
конкретному магазину.
"""
from __future__ import annotations

import json
import sqlite3

FBW_WAREHOUSE_TYPE = "Склад WB"  # WB: warehouseType заказа == это -> модель "поставщик" (FBW)


def is_fbw(warehouse_type: str | None) -> bool:
    return warehouse_type == FBW_WAREHOUSE_TYPE


def _parse_ru_number(value: str | None) -> float | None:
    """WB отдаёт тарифы строками с запятой как десятичным разделителем, и "-"
    как "не применимо для этого склада"."""
    if not value or value == "-":
        return None
    return float(value.replace(",", "."))


def get_commission_pct(conn: sqlite3.Connection, subject: str | None, warehouse_type: str | None) -> float | None:
    """ВАЖНО: `kgvpSupplier` — это НЕ комиссия FBW, несмотря на название поля.

    Проверено пользователем вживую сверкой с реальным кабинетом WB Partners
    (Тарифы → Комиссия, категория "Ботинки"): "Склад WB (FBW)" = 37.5% в
    кабинете — это ровно значение `paidStorageKgvp` в API, а не `kgvpSupplier`
    (который в кабинете оказался равен графе "Витрина (DBS)/Курьер WB (DBW)" =
    45%, к нашей модели вообще не относится). "Маркетплейс (FBS)" в кабинете
    (42%) действительно совпадает с `kgvpMarketplace` — эта половина была
    верна и раньше. См. CLAUDE.md.
    """
    if not subject:
        return None
    row = conn.execute(
        "SELECT value_json FROM tariffs_cache WHERE kind = 'commission' AND subject_or_category = ?", (subject,)
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row["value_json"])
    return data["paidStorageKgvp"] if is_fbw(warehouse_type) else data["kgvpMarketplace"]


# Решение пользователя: для складов, которых нет в /tariffs/box (а там нет ни
# одного домашнего фулфилмент-центра вроде "Коледино"/"Тула" — только крупные
# логистические хабы + 2 "Свой склад..." записи), всегда брать конкретный эталон
# — "Свой склад РФ" (её товары малогабаритные, это её собственный выбор по
# сверке с кабинетом). У этого склада заполнены только Marketplace-поля —
# используем их независимо от FBW/FBS исходного заказа, раз пользователь
# указала явное значение (base=78,2 liter=23,8), а не выбор по схеме.
FALLBACK_WAREHOUSE_NAME = "Свой склад РФ"

# Обратная доставка (возврат от покупателя на склад WB) — официальная методика
# WB (справка ЛК "Виды логистики и расчёт стоимости", скриншоты пользователя):
#   * коэффициент склада и ИЛ в расчёте обратной доставки НЕ участвуют —
#     только базовый тариф, зависящий ТОЛЬКО от объёма товара;
#   * до 1 литра — фиксированная ставка по диапазону объёма (таблица ниже);
#   * от 1 литра — 46₽ за первый литр + 14₽ за каждый дополнительный литр
#     (формула одинакова для МГТ и СГТ).
# Это плоские платформенные константы, НЕ per-warehouse поля — поэтому в
# кабинете они одинаковы для всех складов, и поэтому же ранее найденные
# per-warehouse поля /api/v1/tariffs/return ("не принимает") к этому расчёту
# отношения не имеют.
RETURN_VOLUME_TIERS_UNDER_1L = [
    (0.200, 23.0),
    (0.400, 26.0),
    (0.600, 29.0),
    (0.800, 30.0),
    (1.000, 32.0),
]
RETURN_FIRST_LITER = 46.0
RETURN_EXTRA_LITER = 14.0


def get_return_logistics_sum(volume_l: float | None) -> float | None:
    """Стоимость обратной доставки от покупателя на склад WB по объёму товара.
    None — если объём неизвестен (нет габаритов в карточке)."""
    if volume_l is None or volume_l <= 0:
        return None
    if volume_l <= 1.0:
        for max_volume, price in RETURN_VOLUME_TIERS_UNDER_1L:
            if volume_l <= max_volume:
                return price
    return RETURN_FIRST_LITER + RETURN_EXTRA_LITER * (volume_l - 1.0)


def _box_row(conn: sqlite3.Connection, warehouse_name: str) -> dict | None:
    row = conn.execute(
        "SELECT value_json FROM tariffs_cache WHERE kind = 'box' AND warehouse_name = ?", (warehouse_name,)
    ).fetchone()
    return json.loads(row["value_json"]) if row else None


def get_box_tariff(conn: sqlite3.Connection, warehouse_name: str | None, warehouse_type: str | None) -> dict | None:
    """{'base': ₽/1л, 'liter': ₽/доп.л, 'coef': ИЛ-коэффициент (%)}.

    Точное совпадение по имени склада — используем FBW/FBS-поля как обычно.
    Если совпадения нет (типичный случай для домашних фулфилмент-центров) —
    эталонный фоллбек FALLBACK_WAREHOUSE_NAME, всегда его Marketplace-поля
    (решение пользователя, см. выше). None — только если эталона тоже нет
    в кэше (например, тарифы ещё ни разу не синкались)."""
    data = _box_row(conn, warehouse_name) if warehouse_name else None
    if data is not None:
        prefix = "" if is_fbw(warehouse_type) else "Marketplace"
        base = _parse_ru_number(data.get(f"boxDelivery{prefix}Base"))
        liter = _parse_ru_number(data.get(f"boxDelivery{prefix}Liter"))
        coef = _parse_ru_number(data.get(f"boxDelivery{prefix}CoefExpr"))
        if base is not None or liter is not None or coef is not None:
            return {"base": base, "liter": liter, "coef": coef}

    fallback = _box_row(conn, FALLBACK_WAREHOUSE_NAME)
    if fallback is None:
        return None
    return {
        "base": _parse_ru_number(fallback.get("boxDeliveryMarketplaceBase")),
        "liter": _parse_ru_number(fallback.get("boxDeliveryMarketplaceLiter")),
        "coef": _parse_ru_number(fallback.get("boxDeliveryMarketplaceCoefExpr")),
    }
