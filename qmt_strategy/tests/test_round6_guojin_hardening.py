"""阶段0-B 健壮性加固单测（国金对接核对 T0.4）。

覆盖：
- normalize._STATUS_NUM_MAP 补 255(ORDER_UNKNOWN)→REJECTED；
- order_executor.sweep_expired：撤单 cancel 抛异常不打断整轮巡检（同批其它到期单仍被撤）；
- LocalStorage：父目录不存在的 db 路径 start() 自建目录不崩；
- LocalStorage.sync_to_remote：部分回流失败时升级 error 告警（remote_sync_incomplete）；
- LocalStorage.resync_pending：扫描历史 synced=0 残留并补同步。
全部 fake/内存，不连真实 xtquant/MySQL。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import (
    DataSource,
    EntryAction,
    OrderPhase,
    OrderStatus,
    SnapshotType,
    TradeSide,
)
from qmt_strategy.contracts.models import (
    AccountRecord,
    EntryDecision,
    OrderRecord,
    PositionRecord,
    TradeRecord,
)
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset
from qmt_strategy.data_writer.normalize import default_status_resolver
from qmt_strategy.order.local_ledger import InMemoryLocalLedger
from qmt_strategy.order.order_executor import OrderExecutor
from qmt_strategy.storage import mappers
from qmt_strategy.storage.local_stack import LocalStorage
from qmt_strategy.storage.sqlite_sql import build_upsert, params_for

from conftest import utc_at_east8

ACCOUNT_ID = "acc1"
T_SIGNAL = date(2026, 6, 11)
T_BUY = date(2026, 6, 12)


# ===========================================================================
# 1. 委托状态 255(ORDER_UNKNOWN) → REJECTED
# ===========================================================================
def test_status_resolver_maps_255_unknown_to_rejected():
    """255 显式映射为终态 REJECTED（而非兜底 REPORTED），促发卖单 SELLING 复位/释放名额。"""
    assert default_status_resolver(255) == OrderStatus.REJECTED
    assert default_status_resolver("255") == OrderStatus.REJECTED   # 字符串数字经 _to_int 也命中
    # 未知的其它数值仍走兜底 REPORTED（在途留痕，不臆造终态）。
    assert default_status_resolver(999) == OrderStatus.REPORTED


# ===========================================================================
# 2. 撤单异常不打断整轮 sweep
# ===========================================================================
class _RaisingCancelTrader:
    """order_stock 正常返回 oid；cancel_order_stock 记录后【抛异常】（模拟券商对已成/不存在单撤单报错）。"""

    def __init__(self):
        self.order_calls = []
        self.cancel_calls = []
        self._oid = 2001

    def order_stock(self, account, stock_code, order_type, order_volume, price_type, price,
                    strategy_name="", order_remark=""):
        oid = self._oid
        self._oid += 1
        self.order_calls.append(stock_code)
        return oid

    def cancel_order_stock(self, account, order_id):
        self.cancel_calls.append(order_id)
        raise RuntimeError("broker rejects cancel of filled/unknown order_id")

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc", cash=Decimal("1000000"), frozen_cash=Decimal("0"),
                           market_value=Decimal("0"), total_asset=Decimal("1000000"))


def _opening_decision(ts_code: str) -> EntryDecision:
    return EntryDecision(
        ts_code=ts_code,
        signal_trade_date=T_SIGNAL,
        target_trade_date=T_BUY,
        strategy_family="打板",
        setup="连板接力",
        action=EntryAction.CHASE_LIMIT_UP,
        decided_at=utc_at_east8(T_BUY, 9, 31, 0),
        reason="test",
        limit_price=Decimal("11.00"),
        plan_volume=1000,
        order_phase=OrderPhase.OPENING,
        factors_snapshot={},
        next_best=(),
    )


def test_sweep_cancel_exception_does_not_break_loop():
    """两只到期单,第一单 cancel 抛异常:不得冒泡打断整轮,第二单仍被尝试撤(资金/名额不被漏撤占用)。"""
    clock = FakeClock(utc_at_east8(T_BUY, 9, 31, 0))   # 开盘后可撤段
    trader = _RaisingCancelTrader()
    logger = RecordingLogger()
    ex = OrderExecutor(
        trader=trader,
        account=FakeStockAccount(account_id=ACCOUNT_ID),
        account_id=ACCOUNT_ID,
        ledger=InMemoryLocalLedger(),
        settings=Settings(),
        clock=clock,
        logger=logger,
    )
    b1 = ex.place(_opening_decision("600036.SH"))
    b2 = ex.place(_opening_decision("000001.SZ"))
    assert b1 and b2

    # 越过 TTL(now+60s=09:32)后在 09:33 sweep：cancel 全抛异常,但循环不得中断。
    handled = ex.sweep_expired(now=utc_at_east8(T_BUY, 9, 33, 0))

    assert set(handled) == {b1, b2}                 # 两单都被处理(异常未打断循环)
    assert len(trader.cancel_calls) == 2            # 两单都真的尝试了撤单
    fails = [e for _lvl, e, _f in logger.records if e == "order_cancel_failed"]
    assert len(fails) == 2                          # 两次撤单异常均被留痕、未冒泡


# ===========================================================================
# 3. LocalStorage 父目录守卫（makedirs）
# ===========================================================================
def test_local_storage_makedirs_nested_path(tmp_path):
    """db 路径父目录不存在时 start() 自建目录、不抛 OperationalError。"""
    nested = tmp_path / "a" / "b" / "c" / "q.db"
    assert not nested.parent.exists()
    stack = LocalStorage(str(nested), RecordingLogger(), ACCOUNT_ID)
    stack.start()
    try:
        assert nested.parent.exists()
    finally:
        stack.stop()


# ===========================================================================
# 4 + 5. SYNC 部分失败分级告警 + resync_pending 补同步
# ===========================================================================
class _FlakyRepo:
    """每类 upsert 的前 fail_times 次抛异常,之后恢复（验证部分失败告警 + 重同步补齐）。"""

    def __init__(self, fail_times: int = 1):
        self._left = {
            "upsert_trade": fail_times, "upsert_order": fail_times,
            "upsert_position": fail_times, "upsert_account_daily": fail_times,
        }

    def _maybe_fail(self, name: str) -> None:
        if self._left.get(name, 0) > 0:
            self._left[name] -= 1
            raise RuntimeError("remote down: %s" % name)

    def upsert_trade(self, rec):
        self._maybe_fail("upsert_trade")

    def upsert_order(self, rec):
        self._maybe_fail("upsert_order")

    def upsert_position(self, rec):
        self._maybe_fail("upsert_position")

    def upsert_account_daily(self, rec):
        self._maybe_fail("upsert_account_daily")


def _trade() -> TradeRecord:
    return TradeRecord(
        account_id=ACCOUNT_ID, trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        traded_id="t1", trade_side=TradeSide.BUY, traded_price=Decimal("35.12"), traded_volume=200,
        signal_trade_date=T_SIGNAL, data_source=DataSource.CALLBACK,
    )


def _order() -> OrderRecord:
    return OrderRecord(
        account_id=ACCOUNT_ID, trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        order_id=123, trade_side=TradeSide.BUY, order_volume=200, order_status=OrderStatus.TRADED,
        traded_volume=200, signal_trade_date=T_SIGNAL, data_source=DataSource.CALLBACK,
    )


def _pos() -> PositionRecord:
    return PositionRecord(
        account_id=ACCOUNT_ID, trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        snapshot_type=SnapshotType.CLOSE, volume=200, can_use_volume=0,
        avg_price=Decimal("35.12"), market_value=Decimal("7024.00"), data_source=DataSource.QUERY,
    )


def _acct() -> AccountRecord:
    return AccountRecord(
        account_id=ACCOUNT_ID, trade_date=T_BUY, total_asset=Decimal("100000.50"),
        cash=Decimal("92000.00"), snapshot_type=SnapshotType.CLOSE, market_value=Decimal("7024.00"),
        data_source=DataSource.QUERY,
    )


def _seed_one_each(stack: LocalStorage) -> None:
    """经 LocalStorage 内部写队列把四表各写入一行(build_upsert INSERT 时 synced=0)。"""
    seed = {
        "qmt_trade": (_trade(), mappers.trade_to_row),
        "qmt_order": (_order(), mappers.order_to_row),
        "qmt_position_snapshot": (_pos(), mappers.position_to_row),
        "qmt_account_daily": (_acct(), mappers.account_to_row),
    }
    for table, (rec, to_row) in seed.items():
        sql, _cols = build_upsert(table)
        params = params_for(table, to_row(rec))

        def _task(conn, _sql=sql, _params=params):
            conn.execute(_sql, _params)

        stack._wq.submit(_task)
    assert stack.flush()


def test_sync_incomplete_alerts_and_resync_pending_recovers(tmp_path):
    """首次同步部分失败→ok=False + error 告警;resync_pending 扫到残留交易日并补齐→ok=True。"""
    logger = RecordingLogger()
    flaky = _FlakyRepo(fail_times=1)   # 每类首次失败 → 当日四表各 1 行全失败
    stack = LocalStorage(str(tmp_path / "q.db"), logger, ACCOUNT_ID, remote_repo=flaky)
    stack.start()
    try:
        _seed_one_each(stack)

        # 首次同步：四表各唯一行的 upsert 首次均失败 → 全部 remaining、ok=False，且 error 告警可见。
        rep1 = stack.sync_to_remote(T_BUY)
        assert rep1["ok"] is False
        event_names = [e for _lvl, e, _f in logger.records]
        assert "remote_sync_incomplete" in event_names
        assert "remote_sync_done" not in event_names      # ok=False 不应走 info 完成态
        # 且该告警是 error 级（运维可见）。
        assert any(lvl == "error" and e == "remote_sync_incomplete" for lvl, e, _f in logger.records)

        # 重同步：pending_dates 扫到 T_BUY；FlakyRepo 已不再失败 → 补齐 ok=True。
        rep2 = stack.resync_pending()
        assert rep2["ok"] is True
        assert rep2["resynced_dates"] == 1
        assert rep2["reports"][T_BUY.isoformat()]["ok"] is True
    finally:
        stack.stop()
