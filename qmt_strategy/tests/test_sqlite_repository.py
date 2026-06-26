"""SqliteQmtRepository 单测（doc/05 §三 T2.1）。

覆盖要点：
- 幂等：同唯一键 upsert 两次 → 仅 1 行；
- COALESCE：后到空值不覆盖已回填的 signal_trade_date / *_east8，但其余列（data_source）取后到值；
- 跨日同 traded_id（唯一键含 trade_date）→ 落 2 行；
- mark_cancel_failed：打 cancel_failed/error_*，不改 order_status 终态；
- get_account_daily：CLOSE 行可读回，不存在的日期返回 None；
- 写入非阻塞：upsert_* 调用本身瞬时返回、不抛错（真正落盘在 flush 后才可见）。

夹具范式（务必照搬）：tmp_path 建临时 SQLite，先 init_db 建表（独立连接），
写队列在写线程内自开连接；读断言前务必 flush（写后异步）。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime
from decimal import Decimal

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.contracts.enums import (
    DataSource,
    OrderStatus,
    SnapshotType,
    TradeSide,
)
from qmt_strategy.contracts.models import (
    AccountRecord,
    OrderRecord,
    TradeRecord,
)
from qmt_strategy.storage.schema import init_db
from qmt_strategy.storage.sqlite_repository import SqliteQmtRepository
from qmt_strategy.storage.write_queue import AsyncWriteQueue

T_SIGNAL = date(2026, 6, 11)
T_BUY = date(2026, 6, 12)
T_BUY_NEXT = date(2026, 6, 15)  # 跨日（跳过周末）


# ===========================================================================
# 夹具：临时 SQLite + 写队列 + 被测仓储
# ===========================================================================
@pytest.fixture
def repo_ctx(tmp_path):
    """搭建 SQLite + AsyncWriteQueue + SqliteQmtRepository，并在用例结束后停队列。

    返回 (repo, wq)：用例用 repo 写/读，写后须 wq.flush() 再读（写后异步落盘）。
    """
    db = str(tmp_path / "q.db")
    init_db(sqlite3.connect(db))  # 先建表（独立连接，与写线程连接隔离）
    logger = RecordingLogger()
    # 写线程内开自己的连接：sqlite3 连接默认绑定创建它的线程，故 conn_factory 必须在写线程内调用。
    wq = AsyncWriteQueue(lambda: sqlite3.connect(db), logger)
    wq.start()
    repo = SqliteQmtRepository(db, wq, logger)
    try:
        yield repo, wq
    finally:
        wq.stop()


def _make_trade(*, traded_id="t1", trade_date=T_BUY, signal_trade_date=T_SIGNAL,
                traded_time_east8=datetime(2026, 6, 12, 13, 31, 2),
                data_source=DataSource.CALLBACK, account_id="acc1") -> TradeRecord:
    """构造一行成交记录，默认即「实时回调、带 signal_trade_date 与 east8」的完整票。"""
    return TradeRecord(
        account_id=account_id, trade_date=trade_date, ts_code="600036.SH",
        qmt_stock_code="600036.SH", traded_id=traded_id, trade_side=TradeSide.BUY,
        traded_price=Decimal("35.12"), traded_volume=200,
        traded_time=datetime(2026, 6, 12, 5, 31, 2), traded_time_east8=traded_time_east8,
        order_id=123, traded_amount=Decimal("7024.00"), signal_trade_date=signal_trade_date,
        data_source=data_source,
    )


def _make_order(*, order_id=9, order_status=OrderStatus.REPORTED, account_id="acc1") -> OrderRecord:
    return OrderRecord(
        account_id=account_id, trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        order_id=order_id, trade_side=TradeSide.BUY, order_volume=100, order_status=order_status,
        order_price=Decimal("11.00"), signal_trade_date=T_SIGNAL,
    )


# ===========================================================================
# 幂等：同唯一键 upsert 两次 → 仅 1 行
# ===========================================================================
def test_upsert_trade_idempotent_same_key_one_row(repo_ctx):
    repo, wq = repo_ctx
    repo.upsert_trade(_make_trade(traded_id="t1"))
    repo.upsert_trade(_make_trade(traded_id="t1"))  # 同唯一键 (acc1, 2026-06-12, t1)
    assert wq.flush(timeout=2.0) is True
    trades = repo.get_trades("acc1", T_BUY)
    assert len(trades) == 1  # 幂等：ON CONFLICT DO UPDATE，不产生重复行
    assert trades[0].traded_id == "t1"
    assert trades[0].traded_price == Decimal("35.12")


# ===========================================================================
# COALESCE：后到空值不覆盖已回填值；其余列取后到值
# ===========================================================================
def test_upsert_trade_coalesce_keeps_backfilled_values(repo_ctx):
    repo, wq = repo_ctx
    # 先写带 signal_trade_date + east8 的完整成交（CALLBACK）。
    repo.upsert_trade(_make_trade(
        traded_id="t1", signal_trade_date=T_SIGNAL,
        traded_time_east8=datetime(2026, 6, 12, 13, 31, 2), data_source=DataSource.CALLBACK,
    ))
    # 再写同 traded_id、但 signal_trade_date / east8 为 None 的兜底成交（QUERY_BACKFILL）。
    repo.upsert_trade(_make_trade(
        traded_id="t1", signal_trade_date=None, traded_time_east8=None,
        data_source=DataSource.QUERY_BACKFILL,
    ))
    assert wq.flush(timeout=2.0) is True

    trades = repo.get_trades("acc1", T_BUY)
    assert len(trades) == 1
    t = trades[0]
    # COALESCE 列：后到空值不得覆盖已回填的非空值。
    assert t.signal_trade_date == T_SIGNAL
    assert t.traded_time_east8 == datetime(2026, 6, 12, 13, 31, 2)
    # 非 COALESCE 列：取后到值（后到覆盖为最新）。
    assert t.data_source == DataSource.QUERY_BACKFILL


# ===========================================================================
# 跨日同 traded_id（唯一键含 trade_date）→ 落 2 行
# ===========================================================================
def test_cross_day_same_traded_id_two_rows(repo_ctx):
    repo, wq = repo_ctx
    repo.upsert_trade(_make_trade(traded_id="t1", trade_date=T_BUY))
    repo.upsert_trade(_make_trade(traded_id="t1", trade_date=T_BUY_NEXT))  # 不同 trade_date
    assert wq.flush(timeout=2.0) is True

    # 唯一键含 trade_date：同 traded_id 跨日 → 两行，互不覆盖（防跨日 ID 复用串号）。
    assert len(repo.get_trades("acc1", T_BUY)) == 1
    assert len(repo.get_trades("acc1", T_BUY_NEXT)) == 1


# ===========================================================================
# mark_cancel_failed：打标 cancel_failed/error_*，不改 order_status 终态
# ===========================================================================
def test_mark_cancel_failed_keeps_order_status(repo_ctx):
    repo, wq = repo_ctx
    repo.upsert_order(_make_order(order_id=9, order_status=OrderStatus.REPORTED))
    assert wq.flush(timeout=2.0) is True

    repo.mark_cancel_failed("acc1", 9, error_id=42, error_msg="撤单失败:已成交")
    assert wq.flush(timeout=2.0) is True

    orders = repo.get_orders("acc1", T_BUY)
    assert len(orders) == 1
    o = orders[0]
    assert o.cancel_failed is True
    assert o.error_id == 42
    assert o.error_msg == "撤单失败:已成交"
    assert o.order_status == OrderStatus.REPORTED  # 不改终态


def test_mark_cancel_failed_bumps_row_version(repo_ctx):
    """评审修复 SYNC-1/A1：mark_cancel_failed 与 build_upsert 同为「重置 synced=0」的写者，必须一并自增 row_version。
    否则盘后 sync 的 (synced=0 AND row_version=读时版本) CAS 会因 0→0、版本未变而误命中，把迟到撤单失败带回的
    cancel_failed/error_* 误标 synced=1、永不再推远端。"""
    repo, wq = repo_ctx
    repo.upsert_order(_make_order(order_id=9, order_status=OrderStatus.REPORTED))
    assert wq.flush(timeout=2.0) is True

    def _rv_synced():
        conn = sqlite3.connect(repo._db_path)
        try:
            return conn.execute(
                "SELECT row_version, synced FROM qmt_order WHERE account_id='acc1' AND order_id=9"
            ).fetchone()
        finally:
            conn.close()

    rv0, _ = _rv_synced()
    assert rv0 == 0
    # 模拟该行已被盘后 sync 标记同步（synced=1），仅改 synced、不动 row_version。
    c = sqlite3.connect(repo._db_path)
    c.execute("UPDATE qmt_order SET synced=1 WHERE account_id='acc1' AND order_id=9")
    c.commit()
    c.close()
    # 迟到撤单失败回报 → mark_cancel_failed：row_version 自增、synced 重置 0（使 sync 的 CAS 能识别「读后被改写」）。
    repo.mark_cancel_failed("acc1", 9, error_id=42, error_msg="撤单失败")
    assert wq.flush(timeout=2.0) is True
    rv1, synced1 = _rv_synced()
    assert rv1 == rv0 + 1   # A1 修复前此处 rv1==0（不自增）→ CAS 被绕过
    assert synced1 == 0     # 重新标记待同步


def test_mark_cancel_failed_coalesce_keeps_existing_error(repo_ctx):
    repo, wq = repo_ctx
    # 先写一条带 error_id/error_msg 的委托。
    rec = _make_order(order_id=10, order_status=OrderStatus.REPORTED)
    rec.error_id = 7
    rec.error_msg = "既有错误"
    repo.upsert_order(rec)
    assert wq.flush(timeout=2.0) is True

    # mark_cancel_failed 传 None：COALESCE 应保留库内既有 error_*。
    repo.mark_cancel_failed("acc1", 10, error_id=None, error_msg=None)
    assert wq.flush(timeout=2.0) is True

    o = repo.get_orders("acc1", T_BUY)[0]
    assert o.cancel_failed is True
    assert o.error_id == 7
    assert o.error_msg == "既有错误"
    assert o.order_status == OrderStatus.REPORTED


# ===========================================================================
# get_account_daily：CLOSE 行可读回；不存在的日期返回 None
# ===========================================================================
def test_get_account_daily_close_and_missing(repo_ctx):
    repo, wq = repo_ctx
    acc = AccountRecord(
        account_id="acc1", trade_date=T_BUY, total_asset=Decimal("100000.50"),
        cash=Decimal("92000"), snapshot_type=SnapshotType.CLOSE,
    )
    repo.upsert_account_daily(acc)
    assert wq.flush(timeout=2.0) is True

    got = repo.get_account_daily("acc1", T_BUY, SnapshotType.CLOSE)
    assert got is not None
    assert got.total_asset == Decimal("100000.50")
    assert got.snapshot_type == SnapshotType.CLOSE

    # 默认 snapshot_type=CLOSE，签名默认值生效。
    assert repo.get_account_daily("acc1", T_BUY) is not None
    # 不存在的日期 → None。
    assert repo.get_account_daily("acc1", T_BUY_NEXT) is None
    # 同日但不同快照类型（OPEN）未写 → None（唯一键含 snapshot_type）。
    assert repo.get_account_daily("acc1", T_BUY, SnapshotType.OPEN) is None


# ===========================================================================
# 写入非阻塞：upsert_* 调用本身瞬时返回、不抛错（落盘在 flush 后才可见）
# ===========================================================================
def test_writes_are_non_blocking_and_visible_only_after_flush(repo_ctx):
    repo, wq = repo_ctx
    # 写调用本身应瞬时返回（仅入队，不在调用线程落盘）。
    t0 = time.monotonic()
    for i in range(50):
        repo.upsert_trade(_make_trade(traded_id=f"t{i}"))
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, f"upsert 阻塞了 {elapsed:.3f}s（应近乎瞬时入队）"

    # flush 之后才保证全部落盘可见。
    assert wq.flush(timeout=2.0) is True
    assert len(repo.get_trades("acc1", T_BUY)) == 50
