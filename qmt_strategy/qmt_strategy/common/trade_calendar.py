"""交易日历实现（对齐信号侧 a_trade_calendar，§5.2.2 / §6.8）。

业务意图：earliest_sellable_date（B → trade_cal_next(B)）、「次日」、signal_trade_date 反推
（trade_date → pretrade_date）全部经此，禁止自然日 ±1（跨周末/节假日会错）。

落地：真实环境应注入「读 a_trade_calendar 物理表」的实现（DbTradeCalendar）。本仓库提供：
- StaticTradeCalendar：用显式交易日集合，单测 / 离线用，行为确定可断言；
- WeekdayTradeCalendar：仅排除周六周日的近似实现（不含法定节假日），仅供无日历数据时的兜底，
  生产严禁单独使用（会把节假日当交易日），仅作降级占位。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional, Set


class StaticTradeCalendar:
    """基于显式交易日集合的日历。实现 contracts.TradeCalendar 协议。"""

    def __init__(self, open_days: Iterable[date]):
        self._days: Set[date] = set(open_days)
        if not self._days:
            raise ValueError("StaticTradeCalendar 需要至少一个交易日")
        self._sorted = sorted(self._days)

    def is_open(self, d: date) -> bool:
        return d in self._days

    def next_open(self, d: date) -> date:
        """d 之后的下一交易日（严格大于 d）。超出已知范围抛 ValueError，提示补齐日历。"""
        for day in self._sorted:
            if day > d:
                return day
        raise ValueError(f"next_open: 已知交易日历无 {d} 之后的交易日，请补齐 a_trade_calendar")

    def prev_open(self, d: date) -> date:
        """d 之前的上一交易日（严格小于 d）。"""
        for day in reversed(self._sorted):
            if day < d:
                return day
        raise ValueError(f"prev_open: 已知交易日历无 {d} 之前的交易日，请补齐 a_trade_calendar")


class WeekdayTradeCalendar:
    """仅排除周末的近似日历（不含法定节假日）。仅作无日历数据时的降级兜底。

    警告：生产环境会把节假日误判为交易日，导致 earliest_sellable_date 偏早；
    须以 DbTradeCalendar / StaticTradeCalendar 为准，本类只用于离线占位。
    """

    def is_open(self, d: date) -> bool:
        return d.weekday() < 5  # 周一=0 ... 周五=4

    def next_open(self, d: date) -> date:
        nxt = d + timedelta(days=1)
        while not self.is_open(nxt):
            nxt += timedelta(days=1)
        return nxt

    def prev_open(self, d: date) -> date:
        prev = d - timedelta(days=1)
        while not self.is_open(prev):
            prev -= timedelta(days=1)
        return prev
