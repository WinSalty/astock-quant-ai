"""存储层评审修复回归锁定测试（对应 SQLite 本地化对抗式评审确认的 6 条发现）。

- medium#1/#2：写队列建连失败 → start() fail-fast；写线程已死 → submit 显式告警不静默丢
- medium#3：sync mark_synced 带 synced=0 守卫 + 起点 flush（幂等重跑不重复同步）
- medium#4：watchlist fetch 解码异常也收敛为 WatchlistLoadError（不逸出拖垮进程）
- medium#5：mark_cancel_failed 只标最新交易日行（跨日同 order_id 不误标历史行）
- low#1：积压告警边沿触发（不依赖取模相等）
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.contracts.enums import OrderStatus, TradeSide
from qmt_strategy.contracts.errors import WatchlistLoadError
from qmt_strategy.contracts.models import OrderRecord
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.storage.schema import init_db
from qmt_strategy.storage.sqlite_repository import SqliteQmtRepository
from qmt_strategy.storage.watchlist_source import SqliteSelectedStockSource
from qmt_strategy.storage.write_queue import AsyncWriteQueue

D11 = date(2026, 6, 11)
D12 = date(2026, 6, 12)


class _FakeConn:
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


# ===========================================================================
# medium#1/#2：写队列存储级故障显式暴露
# ===========================================================================
def test_write_queue_fail_fast_on_conn_open_error():
    """建连失败 → start() 直接抛错(fail-fast),进程不在持久化失效下带病运行。"""
    def bad_factory():
        raise OSError("disk full / permission denied")
    q = AsyncWriteQueue(bad_factory, RecordingLogger(), name="t", )
    with pytest.raises(RuntimeError):
        q.start(ready_timeout=2.0)


def test_submit_on_dead_writer_alerts_not_silent():
    """写线程已死 → submit 不静默吞:触发告警钩子 + 记 error,且不抛错(不影响交易)、不入无消费队列。"""
    alerts = []
    q = AsyncWriteQueue(lambda: _FakeConn(), RecordingLogger(), name="t",
                        on_failure=lambda reason: alerts.append(reason))
    q.start()
    try:
        q._failed.set()          # 模拟运行期写线程意外失效
        out = []
        q.submit(lambda conn: out.append(1))   # 不应抛错
        assert alerts == ["write_queue_dead"]   # 显式告警(非静默)
        assert q.flush(timeout=1.0)
        assert out == []                         # 未入队、未执行
    finally:
        q.stop()


def test_backlog_warn_edge_triggered():
    """积压告警边沿触发:写线程卡住时,跨过阈值档位才告警,不依赖取模相等(low#1)。"""
    logger = RecordingLogger()
    q = AsyncWriteQueue(lambda: _FakeConn(), logger, name="t", depth_warn=10)
    q.start()
    try:
        import threading
        block = threading.Event()
        q.submit(lambda conn: block.wait(2.0))   # 卡住写线程,后续任务积压
        for _ in range(25):
            q.submit(lambda conn: None)
        # 跨过 10 / 20 两档 → 至少告警过(边沿触发,不要求精确次数)。
        assert "write_queue_backlog" in logger.events()
        block.set()
    finally:
        q.stop()


# ===========================================================================
# medium#5：mark_cancel_failed 只标最新交易日行（跨日同 order_id 不误标历史）
# ===========================================================================
def _order(account_id, trade_date, order_id):
    return OrderRecord(
        account_id=account_id, trade_date=trade_date, ts_code="600036.SH",
        qmt_stock_code="600036.SH", order_id=order_id, trade_side=TradeSide.BUY,
        order_volume=100, order_status=OrderStatus.REPORTED,
    )


def test_inmemory_mark_cancel_failed_only_latest_day():
    repo = InMemoryQmtRepository()
    repo.upsert_order(_order("acc1", D11, 5))   # 历史日同号委托
    repo.upsert_order(_order("acc1", D12, 5))   # 当日同号委托
    repo.mark_cancel_failed("acc1", 5, 99, "撤单失败")
    assert repo.get_orders("acc1", D12)[0].cancel_failed is True
    assert repo.get_orders("acc1", D11)[0].cancel_failed is False   # 历史行不被误标
    assert repo.get_orders("acc1", D12)[0].order_status == OrderStatus.REPORTED  # 终态不动


def test_sqlite_mark_cancel_failed_only_latest_day(tmp_path):
    db = str(tmp_path / "q.db")
    init_db(sqlite3.connect(db))
    wq = AsyncWriteQueue(lambda: sqlite3.connect(db), RecordingLogger())
    wq.start()
    try:
        repo = SqliteQmtRepository(db, wq, RecordingLogger())
        repo.upsert_order(_order("acc1", D11, 5))
        repo.upsert_order(_order("acc1", D12, 5))
        repo.mark_cancel_failed("acc1", 5, 99, "撤单失败")
        assert wq.flush(timeout=3.0)
        assert repo.get_orders("acc1", D12)[0].cancel_failed is True
        assert repo.get_orders("acc1", D11)[0].cancel_failed is False
    finally:
        wq.stop()


# ===========================================================================
# medium#4：watchlist fetch 解码异常收敛为 WatchlistLoadError
# ===========================================================================
def test_watchlist_fetch_decode_error_raises_load_error(tmp_path):
    db = str(tmp_path / "q.db")
    conn = sqlite3.connect(db)
    init_db(conn)
    # 插入一行 target_trade_date 合法(能被查中)但 trade_date 为脏值 → row_to_selected 解码会抛 ValueError。
    conn.execute(
        "INSERT INTO watchlist (ts_code, trade_date, target_trade_date) VALUES (?,?,?)",
        ("600036.SH", "notadate", "2026-06-12"),
    )
    conn.commit()
    conn.close()
    src = SqliteSelectedStockSource(db, RecordingLogger())
    # 解码异常被 fetch 的 try 捕获并收敛为 WatchlistLoadError(不逸出),loader 据此降级。
    with pytest.raises(WatchlistLoadError):
        src.fetch(date(2026, 6, 12))


# ===========================================================================
# 评审三轮 EXEC-storage-03：磁盘满 fail-closed 可观测 + 队列上限熔断
# ===========================================================================
def test_write_queue_persistent_commit_failure_marks_unhealthy():
    """连续 commit 失败达阈值 → _failed.set()+on_failure，is_healthy 转 False（不再静默丢数据）。"""
    from qmt_strategy.common.logger import RecordingLogger
    from qmt_strategy.storage.write_queue import AsyncWriteQueue

    class _DiskFullConn:
        def commit(self):
            raise OSError("disk full")
        def rollback(self):
            pass
        def close(self):
            pass

    failures: list[str] = []
    q = AsyncWriteQueue(
        lambda: _DiskFullConn(), RecordingLogger(), name="t",
        on_failure=failures.append, fail_after=3,
    )
    q.start()
    try:
        for _ in range(4):
            q.submit(lambda conn: None)
        q.flush(1.0)
        assert "write_persist_failed" in failures   # 连续失败达阈值触发 fail-closed
        assert q.is_healthy() is False               # 健康检查纳入"最近写成功"，转不健康
    finally:
        q.stop()


def test_write_queue_overflow_triggers_on_failure():
    """max_queue 积压超限（写线程阻塞挂起）→ on_failure + 丢弃 + 告警，绝不无界堆积内存爆掉。"""
    import threading

    from qmt_strategy.common.logger import RecordingLogger
    from qmt_strategy.storage.write_queue import AsyncWriteQueue

    block = threading.Event()

    class _OkConn:
        def commit(self):
            pass
        def rollback(self):
            pass
        def close(self):
            pass

    failures: list[str] = []
    q = AsyncWriteQueue(
        lambda: _OkConn(), RecordingLogger(), name="t",
        on_failure=failures.append, max_queue=5,
    )
    q.start()
    try:
        q.submit(lambda conn: block.wait(2.0))   # 卡住写线程
        for _ in range(12):                       # 后续任务积压超 max_queue
            q.submit(lambda conn: None)
        assert "write_queue_overflow" in failures
    finally:
        block.set()
        q.stop()
