"""持仓状态机 position_manager 单测（§5.2 / §5.9）。

覆盖：T+1 锁定、可卖日推进（跨周末非自然日 +1）、连板续持先验衔接（SIGNAL_DRIVEN）、
次日没涨停转纯技术退出（TECH_EXIT）、同一 traded_id 幂等去重、FIFO 成本加权合并。

全部用 fake/内存实现：交易日历用 conftest 的 StaticTradeCalendar 夹具，时钟用 FakeClock，
日志用 RecordingLogger，成交回报用 contracts.xt_objects.FakeXtTrade，不连真实 xttrader/MySQL。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.contracts.enums import PositionMode, PositionState
from qmt_strategy.contracts.models import SignalPrior
from qmt_strategy.contracts.xt_objects import FakeXtTrade
from qmt_strategy.position.position_manager import PositionManager

# 领域基准日期（与 conftest / doc 示例一致）：
#   买入日 B = 2026-06-12(周五)；可卖日 = next_open(B) = 2026-06-15(周一，跨周末)。
T_BUY = date(2026, 6, 12)
T_SELL = date(2026, 6, 15)
ACCOUNT = "acc1"
TS = "600036.SH"


def _clock() -> FakeClock:
    """固定一个 UTC naive 时钟即可（本模块只做 date 级推进，不依赖具体钟点）。"""
    from datetime import datetime

    return FakeClock(datetime(2026, 6, 12, 1, 0, 0))


def _fill(traded_id: str = "T1", price: str = "11.00", volume: int = 1000) -> FakeXtTrade:
    """构造一笔买入成交回报（XtTrade 形态）。"""
    return FakeXtTrade(
        account_id=ACCOUNT,
        stock_code=TS,
        traded_id=traded_id,
        traded_price=float(price),
        traded_volume=volume,
    )


def _make_pm(calendar, prior_provider=None) -> PositionManager:
    """组装 PositionManager（注入日历夹具 + 固定时钟 + 记录型日志 + 可选先验回调）。"""
    if prior_provider is None:
        return PositionManager(calendar, _clock(), RecordingLogger())
    return PositionManager(calendar, _clock(), RecordingLogger(), prior_provider)


# ---------------------------------------------------------------------------
# 用例 1：T+1 锁定——买入当日 B 状态 LOCKED_T1，sellable_units 不含该单元
# ---------------------------------------------------------------------------
def test_t1_locked_on_buy_day(calendar):
    pm = _make_pm(calendar)
    unit = pm.mark_position_on_fill(_fill(), T_BUY, account_id=ACCOUNT)

    # 买入当日守 T+1：状态 LOCKED_T1，最早可卖日为下一交易日（跨周末到 2026-06-15）。
    assert unit.state == PositionState.LOCKED_T1
    assert unit.earliest_sellable_date == T_SELL
    assert unit.buy_date == T_BUY
    # 当日买入部分不进可用量（守 T+1 量闸）。
    assert unit.can_use_volume == 0
    # 买入当日 sellable_units 不含该单元（卖出只对昨日及更早持仓生效）。
    assert pm.sellable_units(T_BUY) == []


# ---------------------------------------------------------------------------
# 用例 2：可卖日推进——次日 refresh_state 后 LOCKED_T1 → HOLDING；
#          earliest_sellable_date 跨周末为 2026-06-15（非自然日 +1）
# ---------------------------------------------------------------------------
def test_sellable_date_advance_skips_weekend(calendar):
    pm = _make_pm(calendar)
    unit = pm.mark_position_on_fill(_fill(), T_BUY, account_id=ACCOUNT)

    # 验证最早可卖日经交易日历跨过周末（6/13 周六、6/14 周日），非自然日 +1（否则会是 6/13）。
    assert unit.earliest_sellable_date == T_SELL
    assert unit.earliest_sellable_date != date(2026, 6, 13)

    # 可卖日 refresh_state：跨过买入日 → HOLDING，昨日持仓计入可用量。
    pm.refresh_state(T_SELL)
    advanced = pm.get_unit(ACCOUNT, TS)
    assert advanced.state == PositionState.HOLDING
    assert advanced.can_use_volume == 1000
    # 可卖日 sellable_units 含该单元。
    units = pm.sellable_units(T_SELL)
    assert len(units) == 1 and units[0].ts_code == TS


# ---------------------------------------------------------------------------
# 用例 3：连板续持——prior_provider 返回 SignalPrior（次日又涨停）→ mode=SIGNAL_DRIVEN
# ---------------------------------------------------------------------------
def test_continuation_prior_signal_driven(calendar):
    def prior_provider(ts_code: str, today: date) -> Optional[SignalPrior]:
        # 次日又涨停：命中当日 limit_up_selected_stock，返回先验视图。
        if ts_code == TS and today == T_SELL:
            return SignalPrior(
                ts_code=TS,
                trade_date=date(2026, 6, 14),
                target_trade_date=T_SELL,
                continuation_prob=Decimal("0.7"),
            )
        return None

    pm = _make_pm(calendar, prior_provider)
    pm.mark_position_on_fill(_fill(), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)

    unit = pm.get_unit(ACCOUNT, TS)
    # 次日又涨停 → 吃信号侧先验续持。
    assert unit.mode == PositionMode.SIGNAL_DRIVEN
    assert unit.state == PositionState.HOLDING


# ---------------------------------------------------------------------------
# 用例 4：次日没涨停转纯技术退出——prior_provider 返回 None → mode=TECH_EXIT
# ---------------------------------------------------------------------------
def test_no_prior_tech_exit(calendar):
    # 默认 prior_provider 恒返回 None（等价信号池无该股）。
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)

    unit = pm.get_unit(ACCOUNT, TS)
    # 次日没涨停 / 退出信号池 → 纯技术退出。
    assert unit.mode == PositionMode.TECH_EXIT
    assert unit.state == PositionState.HOLDING


# ---------------------------------------------------------------------------
# 用例 5：幂等——同一 traded_id 两次 mark_position_on_fill，volume 不翻倍
# ---------------------------------------------------------------------------
def test_idempotent_same_traded_id(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(traded_id="T1", volume=1000), T_BUY, account_id=ACCOUNT)
    # 同一 traded_id 重复回报：不重复加仓。
    pm.mark_position_on_fill(_fill(traded_id="T1", volume=1000), T_BUY, account_id=ACCOUNT)

    unit = pm.get_unit(ACCOUNT, TS)
    assert unit.volume == 1000  # 未翻倍
    assert unit.counted_trade_ids == {"T1"}


# ---------------------------------------------------------------------------
# 用例 6：FIFO 成本——两笔不同价买入合并 avg_cost 加权正确
# ---------------------------------------------------------------------------
def test_fifo_weighted_avg_cost(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(traded_id="T1", price="10.00", volume=100), T_BUY, account_id=ACCOUNT)
    pm.mark_position_on_fill(_fill(traded_id="T2", price="13.00", volume=200), T_BUY, account_id=ACCOUNT)

    unit = pm.get_unit(ACCOUNT, TS)
    # (100*10 + 200*13) / 300 = 12.00
    assert unit.volume == 300
    assert unit.avg_cost == Decimal("12.00")
    # 两笔均计入去重集合。
    assert unit.counted_trade_ids == {"T1", "T2"}


# ---------------------------------------------------------------------------
# 补充：卖出成交回写——全成 SOLD、部成 PART_SOLD（覆盖 mark_selling / apply_sell_fill）
# ---------------------------------------------------------------------------
def test_apply_sell_fill_full_and_part(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(volume=1000), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)
    unit = pm.get_unit(ACCOUNT, TS)

    # 发卖出委托：进入 SELLING。
    pm.mark_selling(unit)
    assert unit.state == PositionState.SELLING

    # 部成 600：PART_SOLD，剩余 400。
    pm.apply_sell_fill(unit, 600)
    assert unit.state == PositionState.PART_SOLD
    assert unit.volume == 400

    # 剩余 400 全成：SOLD、单元归零关闭。
    pm.apply_sell_fill(unit, 400)
    assert unit.state == PositionState.SOLD
    assert unit.volume == 0
    # 已关闭单元不再进入可卖集合。
    assert pm.sellable_units(T_SELL) == []
