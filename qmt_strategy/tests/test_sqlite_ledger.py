"""PersistentLocalLedger 单测（doc/05 §三 T2.2）。

覆盖点（与方案验收对齐）：
- 基本委托：insert 后 has_active=True；不同战法不串。
- 重启幂等：insert + flush → 同一 db 新建实例 + load_from_db() → has_active 仍 True、get 取回一致。
- add_fill 持久化：累计成交 flush + reload → filled_volume/state 持久；同 traded_id 重投不翻倍（去重）。
- sync_status 持久化：改状态 flush + reload → 状态持久。
- 写不阻塞：写线程被人为卡死时，写方法仍瞬时返回（证明热路径不做磁盘 I/O）。

夹具口径：tmp_path 建临时 SQLite，先用独立连接 init_db 建表，写队列在写线程内开自己的连接；
读断言前务必 flush（写后异步，盘后/对账前才读）。绝不连真实 MySQL/xtquant。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date
from decimal import Decimal

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.contracts.enums import OrderState, TradeSide
from qmt_strategy.contracts.models import LedgerEntry
from qmt_strategy.storage.schema import init_db
from qmt_strategy.storage.sqlite_ledger import PersistentLocalLedger
from qmt_strategy.storage.write_queue import AsyncWriteQueue

T_BUY = date(2026, 6, 12)
T_SIGNAL = date(2026, 6, 11)


def _entry(
    biz: str = "20260612_600036.SH_CHASE_LIMIT_UP_001",
    *,
    plan_volume: int = 1000,
    state: OrderState = OrderState.PLANNED,
    order_id=None,
    ts_code: str = "600036.SH",
    strategy_family: str = "打板",
    target_trade_date: date = T_BUY,
) -> LedgerEntry:
    """构造一行台账：默认主板打板计划单。"""
    return LedgerEntry(
        biz_order_no=biz,
        account_id="acc1",
        target_trade_date=target_trade_date,
        ts_code=ts_code,
        strategy_family=strategy_family,
        side=TradeSide.BUY,
        plan_volume=plan_volume,
        plan_price=Decimal("11.00"),
        order_remark="LUP|2026-06-11|600036.SH",
        signal_trade_date=T_SIGNAL,
        state=state,
        order_id=order_id,
    )


@pytest.fixture
def db_path(tmp_path) -> str:
    """临时 SQLite 文件 + 建表（独立连接建表，与写线程连接隔离）。"""
    p = str(tmp_path / "q.db")
    conn = sqlite3.connect(p)
    try:
        init_db(conn)
    finally:
        conn.close()
    return p


@pytest.fixture
def write_queue(db_path):
    """已启动的异步写队列：写线程内开自己的连接。测试结束 stop（drain）。"""
    wq = AsyncWriteQueue(lambda: sqlite3.connect(db_path), RecordingLogger())
    wq.start()
    yield wq
    wq.stop()


def _new_ledger(db_path, write_queue) -> PersistentLocalLedger:
    return PersistentLocalLedger(db_path, write_queue, RecordingLogger())


# ---------------------------------------------------------------------------
# 基本委托
# ---------------------------------------------------------------------------
def test_insert_then_has_active(db_path, write_queue):
    led = _new_ledger(db_path, write_queue)
    led.insert(_entry())
    # 内存权威立即可见（不必等 flush）。
    assert led.has_active(T_BUY, "600036.SH", "打板") is True
    # 不同战法不串（幂等键含 strategy_family）。
    assert led.has_active(T_BUY, "600036.SH", "低吸") is False
    # 不同 ts_code 不串。
    assert led.has_active(T_BUY, "000001.SZ", "打板") is False


def test_get_and_find_active_delegate(db_path, write_queue):
    led = _new_ledger(db_path, write_queue)
    e = _entry(order_id=777)
    led.insert(e)
    got = led.get(e.biz_order_no)
    assert got is not None and got.plan_volume == 1000
    assert led.get_by_order_id(777).biz_order_no == e.biz_order_no
    active = led.find_active(T_BUY, "600036.SH", "打板")
    assert active is not None and active.biz_order_no == e.biz_order_no


# ---------------------------------------------------------------------------
# 重启幂等：flush 落盘 → 新实例 reload → 内存重建一致
# ---------------------------------------------------------------------------
def test_restart_idempotent_reload(db_path, write_queue):
    led = _new_ledger(db_path, write_queue)
    e = _entry(order_id=555)
    led.insert(e)
    assert write_queue.flush(timeout=2.0)  # 读/重建前务必 flush

    # 用同一 db 新建实例并重建内存 → 模拟进程重启。
    led2 = _new_ledger(db_path, write_queue)
    assert led2.has_active(T_BUY, "600036.SH", "打板") is False  # 重建前内存为空
    led2.load_from_db()
    # 重启后 has_active 仍有效（不会重复下单）。
    assert led2.has_active(T_BUY, "600036.SH", "打板") is True
    got = led2.get(e.biz_order_no)
    assert got is not None
    assert got.plan_volume == 1000
    assert got.plan_price == Decimal("11.00")
    assert got.signal_trade_date == T_SIGNAL
    # order_id 反查索引也随重建恢复。
    assert led2.get_by_order_id(555).biz_order_no == e.biz_order_no


# ---------------------------------------------------------------------------
# add_fill 持久化 + 去重
# ---------------------------------------------------------------------------
def test_add_fill_persisted_and_dedup(db_path, write_queue):
    led = _new_ledger(db_path, write_queue)
    e = _entry(plan_volume=1000, order_id=900)
    led.insert(e)
    led.add_fill(900, "tr1", 600, Decimal("11.00"))
    # 同 traded_id 重投：内存去重，filled_volume 不翻倍。
    led.add_fill(900, "tr1", 600, Decimal("11.00"))
    led.add_fill(900, "tr2", 400, Decimal("11.20"))
    assert write_queue.flush(timeout=2.0)

    led2 = _new_ledger(db_path, write_queue)
    led2.load_from_db()
    got = led2.get_by_order_id(900)
    assert got is not None
    # 600 + 400 = 1000（tr1 只计一次），达计划量 → TRADED。
    assert got.filled_volume == 1000
    assert got.state == OrderState.TRADED
    # 去重集合也持久化（重投不再翻倍的事实源）。
    assert got.counted_trade_ids == {"tr1", "tr2"}
    # 加权均价：(600*11.00 + 400*11.20)/1000 = 11.08。
    assert got.avg_filled_price == Decimal("11.08")


def test_add_fill_unknown_order_id_no_row(db_path, write_queue):
    led = _new_ledger(db_path, write_queue)
    led.insert(_entry(order_id=111))
    # 未知 order_id（非本系统单）：内存忽略，不落盘臆造行。
    led.add_fill(999999, "trX", 100, Decimal("9.99"))
    assert write_queue.flush(timeout=2.0)

    led2 = _new_ledger(db_path, write_queue)
    led2.load_from_db()
    assert led2.get_by_order_id(999999) is None
    # 仅原计划单一行被重建。
    assert len(led2.all()) == 1


# ---------------------------------------------------------------------------
# sync_status 持久化
# ---------------------------------------------------------------------------
def test_sync_status_persisted(db_path, write_queue):
    led = _new_ledger(db_path, write_queue)
    e = _entry(order_id=321, state=OrderState.SUBMITTED)
    led.insert(e)
    led.sync_status(321, OrderState.REPORTED)
    assert write_queue.flush(timeout=2.0)

    led2 = _new_ledger(db_path, write_queue)
    led2.load_from_db()
    got = led2.get_by_order_id(321)
    assert got is not None and got.state == OrderState.REPORTED


def test_update_persisted(db_path, write_queue):
    led = _new_ledger(db_path, write_queue)
    e = _entry()
    led.insert(e)
    led.update(e.biz_order_no, miss_reason="一字未成", cancelable=False)
    assert write_queue.flush(timeout=2.0)

    led2 = _new_ledger(db_path, write_queue)
    led2.load_from_db()
    got = led2.get(e.biz_order_no)
    assert got is not None
    assert got.miss_reason == "一字未成"
    assert got.cancelable is False


# ---------------------------------------------------------------------------
# 写不阻塞：写线程被人为卡死时，写方法仍瞬时返回
# ---------------------------------------------------------------------------
def test_write_does_not_block_when_writer_stalled(db_path):
    """证明热路径不做磁盘 I/O：写线程卡在第一个任务里，后续写方法仍立即返回。"""
    blocker = threading.Event()

    def stalling_conn_factory():
        # 写线程一上来就被这个连接里的写任务卡住（见下方哨兵任务）。
        return sqlite3.connect(db_path)

    wq = AsyncWriteQueue(stalling_conn_factory, RecordingLogger())
    wq.start()
    # 投入一个会卡住写线程的哨兵任务，占住唯一的写线程。
    wq.submit(lambda conn: blocker.wait(timeout=5.0))
    try:
        led = _new_ledger(db_path, wq)
        # 在写线程被卡死期间执行多次写方法：每次都只改内存 + 入队，应当瞬时返回。
        start = time.monotonic()
        led.insert(_entry())
        led.sync_status(123, OrderState.REPORTED)  # 未知单：内存忽略，但仍走入队路径
        led.update("20260612_600036.SH_CHASE_LIMIT_UP_001", cancelable=False)
        elapsed = time.monotonic() - start
        # 阈值放宽到 0.5s：即便机器抖动也远小于哨兵的 5s 阻塞，足以证明未被磁盘写阻塞。
        assert elapsed < 0.5, f"写方法被阻塞了 {elapsed:.3f}s"
        # 内存权威即时可见（无需等盘）。
        assert led.has_active(T_BUY, "600036.SH", "打板") is True
    finally:
        blocker.set()  # 释放写线程
        wq.stop()


# ===========================================================================
# 评审三轮 EXEC-order-01 / storage-05：成交明细去重重建 + 窗口装载 + 盘后清理
# ===========================================================================
def test_fill_detail_idempotent_across_restart(db_path, write_queue):
    """EXEC-order-01：同 traded_id 重投 + 重启按明细重算 → filled_volume 不翻倍，明细表只一行。"""
    led = _new_ledger(db_path, write_queue)
    led.insert(_entry(biz="b1", order_id=1, plan_volume=1000, state=OrderState.SUBMITTED))
    led.add_fill(1, "t1", 500, Decimal("11.00"))
    led.add_fill(1, "t1", 500, Decimal("11.00"))   # 重投（同 traded_id）
    assert led.flush_pending(2.0)

    led2 = _new_ledger(db_path, write_queue)
    led2.load_from_db()
    assert led2.get("b1").filled_volume == 500     # 去重，不翻倍

    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM local_order_fill WHERE biz_order_no='b1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1                                  # 明细表唯一键去重，只一行


def test_fill_detail_recompute_corrects_stale_snapshot(db_path, write_queue):
    """EXEC-order-01：崩溃窗口（整行快照偏旧）重启后，以明细重算纠正 filled_volume 并按量收口状态。"""
    led = _new_ledger(db_path, write_queue)
    led.insert(_entry(biz="b2", order_id=2, plan_volume=1000, state=OrderState.SUBMITTED))
    led.add_fill(2, "t1", 600, Decimal("11.00"))
    led.add_fill(2, "t2", 400, Decimal("11.00"))   # 累计达计划量 1000 → TRADED
    assert led.flush_pending(2.0)
    # 人为把整行快照的 filled_volume 改旧（模拟崩溃窗口快照偏差），明细表不动。
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE local_order_ledger SET filled_volume=600, state='PART_TRADED' WHERE biz_order_no='b2'")
        conn.commit()
    finally:
        conn.close()

    led2 = _new_ledger(db_path, write_queue)
    led2.load_from_db()
    e = led2.get("b2")
    assert e.filled_volume == 1000                 # 以明细重算纠正快照偏差
    assert e.state == OrderState.TRADED            # 达量重新收口
    # 重启后同 traded_id 再投不二次累计（counted_trade_ids 已由明细重建）
    led2.add_fill(2, "t1", 600, Decimal("11.00"))
    assert led2.get("b2").filled_volume == 1000


def test_fill_missing_traded_id_synthetic_key(db_path, write_queue):
    """EXEC-order-01：traded_id 缺失走合成键，重投两次明细仍只一行、filled 不翻倍。"""
    led = _new_ledger(db_path, write_queue)
    led.insert(_entry(biz="b3", order_id=3, plan_volume=1000, state=OrderState.SUBMITTED))
    led.add_fill(3, None, 300, Decimal("11.00"))
    led.add_fill(3, None, 300, Decimal("11.00"))   # 同合成键 → 去重
    assert led.flush_pending(2.0)
    led2 = _new_ledger(db_path, write_queue)
    led2.load_from_db()
    assert led2.get("b3").filled_volume == 300
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM local_order_fill WHERE biz_order_no='b3'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_load_from_db_window_filters_old_dates(db_path, write_queue):
    """EXEC-storage-05：load_from_db(today, keep_days) 只装载窗口内行，窗口外历史行不进内存。"""
    led = _new_ledger(db_path, write_queue)
    led.insert(_entry(biz="recent", target_trade_date=date(2026, 6, 15)))
    led.insert(_entry(biz="old", target_trade_date=date(2026, 5, 1)))   # 30+ 天前
    assert led.flush_pending(2.0)

    led2 = _new_ledger(db_path, write_queue)
    led2.load_from_db(today=date(2026, 6, 16), keep_days=7)
    assert led2.get("recent") is not None
    assert led2.get("old") is None                 # 窗口外未装载


def test_purge_before_removes_expired_rows(db_path, write_queue):
    """EXEC-storage-05：purge_before 清理过期台账行及其成交明细，当日行与明细保留。"""
    led = _new_ledger(db_path, write_queue)
    led.insert(_entry(biz="keep", order_id=10, target_trade_date=date(2026, 6, 16),
                      state=OrderState.SUBMITTED))
    led.insert(_entry(biz="expire", order_id=11, target_trade_date=date(2026, 5, 1),
                      state=OrderState.SUBMITTED))
    led.add_fill(10, "k1", 500, Decimal("11.00"))   # keep 的明细
    led.add_fill(11, "e1", 500, Decimal("11.00"))   # expire 的明细
    assert led.flush_pending(2.0)

    led.purge_before(date(2026, 6, 1))              # 清理 < 2026-06-01 的行
    assert led.flush_pending(2.0)

    conn = sqlite3.connect(db_path)
    try:
        ledger_bizes = {r[0] for r in conn.execute("SELECT biz_order_no FROM local_order_ledger")}
        fill_bizes = {r[0] for r in conn.execute("SELECT biz_order_no FROM local_order_fill")}
    finally:
        conn.close()
    assert "keep" in ledger_bizes and "expire" not in ledger_bizes
    assert "keep" in fill_bizes and "expire" not in fill_bizes   # 明细随台账一并清理
