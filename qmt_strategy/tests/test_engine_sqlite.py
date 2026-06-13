"""本地化方案集成测试（doc/05 阶段3）：LocalStorage + Engine 端到端 + 重启幂等 + 盘后同步。

验证：盘前名单入本机 SQLite → 盘中异步落库(不阻塞) → close_batch 先 flush 再对账(一致读) →
重启从 SQLite 重建台账(不重复下单) → 盘后幂等同步回「远端」。全程用 fake,不连真实 xtquant/MySQL。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import List

from qmt_strategy.app.main import EngineDeps, build_engine
from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import AuctionPhase, CentroidTrend, OrderState, TradeSide
from qmt_strategy.contracts.models import AuctionSnapshot, LedgerEntry, SelectedStockRow
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset, FakeXtTrade
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.storage.local_stack import LocalStorage
from tests.conftest import T_BUY, T_SIGNAL, utc_at_east8


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
    def __init__(self):
        self.order_calls = []

    def order_stock(self, account, code, otype, vol, ptype, price, sname, remark):
        self.order_calls.append((code, vol))
        return 100

    def cancel_order_stock(self, account, order_id):
        return 0

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc1", cash=1_000_000, frozen_cash=0,
                           market_value=0, total_asset=1_000_000)

    def query_stock_positions(self, account):
        return []

    def query_stock_orders(self, account):
        return []

    def query_stock_trades(self, account):
        return []


class _StubTick:
    def get_full_tick(self, codes):
        return {}


def _watchlist_row() -> SelectedStockRow:
    r = SelectedStockRow(
        ts_code="600036.SH", trade_date=T_SIGNAL, target_trade_date=T_BUY,
        signal_close=Decimal("10.00"), limit_up_price=Decimal("11.00"),
        reasonable_open_high_low=Decimal("10.20"), reasonable_open_high_high=Decimal("10.80"),
        market_state="启动", tradable_flag=True, strategy="打板", role="龙头",
        continuation_prob=Decimal("0.6"),
    )
    r.strategy_family = "打板"
    r.setup = "首板"
    return r


def _strong_snap() -> AuctionSnapshot:
    return AuctionSnapshot(
        ts_code="600036.SH", phase=AuctionPhase.AUCTION_CANCELABLE, ts=utc_at_east8(T_BUY, 9, 16),
        open_pct=Decimal("0.10"), auction_vol_ratio=Decimal("0.5"), auction_centroid=Decimal("10.50"),
        centroid_trend=CentroidTrend.UP, is_limit_up=False, last_price=Decimal("10.50"),
        pre_close=Decimal("10.00"),
    )


def _build(db, logger, remote=None, timing="true"):
    stack = LocalStorage(db, logger, account_id="acc1", remote_repo=remote)
    stack.start()
    deps = EngineDeps(
        settings=Settings.from_env({"QMT_AUCTION_TIMING_ENABLED": timing}),
        clock=FakeClock(utc_at_east8(T_BUY, 9, 16)),
        logger=logger, calendar=_Cal(), trader=_FakeTrader(), account=FakeStockAccount("acc1"),
        account_id="acc1", tick_source=_StubTick(), selected_source=stack.watchlist_source,
        repository=stack.repository, ledger=stack.ledger, flush_hook=stack.flush,
    )
    return stack, build_engine(deps), deps


def test_end_to_end_buy_callback_persist_and_reconcile(tmp_path):
    db = str(tmp_path / "qmt.db")
    logger = RecordingLogger()
    remote = InMemoryQmtRepository()
    stack, eng, deps = _build(db, logger, remote=remote)
    try:
        # 盘前：信号侧交付的名单写入本机 SQLite。
        assert stack.save_watchlist([_watchlist_row()]) == 1
        ctx = eng.prewarm(T_BUY)
        assert "600036.SH" in ctx.tradable and ctx.open_new_position_allowed is True

        # 盘中：强开 → 竞价择时开 → 下单（写台账,异步落盘,不阻塞）。
        eng._router_sink(_strong_snap())
        assert deps.trader.order_calls == [("600036.SH", 95200)]  # 1e6/10.50 取整到 100 股

        # 成交回报：on_stock_trade → 异步落 qmt_trade + 台账 add_fill。
        eng.callback.on_stock_trade(FakeXtTrade(
            stock_code="600036.SH", traded_id="tr1", order_id=100, order_type="BUY",
            traded_price=10.50, traded_volume=95200, traded_time=0,
            order_remark="LUP|2026-06-11|600036.SH",
        ))
        # flush 后回流才可见（写后异步）。
        assert stack.flush(timeout=3.0)
        trades = stack.repository.get_trades("acc1", T_BUY)
        assert len(trades) == 1 and trades[0].traded_id == "tr1"
        assert trades[0].signal_trade_date == T_SIGNAL  # order_remark 回填

        # 收盘批次：snapshot + flush + 对账，应跑通无异常。
        eng.close_batch(T_BUY)
        assert stack.repository.get_account_daily("acc1", T_BUY) is not None  # CLOSE 资产快照已落

        # 盘后同步回「远端」：本机 qmt_trade → remote。
        report = stack.sync_to_remote(T_BUY)
        assert report["ok"] is True
        assert len(remote.get_trades("acc1", T_BUY)) == 1   # 远端已收到
        # 幂等重跑：无新数据可同步。
        report2 = stack.sync_to_remote(T_BUY)
        assert report2["qmt_trade"]["pushed"] == 0
        assert len(remote.get_trades("acc1", T_BUY)) == 1   # 不产生重复行
    finally:
        stack.stop()


def test_ledger_restart_idempotent(tmp_path):
    """重启幂等：台账落盘后,新进程从 SQLite 重建 → has_active 仍有效,不会重复下单。"""
    db = str(tmp_path / "qmt.db")
    logger = RecordingLogger()
    entry = LedgerEntry(
        biz_order_no="20260612_600036.SH_打板_001", account_id="acc1", target_trade_date=T_BUY,
        ts_code="600036.SH", strategy_family="打板", side=TradeSide.BUY, plan_volume=1000,
        plan_price=Decimal("11.00"), order_remark="LUP|2026-06-11|600036.SH", signal_trade_date=T_SIGNAL,
        state=OrderState.SUBMITTED, order_id=100,
    )
    # 进程 1：写台账并落盘。
    stack1 = LocalStorage(db, logger, account_id="acc1")
    stack1.start()
    stack1.ledger.insert(entry)
    assert stack1.flush(timeout=3.0)
    stack1.stop()

    # 进程 2（模拟重启）：同一 db 重建台账 → 活跃单仍在 → 不会重复下单。
    stack2 = LocalStorage(db, logger, account_id="acc1")
    stack2.start()
    try:
        assert stack2.ledger.has_active(T_BUY, "600036.SH", "打板") is True
        got = stack2.ledger.get("20260612_600036.SH_打板_001")
        assert got is not None and got.state == OrderState.SUBMITTED and got.order_id == 100
    finally:
        stack2.stop()
