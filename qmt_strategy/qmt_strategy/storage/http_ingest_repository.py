"""HTTP 回流仓储 HttpIngestQmtRepository（doc/07：执行侧 → 信号侧 /qmt/ingest）。

业务意图：把「盘后把本机 qmt_* 行搬回远端」的远端写端，从直连 MySQL（MySqlQmtRepository）换成
    调用信号侧 HTTP 接口 ``POST /api/internal/qmt/ingest``。实现 contracts.QmtRepository 协议，
    在 ``app/run.py:_build_remote_repo`` 接缝处直接替换 MySqlQmtRepository 注入给 RemoteSyncJob——
    **RemoteSyncJob 与全部业务模块零改动**。

为何「逐行 POST」而非批量：RemoteSyncJob 对每行 ``upsert_X`` 成功后才入队 ``mark_synced``；
    失败（抛异常）则该行保 ``synced=0`` 下轮重试。本仓储每个 upsert 即「POST 单条记录、非 2xx 抛错」，
    完美复用其「单行幂等重试」模型：成功→标 synced；任何失败→异常上抛→保 synced=0。
    打板账户单日回流量很小（个位数票），逐行 POST 的往返开销可忽略。

幂等：信号侧按加固唯一键 ON DUPLICATE KEY UPDATE，重传不产生重复行；故网络抖动下重跑安全。

安全口径：token 由注入的 SignalHttpClient 经 X-Internal-Token 头携带，不入日志；本仓储不持有/不打印 token。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

from ..contracts.models import AccountRecord, OrderRecord, PositionRecord, TradeRecord
from . import mappers

# 信号侧回流接口路径（与 backend/app/api/routes_qmt_ingest.py 一致，含 /api 前缀）。
INGEST_PATH = "/api/internal/qmt/ingest"


class HttpIngestQmtRepository:
    """经 HTTP /qmt/ingest 回流的远端仓储（实现 contracts.QmtRepository）。

    依赖注入：
    - client：SignalHttpClient（已带 base_url + token + 超时）；
    - logger：结构化日志（只记表名/账户/计数，不记 token / 不记整行明细）；
    - ingest_path：接口路径，默认 INGEST_PATH（便于测试覆盖）。
    """

    def __init__(self, client, logger=None, ingest_path: str = INGEST_PATH):
        self._client = client
        self._logger = logger or logging.getLogger(__name__)
        self._path = ingest_path

    # ------------------------------------------------------------------
    # 四表 upsert：序列化为 mappers 行 → POST 单条记录；非 2xx / 网络失败抛错（RemoteSyncJob 兜底重试）
    # ------------------------------------------------------------------
    def upsert_trade(self, rec: TradeRecord) -> None:
        self._post_one("qmt_trade", mappers.trade_to_row(rec), rec.account_id, rec.trade_date)

    def upsert_order(self, rec: OrderRecord) -> None:
        self._post_one("qmt_order", mappers.order_to_row(rec), rec.account_id, rec.trade_date)

    def upsert_position(self, rec: PositionRecord) -> None:
        self._post_one(
            "qmt_position_snapshot", mappers.position_to_row(rec), rec.account_id, rec.trade_date
        )

    def upsert_account_daily(self, rec: AccountRecord) -> None:
        self._post_one(
            "qmt_account_daily", mappers.account_to_row(rec), rec.account_id, rec.trade_date
        )

    def _post_one(self, table: str, row: dict, account_id: str, trade_date: date) -> None:
        """POST 单条记录到 /qmt/ingest；非 2xx / 网络失败由 client 抛 SignalHttpError，原样上抛。

        载荷形如 {account_id, trade_date, records:[{table, data}]}（data = mappers 行，JSON 友好）。
        envelope 的 account_id/trade_date 仅供信号侧审计/日志，落库以 data 内值为准。
        """
        payload = {
            "account_id": account_id,
            "trade_date": trade_date.isoformat() if trade_date is not None else None,
            "records": [{"table": table, "data": row}],
        }
        # 抛错即由 RemoteSyncJob 捕获 → 该行保 synced=0 下轮重试；这里不吞异常。
        self._client.post_json(self._path, payload)

    # ------------------------------------------------------------------
    # 以下协议方法在「盘后 HTTP 回流」链路中不会被调用，给出安全实现以满足协议
    # ------------------------------------------------------------------
    def mark_cancel_failed(
        self, account_id: str, order_id: int, error_id: Optional[int], error_msg: Optional[str]
    ) -> None:
        """撤单失败打标只发生在盘中【本地】仓储；远端 qmt_order 行的 cancel_failed 经 upsert_order
        同步带过去，故远端无需单独打标。本方法仅留痕告警（理论上不应被 RemoteSyncJob 调用）。"""
        self._logger.warn(
            "http_ingest_mark_cancel_failed_noop", account_id=account_id, order_id=order_id
        )

    def get_orders(self, account_id: str, trade_date: date) -> List[OrderRecord]:
        # 对账只读走本地仓储（close_batch 读本机 SQLite），远端不提供读回。
        raise NotImplementedError("HTTP 回流仓储只写不读；对账只读请用本地 SqliteQmtRepository")

    def get_trades(self, account_id: str, trade_date: date) -> List[TradeRecord]:
        raise NotImplementedError("HTTP 回流仓储只写不读；对账只读请用本地 SqliteQmtRepository")

    def get_account_daily(self, account_id, trade_date, snapshot_type=None):
        raise NotImplementedError("HTTP 回流仓储只写不读；对账只读请用本地 SqliteQmtRepository")
