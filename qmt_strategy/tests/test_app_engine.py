"""Engine 编排层集成 smoke 测试（§1.5 主链路 + §7.1.6 竞价择时闸门 + 收盘对账闭环）。

用全 fake 依赖装配引擎，验证：装配不报错、盘前装载、竞价择时闸门（默认关只采集 / 开则下单）、
空仓禁开新仓、收盘批次 + 对账可跑。不连真实 xttrader / MySQL。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List

from qmt_strategy.app.main import EngineDeps, build_engine
from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import AuctionPhase, CentroidTrend, EntryAction
from qmt_strategy.contracts.models import AuctionSnapshot
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.watchlist.sources import CallableSelectedStockSource
from tests.conftest import T_BUY, make_selected_row, utc_at_east8


class _Cal:
    def is_open(self, d):
        return d.weekday() < 5

    def next_open(self, d):
        x = d + timedelta(days=1)
        while x.weekday() >= 5:
            x += timedelta(days=1)
        return x

    def prev_open(self, d):
        x = d - timedelta(days=1)
        while x.weekday() >= 5:
            x -= timedelta(days=1)
        return x


class _FakeTrader:
    """实现 query_* / order_stock，供 Engine 装配与收盘批次跑通。"""

    def __init__(self, cash=1_000_000):
        self._cash = cash
        self.order_calls: List[tuple] = []

    def order_stock(self, account, code, otype, vol, ptype, price, sname, remark):
        self.order_calls.append((code, otype, vol))
        return 100 + len(self.order_calls)

    def cancel_order_stock(self, account, order_id):
        return 0

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc1", cash=self._cash, frozen_cash=0,
                           market_value=0, total_asset=self._cash)

    def query_stock_positions(self, account):
        return []

    def query_stock_orders(self, account):
        return []

    def query_stock_trades(self, account):
        return []


class _StubTickSource:
    def get_full_tick(self, codes):
        return {}


def _deps(env=None, source_rows=None):
    rows = source_rows if source_rows is not None else [
        make_selected_row(
            ts_code="600036.SH", signal_close=Decimal("10.00"),
            limit_up_price=Decimal("11.00"),
            reasonable_open_high_low=Decimal("10.20"),
            reasonable_open_high_high=Decimal("10.80"),
            market_state="启动", tradable_flag=True,
            strategy="打板", role="龙头",
        )
    ]
    # 补 strategy_family / setup（路由到 CHASE_AUCTION_STRONG）。
    for r in rows:
        r.strategy_family = "打板"
        r.setup = "首板"
    src = CallableSelectedStockSource(lambda d: rows, source_name="test")
    return EngineDeps(
        settings=Settings.from_env(env or {}),
        clock=FakeClock(utc_at_east8(T_BUY, 9, 16)),
        logger=RecordingLogger(),
        calendar=_Cal(),
        trader=_FakeTrader(),
        account=FakeStockAccount("acc1"),
        account_id="acc1",
        tick_source=_StubTickSource(),
        selected_source=src,
        repository=InMemoryQmtRepository(),
    )


def _strong_auction_snap():
    """强开越竞越强快照（CHASE_AUCTION_STRONG 应判买、order_phase=AUCTION）。"""
    return AuctionSnapshot(
        ts_code="600036.SH", phase=AuctionPhase.AUCTION_CANCELABLE, ts=utc_at_east8(T_BUY, 9, 16),
        open_pct=Decimal("0.10"), auction_vol_ratio=Decimal("0.5"), auction_centroid=Decimal("10.50"),
        centroid_trend=CentroidTrend.UP, is_limit_up=False, last_price=Decimal("10.50"),
        pre_close=Decimal("10.00"),
    )


def test_engine_assembles_and_prewarms():
    eng = build_engine(_deps())
    ctx = eng.prewarm(T_BUY)
    assert ctx.is_open is True
    assert ctx.open_new_position_allowed is True       # 启动态 → 允许开新仓
    assert "600036.SH" in ctx.tradable
    assert eng.callback is not None                     # 回调对象可供连接守护注册


def test_auction_timing_disabled_collect_only():
    """§7.1.6：竞价择时默认关 → 竞价段 BUY 决策只采集留痕、不下单。"""
    deps = _deps()  # auction_timing_enabled 默认 False
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng._router_sink(_strong_auction_snap())
    assert deps.trader.order_calls == []                # 竞价段未下单（只采集）


def test_auction_timing_enabled_places_order():
    """竞价择时开启 + 非空仓 → 竞价段强开决策真正下单。"""
    deps = _deps({"QMT_AUCTION_TIMING_ENABLED": "true"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng._router_sink(_strong_auction_snap())
    assert len(deps.trader.order_calls) == 1
    code, _otype, vol = deps.trader.order_calls[0]
    assert code == "600036.SH" and vol > 0


def test_empty_position_state_blocks_open():
    """空仓日 → open_new_position_allowed=False，竞价择时即便开启也不开新仓（§2.6）。"""
    rows = [make_selected_row(
        ts_code="600036.SH", signal_close=Decimal("10.00"),
        limit_up_price=Decimal("11.00"),
        reasonable_open_high_low=Decimal("10.20"), reasonable_open_high_high=Decimal("10.80"),
        market_state="空仓", tradable_flag=True,
    )]
    deps = _deps({"QMT_AUCTION_TIMING_ENABLED": "true"}, source_rows=rows)
    eng = build_engine(deps)
    ctx = eng.prewarm(T_BUY)
    assert ctx.open_new_position_allowed is False
    eng._router_sink(_strong_auction_snap())
    assert deps.trader.order_calls == []                # 空仓禁开新仓


def test_kill_switch_blocks_order_even_if_timing_on():
    """全局熔断：kill_switch=True → 即便竞价择时开启也不下单（§7.1.5 双保险）。"""
    deps = _deps({"QMT_AUCTION_TIMING_ENABLED": "true", "QMT_KILL_SWITCH": "true"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng._router_sink(_strong_auction_snap())
    assert deps.trader.order_calls == []


def test_close_batch_and_reconcile_run():
    """收盘批次 + 对账闭环可跑通（query_* 空集 → CLOSE 快照落库、对账无异常）。"""
    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng.close_batch(T_BUY)
    # 收盘资产快照已落库（FakeXtAsset → qmt_account_daily CLOSE）。
    assert deps.repository.get_account_daily("acc1", T_BUY) is not None


def test_run_sell_pass_no_sellable_units():
    """无可卖持仓（当日无昨仓）→ run_sell_pass 返回空、不下卖单。"""
    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    assert eng.run_sell_pass(T_BUY, books={}, session="intraday") == []
