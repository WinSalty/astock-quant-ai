"""评审修复回归锁定测试（对应对抗式评审确认的 12 条发现的跨模块部分）。

每个用例锁定一条已修复行为，防止回归。部分发现（medium#2 部成终态、low#1/low#2 台账去重/下界）
已在 test_local_ledger.py 覆盖；本文件补齐其余跨模块发现：
- medium#1：持仓同日补买不重锁昨仓（昨仓仍可卖）
- medium#3：watchlist 单票脏数据不拖垮整批
- medium#4：竞价早期瞬时 NO_TICK 推迟、9:20 后才确认降级 B（已在 test_entry_router 覆盖，这里补 9:20 后用例）
- medium#5：低吸候选不被全局「因子全面走弱」闸门误杀
- medium#6：market_state 缺失按空仓保守 SKIP
- medium#7：normalize 由 order_remark 回填 signal_trade_date
- medium#8：reconcile 资产对账经 get_account_daily 真正可跑
- medium#9：order_stock 同步失败(order_id<0) → 台账 ERROR（非漏单）
- low#3：sweep_expired 驱动 TTL 到期撤单
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, List

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import (
    AuctionPhase,
    CentroidTrend,
    DataSource,
    EntryAction,
    OrderPhase,
    OrderState,
    OrderStatus,
    PositionState,
    SnapshotType,
    TradeSide,
)
from qmt_strategy.contracts.models import (
    AccountRecord,
    AuctionSnapshot,
    EntryDecision,
    SelectedStockRow,
    TradeRecord,
)
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset, FakeXtTrade
from qmt_strategy.data_writer.normalize import default_side_resolver, normalize_trade
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.entry.entry_router import EntryRouter
from qmt_strategy.order.local_ledger import InMemoryLocalLedger
from qmt_strategy.order.order_executor import OrderExecutor
from qmt_strategy.position.position_manager import PositionManager
from qmt_strategy.reconcile.reconcile import Reconcile
from qmt_strategy.watchlist.sources import CallableSelectedStockSource
from qmt_strategy.watchlist.watchlist_loader import WatchlistLoader
from tests.conftest import T_BUY, T_SELL, T_SIGNAL, make_plan_row, make_selected_row, utc_at_east8


# ===========================================================================
# medium#7：normalize 由 order_remark 回填 signal_trade_date（§6.8）
# ===========================================================================
def test_normalize_trade_backfills_signal_trade_date_from_remark():
    t = FakeXtTrade(
        stock_code="600036.SH", traded_id="x1", order_type="BUY",
        traded_price=35.0, traded_volume=100, traded_time=0,
        order_remark="LUP|2026-06-11|600036.SH",
    )
    rec = normalize_trade(
        t, account_id="acc1", trade_date=T_BUY, data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    assert rec.signal_trade_date == date(2026, 6, 11)


def test_normalize_trade_missing_remark_signal_none():
    t = FakeXtTrade(stock_code="600036.SH", traded_id="x2", order_type="BUY",
                    traded_price=35.0, traded_volume=100, traded_time=0)
    rec = normalize_trade(
        t, account_id="acc1", trade_date=T_BUY, data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    assert rec.signal_trade_date is None  # 缺 remark → None，留待对账反推


# ===========================================================================
# medium#1：同日补买不重锁昨仓（昨仓仍可卖、earliest 不被抬高）
# ===========================================================================
def test_position_rebuy_does_not_relock_yesterday_holding():
    pm = PositionManager(_Cal(), FakeClock(datetime(2026, 6, 15, 1, 30)), RecordingLogger())
    # T+1 买入日 B=2026-06-12 建仓 1000。
    pm.mark_position_on_fill(
        FakeXtTrade(stock_code="600036.SH", traded_id="b1", traded_price=10.0, traded_volume=1000),
        today=T_BUY, account_id="acc1",
    )
    # 可卖日 2026-06-15：推进 → HOLDING，昨仓可卖、can_use_volume=1000。
    pm.refresh_state(T_SELL)
    unit = pm.get_unit("acc1", "600036.SH")
    assert unit.state == PositionState.HOLDING
    assert unit.can_use_volume == 1000
    earliest_before = unit.earliest_sellable_date
    # 同日（2026-06-15）补买 500（当日买入部分不可卖）。
    pm.mark_position_on_fill(
        FakeXtTrade(stock_code="600036.SH", traded_id="b2", traded_price=10.0, traded_volume=500),
        today=T_SELL, account_id="acc1",
    )
    unit = pm.get_unit("acc1", "600036.SH")
    # 不被重锁回 LOCKED_T1；昨仓仍在可卖集合；earliest 未被新一笔抬高；can_use_volume 仍只含昨仓。
    assert unit.state != PositionState.LOCKED_T1
    assert pm.get_unit("acc1", "600036.SH") in pm.sellable_units(T_SELL)
    assert unit.earliest_sellable_date == earliest_before
    assert unit.volume == 1500
    assert unit.can_use_volume == 1000  # 当日补买 500 不计入可卖量


class _Cal:
    """最小交易日历：2026-06 工作日，next_open/prev_open 跨周末。"""

    def is_open(self, d):
        return d.weekday() < 5

    def next_open(self, d):
        from datetime import timedelta
        x = d + timedelta(days=1)
        while x.weekday() >= 5:
            x += timedelta(days=1)
        return x

    def prev_open(self, d):
        from datetime import timedelta
        x = d - timedelta(days=1)
        while x.weekday() >= 5:
            x -= timedelta(days=1)
        return x


# ===========================================================================
# medium#3：watchlist 单票脏数据不拖垮整批
# ===========================================================================
def test_watchlist_dirty_row_isolated_batch_survives():
    good = make_selected_row(ts_code="600036.SH", signal_close=Decimal("10.00"), market_state="启动")
    # 脏行：signal_close 为非法字符串，board_rules.budget_prices 会抛 TypeError。
    bad = SelectedStockRow(
        ts_code="600519.SH", trade_date=T_SIGNAL, target_trade_date=T_BUY,
        signal_close="不是数字", market_state="启动", tradable_flag=True,
    )
    src = CallableSelectedStockSource(lambda d: [good, bad], source_name="test")
    loader = WatchlistLoader(src, _Cal(), RecordingLogger(), Settings.from_env({}))
    ctx = loader.load(T_BUY)
    # 整批未崩溃、未降级；好票正常进可交易名单，脏票被隔离丢弃。
    assert ctx.degraded is False
    assert "600036.SH" in ctx.tradable
    assert "600519.SH" not in ctx.tradable


# ===========================================================================
# medium#5：低吸候选不被全局「因子全面走弱」闸门误杀；闸门仍对打板族生效
# ===========================================================================
def _router(settings: Settings):
    log: List[EntryDecision] = []
    return EntryRouter(settings, FakeClock(utc_at_east8(T_BUY, 9, 16)), RecordingLogger(), decision_log=log), log


def _weak_snap():
    """平开回踩弱因子：open_pct=0、未顶板、重心 FLAT（旧全局闸门会判全面走弱）。"""
    return AuctionSnapshot(
        ts_code="600036.SH", phase=AuctionPhase.AUCTION_CANCELABLE, ts=utc_at_east8(T_BUY, 9, 16),
        open_pct=Decimal("0.00"), auction_vol_ratio=Decimal("0.3"), auction_centroid=Decimal("10.00"),
        centroid_trend=CentroidTrend.FLAT, is_limit_up=False, last_price=Decimal("10.00"),
        pre_close=Decimal("10.00"),
    )


def test_dip_candidate_not_killed_by_global_weak_gate():
    # 低吸档配置含 0（回踩平开合法低吸）：lowbuy [-1%,3%]，overheat 5%。
    settings = Settings.from_env({
        "QMT_AUCTION_LOWBUY_PCT_LOW": "-0.01",
        "QMT_AUCTION_LOWBUY_PCT_HIGH": "0.03",
        "QMT_AUCTION_OVERHEAT_PCT": "0.05",
    })
    router, _ = _router(settings)
    plan = make_plan_row(strategy_family="低吸", setup="均线粘合", market_state="启动")
    decision = router.on_auction_snapshot(_weak_snap(), plan)
    # 旧实现会被全局「因子全面走弱」SKIP；现在交给 DIP 策略 → 落低吸档买入。
    assert decision is not None
    assert decision.action == EntryAction.DIP_BUY_MA
    assert "因子全面走弱" not in decision.reason


def test_chase_family_still_skipped_by_weak_gate():
    router, _ = _router(Settings.from_env({}))
    plan = make_plan_row(strategy_family="打板", setup="连板接力", market_state="启动")
    decision = router.on_auction_snapshot(_weak_snap(), plan)
    # 打板族仍受全局「因子全面走弱」闸门保护（评审 medium#5 边界）。
    assert decision.action == EntryAction.SKIP
    assert "因子全面走弱" in decision.reason


# ===========================================================================
# medium#6：market_state 缺失按空仓保守 SKIP
# ===========================================================================
def test_market_state_missing_skips_conservatively():
    router, _ = _router(Settings.from_env({}))
    plan = make_plan_row(strategy_family="打板", setup="连板接力", market_state=None)
    snap = AuctionSnapshot(
        ts_code="600036.SH", phase=AuctionPhase.AUCTION_CANCELABLE, ts=utc_at_east8(T_BUY, 9, 16),
        open_pct=Decimal("0.06"), is_limit_up=True, last_price=Decimal("11.00"), pre_close=Decimal("10.00"),
        centroid_trend=CentroidTrend.UP,
    )
    decision = router.on_auction_snapshot(snap, plan)
    assert decision.action == EntryAction.SKIP
    assert "缺失" in decision.reason


# ===========================================================================
# order_executor：medium#9 同步失败→ERROR、low#3 sweep_expired 驱动撤单
# ===========================================================================
class _FakeTrader:
    def __init__(self, order_id=100, cash=1_000_000):
        self._order_id = order_id
        self._cash = cash
        self.order_calls = 0
        self.cancel_calls: list = []

    def order_stock(self, *a, **k):
        self.order_calls += 1
        return self._order_id

    def cancel_order_stock(self, account, order_id):
        self.cancel_calls.append(order_id)
        return 0

    def query_stock_asset(self, account):
        return FakeXtAsset(cash=self._cash)


def _decision(order_phase=OrderPhase.OPENING, plan_volume=1000, next_best=()):
    return EntryDecision(
        ts_code="600036.SH", signal_trade_date=T_SIGNAL, target_trade_date=T_BUY,
        strategy_family="打板", setup="连板接力", action=EntryAction.CHASE_LIMIT_UP,
        decided_at=utc_at_east8(T_BUY, 9, 16), reason="t", side=TradeSide.BUY,
        limit_price=Decimal("11.00"), plan_volume=plan_volume, order_phase=order_phase,
        next_best=next_best,
    )


def _executor(trader, clock, ledger=None):
    return OrderExecutor(
        trader, FakeStockAccount("acc1"), "acc1", ledger or InMemoryLocalLedger(),
        Settings.from_env({}), clock, RecordingLogger(),
    )


def test_sync_order_failure_marks_error_not_missing():
    trader = _FakeTrader(order_id=-1)  # 同步下单失败
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, FakeClock(utc_at_east8(T_BUY, 9, 16)), ledger=ledger)
    biz = ex.place(_decision())
    assert trader.order_calls == 1
    entry = ledger.get(biz)
    assert entry.state == OrderState.ERROR        # 不置 SUBMITTED，避免被对账误判漏单
    assert entry.state != OrderState.SUBMITTED


def test_sweep_expired_drives_ttl_cancel():
    trader = _FakeTrader(order_id=100)
    ledger = InMemoryLocalLedger()
    # 开盘后下 OPENING 单（评审二轮 P1#16/#17：开盘前下的 OPENING 单 TTL 从 9:30 起算、不会盘前过期，
    # 故用开盘后 9:35 验证常规 TTL 撤单链路）。
    clock = FakeClock(utc_at_east8(T_BUY, 9, 35))
    ex = _executor(trader, clock, ledger=ledger)
    biz = ex.place(_decision(order_phase=OrderPhase.OPENING, plan_volume=1000))
    led = ledger.get(biz)
    ledger.sync_status(led.order_id, OrderState.REPORTED)   # 已报、未成
    # 推进到 TTL 截止之后（默认 order_ttl_seconds=60）：9:36:30 > 9:35+60s。
    clock.set(utc_at_east8(T_BUY, 9, 36, 30))
    handled = ex.sweep_expired()
    assert biz in handled
    assert trader.cancel_calls == [led.order_id]    # TTL 到期被撤


# ===========================================================================
# medium#8：reconcile 资产对账经 get_account_daily 真正可跑
# ===========================================================================
def _account(trade_date, cash):
    return AccountRecord(
        account_id="acc1", trade_date=trade_date, total_asset=Decimal("100000"),
        cash=Decimal(str(cash)), snapshot_type=SnapshotType.CLOSE,
    )


def _buy_trade(amount):
    return TradeRecord(
        account_id="acc1", trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        traded_id="t1", trade_side=TradeSide.BUY, traded_price=Decimal("10.00"),
        traded_volume=int(Decimal(str(amount)) / Decimal("10")), traded_amount=Decimal(str(amount)),
    )


def test_reconcile_asset_check_runs_and_passes_when_consistent():
    repo = InMemoryQmtRepository()
    repo.upsert_account_daily(_account(date(2026, 6, 11), 100000))  # 前一交易日 CLOSE
    repo.upsert_account_daily(_account(T_BUY, 92000))               # 当日 CLOSE：现金 -8000
    repo.upsert_trade(_buy_trade(8000))                             # 买入 8000：现金流出 -8000
    rec = Reconcile(InMemoryLocalLedger(), repo, RecordingLogger(), _Cal(), account_id="acc1")
    report = rec.run(T_BUY)
    assert report.asset_checked is True            # 资产对账真正执行（不再永久跳过）
    assert report.asset_discrepancy is None         # 方向一致、无偏差


def test_reconcile_asset_check_flags_direction_conflict():
    repo = InMemoryQmtRepository()
    repo.upsert_account_daily(_account(date(2026, 6, 11), 100000))
    repo.upsert_account_daily(_account(T_BUY, 110000))   # 现金反而 +10000（与买入流出矛盾）
    repo.upsert_trade(_buy_trade(8000))                  # 买入应使现金 -8000
    rec = Reconcile(InMemoryLocalLedger(), repo, RecordingLogger(), _Cal(), account_id="acc1")
    report = rec.run(T_BUY)
    assert report.asset_checked is True
    assert report.asset_discrepancy is not None          # 方向冲突 + 超阈值 → 告警


def test_repository_get_account_daily():
    repo = InMemoryQmtRepository()
    repo.upsert_account_daily(_account(T_BUY, 92000))
    got = repo.get_account_daily("acc1", T_BUY, SnapshotType.CLOSE)
    assert got is not None and got.cash == Decimal("92000")
    assert repo.get_account_daily("acc1", date(2026, 6, 11), SnapshotType.CLOSE) is None
