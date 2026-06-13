"""pytest 共享夹具与构造助手。

提供：交易日历（覆盖 2026-06 工作日，含跨周末场景）、固定时钟、信号行/计划行/盘口构造器，
供各模块单测复用，避免每个测试重复样板。所有时间一律 UTC naive。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from qmt_strategy.common.time_utils import SHANGHAI, FakeClock
from qmt_strategy.common.trade_calendar import StaticTradeCalendar
from qmt_strategy.contracts.models import OrderBook, PlanRow, SelectedStockRow
from qmt_strategy.contracts.enums import Board

# 领域基准日期（与 doc 示例一致）：
#   T 信号日 = 2026-06-11(周四)；T+1 买入日 = 2026-06-12(周五)；
#   可卖日 = trade_cal_next(2026-06-12) = 2026-06-15(周一，跨周末)。
T_SIGNAL = date(2026, 6, 11)
T_BUY = date(2026, 6, 12)
T_SELL = date(2026, 6, 15)


def _june_2026_weekdays():
    """2026-06 全月工作日（近似交易日，含跨周末验证；不含法定节假日，测试足够）。"""
    days = []
    d = date(2026, 6, 1)
    end = date(2026, 7, 10)
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


@pytest.fixture
def calendar() -> StaticTradeCalendar:
    return StaticTradeCalendar(_june_2026_weekdays())


def utc_at_east8(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    """给定东八区日期与钟点，返回对应的 UTC naive 时刻（供时段判定测试）。"""
    from datetime import timezone

    east8 = datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=SHANGHAI)
    return east8.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def clock_at_buy_open() -> FakeClock:
    """固定在 T+1 买入日 09:16(东八区) 的时钟（竞价可撤段）。"""
    from datetime import timezone

    east8 = datetime(2026, 6, 12, 9, 16, 0, tzinfo=SHANGHAI)
    return FakeClock(east8.astimezone(timezone.utc).replace(tzinfo=None))


def make_selected_row(
    ts_code: str = "600000.SH",
    *,
    trade_date: date = T_SIGNAL,
    target_trade_date: date = T_BUY,
    signal_close: Decimal = Decimal("10.00"),
    limit_up_price=None,
    reasonable_open_high_low=None,
    reasonable_open_high_high=None,
    market_state: str = "启动",
    tradable_flag: bool = True,
    strategy: str = "打板",
    role: str = "龙头",
    continuation_prob: Decimal = Decimal("0.6"),
    leader_strength_score: Decimal = Decimal("0.8"),
    first_board_vol=None,
    float_mktcap=None,
    strategy_family=None,
    setup=None,
    fail_conditions=None,
) -> SelectedStockRow:
    """构造一行信号契约，默认值即「主板可交易强势票」。"""
    return SelectedStockRow(
        ts_code=ts_code,
        trade_date=trade_date,
        target_trade_date=target_trade_date,
        signal_close=signal_close,
        limit_up_price=limit_up_price,
        reasonable_open_high_low=reasonable_open_high_low,
        reasonable_open_high_high=reasonable_open_high_high,
        market_state=market_state,
        tradable_flag=tradable_flag,
        strategy=strategy,
        role=role,
        continuation_prob=continuation_prob,
        leader_strength_score=leader_strength_score,
        first_board_vol=first_board_vol,
        float_mktcap=float_mktcap,
        strategy_family=strategy_family,
        setup=setup,
        fail_conditions=fail_conditions,
    )


def make_plan_row(
    ts_code: str = "600000.SH",
    *,
    limit_up_price: Decimal = Decimal("11.00"),
    reasonable_open_low: Decimal = Decimal("10.20"),
    reasonable_open_high: Decimal = Decimal("10.80"),
    board: Board = Board.MAIN,
    first_board_vol=None,
    float_mktcap=None,
    strategy_family: str = "打板",
    setup: str = "连板接力",
    market_state: str = "启动",
    tradable_flag: bool = True,
    continuation_prob: Decimal = Decimal("0.6"),
    leader_strength_score: Decimal = Decimal("0.8"),
) -> PlanRow:
    return PlanRow(
        ts_code=ts_code,
        signal_trade_date=T_SIGNAL,
        target_trade_date=T_BUY,
        limit_up_price=limit_up_price,
        reasonable_open_low=reasonable_open_low,
        reasonable_open_high=reasonable_open_high,
        board=board,
        first_board_vol=first_board_vol,
        float_mktcap=float_mktcap,
        strategy_family=strategy_family,
        setup=setup,
        market_state=market_state,
        tradable_flag=tradable_flag,
        continuation_prob=continuation_prob,
        leader_strength_score=leader_strength_score,
    )


@pytest.fixture
def selected_row_factory():
    return make_selected_row


@pytest.fixture
def plan_row_factory():
    return make_plan_row
