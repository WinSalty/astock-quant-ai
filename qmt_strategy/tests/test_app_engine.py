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


# ---------------------------------------------------------------------------
# 评审 P0-A2 / P0-C2：买入成交回写持仓 → 次日可卖 → 卖出 → 在途不重复卖（端到端闭环）
# ---------------------------------------------------------------------------
def test_buy_fill_writes_position_then_sellable_next_day_and_no_double_sell():
    from qmt_strategy.contracts.models import OrderBook
    from qmt_strategy.contracts.enums import PositionState
    from qmt_strategy.contracts.xt_objects import FakeXtTrade

    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)

    # (1) 买入成交回报经回调落地持仓状态机（评审 P0-A2：原实现从不回写，此前恒空集）。
    fill = FakeXtTrade(
        account_id="acc1", stock_code="600036.SH", traded_id="TR1", order_id=1,
        traded_price=11.00, traded_volume=1000,
    )
    eng.callback.on_stock_trade(fill)
    unit = eng._position.get_unit("acc1", "600036.SH")
    assert unit is not None and unit.volume == 1000
    # 守 T+1：买入当日不可卖（LOCKED_T1，can_use_volume=0）。
    assert unit.state == PositionState.LOCKED_T1
    assert eng.run_sell_pass(T_BUY, books={"600036.SH": OrderBook(ts_code="600036.SH", broke_board=True)}) == []

    # (2) 次日推进 → HOLDING、可卖量放开 → sellable_units 非空（此前恒空、永不卖出）。
    next_day = deps.calendar.next_open(T_BUY)
    eng._position.refresh_state(next_day)
    assert len(eng._position.sellable_units(next_day)) == 1

    # (3) 炸板盘口 → CLEAR → 下且仅下一张卖单，单元转 SELLING。
    book = OrderBook(ts_code="600036.SH", broke_board=True, is_sealed=False, last_price=Decimal("10.50"))
    sold = eng.run_sell_pass(next_day, books={"600036.SH": book})
    assert sold == ["600036.SH"]
    sell_calls = [c for c in deps.trader.order_calls if c[1] == 24]  # otype=24 卖出
    assert len(sell_calls) == 1
    assert eng._position.get_unit("acc1", "600036.SH").state == PositionState.SELLING

    # (4) 同一在途 SELLING 单元再跑一轮 → 跳过、不重复下卖单、不抛错（评审 P0-C2）。
    sold2 = eng.run_sell_pass(next_day, books={"600036.SH": book})
    assert sold2 == []
    sell_calls2 = [c for c in deps.trader.order_calls if c[1] == 24]
    assert len(sell_calls2) == 1   # 仍只有 1 张卖单，未重复


def test_sell_fill_reduces_position_idempotently():
    """卖出成交回报回写：扣减持仓、推进 SOLD/PART_SOLD；同一 traded_id 重投不重复扣减。"""
    from qmt_strategy.contracts.enums import PositionState, TradeSide
    from qmt_strategy.contracts.xt_objects import FakeXtTrade

    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    # 先买入建仓并推进到次日可卖。
    eng.callback.on_stock_trade(FakeXtTrade(
        account_id="acc1", stock_code="600036.SH", traded_id="B1", order_id=1,
        traded_price=11.00, traded_volume=1000,
    ))
    next_day = deps.calendar.next_open(T_BUY)
    eng._position.refresh_state(next_day)
    unit = eng._position.get_unit("acc1", "600036.SH")
    unit.state = PositionState.SELLING  # 模拟已发卖单在途

    # 卖出成交回报（offset_flag 触发 SELL 方向）：部分成交 600 → PART_SOLD。
    sell = FakeXtTrade(
        account_id="acc1", stock_code="600036.SH", traded_id="S1", order_id=2,
        traded_price=10.50, traded_volume=600, order_type=24, offset_flag=48,
    )
    # 强制方向为 SELL（规整层方向映射为占位实测值，这里直接断言回写按 rec.trade_side 分流）：
    # 用引擎内部分流函数直接喂一个 SELL 方向的轻量记录，避免依赖占位 side_resolver。
    class _Rec:
        ts_code = "600036.SH"; trade_side = TradeSide.SELL; traded_id = "S1"
        traded_volume = 600; traded_price = 10.50
        trade_date = next_day
    eng._apply_trade_to_position(_Rec())
    assert eng._position.get_unit("acc1", "600036.SH").volume == 400
    # 同一 traded_id 重投 → 不重复扣减。
    eng._apply_trade_to_position(_Rec())
    assert eng._position.get_unit("acc1", "600036.SH").volume == 400
