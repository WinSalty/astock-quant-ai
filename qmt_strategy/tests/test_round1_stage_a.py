"""第二轮评审修复 · 阶段A 回归锁定测试（资金安全与状态可恢复）。

覆盖：
- 写队列 fail-closed：flush_confirm 区分"已 drain"与"已持久"（#1）、写线程死亡判失败+告警（#2）；
- 持仓状态机崩溃/断线后可重建（#6/#29/#30/#38）、再买入命中 SOLD 开新单元（#13）、卖出卡死复位（#11/#12/#31）；
- order_executor 内存运行态重建（#28/#32/#40/#69）、SUBMITTED 超时撤单（#8）、已承诺金额按成交+剩余计（#92）；
- 回调顺序：qmt_trade 镜像失败不丢台账/持仓事实（#62）；
- 日初回撤基线同日重启复用（#19）、REDUCE 整手（#37）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import (
    OrderPhase,
    OrderState,
    OrderStatus,
    PositionState,
    SellActionType,
    TradeSide,
)
from qmt_strategy.contracts.models import LedgerEntry, PositionRecord
from qmt_strategy.contracts.xt_objects import (
    FakeStockAccount,
    FakeXtAsset,
    FakeXtOrder,
    FakeXtPosition,
    FakeXtTrade,
)
from qmt_strategy.order.local_ledger import InMemoryLocalLedger
from qmt_strategy.order.order_executor import OrderExecutor
from qmt_strategy.position.position_manager import PositionManager
from qmt_strategy.storage.write_queue import AsyncWriteQueue

from conftest import T_BUY, T_SELL, T_SIGNAL, utc_at_east8

T_NEXT = date(2026, 6, 16)  # T_SELL 的下一交易日


# ===========================================================================
# 写队列 fail-closed（#1 / #2）
# ===========================================================================
class _OkConn:
    """提交永远成功的假连接。"""

    def __init__(self):
        self.commits = 0

    def execute(self, *a):
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


class _FailCommitConn(_OkConn):
    """commit 永远抛错的假连接（模拟磁盘满/锁/约束失败）。"""

    def commit(self):
        raise OSError("disk I/O error")


def test_flush_confirm_true_when_all_committed():
    """#1：写任务全部 commit 成功 → flush_confirm 返回 True。"""
    q = AsyncWriteQueue(lambda: _OkConn(), RecordingLogger(), name="t")
    q.start()
    try:
        q.submit(lambda c: c.execute("x"))
        assert q.flush_confirm(timeout=2.0) is True
    finally:
        q.stop()


def test_flush_confirm_false_when_commit_fails():
    """#1：写任务 commit 失败（被 rollback+task_done）→ flush_confirm 必判 False（不再误报成功）。"""
    q = AsyncWriteQueue(lambda: _FailCommitConn(), RecordingLogger(), name="t")
    q.start()
    try:
        q.submit(lambda c: c.execute("x"))
        assert q.flush_confirm(timeout=2.0) is False
        # 旧 flush（只看 drain）仍会返回 True——证明 flush_confirm 才是"已持久"判据。
        assert q.flush(timeout=2.0) is True
    finally:
        q.stop()


def test_flush_confirm_false_and_alerts_on_dead_writer():
    """#2：写线程死亡 → flush_confirm 判失败 + 触发 on_failure 告警（fail-closed）。"""
    alerts = []
    q = AsyncWriteQueue(lambda: _OkConn(), RecordingLogger(), name="t",
                        on_failure=lambda r: alerts.append(r))
    q.start()
    try:
        q._failed.set()  # 模拟运行期写线程意外失效
        assert q.flush_confirm(timeout=1.0) is False
        assert alerts == ["write_queue_dead"]
    finally:
        q.stop()


def test_set_on_failure_late_binding():
    """#2：装配后 set_on_failure 接线（Engine 在 LocalStorage 之后装配）也能触发告警。"""
    alerts = []
    q = AsyncWriteQueue(lambda: _OkConn(), RecordingLogger(), name="t")
    q.set_on_failure(lambda r: alerts.append(r))
    q.start()
    try:
        q._failed.set()
        assert q.flush_confirm(timeout=0.5) is False
        assert alerts == ["write_queue_dead"]
    finally:
        q.stop()


# ===========================================================================
# 持仓状态机重建 / 复位（#6 / #29 / #30 / #38 / #13 / #11 / #12 / #31）
# ===========================================================================
def _pm(calendar):
    return PositionManager(calendar, FakeClock(utc_at_east8(T_BUY, 9, 30)), RecordingLogger())


def _posrec(ts_code, volume, can_use, avg="10.00", account_id="acc1"):
    return PositionRecord(
        account_id=account_id, trade_date=T_BUY, ts_code=ts_code, qmt_stock_code=ts_code,
        volume=volume, can_use_volume=can_use, avg_price=Decimal(avg),
    )


def test_rebuild_creates_sellable_overnight_unit(calendar):
    """#6/#30：进程重启/断线后用 QMT 权威持仓重建——隔夜可卖持仓必进卖出决策集合。"""
    pm = _pm(calendar)
    n = pm.rebuild_from_broker_positions([_posrec("600000.SH", 1000, 1000)], T_BUY)
    assert n == 1
    units = pm.sellable_units(T_BUY)
    assert [u.ts_code for u in units] == ["600000.SH"]
    assert units[0].can_use_volume == 1000
    assert units[0].state == PositionState.HOLDING


def test_rebuild_locked_unit_not_sellable_same_day(calendar):
    """#6：QMT 可卖量为 0（当日新建仓/全冻结）→ 重建为 LOCKED_T1，当日不可卖（守 T+1）。"""
    pm = _pm(calendar)
    pm.rebuild_from_broker_positions([_posrec("600000.SH", 1000, 0)], T_BUY)
    assert pm.sellable_units(T_BUY) == []
    u = pm.get_unit("acc1", "600000.SH")
    assert u.state == PositionState.LOCKED_T1


def test_rebuild_calibrates_existing_can_use(calendar):
    """#29/#38：apply_position_snapshot 口径接线——已有单元用 QMT 权威可卖量校准。"""
    pm = _pm(calendar)
    fill = FakeXtTrade(stock_code="600000.SH", traded_id="t1", traded_price=Decimal("10"), traded_volume=1000)
    pm.mark_position_on_fill(fill, T_BUY, account_id="acc1", ts_code="600000.SH")
    pm.refresh_state(T_SELL)  # 跨买入日 → can_use=volume=1000
    assert pm.get_unit("acc1", "600000.SH").can_use_volume == 1000
    # QMT 实际只有 500 可卖（如部分冻结）→ 校准为 500。
    pm.rebuild_from_broker_positions([_posrec("600000.SH", 1000, 500)], T_SELL)
    assert pm.get_unit("acc1", "600000.SH").can_use_volume == 500


def test_rebuild_resurrects_sold_unit_when_broker_has_volume(calendar):
    """#30：本地终态 SOLD 但券商仍有量（断线期间又买入）→ 重建为可卖单元，不漏卖。"""
    pm = _pm(calendar)
    fill = FakeXtTrade(stock_code="600000.SH", traded_id="t1", traded_price=Decimal("10"), traded_volume=1000)
    u = pm.mark_position_on_fill(fill, T_BUY, account_id="acc1", ts_code="600000.SH")
    pm.refresh_state(T_SELL)
    pm.mark_selling(u)
    pm.apply_sell_fill(u, 1000)  # 全卖 → SOLD
    assert pm.get_unit("acc1", "600000.SH").state == PositionState.SOLD
    pm.rebuild_from_broker_positions([_posrec("600000.SH", 1000, 1000)], T_SELL)
    u2 = pm.get_unit("acc1", "600000.SH")
    assert u2.state == PositionState.HOLDING and u2.volume == 1000


def test_rebuy_after_sold_creates_new_unit(calendar):
    """#13：对已 SOLD 标的再买入 → 全新单元（不并入 SOLD），新仓守 T+1 后可卖。"""
    pm = _pm(calendar)
    fill = FakeXtTrade(stock_code="600000.SH", traded_id="t1", traded_price=Decimal("10"), traded_volume=1000)
    u = pm.mark_position_on_fill(fill, T_BUY, account_id="acc1", ts_code="600000.SH")
    pm.refresh_state(T_SELL)
    pm.mark_selling(u)
    pm.apply_sell_fill(u, 1000)  # SOLD
    # 次日再买入同一标的。
    fill2 = FakeXtTrade(stock_code="600000.SH", traded_id="t2", traded_price=Decimal("12"), traded_volume=500)
    u2 = pm.mark_position_on_fill(fill2, T_SELL, account_id="acc1", ts_code="600000.SH")
    assert u2.state == PositionState.LOCKED_T1 and u2.volume == 500
    assert u2.earliest_sellable_date == T_NEXT
    pm.refresh_state(T_NEXT)
    assert [x.ts_code for x in pm.sellable_units(T_NEXT)] == ["600000.SH"]


def test_revert_selling_back_to_holding(calendar):
    """#11/#12/#31：卖单终态失败 → revert_selling 把单元从 SELLING 复位回 HOLDING 供重挂。"""
    pm = _pm(calendar)
    fill = FakeXtTrade(stock_code="600000.SH", traded_id="t1", traded_price=Decimal("10"), traded_volume=1000)
    u = pm.mark_position_on_fill(fill, T_BUY, account_id="acc1", ts_code="600000.SH")
    pm.refresh_state(T_SELL)
    pm.mark_selling(u)
    assert u.state == PositionState.SELLING
    pm.revert_selling(u, reason="rejected")
    assert u.state == PositionState.HOLDING
    assert [x.ts_code for x in pm.sellable_units(T_SELL)] == ["600000.SH"]


# ===========================================================================
# order_executor：内存态重建 / TTL 撤单 / 卖单 / 已承诺金额（#28/#32/#40/#69/#8/#11/#12/#92）
# ===========================================================================
class _SellFakeTrader:
    """可配置 order_stock 返回值（含同步失败 <0），记录 cancel。"""

    def __init__(self, sell_order_id=2001):
        self.order_calls = []
        self.cancel_calls = []
        self._sell_order_id = sell_order_id

    def order_stock(self, account, code, otype, vol, ptype, price, sname="", remark=""):
        self.order_calls.append((code, otype, vol, price, remark))
        return self._sell_order_id

    def cancel_order_stock(self, account, order_id):
        self.cancel_calls.append(order_id)
        return 0

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc1", cash=Decimal("1000000"), frozen_cash=Decimal("0"),
                           market_value=Decimal("0"), total_asset=Decimal("1000000"))


def _executor(trader, clock, ledger=None, settings=None):
    return OrderExecutor(
        trader=trader, account=FakeStockAccount("acc1"), account_id="acc1",
        ledger=ledger or InMemoryLocalLedger(), settings=settings or Settings(),
        clock=clock, logger=RecordingLogger(),
    )


def _ledger_entry(biz, ts_code, state, *, side=TradeSide.BUY, order_id=None, plan_volume=1000,
                  plan_price="11.00", filled_volume=0, avg_filled=None, target=T_BUY):
    return LedgerEntry(
        biz_order_no=biz, account_id="acc1", target_trade_date=target, ts_code=ts_code,
        strategy_family="打板" if side == TradeSide.BUY else "SELL", side=side,
        plan_volume=plan_volume, plan_price=Decimal(plan_price), order_remark="r",
        signal_trade_date=T_SIGNAL, state=state, order_id=order_id,
        filled_volume=filled_volume,
        avg_filled_price=Decimal(avg_filled) if avg_filled is not None else None,
    )


def test_rebuild_runtime_state_ttl_and_order_count():
    """#28/#32/#40/#69：重启后从台账重建 TTL 截止表 + 单日下单次数计数器。"""
    led = InMemoryLocalLedger()
    led.insert(_ledger_entry("b1", "600000.SH", OrderState.SUBMITTED, order_id=10))   # 在途，应重建 TTL + 计数
    led.insert(_ledger_entry("b2", "600001.SH", OrderState.TRADED, order_id=11))       # 已成，计数不重建 TTL
    led.insert(_ledger_entry("b3", "600002.SH", OrderState.PLANNED))                    # 未发单，不计数
    clock = FakeClock(utc_at_east8(T_BUY, 10, 0))
    ex = _executor(_SellFakeTrader(), clock, ledger=led)
    ex.rebuild_runtime_state()
    assert "b1" in ex._ttl_deadline and "b2" not in ex._ttl_deadline and "b3" not in ex._ttl_deadline
    assert ex._orders_count_by_date[T_BUY] == 2  # SUBMITTED + TRADED（PLANNED 不计）


def test_ttl_cancels_expired_submitted_order():
    """#8：SUBMITTED 单到期 → on_ttl_expired 真正撤单（不再被 sweep 移出截止表却从不撤）。"""
    led = InMemoryLocalLedger()
    led.insert(_ledger_entry("b1", "600000.SH", OrderState.SUBMITTED, order_id=10))
    clock = FakeClock(utc_at_east8(T_BUY, 10, 0))
    trader = _SellFakeTrader()
    ex = _executor(trader, clock, ledger=led)
    ex._ttl_deadline["b1"] = utc_at_east8(T_BUY, 9, 59)  # 已过期
    handled = ex.sweep_expired(now=utc_at_east8(T_BUY, 10, 0))
    assert handled == ["b1"]
    assert trader.cancel_calls == [10]  # 真正撤单


def test_place_sell_returns_none_on_sync_failure():
    """#11：卖单同步下单失败（order_id<0）→ place_sell 返回 None（编排层据此不置 SELLING 卡死）。"""
    clock = FakeClock(utc_at_east8(T_BUY, 10, 0))
    trader = _SellFakeTrader(sell_order_id=-1)
    ex = _executor(trader, clock)
    biz = ex.place_sell("600000.SH", T_BUY, T_SIGNAL, 1000, Decimal("10.00"), "止损")
    assert biz is None


def test_place_sell_registers_ttl_and_sweep_cancels():
    """#12：卖单登记 TTL，挂不到价到期被 sweep 撤单（→ 回报触发持仓 revert 重挂）。"""
    clock = FakeClock(utc_at_east8(T_BUY, 10, 0))
    trader = _SellFakeTrader(sell_order_id=2001)
    ex = _executor(trader, clock)
    biz = ex.place_sell("600000.SH", T_BUY, T_SIGNAL, 1000, Decimal("10.00"), "止损",
                        order_phase=OrderPhase.OPENING)
    assert biz in ex._ttl_deadline
    # 推进时钟越过 TTL（默认 order_ttl_seconds=60）→ sweep 撤单。
    ex.sweep_expired(now=utc_at_east8(T_BUY, 10, 2))
    assert trader.cancel_calls == [2001]


def test_committed_amount_filled_plus_remaining():
    """#92：已承诺金额 = 已成(按成交均价) + 剩余(按计划价)，不再对部成单全额计入。"""
    led = InMemoryLocalLedger()
    led.insert(_ledger_entry("b1", "600000.SH", OrderState.PART_TRADED, order_id=10,
                             plan_volume=1000, plan_price="11.00", filled_volume=400, avg_filled="10.50"))
    ex = _executor(_SellFakeTrader(), FakeClock(utc_at_east8(T_BUY, 10, 0)), ledger=led)
    # 400×10.50 + 600×11.00 = 4200 + 6600 = 10800（旧口径会算 1000×11=11000）。
    assert ex.committed_amount(T_BUY) == Decimal("10800.00")


def test_order_id_persisted_after_place(tmp_path):
    """#7：发单成功后 order_id 同步落盘——重启读盘能据 order_id 关联（不产生孤儿单）。"""
    import sqlite3

    from qmt_strategy.storage.schema import init_db
    from qmt_strategy.storage.sqlite_ledger import PersistentLocalLedger

    db = str(tmp_path / "q.db")
    init_db(sqlite3.connect(db))
    wq = AsyncWriteQueue(lambda: sqlite3.connect(db), RecordingLogger())
    wq.start()
    try:
        led = PersistentLocalLedger(db, wq, RecordingLogger())
        clock = FakeClock(utc_at_east8(T_BUY, 10, 0))
        ex = _executor(_SellFakeTrader(sell_order_id=2001), clock, ledger=led)
        ex.place_sell("600000.SH", T_BUY, T_SIGNAL, 1000, Decimal("10.00"), "止损")
        # 不显式 flush：place_sell 内部 _sync_persist_before_order 已确认落盘。
        led2 = PersistentLocalLedger(db, wq, RecordingLogger())
        led2.load_from_db()
        rebuilt = led2.get_by_order_id(2001)
        assert rebuilt is not None and rebuilt.order_id == 2001
    finally:
        wq.stop()


# ===========================================================================
# 回调顺序：镜像失败不丢台账/持仓（#62）
# ===========================================================================
class _RaisingDataWriter:
    """upsert_trade 抛错的写端（模拟 qmt_trade 镜像落库异常）。"""

    def upsert_trade(self, rec):
        raise RuntimeError("qmt_trade mirror down")

    def upsert_order(self, rec):
        pass

    def upsert_position(self, rec, st):
        pass

    def upsert_account_daily(self, rec, st):
        pass

    def mark_cancel_failed(self, *a):
        pass


def test_on_stock_trade_updates_ledger_even_if_mirror_fails():
    """#62：qmt_trade 镜像 upsert 抛错时，台账 add_fill 与持仓回写仍生效（成交事实不丢）。"""
    from qmt_strategy.data_writer.callbacks import ExecCallback

    led = InMemoryLocalLedger()
    led.insert(_ledger_entry("b1", "600000.SH", OrderState.SUBMITTED, order_id=10, plan_volume=1000))
    sunk = []
    cb = ExecCallback(
        _RaisingDataWriter(), led, RecordingLogger(),
        account_id="acc1", trade_date_provider=lambda: T_BUY,
        position_sink=lambda rec: sunk.append(rec),
    )
    t = FakeXtTrade(stock_code="600000.SH", traded_id="x1", order_id=10,
                    order_type=23, traded_price=Decimal("10.00"), traded_volume=1000,
                    traded_time=None)
    cb.on_stock_trade(t)  # 不应抛出
    assert led.get("b1").filled_volume == 1000   # 台账累计成交成功
    assert len(sunk) == 1                         # 持仓回写仍被调用


def test_on_stock_order_reverts_sell_unit_on_cancel():
    """#31：卖单回报 CANCELLED 且零成交 → 经 sell_revert_sink 复位持仓 SELLING 态。"""
    from qmt_strategy.data_writer.callbacks import ExecCallback

    led = InMemoryLocalLedger()
    led.insert(_ledger_entry("s1", "600000.SH", OrderState.SUBMITTED, side=TradeSide.SELL, order_id=20))
    reverted = []
    cb = ExecCallback(
        _RaisingDataWriter(), led, RecordingLogger(),
        account_id="acc1", trade_date_provider=lambda: T_BUY,
        sell_revert_sink=lambda ts: reverted.append(ts),
    )
    o = FakeXtOrder(stock_code="600000.SH", order_id=20, order_type=24,
                    order_status="CANCELLED", order_volume=1000, traded_volume=0, order_time=None)
    cb.on_stock_order(o)
    assert reverted == ["600000.SH"]


# ===========================================================================
# Engine 级：日初基线同日重启复用（#19）、REDUCE 整手（#37）
# ===========================================================================
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


class _StubTick:
    def get_full_tick(self, codes):
        return {}


class _CfgTrader:
    """可配置 total_asset 与持仓的假 trader。"""

    def __init__(self, total_asset, positions=None):
        self._ta = Decimal(str(total_asset))
        self._positions = positions or []
        self.order_calls = []

    def order_stock(self, account, code, otype, vol, ptype, price, sname="", remark=""):
        self.order_calls.append((code, otype, vol, price))
        return 100 + len(self.order_calls)

    def cancel_order_stock(self, account, oid):
        return 0

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc1", cash=self._ta, frozen_cash=Decimal("0"),
                           market_value=Decimal("0"), total_asset=self._ta)

    def query_stock_positions(self, account):
        return self._positions

    def query_stock_orders(self, account):
        return []

    def query_stock_trades(self, account):
        return []


def _engine(trader, repository=None):
    from qmt_strategy.app.main import EngineDeps, build_engine
    from qmt_strategy.data_writer.repository import InMemoryQmtRepository
    from qmt_strategy.watchlist.sources import CallableSelectedStockSource

    repo = repository or InMemoryQmtRepository()
    deps = EngineDeps(
        settings=Settings.from_env({}),
        clock=FakeClock(utc_at_east8(T_BUY, 9, 0)),
        logger=RecordingLogger(),
        calendar=_Cal(),
        trader=trader,
        account=FakeStockAccount("acc1"),
        account_id="acc1",
        tick_source=_StubTick(),
        selected_source=CallableSelectedStockSource(lambda d: []),
        repository=repo,
    )
    return build_engine(deps), repo


def test_day_open_equity_reused_across_restart():
    """#19：同日盘中重启 prewarm 重跑时，复用已持久化的 OPEN 基线，绝不把当前(已亏损)资产当日初基线。"""
    repo = None
    eng1, repo = _engine(_CfgTrader(1_000_000))
    eng1.prewarm(T_BUY)
    assert eng1._day_open_equity == Decimal("1000000")
    # 同日盘中崩溃重启：新进程权益已亏到 80 万；应读回持久化的 100 万基线而非用 80 万。
    eng2, _ = _engine(_CfgTrader(800_000), repository=repo)
    eng2.prewarm(T_BUY)
    assert eng2._day_open_equity == Decimal("1000000")


def test_reduce_sell_rounds_down_to_round_lot():
    """#37：REDUCE 减仓量向下取整到 100 股，避免奇数股废单。"""
    from qmt_strategy.contracts.models import OrderBook

    pos = FakeXtPosition(stock_code="600000.SH", volume=1050, can_use_volume=1050,
                         avg_price=Decimal("10.00"))
    trader = _CfgTrader(1_000_000, positions=[pos])
    eng, _ = _engine(trader)
    eng.prewarm(T_BUY)  # rebuild 用 QMT 权威建可卖单元 can_use=1050
    unit = eng._position.get_unit("acc1", "600000.SH")
    assert unit is not None and unit.can_use_volume == 1050 and unit.state == PositionState.HOLDING
    book = OrderBook(ts_code="600000.SH", last_price=Decimal("9.50"))
    ok = eng._place_sell(unit, SellActionType.REDUCE, Decimal("0.5"), "减仓", book, T_BUY)
    assert ok is True
    # int(1050*0.5)=525 → //100*100 = 500（整手）。
    assert trader.order_calls[-1][2] == 500


def test_run_sell_pass_isolates_per_unit_failure():
    """评审复审 P1：单只票卖出评估异常被隔离，不中断整轮——其余该卖的票照常卖出。"""
    from qmt_strategy.contracts.models import OrderBook, SellAction

    pos1 = FakeXtPosition(stock_code="600000.SH", volume=1000, can_use_volume=1000, avg_price=Decimal("10"))
    pos2 = FakeXtPosition(stock_code="600001.SH", volume=1000, can_use_volume=1000, avg_price=Decimal("10"))
    trader = _CfgTrader(1_000_000, positions=[pos1, pos2])
    eng, _ = _engine(trader)
    eng.prewarm(T_BUY)

    class _Decider:
        def decide_intraday(self, unit, prior, book, risk_verdict=None):
            if unit.ts_code == "600000.SH":
                raise RuntimeError("boom")  # 第一只票决策抛错
            return SellAction(ts_code=unit.ts_code, action=SellActionType.CLEAR, reason="清仓")

        def decide_auction(self, *a, **k):
            return SellAction(ts_code="x", action=SellActionType.HOLD, reason="")

    eng._sell = _Decider()
    books = {
        "600000.SH": OrderBook(ts_code="600000.SH", last_price=Decimal("9")),
        "600001.SH": OrderBook(ts_code="600001.SH", last_price=Decimal("9")),
    }
    sold = eng.run_sell_pass(T_BUY, books, session="intraday")
    assert sold == ["600001.SH"]  # 第一只抛错被隔离，第二只照常卖


def test_concurrent_rebuild_and_iteration_no_crash(calendar):
    """评审复审 P1：回调线程 rebuild 批量改 _units 与调度线程迭代并发，加锁后不抛"字典 size 改变"。"""
    import threading

    pm = _pm(calendar)
    base = [_posrec(f"6000{i:02d}.SH", 1000, 1000) for i in range(20)]
    pm.rebuild_from_broker_positions(base, T_BUY)
    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        while not stop.is_set():
            try:
                extra = [_posrec(f"60010{j:02d}.SH", 1000, 1000) for j in range(i % 6)]
                pm.rebuild_from_broker_positions(base + extra, T_BUY)
            except Exception as e:  # noqa: BLE001 记录任何并发异常
                errors.append(repr(e))
                return
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(3000):
            pm.sellable_units(T_BUY)
            pm.refresh_state(T_BUY)
    except Exception as e:  # noqa: BLE001
        errors.append(repr(e))
    finally:
        stop.set()
        t.join(timeout=3)
    assert errors == []
