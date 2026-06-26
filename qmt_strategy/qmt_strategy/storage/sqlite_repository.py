"""qmt_* 四表的本机 SQLite 仓储实现（doc/05 §三 T2.1）。

业务意图：本类是 ``contracts.QmtRepository`` 的「本地化存储栈」落地实现，与
``data_writer.InMemoryQmtRepository`` 同语义（ON CONFLICT DO UPDATE + COALESCE 幂等口径），
但把数据真正落到本机 SQLite，供盘后对账 / 远端同步消费。

最重要的不变量（务必守住）：
- **持久化绝不阻塞交易热路径**：所有写（upsert_* / mark_cancel_failed）一律经
  ``AsyncWriteQueue.submit`` 入队即返回，绝不在调用线程同步 ``execute``/``commit`` 写盘；
  真正落盘由唯一的后台写线程串行执行（配合 WAL，1 写 N 读）。
- **读用短连接**：对账只读（get_orders / get_trades / get_account_daily）经
  ``sqlite_sql.read_conn`` 开短连接，查完即关；WAL 下读不阻塞写线程。

口径对齐（单一来源，禁二次换算）：
- SQL 文本与列序由 ``sqlite_sql.build_upsert`` 生成（含 COALESCE 不空覆盖、synced=0 标记）；
- 记录 ↔ 行的编解码（Decimal→TEXT 保精度、date/datetime→ISO、枚举→值）由 ``mappers`` 负责；
- trade_date 入参用 ``date.isoformat()`` 与库内 TEXT 列对齐，避免 date 对象与文本比较失配。
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from ..contracts.enums import SnapshotType
from ..contracts.models import AccountRecord, OrderRecord, PositionRecord, TradeRecord
from . import mappers, sqlite_sql
from .write_queue import AsyncWriteQueue


class SqliteQmtRepository:
    """本机 SQLite 版 qmt_* 四表仓储。实现 ``contracts.QmtRepository`` 协议。

    线程安全：写一律投递到单写线程（AsyncWriteQueue 串行执行），调用线程不持锁、不写盘；
    读各自短连接，互不影响。故本类自身无共享可变状态，多线程调用安全。
    """

    def __init__(
        self,
        db_path: str,
        write_queue: AsyncWriteQueue,
        logger,
        unique_with_trade_date: bool = True,
    ):
        # 本机 SQLite 文件路径（读连接据此开短连接；写连接由 write_queue 在写线程内自持）。
        self._db_path = db_path
        # 异步写队列：所有写经它入队，绝不在调用线程落盘（最重要不变量）。
        self._wq = write_queue
        self._logger = logger
        # 唯一键是否纳入 trade_date（§6.5 加固开关）。SQLite 侧唯一键由 schema DDL 固化为
        # 「含 trade_date」，故此开关仅作语义留痕 / 与 InMemory 实现签名对齐，不改变 SQL。
        self._uwd = unique_with_trade_date

    # ------------------------------------------------------------------
    # 写：一律入队（非阻塞），绝不在调用线程 execute
    # ------------------------------------------------------------------
    def upsert_trade(self, rec: TradeRecord) -> None:
        """成交明细 upsert（幂等：同 (account_id, trade_date, traded_id) 后到覆盖，
        已回填的 signal_trade_date / traded_time_east8 不被空值覆盖——COALESCE 由 build_upsert 保证）。"""
        self._submit_upsert("qmt_trade", mappers.trade_to_row(rec))

    def upsert_order(self, rec: OrderRecord) -> None:
        """委托 upsert（幂等：同 (account_id, trade_date, order_id) 后到覆盖为终态；
        signal_trade_date / order_time_east8 走 COALESCE 不空覆盖）。"""
        self._submit_upsert("qmt_order", mappers.order_to_row(rec))

    def upsert_position(self, rec: PositionRecord) -> None:
        """持仓快照 upsert（唯一键含 snapshot_type，OPEN/INTRADAY/CLOSE 互不覆盖）。"""
        self._submit_upsert("qmt_position_snapshot", mappers.position_to_row(rec))

    def upsert_account_daily(self, rec: AccountRecord) -> None:
        """账户资产日快照 upsert（唯一键含 snapshot_type；历史净值只认 CLOSE）。"""
        self._submit_upsert("qmt_account_daily", mappers.account_to_row(rec))

    def _submit_upsert(self, table: str, row: dict) -> None:
        """统一的「构造 SQL + 取参 + 入队」路径。

        关键：在调用线程只做纯内存的 SQL 文本生成与参数序列化，真正的 ``conn.execute`` 被包进
        闭包投递给写线程执行——故本方法瞬时返回，绝不阻塞交易热路径（最重要不变量）。
        """
        sql, _cols = sqlite_sql.build_upsert(table)
        params = sqlite_sql.params_for(table, row)
        # lambda 捕获 sql/params（值已在调用线程算好），写线程内才真正 execute；不在此 commit。
        self._wq.submit(lambda conn: conn.execute(sql, params))

    def mark_cancel_failed(
        self, account_id: str, order_id: int, error_id: Optional[int], error_msg: Optional[str]
    ) -> None:
        """on_cancel_error：在既有委托行追加 cancel_failed=1 + error_*，不改 order_status 终态（§6.2.1）。

        口径：
        - 只打 cancel_failed/error_*，绝不动 order_status（撤单失败不改变委托既有终态语义）；
        - error_id / error_msg 用 COALESCE，传 None 时保留库内既有值（不被空覆盖）；
        - synced=0 重新标记「待同步回远端」（变更需重新同步，对远端幂等安全）；
        - row_version 自增（评审修复 SYNC-1 / A1）：本路径与 build_upsert 同为「重置 synced=0」的写入者，必须一并
          自增版本号，否则盘后 sync 的 CAS 守卫（synced=0 AND row_version=读时版本）会因 0→0、版本未变而误命中，
          把这条迟到撤单失败带回的 cancel_failed/error_* 误标 synced=1、永不再推远端（远端长期停在撤单失败前旧态）；
        - 同样经写队列入队，绝不在调用线程执行 UPDATE。

        跨日防误标（评审 medium#5）：QMT order_id 按交易日重置可跨日复用，而唯一键含 trade_date。
        撤单失败回报只针对【当日】那笔委托，故 WHERE 限定为该 (account_id, order_id) 的【最新交易日】行
        （MAX(trade_date)），绝不波及历史同号委托行。
        """
        sql = (
            "UPDATE qmt_order SET cancel_failed=1, "
            "error_id=COALESCE(?, error_id), error_msg=COALESCE(?, error_msg), synced=0, "
            "row_version = row_version + 1 "  # SYNC-1/A1：与 build_upsert 同步自增，避免 0→0 重写绕过 mark_synced 的 CAS
            "WHERE account_id=? AND order_id=? AND trade_date=("
            "  SELECT MAX(trade_date) FROM qmt_order WHERE account_id=? AND order_id=?)"
        )
        params = (error_id, error_msg, account_id, order_id, account_id, order_id)
        self._wq.submit(lambda conn: conn.execute(sql, params))

    # ------------------------------------------------------------------
    # 读：短连接直读本地 SQLite（盘后对账用，不在交易热路径）
    # ------------------------------------------------------------------
    def get_orders(self, account_id: str, trade_date: date) -> List[OrderRecord]:
        """读某账户某交易日的全部委托行（trade_date 用 isoformat 与库内 TEXT 对齐）。"""
        rows = self._select_by_account_date("qmt_order", account_id, trade_date)
        return [mappers.row_to_order(r) for r in rows]

    def get_trades(self, account_id: str, trade_date: date) -> List[TradeRecord]:
        """读某账户某交易日的全部成交行。"""
        rows = self._select_by_account_date("qmt_trade", account_id, trade_date)
        return [mappers.row_to_trade(r) for r in rows]

    def _select_by_account_date(self, table: str, account_id: str, trade_date: date) -> list:
        """按 (account_id, trade_date) 全列查询并返回行列表；短连接查完即关。

        WAL 下读不阻塞写线程；用参数化查询防注入；date 入参统一 isoformat 与 TEXT 列对齐。
        """
        conn = sqlite_sql.read_conn(self._db_path)
        try:
            cur = conn.execute(
                f"SELECT * FROM {table} WHERE account_id=? AND trade_date=?",
                (account_id, trade_date.isoformat()),
            )
            return cur.fetchall()
        finally:
            conn.close()  # 读连接由调用方（本方法）负责关闭，避免连接泄漏

    # ------------------------------------------------------------------
    # 系统标志位 kv（评审二轮 P1#9）：对账未通过阻断次日开仓等跨进程/跨日运行态
    # ------------------------------------------------------------------
    def set_flag(self, flag_key: str, flag_value: Optional[str]) -> None:
        """设置系统标志位（非热路径，直连同步写，立即持久）。flag_value=None 视为清除该标志。

        带退避重试 + 失败显式抛出（评审三轮 EXEC-storage-04）：set_flag 写安全关键标志（对账未通过的次日禁开仓
        阻断），与异步写线程争 WAL 写锁时 5s 等锁仍可能 database is locked。这里对瞬时 locked 带退避重试 3 次
        （0.1/0.2s）；耗尽仍失败则【显式抛出】（绝不静默丢标志），由调用方 _set_reconcile_block fail-closed+强告警，
        杜绝"标志没写进、隔日重启误恢复开仓"的静默路径。重试在非热路径，阻塞可接受。
        """
        import sqlite3 as _sqlite3
        import time as _time

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            conn = _sqlite3.connect(self._db_path)
            # 等锁 5s（评审复审 P2-2，与 schema.apply_pragmas 同口径）。
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                if flag_value is None:
                    conn.execute("DELETE FROM system_flags WHERE flag_key=?", (flag_key,))
                else:
                    # updated_at 用调用方可不传的简化口径：这里不引时钟，仅记值；时间留痕由日志承载。
                    conn.execute(
                        "INSERT INTO system_flags (flag_key, flag_value, updated_at) VALUES (?,?,NULL) "
                        "ON CONFLICT(flag_key) DO UPDATE SET flag_value=excluded.flag_value",
                        (flag_key, flag_value),
                    )
                conn.commit()
                return
            except _sqlite3.OperationalError as exc:  # 瞬时 database is locked：退避重试
                last_exc = exc
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                conn.close()
            if attempt < 2:
                _time.sleep(0.1 * (2 ** attempt))
        # 重试耗尽仍失败：显式抛出（绝不静默），由调用方 fail-closed。
        raise last_exc if last_exc is not None else RuntimeError("set_flag 写入失败")

    def get_flag(self, flag_key: str) -> Optional[str]:
        """读系统标志位（短读连接）；无该标志返回 None。"""
        conn = sqlite_sql.read_conn(self._db_path)
        try:
            row = conn.execute(
                "SELECT flag_value FROM system_flags WHERE flag_key=?", (flag_key,)
            ).fetchone()
            return row[0] if row is not None else None
        finally:
            conn.close()

    def get_account_daily(
        self, account_id: str, trade_date: date, snapshot_type: SnapshotType = SnapshotType.CLOSE
    ) -> Optional[AccountRecord]:
        """取账户日快照（供资产对账，§6.7 第三类）。无该日该类型快照返回 None。

        默认 snapshot_type=CLOSE（净值/对账权威）；唯一键含 snapshot_type，故单行命中。
        """
        conn = sqlite_sql.read_conn(self._db_path)
        try:
            cur = conn.execute(
                "SELECT * FROM qmt_account_daily "
                "WHERE account_id=? AND trade_date=? AND snapshot_type=?",
                (account_id, trade_date.isoformat(), str(snapshot_type)),
            )
            row = cur.fetchone()
            return mappers.row_to_account(row) if row is not None else None
        finally:
            conn.close()
