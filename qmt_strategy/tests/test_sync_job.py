"""盘后回流同步 RemoteSyncJob 单测（doc/05 阶段2 T2.4）。

覆盖：
- 正常同步：本地写 1 成交 + 1 委托 + 1 持仓CLOSE + 1 账户CLOSE → run() → 远端收到对应记录、
  report 各表 pushed>=1 / remaining=0 / ok=True。
- 幂等重跑：再 run 一次 → pushed=0（已无 synced=0），远端不产生重复行。
- 失败恢复：用「前若干次 upsert 抛异常」的假远端 → 首次 run 有 errors、remaining>0、对应行仍 synced=0；
  换正常远端再 run → 剩余补齐、remaining=0。

测试夹具范式：tmp_path 建临时 SQLite（init_db 独立连接建表）+ AsyncWriteQueue（写线程内开自己的连接）；
造本地数据走 build_upsert（synced=0）经写队列异步落盘，读前务必 flush；不连真实 MySQL/xtquant。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.contracts.enums import DataSource, OrderStatus, SnapshotType, TradeSide
from qmt_strategy.contracts.models import (
    AccountRecord,
    OrderRecord,
    PositionRecord,
    TradeRecord,
)
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.storage import mappers
from qmt_strategy.storage.schema import QMT_TABLES, init_db
from qmt_strategy.storage.sqlite_sql import build_upsert, params_for, read_conn
from qmt_strategy.storage.sync_job import RemoteSyncJob
from qmt_strategy.storage.write_queue import AsyncWriteQueue

ACCOUNT_ID = "acc1"
T_SIGNAL = date(2026, 6, 11)
T_BUY = date(2026, 6, 12)


# ---------------------------------------------------------------------------
# 造数据助手：构造四表记录（默认值即「当日一笔完整回流」）
# ---------------------------------------------------------------------------
def _make_trade(traded_id: str = "t1") -> TradeRecord:
    return TradeRecord(
        account_id=ACCOUNT_ID, trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        traded_id=traded_id, trade_side=TradeSide.BUY, traded_price=Decimal("35.12"),
        traded_volume=200, traded_time=datetime(2026, 6, 12, 5, 31, 2),
        traded_time_east8=datetime(2026, 6, 12, 13, 31, 2), order_id=123,
        traded_amount=Decimal("7024.00"), signal_trade_date=T_SIGNAL, data_source=DataSource.CALLBACK,
    )


def _make_order(order_id: int = 123) -> OrderRecord:
    return OrderRecord(
        account_id=ACCOUNT_ID, trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        order_id=order_id, trade_side=TradeSide.BUY, order_volume=200, order_status=OrderStatus.TRADED,
        traded_volume=200, order_price=Decimal("35.12"), signal_trade_date=T_SIGNAL,
        data_source=DataSource.CALLBACK,
    )


def _make_position() -> PositionRecord:
    return PositionRecord(
        account_id=ACCOUNT_ID, trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        snapshot_type=SnapshotType.CLOSE, volume=200, can_use_volume=0,
        avg_price=Decimal("35.12"), market_value=Decimal("7024.00"), data_source=DataSource.QUERY,
    )


def _make_account() -> AccountRecord:
    return AccountRecord(
        account_id=ACCOUNT_ID, trade_date=T_BUY, total_asset=Decimal("100000.50"),
        cash=Decimal("92000.00"), snapshot_type=SnapshotType.CLOSE, market_value=Decimal("7024.00"),
        data_source=DataSource.QUERY,
    )


# 表名 → (记录工厂, 记录→行序列化函数)；用于经 build_upsert 把记录以 synced=0 写入本地。
_SEED = {
    "qmt_trade": (_make_trade, mappers.trade_to_row),
    "qmt_order": (_make_order, mappers.order_to_row),
    "qmt_position_snapshot": (_make_position, mappers.position_to_row),
    "qmt_account_daily": (_make_account, mappers.account_to_row),
}


def _seed_local_one_each(wq: AsyncWriteQueue) -> None:
    """经写队列把四表各写入一行（build_upsert 在 INSERT 时把 synced 置 0）。"""
    for table, (factory, to_row) in _SEED.items():
        rec = factory()
        sql, _cols = build_upsert(table)
        params = params_for(table, to_row(rec))

        def _task(conn, _sql=sql, _params=params):
            conn.execute(_sql, _params)

        wq.submit(_task)
    assert wq.flush(timeout=2.0)  # 写后异步，读/同步前务必落盘


def _count_synced(db: str, table: str, synced: int) -> int:
    """读某表指定 synced 值的行数（对账断言用）。"""
    conn = read_conn(db)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM %s WHERE synced=?" % table, (synced,))
        return int(cur.fetchone()[0])
    finally:
        conn.close()


@pytest.fixture()
def db_and_wq(tmp_path):
    """临时 SQLite + 已启动的写队列；测试结束 stop 写线程。"""
    db = str(tmp_path / "q.db")
    init_db(sqlite3.connect(db))                       # 先建表（独立连接）
    wq = AsyncWriteQueue(lambda: sqlite3.connect(db), RecordingLogger())  # 写线程内开自己的连接
    wq.start()
    try:
        yield db, wq
    finally:
        wq.stop()


# ---------------------------------------------------------------------------
# 假远端：前 N 次 upsert_* 抛异常，之后恢复正常（验证失败恢复 / 一行失败不中断整表）
# ---------------------------------------------------------------------------
class FlakyRepo:
    """包裹一个真实 InMemoryQmtRepository；让每类 upsert 的前 fail_times 次抛异常。"""

    def __init__(self, fail_times: int = 1):
        self._inner = InMemoryQmtRepository()
        self._fail_left = {
            "upsert_trade": fail_times, "upsert_order": fail_times,
            "upsert_position": fail_times, "upsert_account_daily": fail_times,
        }

    def _maybe_fail(self, name: str) -> None:
        if self._fail_left.get(name, 0) > 0:
            self._fail_left[name] -= 1
            raise RuntimeError("remote down: %s" % name)

    def upsert_trade(self, rec):
        self._maybe_fail("upsert_trade")
        self._inner.upsert_trade(rec)

    def upsert_order(self, rec):
        self._maybe_fail("upsert_order")
        self._inner.upsert_order(rec)

    def upsert_position(self, rec):
        self._maybe_fail("upsert_position")
        self._inner.upsert_position(rec)

    def upsert_account_daily(self, rec):
        self._maybe_fail("upsert_account_daily")
        self._inner.upsert_account_daily(rec)

    # 透传对账只读，便于断言远端收到的记录。
    def get_trades(self, account_id, trade_date):
        return self._inner.get_trades(account_id, trade_date)

    def get_orders(self, account_id, trade_date):
        return self._inner.get_orders(account_id, trade_date)

    def all_positions(self):
        return self._inner.all_positions()

    def all_accounts(self):
        return self._inner.all_accounts()


# ===========================================================================
# 1. 正常同步
# ===========================================================================
def test_run_syncs_all_tables_ok(db_and_wq):
    db, wq = db_and_wq
    _seed_local_one_each(wq)
    remote = InMemoryQmtRepository()
    job = RemoteSyncJob(db, remote, wq, RecordingLogger(), account_id=ACCOUNT_ID)

    report = job.run(T_BUY)

    # report：各表 pushed>=1、remaining=0、errors=0；整体 ok。
    assert report["ok"] is True
    for table in QMT_TABLES:
        assert report[table]["pushed"] >= 1, table
        assert report[table]["remaining"] == 0, table
        assert report[table]["errors"] == 0, table

    # 远端确实收到对应记录。
    assert len(remote.get_trades(ACCOUNT_ID, T_BUY)) == 1
    assert len(remote.get_orders(ACCOUNT_ID, T_BUY)) == 1
    assert len(remote.all_positions()) == 1
    assert len(remote.all_accounts()) == 1

    # 本地四表全部被标记 synced=1。
    for table in QMT_TABLES:
        assert _count_synced(db, table, 0) == 0, table
        assert _count_synced(db, table, 1) == 1, table

    # 远端成交记录精度无损（Decimal）。
    t = remote.get_trades(ACCOUNT_ID, T_BUY)[0]
    assert t.traded_price == Decimal("35.12")
    assert t.signal_trade_date == T_SIGNAL


# ===========================================================================
# 2. 幂等重跑：再 run 一次不重复推送、远端不产生重复行
# ===========================================================================
def test_idempotent_rerun_no_duplicate(db_and_wq):
    db, wq = db_and_wq
    _seed_local_one_each(wq)
    remote = InMemoryQmtRepository()
    job = RemoteSyncJob(db, remote, wq, RecordingLogger(), account_id=ACCOUNT_ID)

    first = job.run(T_BUY)
    assert first["ok"] is True

    # 第二次：已无 synced=0，pushed 全为 0，remaining 仍为 0、ok 仍 True。
    second = job.run(T_BUY)
    assert second["ok"] is True
    for table in QMT_TABLES:
        assert second[table]["pushed"] == 0, table
        assert second[table]["remaining"] == 0, table
        assert second[table]["errors"] == 0, table

    # 远端不产生重复行（唯一键幂等）。
    assert len(remote.get_trades(ACCOUNT_ID, T_BUY)) == 1
    assert len(remote.get_orders(ACCOUNT_ID, T_BUY)) == 1
    assert len(remote.all_positions()) == 1
    assert len(remote.all_accounts()) == 1


# ===========================================================================
# 3. 失败恢复：首跑远端报错 → 行保持 synced=0 → 换正常远端补齐
# ===========================================================================
def test_failure_then_recovery(db_and_wq):
    db, wq = db_and_wq
    _seed_local_one_each(wq)

    # 每类 upsert 第一次必抛异常 → 首跑四表各 1 行失败。
    flaky = FlakyRepo(fail_times=1)
    log = RecordingLogger()
    job_flaky = RemoteSyncJob(db, flaky, wq, log, account_id=ACCOUNT_ID)

    first = job_flaky.run(T_BUY)
    assert first["ok"] is False
    for table in QMT_TABLES:
        assert first[table]["errors"] == 1, table
        assert first[table]["pushed"] == 0, table
        assert first[table]["remaining"] == 1, table   # 失败行仍待同步
        # 本地行仍 synced=0（未被错误标记）。
        assert _count_synced(db, table, 0) == 1, table
        assert _count_synced(db, table, 1) == 0, table
    # 失败有 error 日志留痕。
    assert "remote_sync_row_failed" in log.events()
    # 远端此时未收到任何行。
    assert len(flaky.get_trades(ACCOUNT_ID, T_BUY)) == 0

    # 换正常远端再 run → 剩余被补齐。
    remote = InMemoryQmtRepository()
    job_ok = RemoteSyncJob(db, remote, wq, RecordingLogger(), account_id=ACCOUNT_ID)
    second = job_ok.run(T_BUY)
    assert second["ok"] is True
    for table in QMT_TABLES:
        assert second[table]["pushed"] == 1, table
        assert second[table]["remaining"] == 0, table
        assert second[table]["errors"] == 0, table
        assert _count_synced(db, table, 0) == 0, table

    # 补齐后远端收到全部四表记录。
    assert len(remote.get_trades(ACCOUNT_ID, T_BUY)) == 1
    assert len(remote.get_orders(ACCOUNT_ID, T_BUY)) == 1
    assert len(remote.all_positions()) == 1
    assert len(remote.all_accounts()) == 1


# ===========================================================================
# 4. 只挑当日：非当日的未同步行不被本次 run 触碰
# ===========================================================================
def test_run_only_picks_target_date(db_and_wq):
    db, wq = db_and_wq
    _seed_local_one_each(wq)
    remote = InMemoryQmtRepository()
    job = RemoteSyncJob(db, remote, wq, RecordingLogger(), account_id=ACCOUNT_ID)

    other_day = date(2026, 6, 11)  # 非 T_BUY
    report = job.run(other_day)

    # 当日（other_day）无数据 → 各表 pushed=0、remaining=0、ok=True（本日确无待同步）。
    assert report["ok"] is True
    for table in QMT_TABLES:
        assert report[table]["pushed"] == 0, table
        assert report[table]["remaining"] == 0, table
    # 远端未收到任何记录；T_BUY 行仍 synced=0（没被误同步）。
    assert len(remote.get_trades(ACCOUNT_ID, T_BUY)) == 0
    assert _count_synced(db, "qmt_trade", 0) == 1


# ---------------------------------------------------------------------------
# 评审修复 SYNC-1：mark_synced 的 row_version CAS 守卫挡住「POST 后被迟到回报改写」的误标
# ---------------------------------------------------------------------------
class _BumpRowVersionRemote(InMemoryQmtRepository):
    """模拟盘后同步窗内迟到回报：upsert_order 被调用(=POST 成功)后，立即对本地同一行做一次写入
    （row_version+1、synced 重置 0），模拟『SELECT 读到旧版本 → POST(旧值) → mark_synced 前该行被改写为新值』。"""

    def __init__(self, db, account_id, trade_date, order_id):
        super().__init__()
        self._db = db
        self._acc = account_id
        self._td = trade_date.isoformat()
        self._oid = order_id
        self._fired = False

    def upsert_order(self, rec):
        super().upsert_order(rec)  # POST 成功（远端落到旧值 V1）
        if not self._fired:
            self._fired = True
            conn = sqlite3.connect(self._db)
            try:
                conn.execute(
                    "UPDATE qmt_order SET row_version = row_version + 1, synced = 0 "
                    "WHERE account_id=? AND trade_date=? AND order_id=?",
                    (self._acc, self._td, self._oid),
                )
                conn.commit()
            finally:
                conn.close()


def test_mark_synced_cas_guards_against_late_rewrite(db_and_wq):
    """POST 与 mark_synced 之间该行被迟到回报改写(row_version+1、synced=0)，CAS 守卫(row_version=读时版本)
    使 mark_synced 命中 0 行 → 本机最新值仍 synced=0、下次重跑再推，绝不被误标已同步（原仅 synced=0 守卫挡不住 0→0）。"""
    db, wq = db_and_wq
    # 仅 seed qmt_order 一行（synced=0, row_version=0）
    rec = _make_order(order_id=123)
    sql, _cols = build_upsert("qmt_order")
    params = params_for("qmt_order", mappers.order_to_row(rec))

    def _seed(conn, _sql=sql, _params=params):
        conn.execute(_sql, _params)

    wq.submit(_seed)
    assert wq.flush(timeout=2.0)
    assert _count_synced(db, "qmt_order", 0) == 1

    remote = _BumpRowVersionRemote(db, ACCOUNT_ID, T_BUY, 123)
    job = RemoteSyncJob(db, remote, wq, RecordingLogger(), account_id=ACCOUNT_ID)
    report = job.run(T_BUY)

    # POST 确实发生（pushed=1），但 CAS 命中 0 行 → 本机仍 synced=0、report 暴露未清干净。
    assert report["qmt_order"]["pushed"] == 1
    assert _count_synced(db, "qmt_order", 1) == 0      # 没被误标已同步（SYNC-1 修复前此处会 ==1）
    assert _count_synced(db, "qmt_order", 0) == 1      # 仍待同步，下次重跑再推
    assert report["qmt_order"]["remaining"] == 1
    assert report["ok"] is False
