"""本机存储栈装配器 LocalStorage（doc/05 §三 / 阶段3）。

业务意图：把「建表 + 异步写队列 + SQLite 仓储 + 持久化台账 + 名单源 + 盘后同步」一站式装配为一个
可注入 `Engine` 的本地数据栈，并统一管理生命周期（start / flush / stop）。`Engine` 只消费它产出的
`repository` / `ledger` / `watchlist_source`（仍是既有协议），业务模块零感知。

生命周期：
- `start()`：建表（幂等）→ 起写线程 → 从 SQLite 重建内存台账（重启幂等）。
- `flush()`：阻塞至写队列清空（盘后 / 对账前调用，不在交易热路径）。
- `stop()`：drain 后停写线程、关连接。
- `save_watchlist(rows)`：盘前把信号侧交付的当日名单写入本机 SQLite（非热路径，直连写）。
- `sync_to_remote(trade_date)`：盘后把本机 `qmt_*` 当日数据幂等同步回远端 MySQL。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date
from typing import List, Optional

from ..contracts.models import SelectedStockRow
from ..contracts.protocols import QmtRepository, StructLogger
from .schema import init_db
from .sqlite_ledger import PersistentLocalLedger
from .sqlite_repository import SqliteQmtRepository
from .sync_job import RemoteSyncJob
from .watchlist_source import SqliteSelectedStockSource
from .write_queue import AsyncWriteQueue


class LocalStorage:
    """执行侧本机 SQLite 数据栈（单进程 + 异步持久化）。"""

    def __init__(
        self,
        db_path: str,
        logger: StructLogger,
        account_id: str,
        remote_repo: Optional[QmtRepository] = None,
        *,
        write_queue_max: int = 50000,
        write_queue_stuck_seconds: float = 30.0,
    ):
        self._db_path = db_path
        self._logger = logger
        self._account_id = account_id
        # 单一后台写线程：本机所有 SQLite 写（仓储 upsert / 台账镜像 / 同步标记）共用它，保证单写线程 + WAL。
        # 写线程在其内部自开连接（sqlite3 默认线程绑定），故 conn_factory 用闭包延迟到写线程执行。
        # 写连接须设 busy_timeout（评审二轮 P2#47）：与盘前 watchlist 直连写等并发时，默认 busy_timeout=0 会
        # 瞬间抛 database is locked 且被写线程吞掉、静默丢写。这里复用 schema.apply_pragmas 设 WAL + 5s 等锁。
        def _writer_conn():
            from .schema import apply_pragmas
            conn = sqlite3.connect(db_path)
            apply_pragmas(conn)
            return conn

        # 武装写队列健壮性参数（评审 F07/F09）：max_queue 防写线程挂死无界堆积 OOM；stuck_seconds 看门狗识别
        # 写线程卡死(commit 不抛错的 hang)使 is_healthy 转 False → 上层 fail-closed 停开新仓。生产由 run.py 从配置注入。
        self._wq = AsyncWriteQueue(
            _writer_conn, logger, name="qmt-sqlite-writer",
            max_queue=write_queue_max, stuck_seconds=write_queue_stuck_seconds,
        )
        # 三个协议实现（被 Engine 以既有协议注入消费）。
        self.repository: QmtRepository = SqliteQmtRepository(db_path, self._wq, logger)
        self.ledger = PersistentLocalLedger(db_path, self._wq, logger)
        self.watchlist_source = SqliteSelectedStockSource(db_path, logger)
        # 盘后同步任务（remote_repo 为远端 MySQL 仓储；缺省 None 表示暂不配同步，仅本地）。
        self._sync_job = (
            RemoteSyncJob(db_path, remote_repo, self._wq, logger, account_id)
            if remote_repo is not None
            else None
        )
        self._started = False

    def start(self, today: Optional[date] = None) -> None:
        """建表（幂等）→ 起写线程 → 重建内存台账（重启幂等）。进程启动时调用一次。

        today（评审三轮 EXEC-storage-05）：传入东八区当日时，load_from_db 只装载近 N 日窗口的台账行
        （防跨日只增不减、find_active 不随天数膨胀）；缺省 None 退化全量装载（向后兼容）。生产由 run.py
        传 east8 当日启用窗口。
        """
        if self._started:
            return
        # 父目录守卫（国金对接核对 F08）：QMT_LOCAL_DB_PATH 配成父目录不存在的绝对路径时，sqlite3.connect
        # 会抛 OperationalError 启动崩。这里在任何连接前先建好父目录（dirname 非空才建，默认相对路径不触发）。
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        # 建表用独立连接（与写线程连接分离），完成即关。
        conn = sqlite3.connect(self._db_path)
        try:
            init_db(conn)
        finally:
            conn.close()
        self._wq.start()
        # 从 SQLite 重建内存下单台账：重启后 has_active 仍有效 → 不重复下单（关键不变量「重启幂等」）。
        self.ledger.load_from_db(today=today)
        self._started = True
        self._logger.info("local_storage_started", db_path="***", account_id="***")

    def set_on_failure(self, on_failure) -> None:
        """补设存储故障告警钩子（评审二轮 P0#2）：写线程死亡/关键落盘失败时回调（→ Engine fail-closed）。

        Engine 在 LocalStorage 之后装配，故构造期无法注入，由装配末尾（run.py）接线到 engine.on_storage_failure。
        """
        self._wq.set_on_failure(on_failure)

    def is_healthy(self) -> bool:
        """写线程健康性（供调度器周期性体检；不健康即应 fail-closed 停开新仓 + 告警，评审二轮 P0#2）。"""
        return self._wq.is_healthy()

    def flush(self, timeout: float = 5.0) -> bool:
        """阻塞至写队列清空（盘后 / 对账前调用，保证读到一致数据）。超时返回 False 供告警。"""
        ok = self._wq.flush(timeout)
        if not ok:
            self._logger.error("local_storage_flush_timeout", timeout=timeout)
        return ok

    def stop(self, timeout: float = 5.0) -> None:
        """停机：drain 写队列后停写线程、关连接。"""
        self._wq.stop(drain=True, timeout=timeout)
        self._started = False

    def save_watchlist(self, rows: List[SelectedStockRow]) -> int:
        """盘前把信号侧交付的当日 watchlist 写入本机 SQLite（latest-wins，非热路径直连写）。"""
        n = self.watchlist_source.save_watchlist(rows)
        self._logger.info("watchlist_saved_local", count=n)
        return n

    def sync_to_remote(self, trade_date: date) -> dict:
        """盘后把本机 qmt_* 当日数据幂等同步回远端 MySQL。未配 remote_repo 则跳过并告警。

        分级告警（国金对接核对 F05）：当日 SYNC 一次性触发、失败行保 synced=0 不自动重跑，故 ok=False
        必须升级为 error 级告警（含各表 errors/remaining 计数）让运维可见，而非无条件 info 静默；
        否则部分回流失败时远端 qmt_* 长期残缺却无人发现。残留 synced=0 可经 resync_pending 手动补同步。
        """
        if self._sync_job is None:
            self._logger.warn("remote_sync_skipped_no_remote", trade_date=str(trade_date))
            return {"ok": False, "reason": "no_remote_repo"}
        report = self._sync_job.run(trade_date)
        if report.get("ok"):
            self._logger.info("remote_sync_done", trade_date=str(trade_date), ok=True)
        else:
            # 汇总各表 errors/remaining 供排查；error 级触发告警（部分回流失败、远端数据不齐）。
            errors = {t: v for t, v in report.items()
                      if isinstance(v, dict) and (v.get("errors") or v.get("remaining"))}
            self._logger.error(
                "remote_sync_incomplete",
                trade_date=str(trade_date),
                ok=False,
                detail=errors,
                note="部分回流失败/未同步,远端 qmt_* 当日数据不齐;失败行保 synced=0,可经 resync_pending 手动补同步",
            )
        return report

    def resync_pending(self, max_dates: Optional[int] = None) -> dict:
        """运维入口（国金对接核对 F05）：手动重同步历史所有 synced=0 残留行。

        业务意图：当日 SYNC 失败行保 synced=0 不自动重跑，需一个可手动触发的补同步入口。本方法扫描四表
        中仍有 synced=0 的【全部交易日】，逐日调 sync_to_remote（其本身按 synced=0 幂等挑行、可安全重跑）。
        返回 {trade_date_iso: 该日 report}；无 remote_repo / 无残留则返回空结果并留痕。
        边界：max_dates 限制单次处理的日期数（防一次扫太多）；按交易日升序处理。
        """
        if self._sync_job is None:
            self._logger.warn("resync_pending_skipped_no_remote")
            return {"ok": False, "reason": "no_remote_repo"}
        dates = self._sync_job.pending_dates()
        if max_dates is not None:
            dates = dates[:max_dates]
        if not dates:
            self._logger.info("resync_pending_nothing")
            return {"ok": True, "resynced_dates": 0, "reports": {}}
        reports: dict = {}
        for d in dates:
            reports[d.isoformat()] = self.sync_to_remote(d)
        all_ok = all(r.get("ok") for r in reports.values())
        self._logger.info("resync_pending_done", resynced_dates=len(dates), ok=all_ok)
        return {"ok": all_ok, "resynced_dates": len(dates), "reports": reports}
