"""trade_calendar 单测（§5.2.2 禁自然日 ±1，跨周末/节假日）。"""

from __future__ import annotations

from datetime import date

import pytest

from qmt_strategy.common.trade_calendar import StaticTradeCalendar, WeekdayTradeCalendar


def test_static_next_open_crosses_weekend(calendar: StaticTradeCalendar):
    # 2026-06-12(周五) 的下一交易日应为 2026-06-15(周一)，而非 06-13(周六)
    assert calendar.next_open(date(2026, 6, 12)) == date(2026, 6, 15)


def test_static_prev_open(calendar: StaticTradeCalendar):
    assert calendar.prev_open(date(2026, 6, 15)) == date(2026, 6, 12)


def test_static_is_open(calendar: StaticTradeCalendar):
    assert calendar.is_open(date(2026, 6, 12)) is True
    assert calendar.is_open(date(2026, 6, 13)) is False  # 周六


def test_static_out_of_range_raises():
    cal = StaticTradeCalendar([date(2026, 6, 12)])
    with pytest.raises(ValueError):
        cal.next_open(date(2026, 6, 12))


def test_weekday_calendar_skips_weekend():
    cal = WeekdayTradeCalendar()
    assert cal.next_open(date(2026, 6, 12)) == date(2026, 6, 15)
    assert cal.is_open(date(2026, 6, 13)) is False
