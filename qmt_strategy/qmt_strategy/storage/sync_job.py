"""盘后回流同步任务 RemoteSyncJob（doc/05 §三 / §二第 3 段 "盘后同步回"）。

业务意图：把本机 SQLite 的 `qmt_*` 四表「当日未同步」行（synced=0）幂等地搬回远端
（信号侧 MySQL，经 contracts.QmtRepository 抽象），供信号侧复盘 / 归因。

关键不变量（务必守住）：
1. **不阻塞交易热路径**：本任务只在盘后跑；所有 SQLite 写（标记 synced=1）一律经
   AsyncWriteQueue.submit 入队即返回，由单一后台写线程串行落盘——绝不在本任务调用线程同步写盘；
   读取一律用 sqlite_sql.read_conn 的短连接（WAL 下不阻塞写线程）。
2. **幂等 / 可恢复**：远端 upsert 按唯一键幂等，重跑只挑 synced=0 的行；远端 upsert 失败的行
   **不标 synced**（保持 0），下次重跑可补；同一行重传不会在远端产生重复。
3. **一行失败不中断整表**：单行远端 upsert 抛异常只记 logger.error + errors+1 + continue，
   继续处理本表其余行，最大化「当日完成」。

完成口径（当日完成）：处理完后 flush 写队列（确保 synced=1 已落盘），再读各表 remaining(synced=0) 计数；
report 中 'ok' = 所有表 remaining==0 且全程无 errors，作为「当日同步是否齐」的对账判据。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, List

from . import mappers
from .schema import QMT_TABLES, TABLE_META
from .sqlite_sql import read_conn

# —— 每张回流表的「行→记录映射函数」与「远端 upsert 方法名」配置（单一来源，避免散落 if/elif）——
# 值：(row_to_X 反序列化函数, remote_repo 上的方法名)。
_TABLE_DISPATCH: Dict[str, tuple] = {
    "qmt_trade": (mappers.row_to_trade, "upsert_trade"),
    "qmt_order": (mappers.row_to_order, "upsert_order"),
    "qmt_position_snapshot": (mappers.row_to_position, "upsert_position"),
    "qmt_account_daily": (mappers.row_to_account, "upsert_account_daily"),
}

# —— 每张表「记录→行序列化函数」（用于从记录还原唯一键列的存储态值，拼 mark_synced 的 WHERE）——
# 复用 mappers 的 *_to_row，保证 WHERE 取值与表里实际存储的序列化口径完全一致（避免比对漂移）。
_TO_ROW: Dict[str, Callable[[Any], Dict[str, Any]]] = {
    "qmt_trade": mappers.trade_to_row,
    "qmt_order": mappers.order_to_row,
    "qmt_position_snapshot": mappers.position_to_row,
    "qmt_account_daily": mappers.account_to_row,
}


class RemoteSyncJob:
    """本地 SQLite qmt_* → 远端仓储 的盘后幂等同步任务。"""

    def __init__(self, db_path: str, remote_repo, write_queue, logger, account_id: str):
        # db_path：本机 SQLite 文件路径（读用短连接，写经 write_queue 的写线程连接）。
        self._db_path = db_path
        # remote_repo：实现 contracts.QmtRepository 的「远端」写端（真实落地为 MySqlQmtRepository）。
        self._remote = remote_repo
        # write_queue：AsyncWriteQueue，所有「标记 synced=1」的本地写经此异步落盘（不阻塞、单写线程）。
        self._wq = write_queue
        self._logger = logger
        # account_id：本任务负责同步的账户；mark_synced 的 WHERE 以它为首要过滤键（多账户隔离）。
        self._account_id = account_id

    def run(self, trade_date: date) -> dict:
        """把本地 qmt_* 当日未同步行幂等同步回远端，返回对账 report。

        流程（对 QMT_TABLES 每张表）：
          1) read_conn 短连接 SELECT * WHERE trade_date=? AND synced=0（只挑未同步行，天然支持重跑/补漏）；
          2) 逐行 row_to_X 反序列化为记录 → remote_repo.upsert_X(rec)；
          3) 成功 → 经 write_queue 入队一条 mark_synced UPDATE（按唯一键定位本行），pushed+1；
             失败 → logger.error + errors+1 + 该行保持 synced=0 + continue（不中断整表）。
        全部处理后 flush 写队列，再读各表 remaining(synced=0)。
        返回：{表名: {'pushed':n, 'remaining':m, 'errors':k}, 'ok': 所有 remaining==0 且无 errors}。

        运维口径（评审 medium#3 + 评审修复 SYNC-1）：sync 应在收盘 close_batch flush 完成、盘中回流写已停之后触发(盘后)。
        本方法开头先 flush 写队列,确保从「已落盘的稳定快照」起算;mark_synced 带 (synced=0 AND row_version=读时版本) 的
        CAS 守卫——若某行在 SELECT 后、mark_synced 前被迟到回报改写(row_version 自增、synced 重置 0),CAS 不命中、
        该行保持 synced=0 下次重跑再推,绝不把「只 POST 过旧值、本机已是新值」的行误标 synced=1(SYNC-1:仅 synced=0
        守卫挡不住 0→0 重写)。代价是 POST 后被改写的行当轮 remaining 仍计 1、report['ok'] 暴露未清干净,符合预期。
        """
        trade_date_iso = trade_date.isoformat()
        report: Dict[str, Any] = {}
        all_ok = True

        # 起点对齐：先 flush 把所有在途回流写落盘,再 SELECT,保证读到稳定一致快照（评审 medium#3）。
        if not self._wq.flush(timeout=5.0):
            self._logger.warn(
                "remote_sync_prefush_timeout", account_id=self._account_id, trade_date=trade_date_iso
            )

        for table in QMT_TABLES:
            pushed = 0
            errors = 0
            row_to_rec, upsert_name = _TABLE_DISPATCH[table]
            upsert = getattr(self._remote, upsert_name)

            # —— 第 1 步：读当日未同步行（短连接，读完即关；与写线程互不阻塞）——
            conn = read_conn(self._db_path)
            try:
                rows = conn.execute(
                    "SELECT * FROM %s WHERE trade_date=? AND synced=0" % table,
                    (trade_date_iso,),
                ).fetchall()
            finally:
                conn.close()

            # —— 第 2 步：逐行同步回远端；一行失败不影响后续行 ——
            for row in rows:
                # CAS 版本快照（评审修复 SYNC-1）：记录读到本行时的 row_version，POST 成功后 mark_synced 据此守卫。
                # 若 POST 与 mark_synced 之间该行被迟到回报改写（row_version 自增、synced 重置 0），CAS 不命中、
                # 该行保持 synced=0 下次重跑再推，绝不把「只 POST 了旧值、本机已是新值」的行误标为已同步。
                # 旧库迁移前无该列时取 None → 退化为仅 synced=0 守卫（向后兼容，不致 KeyError）。
                row_version = row["row_version"] if "row_version" in row.keys() else None
                try:
                    rec = row_to_rec(row)
                    upsert(rec)  # 远端按唯一键幂等 upsert；重传不产生重复行
                except Exception as e:
                    # 关键不变量 3：单行失败只记录 + 计数 + 跳过，保持 synced=0 等下次补。
                    self._logger.error(
                        "remote_sync_row_failed",
                        table=table,
                        account_id=self._account_id,
                        trade_date=trade_date_iso,
                        error=repr(e),
                    )
                    errors += 1
                    continue
                # 远端写成功 → 入队一条 mark_synced（异步、单写线程），把本行标记为已同步（带 row_version CAS 守卫）。
                self._enqueue_mark_synced(table, rec, row_version)
                pushed += 1

            report[table] = {"pushed": pushed, "errors": errors}

        # —— 第 3 步：flush 写队列，确保所有 synced=1 已落盘，再做 remaining 对账 ——
        # flush 超时返回 False 时记告警但不抛错：后续 remaining 计数仍会暴露未清干净的行。
        if not self._wq.flush(timeout=5.0):
            self._logger.warn(
                "remote_sync_flush_timeout", account_id=self._account_id, trade_date=trade_date_iso
            )

        # —— 第 4 步：重读各表 remaining(synced=0)，并据此判 ok（当日完成口径）——
        for table in QMT_TABLES:
            remaining = self._count_remaining(table, trade_date_iso)
            report[table]["remaining"] = remaining
            if remaining != 0 or report[table]["errors"] != 0:
                all_ok = False

        report["ok"] = all_ok
        return report

    def _enqueue_mark_synced(self, table: str, rec, row_version=None) -> None:
        """构造并入队一条 mark_synced UPDATE（按 TABLE_META[table]['unique'] 拼 WHERE）。

        WHERE 取值来源：先用 *_to_row 把记录序列化回「存储态」，再按唯一键列取值——保证与表里
        实际存储的列值（Decimal→TEXT、date→ISO、枚举→值）口径一致，定位的就是本行，不会误标他行。
        account_id 也在唯一键里（四表 unique 首列均为 account_id），天然做账户隔离。

        守卫（评审修复 SYNC-1）：除 `synced=0` 外，再叠加 `row_version=?`（CAS）——传入「SELECT 时读到的版本」。
        原仅 `synced=0` 守卫挡不住「读后该行被迟到回报改写、synced 重置回 0」的 0→0 重写：那样会把只 POST 过旧值、
        本机已是新值的行误标 synced=1，新值永不再推。叠加 row_version CAS 后，行被改写过版本即变、UPDATE 命中 0 行、
        保持 synced=0 下次重跑再推。row_version 为 None（旧库迁移前）时退化为仅 synced=0（向后兼容）。
        """
        unique_cols: List[str] = list(TABLE_META[table]["unique"])
        row = _TO_ROW[table](rec)
        where_parts = ["%s=?" % c for c in unique_cols] + ["synced=0"]
        params: List = [row[c] for c in unique_cols]
        if row_version is not None:
            where_parts.append("row_version=?")
            params.append(row_version)
        sql = "UPDATE %s SET synced=1 WHERE %s" % (table, " AND ".join(where_parts))
        params_t = tuple(params)

        # 写任务闭包：在写线程独占连接上执行 UPDATE（commit 由 AsyncWriteQueue worker 统一做）。
        def _task(conn, _sql=sql, _params=params_t):
            conn.execute(_sql, _params)

        self._wq.submit(_task)

    def pending_dates(self) -> List[date]:
        """扫描四表所有仍有 synced=0 残留行的【交易日】，升序去重返回（国金对接核对 F05 重同步入口用）。

        业务意图：当日 SYNC 失败行保 synced=0、不自动重跑；运维补同步需先知道「哪些交易日还有未同步行」。
        本方法跨四表取 DISTINCT trade_date(synced=0) 的并集，供 LocalStorage.resync_pending 逐日补同步。
        边界：读用短连接（不阻塞写线程）；解析 ISO 日期失败的脏行跳过，不拖垮扫描。
        """
        from datetime import date as _date

        seen: set = set()
        for table in QMT_TABLES:
            conn = read_conn(self._db_path)
            try:
                rows = conn.execute(
                    "SELECT DISTINCT trade_date FROM %s WHERE synced=0" % table
                ).fetchall()
            finally:
                conn.close()
            for r in rows:
                raw = r[0]
                if raw is None:
                    continue
                try:
                    seen.add(_date.fromisoformat(str(raw)))
                except ValueError:
                    continue  # 脏日期行跳过，不影响其余
        return sorted(seen)

    def _count_remaining(self, table: str, trade_date_iso: str) -> int:
        """读当日仍未同步（synced=0）行数（短连接，对账用）。"""
        conn = read_conn(self._db_path)
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM %s WHERE trade_date=? AND synced=0" % table,
                (trade_date_iso,),
            )
            return int(cur.fetchone()[0])
        finally:
            conn.close()
