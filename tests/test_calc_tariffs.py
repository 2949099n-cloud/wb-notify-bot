"""Тесты расчёта обратной доставки (возврат от покупателя на склад WB).

Ожидаемые значения взяты ДОСЛОВНО из примеров официальной справки WB
(ЛК → "Виды логистики и расчёт стоимости", скриншоты пользователя) — это не
наши предположения, а числа самой WB.
"""
from __future__ import annotations

import pytest

from wbnotify.calc.tariffs import get_return_logistics_sum


@pytest.mark.parametrize(
    "volume_l, expected",
    [
        (0.150, 23.0),  # диапазон 0,001–0,200
        (0.200, 23.0),  # верхняя граница диапазона включительно
        (0.300, 26.0),  # 0,201–0,400
        (0.450, 29.0),  # 0,401–0,600 — пример WB: 0,450 и 0,550 стоят одинаково
        (0.550, 29.0),  # тот же пример WB, вторая половина
        (0.700, 30.0),  # 0,601–0,800
        (0.900, 32.0),  # 0,801–1,000
        (1.000, 32.0),  # ровно 1 литр — ещё по таблице, не по формуле
    ],
)
def test_return_logistics_under_one_liter_uses_volume_tier_table(volume_l, expected):
    assert get_return_logistics_sum(volume_l) == expected


def test_return_logistics_over_one_liter_matches_wb_example_1_8l():
    """Пример WB: объём 1,8 л -> 46₽ + 0,8 × 14₽ = 11,2₽ -> итого 57,2₽."""
    assert get_return_logistics_sum(1.8) == pytest.approx(57.2)


def test_return_logistics_over_one_liter_matches_wb_example_65l():
    """Пример WB: объём 65 л -> 46₽ + 64 × 14₽ = 896₽ -> итого 942₽."""
    assert get_return_logistics_sum(65) == pytest.approx(942.0)


def test_return_logistics_unknown_volume_returns_none():
    """Нет габаритов в карточке -> None (в уведомлении отрисуется прочерк),
    а не выдуманное число."""
    assert get_return_logistics_sum(None) is None
    assert get_return_logistics_sum(0) is None
