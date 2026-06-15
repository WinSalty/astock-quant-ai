"""data_writer 落库写端实现（§6.2 / §6.3 / §6.4）。

业务意图：callbacks（实时回调）与 snapshot_job（收盘/断线补采）只依赖本写端，本写端负责把
规整后的记录委托给 QmtRepository 做幂等 upsert（ON DUPLICATE KEY UPDATE，后到覆盖为终态，
COALESCE 列不被空覆盖，§6.5）。字段规整在 normalize.py 完成，本类只做委托 + 快照类型注入 + 留痕。

与 QmtRepository 的分工（§6.2 协议注释）：
- DataWriter 负责 snapshot_type 在写端带入（盘中 INTRADAY / 收盘 CLOSE / 开盘前 OPEN）；
- QmtRepository 负责唯一键 upsert 与 COALESCE 幂等语义。

异常口径（§6.10 不静默吞错，按后端类型区分，评审三轮 EXEC-DW-03 修正措辞）：
- 直连/同步后端（如 MySqlQmtRepository）：upsert 遇异常在调用线程记 error 日志并重新抛出，由上游重试/告警；
- write-behind 后端（生产 SqliteQmtRepository）：upsert 只入队即返回，真正落盘在后台单写线程，异常【不会】在
  调用线程抛出——故本写端的 try/except「重抛即暴露」对它无效。该后端的 fail-closed 走带外路径：
  AsyncWriteQueue.flush_confirm + on_failure→Engine.on_storage_failure（停开新仓）+ is_healthy 周期体检；
  发单前关键落盘另由 order_executor._persist_critical_before_order 用 flush_confirm 校验（EXEC-storage-01）。
  切勿误以为同步 try/except 能兜住 write-behind 的落盘失败。
"""

from __future__ import annotations

from typing import Optional

from ..contracts.enums import SnapshotType
from ..contracts.models import AccountRecord, OrderRecord, PositionRecord, TradeRecord
from ..contracts.protocols import Clock, QmtRepository, StructLogger


class DataWriterImpl:
    """data_writer 写端实现。实现 contracts.DataWriter 协议。

    依赖注入：
      - repository：QmtRepository（InMemory / 直连 MySQL / /ingest），承担幂等 upsert；
      - logger：StructLogger，落库失败留痕（不写敏感信息）；
      - clock：Clock（可选），留作后续给记录补 created/updated 时刻等用途，本版不强制使用。
    """

    def __init__(
        self,
        repository: QmtRepository,
        logger: StructLogger,
        clock: Optional[Clock] = None,
    ):
        self._repo = repository
        self._logger = logger
        # clock 预留：落库时刻统一走 time_utils（DB CURRENT_TIMESTAMP 兜底），本版委托语义不直接用。
        self._clock = clock

    def upsert_trade(self, rec: TradeRecord) -> None:
        """成交明细落库（§6.2.1）。直接委托 repository 幂等 upsert。

        异常口径：落库失败记 error 日志并抛出（成交是唯一事实源，绝不可静默丢）。
        日志不带敏感信息：只记 account_id / traded_id / ts_code 便于定位，不记价位以外的口令类字段。
        """
        try:
            self._repo.upsert_trade(rec)
        except Exception as exc:  # noqa: BLE001 — 落库失败须暴露，统一留痕后重抛
            self._logger.error(
                "data_writer_upsert_trade_failed",
                account_id=rec.account_id,
                traded_id=rec.traded_id,
                ts_code=rec.ts_code,
                error=str(exc),
            )
            raise

    def upsert_order(self, rec: OrderRecord) -> None:
        """委托落库（§6.2.1）。直接委托 repository 幂等 upsert。

        异常口径：失败记 error 日志并抛出（废单/拒单/失败态是回调独有事实，丢失会致计划单凭空消失）。
        """
        try:
            self._repo.upsert_order(rec)
        except Exception as exc:  # noqa: BLE001
            self._logger.error(
                "data_writer_upsert_order_failed",
                account_id=rec.account_id,
                order_id=rec.order_id,
                ts_code=rec.ts_code,
                error=str(exc),
            )
            raise

    def upsert_position(self, rec: PositionRecord, snapshot_type: SnapshotType) -> None:
        """持仓快照落库（§6.2）。在写端注入 snapshot_type 后委托 repository。

        业务意图：盘中回调传 INTRADAY、收盘批次传 CLOSE、开盘前基线传 OPEN；
                  唯一键含 snapshot_type，故同日不同快照类型互不覆盖（§6.5）。
        """
        rec.snapshot_type = snapshot_type
        try:
            self._repo.upsert_position(rec)
        except Exception as exc:  # noqa: BLE001
            self._logger.error(
                "data_writer_upsert_position_failed",
                account_id=rec.account_id,
                ts_code=rec.ts_code,
                snapshot_type=str(snapshot_type),
                error=str(exc),
            )
            raise

    def upsert_account_daily(self, rec: AccountRecord, snapshot_type: SnapshotType) -> None:
        """账户资产日快照落库（§6.2）。在写端注入 snapshot_type 后委托 repository。

        业务意图：CLOSE 为净值曲线唯一来源；盘中 INTRADAY 仅供当日页刷新，不进历史净值。
        """
        rec.snapshot_type = snapshot_type
        try:
            self._repo.upsert_account_daily(rec)
        except Exception as exc:  # noqa: BLE001
            self._logger.error(
                "data_writer_upsert_account_failed",
                account_id=rec.account_id,
                snapshot_type=str(snapshot_type),
                error=str(exc),
            )
            raise

    def mark_cancel_failed(
        self,
        account_id: str,
        order_id: int,
        error_id: Optional[int],
        error_msg: Optional[str],
    ) -> None:
        """撤单失败留痕（§6.2.1 on_cancel_error）。委托 repository 在既有委托行打 cancel_failed=1 + error_*。

        业务意图：撤单失败是回调独有事实，仅追加失败标记 + 错误信息，不改 order_status 终态、不新增重复行。
        异常口径：失败记 error 日志并抛出（留痕本身失败须暴露）。
        """
        try:
            self._repo.mark_cancel_failed(account_id, order_id, error_id, error_msg)
        except Exception as exc:  # noqa: BLE001
            self._logger.error(
                "data_writer_mark_cancel_failed_failed",
                account_id=account_id,
                order_id=order_id,
                error=str(exc),
            )
            raise
