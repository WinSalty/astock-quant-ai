"""存储核心单测：schema 建表、mappers round-trip、write_queue 不阻塞/异常隔离（doc/05 阶段1）。"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime
from decimal import Decimal

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.contracts.enums import (
    DataSource,
    OrderState,
    OrderStatus,
    OrderPhase,
    SnapshotType,
    TradeSide,
)
from qmt_strategy.contracts.models import (
    AccountRecord,
    LedgerEntry,
    OrderRecord,
    PositionRecord,
    SelectedStockRow,
    TradeRecord,
)
from qmt_strategy.storage import mappers
from qmt_strategy.storage.schema import TABLE_META, init_db
from qmt_strategy.storage.write_queue import AsyncWriteQueue

T_SIGNAL = date(2026, 6, 11)
T_BUY = date(2026, 6, 12)


# ===========================================================================
# schema
# ===========================================================================
def test_init_db_creates_tables_idempotent(tmp_path):
    db = str(tmp_path / "qmt.db")
    conn = sqlite3.connect(db)
    init_db(conn)
    init_db(conn)  # 幂等：再建不报错
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in TABLE_META:
        assert t in names
    # WAL 生效
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


# ===========================================================================
# mappers round-trip（重点：Decimal 精度、date/datetime、枚举、JSON 字段无损）
# ===========================================================================
def test_trade_round_trip():
    rec = TradeRecord(
        account_id="acc1", trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        traded_id="t1", trade_side=TradeSide.BUY, traded_price=Decimal("35.12"), traded_volume=200,
        traded_time=datetime(2026, 6, 12, 5, 31, 2), traded_time_east8=datetime(2026, 6, 12, 13, 31, 2),
        order_id=123, traded_amount=Decimal("7024.00"), signal_trade_date=T_SIGNAL,
        data_source=DataSource.CALLBACK,
    )
    back = mappers.row_to_trade(mappers.trade_to_row(rec))
    assert back.traded_price == Decimal("35.12")        # 精度无损
    assert back.trade_side == TradeSide.BUY
    assert back.traded_time == datetime(2026, 6, 12, 5, 31, 2)
    assert back.traded_time_east8 == datetime(2026, 6, 12, 13, 31, 2)
    assert back.signal_trade_date == T_SIGNAL
    assert back.data_source == DataSource.CALLBACK


def test_order_round_trip():
    rec = OrderRecord(
        account_id="acc1", trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        order_id=9, trade_side=TradeSide.SELL, order_volume=100, order_status=OrderStatus.PART_TRADED,
        traded_volume=60, order_price=Decimal("11.00"), cancel_failed=True, signal_trade_date=T_SIGNAL,
    )
    back = mappers.row_to_order(mappers.order_to_row(rec))
    assert back.order_status == OrderStatus.PART_TRADED
    assert back.trade_side == TradeSide.SELL
    assert back.cancel_failed is True
    assert back.order_price == Decimal("11.00")
    assert back.traded_volume == 60


def test_position_and_account_round_trip():
    p = PositionRecord(
        account_id="acc1", trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        snapshot_type=SnapshotType.CLOSE, volume=200, can_use_volume=0, open_price=Decimal("10.50"),
    )
    pb = mappers.row_to_position(mappers.position_to_row(p))
    assert pb.snapshot_type == SnapshotType.CLOSE and pb.open_price == Decimal("10.50")
    a = AccountRecord(account_id="acc1", trade_date=T_BUY, total_asset=Decimal("100000.50"),
                      cash=Decimal("92000"), snapshot_type=SnapshotType.CLOSE)
    ab = mappers.row_to_account(mappers.account_to_row(a))
    assert ab.total_asset == Decimal("100000.50") and ab.snapshot_type == SnapshotType.CLOSE


def test_ledger_round_trip_counted_ids():
    e = LedgerEntry(
        biz_order_no="20260612_600036.SH_打板_001", account_id="acc1", target_trade_date=T_BUY,
        ts_code="600036.SH", strategy_family="打板", side=TradeSide.BUY, plan_volume=1000,
        plan_price=Decimal("11.00"), order_remark="LUP|2026-06-11|600036.SH", signal_trade_date=T_SIGNAL,
        state=OrderState.PART_TRADED, order_id=555, filled_volume=600, avg_filled_price=Decimal("11.00"),
        order_phase=OrderPhase.AUCTION, counted_trade_ids={"a", "b"},
    )
    back = mappers.row_to_ledger(mappers.ledger_to_row(e))
    assert back.state == OrderState.PART_TRADED
    assert back.counted_trade_ids == {"a", "b"}        # 去重集合无损
    assert back.avg_filled_price == Decimal("11.00")
    assert back.order_phase == OrderPhase.AUCTION


def test_selected_round_trip_json_fields():
    r = SelectedStockRow(
        ts_code="600036.SH", trade_date=T_SIGNAL, target_trade_date=T_BUY,
        market_state="启动", tradable_flag=True, continuation_prob=Decimal("0.6"),
        fail_conditions=["竞价弱开", "炸板未回封"], signal_close=Decimal("10.00"),
        limit_up_price=Decimal("11.00"),
    )
    back = mappers.row_to_selected(mappers.selected_to_row(r))
    assert back.tradable_flag is True
    assert back.fail_conditions == ["竞价弱开", "炸板未回封"]
    assert back.continuation_prob == Decimal("0.6")
    assert back.limit_up_price == Decimal("11.00")


def test_selected_round_trip_data_missing_sentinel():
    """评审 Stage B 修复：data_missing/data_missing_reason 经 mapper round-trip 无损（默认 False/None 兼容旧行）。"""
    miss = SelectedStockRow(
        ts_code="600036.SH", trade_date=T_SIGNAL, target_trade_date=T_BUY,
        tradable_flag=False, data_missing=True, data_missing_reason="missing:close,open_times",
    )
    back = mappers.row_to_selected(mappers.selected_to_row(miss))
    assert back.data_missing is True
    assert back.data_missing_reason == "missing:close,open_times"
    # 默认（无缺测）行：data_missing=False、reason=None
    ok = SelectedStockRow(ts_code="600036.SH", trade_date=T_SIGNAL, target_trade_date=T_BUY)
    back_ok = mappers.row_to_selected(mappers.selected_to_row(ok))
    assert back_ok.data_missing is False
    assert back_ok.data_missing_reason is None


# ===========================================================================
# write_queue —— 关键不变量：不阻塞 + 异常只吞不传播 + FIFO + flush
# ===========================================================================
class _FakeConn:
    def __init__(self):
        self.commits = 0
    def commit(self):
        self.commits += 1
    def rollback(self):
        pass
    def close(self):
        pass


def _queue():
    return AsyncWriteQueue(lambda: _FakeConn(), RecordingLogger(), name="test-writer")


def test_submit_non_blocking_even_when_worker_blocked():
    """关键不变量①：写线程被任务卡住时,submit 仍立即返回(交易热路径不阻塞)。"""
    q = _queue()
    q.start()
    try:
        block = threading.Event()
        # 第一个任务把写线程卡住（模拟磁盘/锁慢）。
        q.submit(lambda conn: block.wait(2.0))
        # 再提交一批，测量 submit 耗时——即便写线程仍卡在第一个任务上，submit 也应瞬时返回。
        t0 = time.monotonic()
        for _ in range(50):
            q.submit(lambda conn: None)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"submit 阻塞了 {elapsed:.3f}s（应近乎瞬时）"
        block.set()
    finally:
        q.stop()


def test_worker_exception_isolated_and_survives():
    """关键不变量②：任务抛错只吞不传播,且写线程存活、后续任务照常执行。"""
    q = _queue()
    q.start()
    try:
        done = []
        # 这个 submit 本身不应抛错（异常发生在写线程内）。
        q.submit(lambda conn: (_ for _ in ()).throw(RuntimeError("boom")))
        q.submit(lambda conn: done.append("after-error"))
        assert q.flush(timeout=2.0) is True
        assert done == ["after-error"]      # 写线程没被毒死，后续任务照常
    finally:
        q.stop()


def test_fifo_order():
    q = _queue()
    q.start()
    try:
        out = []
        for i in range(20):
            q.submit(lambda conn, n=i: out.append(n))
        assert q.flush(timeout=2.0) is True
        assert out == list(range(20))
    finally:
        q.stop()


def test_flush_drains_then_stop():
    q = _queue()
    q.start()
    out = []
    for i in range(5):
        q.submit(lambda conn, n=i: out.append(n))
    assert q.flush(timeout=2.0) is True
    assert len(out) == 5
    q.stop()
