"""Тесты порогов скидки/штрафа за скорость отгрузки FBS.

Пороги — из ЛК пользователя (действуют до 31.01.2027), пример расчёта штрафа —
из официальной новости WB. См. calc/fbs_shipping.py.
"""
from __future__ import annotations

import pytest

from wbnotify.calc.fbs_shipping import PENALTY_THRESHOLD_HOURS, shipping_adjustment


@pytest.mark.parametrize(
    "hours, expected_discount",
    [
        (0.5, 5.0),    # мгновенная отгрузка
        (9.76, 5.0),   # реальный заказ из живого теста: 9.76 ч
        (13.0, 5.0),   # ровно 13 ч — ещё максимальная скидка
        (13.1, 3.5),   # чуть больше 13 ч — уже пониженная
        (42.0, 3.5),   # ровно 42 ч — ещё пониженная
        (42.5, 0.0),   # 42–48 ч — базовая комиссия
        (48.0, 0.0),   # ровно 48 ч — ещё базовая, просрочки нет
        (60.0, 0.0),   # просрочка — скидки уже нет
    ],
)
def test_discount_tiers(hours, expected_discount):
    adj = shipping_adjustment(hours, retail_price=1000)
    assert adj.discount_pp == expected_discount


@pytest.mark.parametrize(
    "hours, expected_rate",
    [
        (48.0, 0.0),    # ровно порог — просрочки ещё нет
        (50.0, 0.30),   # 48–54
        (54.0, 0.30),   # верхняя граница брекета включительно
        (57.0, 0.35),   # 54–60
        (60.0, 0.35),
        (75.0, 0.45),   # свыше 60
    ],
)
def test_penalty_rate_brackets(hours, expected_rate):
    adj = shipping_adjustment(hours, retail_price=1000)
    assert adj.penalty_rate_pp_per_hour == expected_rate


def test_no_penalty_before_threshold():
    adj = shipping_adjustment(PENALTY_THRESHOLD_HOURS - 0.1, retail_price=10000)
    assert adj.overdue_hours == 0
    assert adj.penalty_amount == 0


def test_penalty_amount_matches_wb_formula_example():
    """Механика из примера WB: просрочка 2 ч при Ко=0,3 -> 0,6% от цены.
    Товар 500₽ -> 3₽. С новыми порогами те же 2 ч просрочки = 50 ч от заказа."""
    adj = shipping_adjustment(PENALTY_THRESHOLD_HOURS + 2, retail_price=500)
    assert adj.overdue_hours == pytest.approx(2.0)
    assert adj.penalty_rate_pp_per_hour == 0.30
    assert adj.penalty_amount == pytest.approx(3.0)


def test_penalty_uses_higher_rate_in_higher_bracket():
    """75 ч = 27 ч просрочки по ставке 0,45 п.п./ч = 12,15% от цены."""
    adj = shipping_adjustment(75, retail_price=10000)
    assert adj.overdue_hours == pytest.approx(27.0)
    assert adj.penalty_amount == pytest.approx(10000 * 27 * 0.45 / 100)


def test_unknown_shipping_time_returns_none():
    """Нет закрытой поставки -> не выдумываем скидку/штраф, показываем базовую комиссию."""
    assert shipping_adjustment(None, retail_price=1000) is None
