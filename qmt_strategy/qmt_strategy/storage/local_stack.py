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
    ):
        self._db_path = db_path
        self._logger = logger
        self._account_id = account_id
        # 单一后台写线程：本机所有 SQLite 写（仓储 upsert / 台账镜像 / 同步标记）共用它，保证单写线程 + WAL。
        # 写线程在其内部自开连接（sqlite3 默认线程绑定），故 conn_factory 用闭包延迟到写线程执行。
        self._wq = AsyncWriteQueue(lambda: sqlite3.connect(db_path), logger, name="qmt-sqlite-writer")
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

    def start(self) -> None:
        """建表（幂等）→ 起写线程 → 重建内存台账（重启幂等）。进程启动时调用一次。"""
        if self._started:
            return
        # 建表用独立连接（与写线程连接分离），完成即关。
        conn = sqlite3.connect(self._db_path)
        try:
            init_db(conn)
        finally:
            conn.close()
        self._wq.start()
        # 从 SQLite 重建内存下单台账：重启后 has_active 仍有效 → 不重复下单（关键不变量「重启幂等」）。
        self.ledger.load_from_db()
        self._started = True
        self._logger.info("local_storage_started", db_path="***", account_id="***")

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
        """盘后把本机 qmt_* 当日数据幂等同步回远端 MySQL。未配 remote_repo 则跳过并告警。"""
        if self._sync_job is None:
            self._logger.warn("remote_sync_skipped_no_remote", trade_date=str(trade_date))
            return {"ok": False, "reason": "no_remote_repo"}
        report = self._sync_job.run(trade_date)
        self._logger.info("remote_sync_done", trade_date=str(trade_date), ok=report.get("ok"))
        return report
