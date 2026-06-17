"""2026-06 多智能体致命 bug 评审修复回归锁定测试（doc/17）。

每个用例锁定一条已修复行为，防回归；并验证新增「绝不买入 ST」三层硬规则。
全部用 fake/内存实现，不连真实 xttrader/xtdata/MySQL。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List, Optional

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.common.universe_filter import is_st_stock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import (
    EntryAction, OrderPhase, OrderState, OrderStatus, PositionMode, PositionState, TradeSide,
)
from qmt_strategy.contracts.models import EntryDecision, PlanRow, SelectedStockRow
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset, FakeXtTrade
from qmt_strategy.order.local_ledger import InMemoryLocalLedger
from qmt_strategy.order.order_executor import OrderExecutor
from qmt_strategy.position.position_manager import PositionManager
from tests.conftest import T_BUY, T_SELL, T_SIGNAL, make_plan_row, make_selected_row, utc_at_east8


# ===========================================================================
# 一、绝不买入 ST 硬规则（三层 + 可靠透传）
# ===========================================================================
def test_is_st_stock_unified_semantics():
    """统一口径：显式 is_st=True 或 name 含 ST/退 即判 ST；最严格取向。"""
    assert is_st_stock(True, "招商银行") is True          # 显式真
    assert is_st_stock(None, "ST华微") is True            # name 兜底
    assert is_st_stock(None, "*ST康得") is True
    assert is_st_stock(None, "退市整理") is True
    # name 含 ST 即便显式 False 也判 ST（name 是 point-in-time 实时事实，防信号侧漂移漏判）。
    assert is_st_stock(False, "ST华微") is True
    # 非 ST 名称 + 缺失/False → 非 ST。
    assert is_st_stock(None, "招商银行") is False
    assert is_st_stock(False, "招商银行") is False
    assert is_st_stock(None, None) is False


def test_is_st_round_trips_through_sqlite():
    """is_st 经 selected_to_row → row_to_selected 无损 round-trip（保三态 None/True/False，堵 F08）。"""
    from qmt_strategy.storage.mappers import row_to_selected, selected_to_row

    for flag in (True, False, None):
        r = make_selected_row(ts_code="600036.SH")
        r.is_st = flag
        back = row_to_selected(selected_to_row(r))
        assert back.is_st is flag, f"is_st round-trip 丢失: {flag}"


def test_watchlist_item_maps_is_st():
    """信号侧 item['is_st'] → SelectedStockRow.is_st（F08 透传）。"""
    from qmt_strategy.watchlist.remote_watchlist import watchlist_item_to_selected

    row = watchlist_item_to_selected(
        {"ts_code": "600036.SH", "trade_date": "2026-06-11", "is_st": True}, target_trade_date=T_BUY
    )
    assert row.is_st is True
    row2 = watchlist_item_to_selected({"ts_code": "600036.SH", "trade_date": "2026-06-11"}, target_trade_date=T_BUY)
    assert row2.is_st is None  # 未下发保留三态 None（回退 name 判定）


def _st_loader(rows):
    from qmt_strategy.watchlist.sources import CallableSelectedStockSource
    from qmt_strategy.watchlist.watchlist_loader import WatchlistLoader

    class _Cal:
        def is_open(self, d):
            return True
        def next_open(self, d):
            return d + timedelta(days=1)
        def prev_open(self, d):
            return d - timedelta(days=1)

    src = CallableSelectedStockSource(lambda d: rows, source_name="t")
    return WatchlistLoader(src, _Cal(), RecordingLogger(), Settings())


def test_st_excluded_from_tradable_layer1():
    """第 1 层：ST 票（显式或 name）被 loader 剔出可交易名单、转观察。"""
    rows = [
        make_selected_row(ts_code="600036.SH", market_state="启动"),  # 非 ST
        make_selected_row(ts_code="600037.SH", market_state="启动"),  # 显式 is_st
        make_selected_row(ts_code="600038.SH", market_state="启动"),  # name ST
    ]
    rows[1].is_st = True
    rows[2].name = "ST华微"
    ctx = _st_loader(rows).load(T_BUY)
    assert "600036.SH" in ctx.tradable
    assert "600037.SH" not in ctx.tradable   # 显式 ST 被剔除
    assert "600038.SH" not in ctx.tradable   # name ST 被剔除
    watch_codes = {e.norm_code for e in ctx.watch_only}
    assert {"600037.SH", "600038.SH"} <= watch_codes


def _router():
    from qmt_strategy.entry.entry_router import EntryRouter
    return EntryRouter(Settings(), FakeClock(utc_at_east8(T_BUY, 9, 16)), RecordingLogger())


def _auction_snap():
    from qmt_strategy.contracts.enums import AuctionPhase, CentroidTrend
    from qmt_strategy.contracts.models import AuctionSnapshot
    return AuctionSnapshot(
        ts_code="600036.SH", phase=AuctionPhase.SETTLED, ts=utc_at_east8(T_BUY, 9, 26),
        open_pct=Decimal("0.10"), is_limit_up=True, seal_to_float_ratio=Decimal("0.01"),
        last_price=Decimal("11.00"), pre_close=Decimal("10.00"), centroid_trend=CentroidTrend.UP,
    )


def test_st_router_skips_layer2():
    """第 2 层：ST 计划行经 entry_router → SKIP（不产 BUY 决策）。"""
    plan = make_plan_row(ts_code="600036.SH")
    plan.name = "ST华微"   # name ST
    dec = _router().route(plan, _auction_snap())
    assert dec.action == EntryAction.SKIP
    assert "ST" in dec.reason

    plan2 = make_plan_row(ts_code="600036.SH")
    plan2.is_st = True     # 显式 ST
    dec2 = _router().route(plan2, _auction_snap())
    assert dec2.action == EntryAction.SKIP


def test_st_decision_carries_flag_and_order_refuses_layer3():
    """第 3 层：唯一下单点 place 对 is_st 决策拒单（绝不发买单），且 _build_decision 已置 is_st。"""
    # _build_decision 把 ST 判定锚到决策（即便决策动作非 SKIP，下游也据此拒单）。
    plan = make_plan_row(ts_code="600036.SH")
    plan.name = "ST华微"
    dec = _router().route(plan, _auction_snap())
    assert dec.is_st is True   # 决策携带 ST 标志

    # 构造一个「绕过路由直接带 BUY + is_st=True」的决策喂给唯一下单点 → 必拒单。
    from tests.conftest import T_BUY as _tb
    buy_st = EntryDecision(
        ts_code="600036.SH", signal_trade_date=T_SIGNAL, target_trade_date=_tb,
        strategy_family="打板", setup="连板接力", action=EntryAction.CHASE_LIMIT_UP,
        decided_at=datetime(2026, 6, 12, 1, 16, 0), reason="forced", limit_price=Decimal("11.00"),
        plan_volume=1000, order_phase=OrderPhase.OPENING, is_st=True,
    )
    trader = _RecTrader()
    ex = OrderExecutor(trader, FakeStockAccount("acc1"), "acc1", InMemoryLocalLedger(),
                       Settings(), FakeClock(utc_at_east8(T_BUY, 9, 16)), RecordingLogger())
    assert ex.place(buy_st) is None
    assert trader.order_calls == []   # 绝不发买单


class _RecTrader:
    def __init__(self, cash="1000000", positions=None):
        self.order_calls = []
        self.cancel_calls = []
        self._cash = Decimal(str(cash))
        self._positions = positions or []
        self._next = 1001

    def order_stock(self, account, code, otype, vol, ptype, price, sname="", remark=""):
        self.order_calls.append((code, otype, vol, price))
        oid = self._next
        self._next += 1
        return oid

    def cancel_order_stock(self, account, order_id):
        self.cancel_calls.append(order_id)
        return 0

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc1", cash=self._cash, frozen_cash=Decimal("0"),
                           market_value=Decimal("0"), total_asset=self._cash)

    def query_stock_positions(self, account):
        return self._positions


# ===========================================================================
# 二、F02/F16：PART_SOLD 在途卖量泄漏
# ===========================================================================
def _pm(calendar):
    return PositionManager(calendar, FakeClock(datetime(2026, 6, 12, 1, 0, 0)), RecordingLogger())


def _buy_fill(pm, today, *, tid="B1", price="11.00", vol=1000):
    pm.mark_position_on_fill(
        FakeXtTrade(account_id="acc1", stock_code="600036.SH", traded_id=tid,
                    traded_price=float(price), traded_volume=vol),
        today, account_id="acc1", ts_code="600036.SH",
    )


def test_release_on_road_precise_partial_cancel(calendar):
    """F02：部成后剩余委托终态失败 → 按本单未成量精确回扣 on_road，可卖量恢复。"""
    pm = _pm(calendar)
    _buy_fill(pm, T_BUY, vol=1000)              # 买入日 B
    pm.refresh_state(T_SELL)                     # 跨日可卖：can_use=1000, HOLDING
    unit = pm.get_unit("acc1", "600036.SH")
    assert unit.can_use_volume == 1000
    # 挂 REDUCE 600 → 部成 300 → PART_SOLD, on_road=300。
    live = pm._units[("acc1", "600036.SH")]
    pm.mark_selling(live, sell_volume=600)
    pm.apply_sell_fill_by_trade("acc1", "600036.SH", "S1", 300)
    live = pm._units[("acc1", "600036.SH")]
    assert live.state == PositionState.PART_SOLD
    assert live.on_road_sell_volume == 300
    assert pm.sellable_remaining(live) == 700 - 300   # 被在途量压制
    # 剩余 300 委托被撤（零进一步成交）→ 精确回扣本单未成量 300。
    pm.release_on_road_by_code("acc1", "600036.SH", 300, reason="cancel")
    live = pm._units[("acc1", "600036.SH")]
    assert live.on_road_sell_volume == 0
    assert pm.sellable_remaining(live) == 700         # 可卖量恢复，不再漏卖


def test_refresh_state_clears_stale_on_road_cross_day(calendar):
    """F16：跨买入日 refresh_state 清零非 SELLING 单元的隔夜残留 on_road（撤单回执丢失兜底）。"""
    pm = _pm(calendar)
    _buy_fill(pm, T_BUY, vol=1000)
    pm.refresh_state(T_SELL)
    live = pm._units[("acc1", "600036.SH")]
    pm.mark_selling(live, sell_volume=600)
    pm.apply_sell_fill_by_trade("acc1", "600036.SH", "S1", 300)   # PART_SOLD, on_road=300
    # 模拟撤单回执断线丢失：on_road 残留 300 跨日。
    next_day = date(2026, 6, 16)
    pm.refresh_state(next_day)
    live = pm._units[("acc1", "600036.SH")]
    assert live.on_road_sell_volume == 0                 # 跨日残留被清零
    assert pm.sellable_remaining(live) == live.can_use_volume   # 可卖量不再被无依据下调


class _DW:
    """最小 DataWriter 桩（on_stock_trade/on_stock_order 落库出口）。"""
    def upsert_trade(self, r): pass
    def upsert_order(self, r): pass


def _sell_callback(pm, ledger):
    """装配 ExecCallback：position_sink 把卖出成交回写持仓，两个卖出复位/回扣 sink 接 pm。"""
    from qmt_strategy.data_writer.callbacks import ExecCallback

    def _pos(rec):
        if rec.trade_side == TradeSide.SELL:
            pm.apply_sell_fill_by_trade("acc1", rec.ts_code, str(rec.traded_id), int(rec.traded_volume or 0))

    return ExecCallback(
        _DW(), ledger, RecordingLogger(), account_id="acc1", trade_date_provider=lambda: T_SELL,
        position_sink=_pos,
        sell_revert_sink=lambda ts: pm.revert_selling_by_code("acc1", ts),
        sell_on_road_release_sink=lambda ts, q, oid: pm.release_on_road_by_code("acc1", ts, q, order_id=oid),
    )


def test_callback_partial_cancel_releases_on_road_real_path(calendar):
    """端到端【真实回调路径】：部成卖单剩余委托 on_stock_order(CANCELLED) → 精确回扣 on_road。

    review F02：on_stock_order 先 sync_status 把部成撤单收口为 PART_TRADED，若按台账 state 判终态则 release
    永不触发（死路）。本用例经真实 on_stock_trade + on_stock_order 驱动，锁定按【回报原始状态】判定的修复。
    """
    from qmt_strategy.contracts.models import LedgerEntry
    from qmt_strategy.contracts.xt_objects import FakeXtOrder, FakeXtTrade as _XT

    pm = _pm(calendar)
    _buy_fill(pm, T_BUY, vol=1000)
    pm.refresh_state(T_SELL)
    live = pm._units[("acc1", "600036.SH")]
    pm.mark_selling(live, sell_volume=600)   # 模拟 place_sell：SELLING, on_road=600

    ledger = InMemoryLocalLedger()
    ledger.insert(LedgerEntry(
        biz_order_no="b1", account_id="acc1", target_trade_date=T_SELL, ts_code="600036.SH",
        strategy_family="SELL", side=TradeSide.SELL, plan_volume=600, plan_price=Decimal("11"),
        order_remark="SELL|reduce", signal_trade_date=T_BUY, state=OrderState.SUBMITTED, order_id=555,
    ))
    cb = _sell_callback(pm, ledger)
    # 1) 部成 300：on_stock_trade → 台账 filled=300、持仓 PART_SOLD/on_road=300。
    cb.on_stock_trade(_XT(account_id="acc1", stock_code="600036.SH", traded_id="S1", order_id=555,
                          order_type=24, traded_price=Decimal("11.00"), traded_volume=300, traded_time=None))
    live = pm._units[("acc1", "600036.SH")]
    assert live.state == PositionState.PART_SOLD and live.on_road_sell_volume == 300
    # 2) 剩余 300 被撤：on_stock_order(CANCELLED, traded=300) → sync_status 收口 PART_TRADED → 仍精确回扣 300。
    cb.on_stock_order(FakeXtOrder(stock_code="600036.SH", order_id=555, order_type=24,
                                  order_status="CANCELLED", order_volume=600, traded_volume=300, order_time=None))
    live = pm._units[("acc1", "600036.SH")]
    assert live.on_road_sell_volume == 0                  # 精确回扣，不再漏卖
    assert pm.sellable_remaining(live) == live.can_use_volume


def test_callback_multi_order_zero_fill_does_not_overclear(calendar):
    """review #2：O1 部成残量在途时，O2 零成交被拒只回扣 O2 自身量，绝不误清 O1 的在途冻结。"""
    from qmt_strategy.contracts.models import LedgerEntry
    from qmt_strategy.contracts.xt_objects import FakeXtOrder, FakeXtTrade as _XT

    pm = _pm(calendar)
    _buy_fill(pm, T_BUY, vol=1000)
    pm.refresh_state(T_SELL)
    live = pm._units[("acc1", "600036.SH")]
    pm.mark_selling(live, sell_volume=600)   # O1：SELLING, on_road=600

    ledger = InMemoryLocalLedger()
    ledger.insert(LedgerEntry(biz_order_no="o1", account_id="acc1", target_trade_date=T_SELL,
        ts_code="600036.SH", strategy_family="SELL", side=TradeSide.SELL, plan_volume=600,
        plan_price=Decimal("11"), order_remark="x", signal_trade_date=T_BUY,
        state=OrderState.SUBMITTED, order_id=1))
    cb = _sell_callback(pm, ledger)
    cb.on_stock_trade(_XT(account_id="acc1", stock_code="600036.SH", traded_id="f1", order_id=1,
                          order_type=24, traded_price=Decimal("11.00"), traded_volume=300, traded_time=None))
    # O1 部成 300 → PART_SOLD, on_road=300（O1 残 300 仍在途）。再挂 O2=400。
    live = pm._units[("acc1", "600036.SH")]
    pm.mark_selling(live, sell_volume=400)   # O2：SELLING, on_road=300+400=700
    assert live.on_road_sell_volume == 700
    ledger.insert(LedgerEntry(biz_order_no="o2", account_id="acc1", target_trade_date=T_SELL,
        ts_code="600036.SH", strategy_family="SELL", side=TradeSide.SELL, plan_volume=400,
        plan_price=Decimal("11"), order_remark="x", signal_trade_date=T_BUY,
        state=OrderState.SUBMITTED, order_id=2))
    # O2 零成交被拒 → 只回扣 O2 自身 400，O1 的 300 在途冻结保留、单元仍 SELLING。
    cb.on_stock_order(FakeXtOrder(stock_code="600036.SH", order_id=2, order_type=24,
                                  order_status="REJECTED", order_volume=400, traded_volume=0, order_time=None))
    live = pm._units[("acc1", "600036.SH")]
    assert live.on_road_sell_volume == 300                # 只清 O2，不误清 O1
    assert live.state == PositionState.SELLING            # O1 仍在途，单元不复位


def test_release_on_road_idempotent_by_order_id(calendar):
    """review 幂等：同一卖单终态回调重复触发（双面回报/重投）→ 只回扣一次，不误清兄弟在途单。"""
    pm = _pm(calendar)
    _buy_fill(pm, T_BUY, vol=1000)
    pm.refresh_state(T_SELL)
    live = pm._units[("acc1", "600036.SH")]
    pm.mark_selling(live, sell_volume=600)                       # O1 on_road=600
    pm.apply_sell_fill_by_trade("acc1", "600036.SH", "f1", 300)  # O1 部成 → on_road=300
    pm.mark_selling(pm._units[("acc1", "600036.SH")], sell_volume=400)  # O2 on_road=700
    # O2(order_id=2) 拒单回扣 400，重复触发两次 → 只回扣一次。
    pm.release_on_road_by_code("acc1", "600036.SH", 400, order_id=2)
    pm.release_on_road_by_code("acc1", "600036.SH", 400, order_id=2)   # 双面回报/重投
    live = pm._units[("acc1", "600036.SH")]
    assert live.on_road_sell_volume == 300       # O1 的 300 完好，未被二次回扣误清
    assert live.state == PositionState.SELLING


def test_callback_zero_fill_cancel_reverts_via_release(calendar):
    """零成交整单撤 → 精确回扣（=plan_volume）使 on_road 归零并复位 SELLING→HOLDING。"""
    from qmt_strategy.contracts.models import LedgerEntry
    from qmt_strategy.contracts.xt_objects import FakeXtOrder

    pm = _pm(calendar)
    _buy_fill(pm, T_BUY, vol=1000)
    pm.refresh_state(T_SELL)
    live = pm._units[("acc1", "600036.SH")]
    pm.mark_selling(live, sell_volume=1000)   # SELLING, on_road=1000
    ledger = InMemoryLocalLedger()
    ledger.insert(LedgerEntry(biz_order_no="z1", account_id="acc1", target_trade_date=T_SELL,
        ts_code="600036.SH", strategy_family="SELL", side=TradeSide.SELL, plan_volume=1000,
        plan_price=Decimal("11"), order_remark="x", signal_trade_date=T_BUY,
        state=OrderState.SUBMITTED, order_id=9))
    cb = _sell_callback(pm, ledger)
    cb.on_stock_order(FakeXtOrder(stock_code="600036.SH", order_id=9, order_type=24,
                                  order_status="CANCELLED", order_volume=1000, traded_volume=0, order_time=None))
    live = pm._units[("acc1", "600036.SH")]
    assert live.on_road_sell_volume == 0
    assert live.state == PositionState.HOLDING            # 无其它在途 → 复位重挂


# ===========================================================================
# 三、F18：低吸档缺省不回退高开区间 + dip fail-closed
# ===========================================================================
def test_resolve_thresholds_lowbuy_no_high_open_fallback():
    """F18：未配 lowbuy → 留 None，绝不回退成 reasonable_open(高开区间)。"""
    from qmt_strategy.entry.strategies.base import resolve_thresholds
    plan = make_plan_row(reasonable_open_low=Decimal("10.20"), reasonable_open_high=Decimal("10.80"))
    from qmt_strategy.contracts.enums import AuctionPhase, CentroidTrend
    from qmt_strategy.contracts.models import AuctionSnapshot
    snap = AuctionSnapshot(ts_code="600000.SH", phase=AuctionPhase.AUCTION_CANCELABLE,
                           ts=utc_at_east8(T_BUY, 9, 18), pre_close=Decimal("10.00"))
    thr = resolve_thresholds(plan, snap, Settings())  # 未配 lowbuy
    assert thr.lowbuy_low is None and thr.lowbuy_high is None
    # overheat/abandon 仍可回退（供强势追买族），不受影响。
    assert thr.overheat_pct is not None


def test_dip_buy_fail_closed_when_lowbuy_unconfigured():
    """F18：低吸档未配置 → dip_buy_ma fail-closed SKIP（不臆造、不方向反）。"""
    from qmt_strategy.entry.strategies.dip_buy_ma import DipBuyMaStrategy
    from qmt_strategy.contracts.enums import AuctionPhase, CentroidTrend
    from qmt_strategy.contracts.models import AuctionSnapshot
    plan = make_plan_row(ts_code="600000.SH", strategy_family="低吸", setup="均线",
                         leader_strength_score=None, continuation_prob=None)
    snap = AuctionSnapshot(ts_code="600000.SH", phase=AuctionPhase.AUCTION_CANCELABLE,
                           ts=utc_at_east8(T_BUY, 9, 18), open_pct=Decimal("0.00"),
                           last_price=Decimal("10.00"), pre_close=Decimal("10.00"))
    out = DipBuyMaStrategy().decide(plan, snap, Settings())   # 未配 lowbuy
    assert out.action == EntryAction.SKIP
    assert "低吸档阈值未配置" in out.reason
    # 配置低吸档后平开(0%)落档 → 买（方向正确）。
    cfg = Settings(auction_lowbuy_pct_low=Decimal("-0.02"), auction_lowbuy_pct_high=Decimal("0.01"),
                   auction_overheat_pct=Decimal("0.05"))
    out2 = DipBuyMaStrategy().decide(plan, snap, cfg)
    assert out2.action == EntryAction.DIP_BUY_MA


# ===========================================================================
# 四、资金/风控口径（Engine harness：F14/F03/F19/F15）
# ===========================================================================
def _engine(env=None, rows=None, cash=1_000_000, positions=None):
    from qmt_strategy.app.main import EngineDeps, build_engine
    from qmt_strategy.data_writer.repository import InMemoryQmtRepository
    from qmt_strategy.watchlist.sources import CallableSelectedStockSource

    class _Cal:
        def is_open(self, d): return d.weekday() < 5
        def next_open(self, d):
            x = d + timedelta(days=1)
            while x.weekday() >= 5: x += timedelta(days=1)
            return x
        def prev_open(self, d):
            x = d - timedelta(days=1)
            while x.weekday() >= 5: x -= timedelta(days=1)
            return x

    if rows is None:
        rows = [make_selected_row(ts_code="600036.SH", signal_close=Decimal("10.00"),
                                  limit_up_price=Decimal("11.00"),
                                  reasonable_open_high_low=Decimal("10.20"),
                                  reasonable_open_high_high=Decimal("10.80"),
                                  market_state="启动", tradable_flag=True, leader_strength_score=Decimal("90"))]
    for r in rows:
        r.strategy_family = "打板"
        r.setup = "首板"
    deps = EngineDeps(
        settings=Settings.from_env(env or {}),
        clock=FakeClock(utc_at_east8(T_BUY, 9, 16)), logger=RecordingLogger(), calendar=_Cal(),
        trader=_RecTrader(cash=cash, positions=positions), account=FakeStockAccount("acc1"),
        account_id="acc1", tick_source=type("T", (), {"get_full_tick": lambda self, c: {}})(),
        selected_source=CallableSelectedStockSource(lambda d: rows, source_name="t"),
        repository=InMemoryQmtRepository(),
    )
    return build_engine(deps), deps


def test_f14_ceiling_respects_ratio_when_no_baseline():
    """F14：日初权益基线缺失时 ceiling=cash×ratio（不再退化全额现金绕过留现金意图）。"""
    eng, _ = _engine({"QMT_TARGET_POSITION_RATIO": "0.5"})
    eng.prewarm(T_BUY)
    eng._day_open_equity = None   # 模拟盘前基线抓取失败
    v = eng._strength_budget_volume(eng._plan_map["600036.SH"], Decimal("10.00"))
    # ceiling = cash(100万)×0.5 = 50万；w=1.0 单票 → 预算≈50万 → 股数≈50000，远小于全额现金的 ~100000。
    assert 0 < v <= 50000
    # 对照：ratio=1.0 + 无基线 → ceiling=cash 全额，股数≈100000。
    eng2, _ = _engine({"QMT_TARGET_POSITION_RATIO": "1.0"})
    eng2.prewarm(T_BUY)
    eng2._day_open_equity = None
    v2 = eng2._strength_budget_volume(eng2._plan_map["600036.SH"], Decimal("10.00"))
    assert v2 > v   # ratio=1.0 部署明显多于 ratio=0.5（证明 ratio 真生效）


def test_f03_per_stock_cap_net_of_holdings():
    """F03：单票上限净额扣已有持仓——已持仓达上限时主链 sizer 返 0 不再加仓。"""
    pos = [type("P", (), {"stock_code": "600036.SH", "market_value": Decimal("300000")})()]
    eng, _ = _engine({"QMT_MAX_POSITION_PER_STOCK": "300000"}, positions=pos)
    eng.prewarm(T_BUY)
    # 已持 30 万 = 单票上限 → effective_cap<=0 → 不再加仓。
    v = eng._strength_budget_volume(eng._plan_map["600036.SH"], Decimal("10.00"))
    assert v == 0
    # 对照：无持仓时同上限可买。
    eng2, _ = _engine({"QMT_MAX_POSITION_PER_STOCK": "300000"})
    eng2.prewarm(T_BUY)
    v2 = eng2._strength_budget_volume(eng2._plan_map["600036.SH"], Decimal("10.00"))
    assert v2 > 0


def test_f13_exposure_for_code_single_counts_filled():
    """F13：exposure_for_code = 持仓市值 + 未成在途，已成 filled 不被持仓与在途双重计入。"""
    led = InMemoryLocalLedger()
    from qmt_strategy.contracts.models import LedgerEntry
    # 一条买单：plan 1000、已成 600（在持仓市值里）、剩余 400 在途。
    led.insert(LedgerEntry(
        biz_order_no="b1", account_id="acc1", target_trade_date=T_BUY, ts_code="600036.SH",
        strategy_family="打板", side=TradeSide.BUY, plan_volume=1000, plan_price=Decimal("10.00"),
        order_remark="x", signal_trade_date=T_SIGNAL, state=OrderState.PART_TRADED,
        order_id=1, filled_volume=600, avg_filled_price=Decimal("10.00"),
    ))
    pos = [type("P", (), {"stock_code": "600036.SH", "market_value": Decimal("6000")})()]  # 600×10
    ex = OrderExecutor(_RecTrader(positions=pos), FakeStockAccount("acc1"), "acc1", led,
                       Settings(), FakeClock(utc_at_east8(T_BUY, 9, 16)), RecordingLogger())
    # 敞口 = 持仓 6000(已成 600) + 未成在途 400×10=4000 = 10000；若 F13 未修则会多计已成 6000 → 16000。
    assert ex.exposure_for_code(T_BUY, "600036.SH") == Decimal("10000")


def test_f19_negative_strength_weight_clamped():
    """F19：负 leader_strength_score 被钳为 0 份额，权重不越界(≤1)、不破坏 Σw。"""
    rows = []
    for code, s in (("600036.SH", Decimal("90")), ("600000.SH", Decimal("-50"))):
        r = make_selected_row(ts_code=code, signal_close=Decimal("10.00"),
                              limit_up_price=Decimal("11.00"),
                              reasonable_open_high_low=Decimal("10.20"),
                              reasonable_open_high_high=Decimal("10.80"),
                              market_state="启动", tradable_flag=True, leader_strength_score=s)
        rows.append(r)
    eng, _ = _engine(rows=rows)
    eng.prewarm(T_BUY)
    w_pos = eng._strength_weights.get("600036.SH")
    w_neg = eng._strength_weights.get("600000.SH")
    assert w_pos is not None and Decimal("0") < w_pos <= Decimal("1")   # 不越界
    assert w_neg == Decimal("0")                                         # 负分 0 份额
    # 负分票预算 0；正分票不超 ceiling。
    assert eng._strength_budget_volume(eng._plan_map["600000.SH"], Decimal("10.00")) == 0


def test_f19_all_non_positive_strength_no_allocation():
    """review#3：top-N 真实分全为非正 → 不分配(weights 空)，不退化等权把弱/坏票买回。"""
    rows = []
    for code, s in (("600036.SH", Decimal("-50")), ("600000.SH", Decimal("-30"))):
        r = make_selected_row(ts_code=code, signal_close=Decimal("10.00"), limit_up_price=Decimal("11.00"),
                              reasonable_open_high_low=Decimal("10.20"), reasonable_open_high_high=Decimal("10.80"),
                              market_state="启动", tradable_flag=True, leader_strength_score=s)
        rows.append(r)
    eng, _ = _engine(rows=rows)
    eng.prewarm(T_BUY)
    assert eng._strength_weights == {}    # 全非正 → 不分配
    assert eng._strength_budget_volume(eng._plan_map["600036.SH"], Decimal("10.00")) == 0
    # 对照：全部缺强度(None) → 合法退化等权（仍可买）。
    rows2 = []
    for code in ("600036.SH", "600000.SH"):
        r = make_selected_row(ts_code=code, signal_close=Decimal("10.00"), limit_up_price=Decimal("11.00"),
                              reasonable_open_high_low=Decimal("10.20"), reasonable_open_high_high=Decimal("10.80"),
                              market_state="启动", tradable_flag=True, leader_strength_score=None)
        rows2.append(r)
    eng2, _ = _engine(rows=rows2)
    eng2.prewarm(T_BUY)
    assert eng2._strength_weights.get("600036.SH") == Decimal("0.5")   # 等权


def test_write_queue_stuck_seconds_zero_disables_watchdog():
    """review#4：QMT_WRITE_QUEUE_STUCK_SECONDS=0 显式关看门狗（不被 or 默认吞成 30）。"""
    s = Settings.from_env({"QMT_WRITE_QUEUE_STUCK_SECONDS": "0"})
    assert s.write_queue_stuck_seconds == 0.0
    s2 = Settings.from_env({})   # 未配 → 默认 30
    assert s2.write_queue_stuck_seconds == 30.0


def test_f15_drawdown_fail_closed_when_no_baseline():
    """F15：配了回撤阈值但日初基线缺失 → fail-closed 禁开新仓（不再整日 fail-open）。"""
    eng, _ = _engine({"QMT_ACCOUNT_DRAWDOWN_LIMIT": "0.05"})
    eng.prewarm(T_BUY)
    eng._day_open_equity = None   # 模拟盘前基线抓取失败
    assert eng._open_blocked_by_risk("600036.SH") is True
    # 对照：未配回撤阈值时缺基线不因此误禁（该闸本就不工作）。
    eng2, _ = _engine({})
    eng2.prewarm(T_BUY)
    eng2._day_open_equity = None
    assert eng2._open_blocked_by_risk("600036.SH") is False


# ===========================================================================
# 五、存储健壮性（F09/F07）
# ===========================================================================
def test_f09_write_queue_stuck_watchdog():
    """F09：写线程卡死(任务永不返回) + 有积压 → is_healthy 转 False（看门狗识别 hang）。"""
    from qmt_strategy.storage.write_queue import AsyncWriteQueue

    started = {"v": False}

    def _conn():
        return sqlite3.connect(":memory:")

    wq = AsyncWriteQueue(_conn, RecordingLogger(), name="t", stuck_seconds=0.3)
    wq.start()
    assert wq.is_healthy() is True
    # 提交一个永不返回的任务模拟 hang。
    block = {"go": False}

    def _stuck(conn):
        while not block["go"]:
            time.sleep(0.01)

    wq.submit(_stuck)
    wq.submit(lambda c: None)   # 第二个任务积压在队列（pending>0）
    time.sleep(0.5)             # 超过 stuck_seconds 仍无推进
    assert wq.is_healthy() is False   # 看门狗判卡死
    block["go"] = True          # 解除，收尾
    wq.stop(drain=False)


def test_f07_local_stack_arms_max_queue():
    """F07：LocalStorage 从配置武装写队列 max_queue（>0 启用溢出熔断），不再默认 0 形同虚设。"""
    from qmt_strategy.storage.local_stack import LocalStorage

    stack = LocalStorage(":memory:", RecordingLogger(), "acc1",
                         write_queue_max=12345, write_queue_stuck_seconds=30.0)
    assert stack._wq._max_queue == 12345
    assert stack._wq._stuck_seconds == 30.0


# ===========================================================================
# 六、对账（F21/F01）
# ===========================================================================
def _reconcile_with(ledger, repo, *, abs_floor=Decimal("1000"), rel_rate=Decimal("0")):
    from qmt_strategy.reconcile.reconcile import Reconcile

    class _Cal:
        def prev_open(self, d): return d - timedelta(days=1)
        def next_open(self, d): return d + timedelta(days=1)
        def is_open(self, d): return True

    return Reconcile(ledger, repo, RecordingLogger(), _Cal(), account_id="acc1",
                     asset_abs_floor=abs_floor, asset_rel_rate=rel_rate)


def test_f21_manual_order_not_masked_by_cross_day_order_id():
    """F21：今日手工单 order_id 恰与历史系统单复用 → 仍正确标 manual_order（按当日台账判定）。"""
    from qmt_strategy.contracts.models import LedgerEntry, OrderRecord
    from qmt_strategy.data_writer.repository import InMemoryQmtRepository

    ledger = InMemoryLocalLedger()
    # 历史日(D1)系统单 order_id=500（仍驻内存，跨日 order_id 复用）。
    ledger.insert(LedgerEntry(
        biz_order_no="d1_500", account_id="acc1", target_trade_date=date(2026, 6, 10),
        ts_code="600036.SH", strategy_family="打板", side=TradeSide.BUY, plan_volume=1000,
        plan_price=Decimal("10"), order_remark="x", signal_trade_date=date(2026, 6, 9),
        state=OrderState.TRADED, order_id=500, filled_volume=1000,
    ))
    repo = InMemoryQmtRepository()
    today = date(2026, 6, 12)
    # 今日券商回报有 order_id=500（手工单/串账户，本系统今日台账无此单）。
    repo.upsert_order(OrderRecord(
        account_id="acc1", trade_date=today, ts_code="000001.SZ", qmt_stock_code="000001.SZ",
        order_id=500, trade_side=TradeSide.BUY, order_volume=500, order_status=OrderStatus.TRADED,
    ))
    report = _reconcile_with(ledger, repo).run(today)
    kinds = {d.kind for d in report.order_discrepancies}
    assert "manual_order" in kinds   # 不被历史 order_id 掩盖


def test_f01_asset_relative_tolerance_no_false_positive():
    """F01：高换手日费用噪声(成交净额 vs 含费用现金变动)在相对容差内 → 不误报资产偏差。"""
    from qmt_strategy.contracts.enums import SnapshotType
    from qmt_strategy.contracts.models import AccountRecord, TradeRecord
    from qmt_strategy.data_writer.repository import InMemoryQmtRepository

    today = date(2026, 6, 12)
    prev = date(2026, 6, 11)
    repo = InMemoryQmtRepository()
    # 当日买入 400 万（成交净额 -400万，不含费用）。
    repo.upsert_trade(TradeRecord(
        account_id="acc1", trade_date=today, ts_code="600036.SH", qmt_stock_code="600036.SH",
        traded_id="t1", trade_side=TradeSide.BUY, traded_price=Decimal("10"), traded_volume=400000,
        traded_amount=Decimal("4000000"), order_id=1,
    ))
    # 现金变动 = -4,001,200（含 1200 元费用噪声，绝对阈值 1000 会误报）。
    repo.upsert_account_daily(AccountRecord(account_id="acc1", trade_date=prev,
        total_asset=Decimal("10000000"), cash=Decimal("10000000"), snapshot_type=SnapshotType.CLOSE))
    repo.upsert_account_daily(AccountRecord(account_id="acc1", trade_date=today,
        total_asset=Decimal("9998800"), cash=Decimal("5998800"), snapshot_type=SnapshotType.CLOSE))
    ledger = InMemoryLocalLedger()
    # 旧绝对阈值 1000 会误报；相对容差 0.3%×400万=1.2万 覆盖费用噪声 → 不报。
    report = _reconcile_with(ledger, repo, rel_rate=Decimal("0.003")).run(today)
    assert report.asset_discrepancy is None


# ===========================================================================
# 七、prefetch 返回口径（F11/F12）
# ===========================================================================
def _prefetcher(client):
    from qmt_strategy.watchlist.remote_watchlist import WatchlistPrefetcher

    class _Cal:
        def prev_open(self, d): return date(2026, 6, 11)
        def next_open(self, d): return d
        def is_open(self, d): return True

    saved = {"rows": None}

    def save_fn(rows):
        saved["rows"] = rows
        return len(rows)

    return WatchlistPrefetcher(client, _Cal(), save_fn, RecordingLogger()), saved


def test_f11_prefetch_distinguishes_failure_from_empty():
    """F11：真失败返回 -1、合法空名单返回 0、成功返回 N（供调度只在真失败重试）。"""
    from qmt_strategy.common.http_client import SignalHttpError

    class _OkEmpty:
        def get_json(self, path, params=None): return {"items": []}
    class _OkOne:
        def get_json(self, path, params=None):
            return {"items": [{"ts_code": "600036.SH", "trade_date": "2026-06-11", "close": "10.0",
                               "tradable_flag": "TRADABLE"}]}
    class _Fail:
        def get_json(self, path, params=None): raise SignalHttpError("boom", status=503)

    pf_empty, _ = _prefetcher(_OkEmpty())
    assert pf_empty.prefetch(date(2026, 6, 12)) == 0     # 合法空 → 不重试
    pf_one, _ = _prefetcher(_OkOne())
    assert pf_one.prefetch(date(2026, 6, 12)) == 1       # 成功
    pf_fail, _ = _prefetcher(_Fail())
    assert pf_fail.prefetch(date(2026, 6, 12)) == -1     # 真失败 → 重试


def test_f12_prefetch_bad_envelope_returns_failure():
    """F12：2xx 但信封非对象(list) → 判失败(-1)，不抛 AttributeError。"""
    class _BadEnvelope:
        def get_json(self, path, params=None): return ["not", "a", "dict"]

    pf, _ = _prefetcher(_BadEnvelope())
    assert pf.prefetch(date(2026, 6, 12)) == -1
