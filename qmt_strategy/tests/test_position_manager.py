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
from qmt_strategy.common.trade_calendar import StaticTradeCalendar
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
    # 评审三轮 EXEC-position-01：get_unit 返回只读深拷贝，写入走原子方法（apply_sell_fill_by_trade）；
    # 发卖单用 sellable_units 的 live 引用（卖出 pass 口径）。
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(volume=1000), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)

    # 发卖出委托：进入 SELLING（live 引用）。
    live = pm.sellable_units(T_SELL)[0]
    pm.mark_selling(live)
    assert pm.get_unit(ACCOUNT, TS).state == PositionState.SELLING

    # 部成 600（原子方法，按 traded_id 去重+扣减）：PART_SOLD，剩余 400。
    pm.apply_sell_fill_by_trade(ACCOUNT, TS, "S1", 600)
    u = pm.get_unit(ACCOUNT, TS)
    assert u.state == PositionState.PART_SOLD
    assert u.volume == 400

    # 剩余 400 全成：SOLD、单元归零关闭。
    pm.apply_sell_fill_by_trade(ACCOUNT, TS, "S2", 400)
    u = pm.get_unit(ACCOUNT, TS)
    assert u.state == PositionState.SOLD
    assert u.volume == 0
    # 已关闭单元不再进入可卖集合。
    assert pm.sellable_units(T_SELL) == []


# ===========================================================================
# 评审三轮 批次3：字段级并发安全 / 在途量冻结 / 量权威 / 卡死 SELLING 对账
# ===========================================================================
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _pos_rec(ts_code=TS, volume=1000, can_use=1000, avg=Decimal("10.0"), account_id=ACCOUNT):
    """构造 rebuild_from_broker_positions 的券商持仓记录（PositionRecord 等价对象）。"""
    return SimpleNamespace(ts_code=ts_code, volume=volume, can_use_volume=can_use,
                           avg_price=avg, account_id=account_id)


# —— EXEC-position-01：get_unit 只读快照 + 原子方法去重/无丢更新 ——
def test_get_unit_returns_readonly_snapshot(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(volume=1000), T_BUY, account_id=ACCOUNT)
    snap = pm.get_unit(ACCOUNT, TS)
    snap.volume = 999  # 改快照不应影响内部 live 单元
    assert pm.get_unit(ACCOUNT, TS).volume == 1000


def test_apply_sell_fill_by_trade_idempotent(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(volume=1000), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)
    pm.apply_sell_fill_by_trade(ACCOUNT, TS, "SX", 300)
    pm.apply_sell_fill_by_trade(ACCOUNT, TS, "SX", 300)  # 同 traded_id 重投：只扣一次
    assert pm.get_unit(ACCOUNT, TS).volume == 700


def test_concurrent_sell_fill_no_lost_update(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(volume=10000), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)
    n = 20

    def worker(i):
        pm.apply_sell_fill_by_trade(ACCOUNT, TS, f"S{i}", 100)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pm.get_unit(ACCOUNT, TS).volume == 10000 - 100 * n  # 无丢更新


# —— EXEC-position-03：在途未成卖量冻结 ——
def test_mark_selling_freezes_on_road_and_sellable_remaining(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(volume=1000), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)
    live = pm.sellable_units(T_SELL)[0]
    pm.mark_selling(live, sell_volume=600)
    assert pm.sellable_remaining(live) == 400        # 1000 - 在途 600
    # 部成 300 → PART_SOLD，可卖上限 = (1000-300) - (600-300) = 400（不含在途未成 300）。
    pm.apply_sell_fill_by_trade(ACCOUNT, TS, "S1", 300)
    u = pm.sellable_units(T_SELL)[0]
    assert u.state == PositionState.PART_SOLD
    assert pm.sellable_remaining(u) == 400


def test_revert_selling_clears_on_road(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(volume=1000), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)
    live = pm.sellable_units(T_SELL)[0]
    pm.mark_selling(live, sell_volume=600)
    pm.revert_selling_by_code(ACCOUNT, TS, reason="terminal_fail")
    u = pm.get_unit(ACCOUNT, TS)
    assert u.state == PositionState.HOLDING
    assert u.on_road_sell_volume == 0                # 终态失败清零在途


# —— EXEC-position-07：量权威迟到回调不二次累加 ——
def test_rebuild_then_late_buy_callback_no_double_count(calendar):
    pm = _make_pm(calendar)
    pm.rebuild_from_broker_positions([_pos_rec(volume=1000, can_use=1000)], T_SELL)
    assert pm.get_unit(ACCOUNT, TS).volume_authoritative is True
    # 迟到买入回调（已被权威量计入）：新 traded_id，但权威量不再二次累加。
    pm.mark_position_on_fill(_fill(traded_id="LATE", volume=1000), T_SELL, account_id=ACCOUNT)
    u = pm.get_unit(ACCOUNT, TS)
    assert u.volume == 1000                          # 不二次累加
    assert "LATE" in u.counted_trade_ids             # 但登记去重


def test_normal_buy_still_accumulates(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(traded_id="A", volume=1000), T_BUY, account_id=ACCOUNT)
    pm.mark_position_on_fill(_fill(traded_id="B", volume=500), T_BUY, account_id=ACCOUNT)
    assert pm.get_unit(ACCOUNT, TS).volume == 1500   # 非权威单元正常加仓


# —— EXEC-position-04：盘前对账跨日卡死 SELLING ——
def test_reconcile_stuck_selling_reverts_on_cancelled(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(volume=1000), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)
    pm.mark_selling(pm.sellable_units(T_SELL)[0], sell_volume=1000)
    n = pm.reconcile_stuck_selling(T_SELL, lambda a, c: "CANCELLED")
    assert n == 1
    assert pm.get_unit(ACCOUNT, TS).state == PositionState.HOLDING  # 零成交终态复位重挂


def test_reconcile_stuck_selling_keeps_on_unknown(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(volume=1000), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)
    pm.mark_selling(pm.sellable_units(T_SELL)[0], sell_volume=1000)
    n = pm.reconcile_stuck_selling(T_SELL, lambda a, c: None)  # 查不到 → 保守不动
    assert n == 0
    assert pm.get_unit(ACCOUNT, TS).state == PositionState.SELLING


def test_reconcile_stuck_selling_keeps_on_filled(calendar):
    pm = _make_pm(calendar)
    pm.mark_position_on_fill(_fill(volume=1000), T_BUY, account_id=ACCOUNT)
    pm.refresh_state(T_SELL)
    pm.mark_selling(pm.sellable_units(T_SELL)[0], sell_volume=1000)
    n = pm.reconcile_stuck_selling(T_SELL, lambda a, c: "FILLED")  # 已成 → 交回报，不复位
    assert n == 0
    assert pm.get_unit(ACCOUNT, TS).state == PositionState.SELLING


# ---------------------------------------------------------------------------
# 评审 doc/21 C1：交易日历末日越界 fail-closed（不丢仓、不崩溃、单条不拖垮整批）
# ---------------------------------------------------------------------------
def test_calendar_exhausted_buy_writeback_keeps_position():
    """日历末日==今日时 next_open 越界 → _safe_next_open fail-closed 占位，已成交持仓仍进状态机(不丢仓裸奔)。"""
    cal = StaticTradeCalendar([date(2026, 6, 11), T_BUY])  # 末日=T_BUY(周五) → next_open(T_BUY) 越界
    pm = _make_pm(cal)
    unit = pm.mark_position_on_fill(_fill(volume=1000), T_BUY, account_id=ACCOUNT)
    assert unit is not None and unit.volume == 1000
    assert unit.state == PositionState.LOCKED_T1                 # 当日买入仍守 T+1，未被丢出状态机
    assert unit.earliest_sellable_date == date(2026, 6, 15)     # 退化为下一非周末自然日(周一)，非抛异常
    assert "position_calendar_next_open_exhausted_fallback" in pm._logger.events()


def test_rebuild_calendar_exhausted_builds_units_and_isolates_failure():
    """rebuild 在日历越界时仍逐条建单元(占位)，且单条坏记录不拖垮整批(per-item 兜底)。"""
    cal = StaticTradeCalendar([date(2026, 6, 11), T_BUY])
    pm = _make_pm(cal)
    recs = [
        _pos_rec(ts_code="600000.SH", volume=500, can_use=0),    # 当日锁定 → 触发 _safe_next_open 占位
        _pos_rec(ts_code="600036.SH", volume=300, can_use=300),  # 隔夜可卖 → 触发 _safe_prev_open
    ]
    touched = pm.rebuild_from_broker_positions(recs, T_BUY)
    assert touched == 2                                          # 两条都重建成功，无 ValueError 中断整批
    u1 = pm.get_unit(ACCOUNT, "600000.SH")
    assert u1 is not None and u1.state == PositionState.LOCKED_T1
    assert u1.earliest_sellable_date == date(2026, 6, 15)       # next_open 占位
    u2 = pm.get_unit(ACCOUNT, "600036.SH")
    assert u2 is not None and u2.state == PositionState.HOLDING
