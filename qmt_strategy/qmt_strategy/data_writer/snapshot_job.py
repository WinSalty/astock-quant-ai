"""定时全量快照与补采作业（§6.2.2 收盘兜底 / §6.2.3 断线补采 / §6.2.2 末 开盘基线）。

业务意图：实时回调（callbacks.ExecCallback）只承载事件驱动的增量，存在两类天然空洞：
1) 盘中回调可能丢帧（网络抖动 / 推送限速）；
2) 断线期间（xttrader 断开不自动重连）漏掉的成交 / 委托无法靠回调补回。
本作业以「主动 query_* 全量拉取」补齐当日权威全集，并落定时快照（资产 / 持仓）作为净值曲线与
持仓复盘的唯一来源。三个入口对应三种时机：

- run_close(trade_date)：收盘批次（如 15:05 / 15:30）。明细全量兜底补全 → 当日权威全集；
  资产 / 持仓落 CLOSE 快照（净值曲线唯一来源）。**收盘批次是历史还原的唯一机会**——隔日 API 清空，
  CLOSE 当日未成功落则永久缺失，故对资产快照写入做退避重试 + 强告警（§6.2.2 第 4 步）。
- run_backfill(trade_date)：断线重连后立即全量补采当日缺失（标 QUERY_BACKFILL）；
  资产 / 持仓刷 INTRADAY（盘中态，不进历史净值）。**必须当日内完成，隔日不可补**（query_* 隔日清空）。
- run_open(trade_date)：开盘前（如 9:10，可选）落 OPEN 持仓快照作昨夜拥股基线。

依赖分层：本模块只依赖 contracts 的 XtTraderLike Protocol、DataWriter / StructLogger / Clock 协议、
P2 的 normalize 纯函数，绝不直接 import xtquant；真实链路注入 XtQuantTrader 实例与真实账户对象，
单测用 contracts.xt_objects 的 FakeXt* + 内存写端驱动。

时间口径：trade_date 由 trade_date_provider 注入（东八区交易日，§6.6），落库时刻经 normalize → time_utils
双写（UTC naive + 东八区原值），本模块不手工 ±8h。
data_source：明细兜底 / 补采一律 QUERY_BACKFILL（区别于回调路径的 CALLBACK）；资产 / 持仓快照沿用
normalize 默认 QUERY（定时拉取来源）。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Optional

from ..contracts.enums import DataSource, SnapshotType
from ..contracts.protocols import Clock, DataWriter, StructLogger, XtTraderLike
from . import normalize
from .normalize import SideResolver, StatusResolver

# 交易日提供器签名：返回当日 trade_date（东八区自然日，§6.6）。
# 用可注入 Callable 而非直接读 clock，便于上游集中口径（如收盘批次可固定取收盘日）。
TradeDateProvider = Callable[[], date]

# 退避休眠器签名：第 n 次重试前等待（单位秒，传入 int 重试序号）。
# 默认 no-op：单测不真正 sleep；真实链路可注入 time.sleep 包装做线性 / 指数退避。
BackoffSleeper = Callable[[int], None]


def _no_backoff(_attempt: int) -> None:
    """默认退避：不休眠（单测用，避免真实阻塞）。真实链路注入带 sleep 的实现做退避。"""
    return None


class SnapshotJob:
    """定时全量快照 / 补采作业（§6.2.2 / §6.2.3）。

    依赖注入：
      - trader：XtTraderLike，提供 query_stock_trades / orders / asset / positions 全量拉取；
      - account：账户对象（query_* 入参，本模块不解析其内部字段，仅透传给 trader）；
      - data_writer：DataWriter，负责规整后记录的幂等 upsert（含 snapshot_type 注入）；
      - logger：StructLogger，补采完成 / CLOSE 失败等事件留痕（不写敏感信息）；
      - clock：Clock，预留落库时刻 / 退避计时口径（本版退避用 backoff_sleeper，clock 仅留作扩展）；
      - account_id：当前账户号（落 qmt_*.account_id）；
      - trade_date_provider：取当日 trade_date（保留入参 trade_date 优先，缺省回落 provider）；
      - max_retries：CLOSE 资产快照写入失败时的最大重试次数（§6.2.2 第 4 步，隔日不可补）；
      - side_resolver / status_resolver：方向 / 状态解析器，默认走 normalize 实测默认表，可注入覆盖；
      - backoff_sleeper：每次重试前的退避休眠器，默认 no-op（单测不阻塞）。

    幂等口径：明细兜底用 QUERY_BACKFILL upsert，唯一键由 repository 保证（同 traded_id / order_id
    后到覆盖为终态，回调先写的不会重复成行，§6.5）；故收盘批次可与盘中回调并发 / 重跑而不产生重复行。
    """

    def __init__(
        self,
        trader: XtTraderLike,
        account: Any,
        data_writer: DataWriter,
        logger: StructLogger,
        clock: Clock,
        *,
        account_id: str,
        trade_date_provider: TradeDateProvider,
        max_retries: int = 3,
        side_resolver: SideResolver = normalize.default_side_resolver,
        status_resolver: StatusResolver = normalize.default_status_resolver,
        backoff_sleeper: BackoffSleeper = _no_backoff,
    ):
        self._trader = trader
        self._account = account
        self._dw = data_writer
        self._logger = logger
        self._clock = clock
        self._account_id = account_id
        self._trade_date_provider = trade_date_provider
        self._max_retries = max_retries
        self._side_resolver = side_resolver
        self._status_resolver = status_resolver
        self._backoff_sleeper = backoff_sleeper

    # ------------------------------------------------------------------
    # 内部：解析当日 trade_date（入参优先，缺省回落 provider）
    # ------------------------------------------------------------------
    def _resolve_trade_date(self, trade_date: Optional[date]) -> date:
        """统一 trade_date 口径：调用方显式传入则用之，否则取 trade_date_provider()。

        业务意图：收盘批次 / 补采通常由调度显式带「东八区交易日」入参，避免跨日 ID 复用串号（§6.5）；
        provider 作为缺省兜底（如未带参的盘中触发）。
        """
        if trade_date is not None:
            return trade_date
        return self._trade_date_provider()

    # ------------------------------------------------------------------
    # 明细全量兜底补全：成交 + 委托 → QUERY_BACKFILL upsert
    # ------------------------------------------------------------------
    def _backfill_details(self, trade_date: date) -> int:
        """拉取当日成交 / 委托全集并以 QUERY_BACKFILL 幂等补全（§6.2.2 第 1 步 / §6.2.3）。

        业务意图：query_* 返回当日全量，故拉一次即可把盘中回调可能漏的明细补齐为「当日权威全集」。
        data_source=QUERY_BACKFILL 明确标记补采来源，便于排查与对账区分回调 / 兜底两条腿。
        幂等：同 traded_id / order_id 经回调先写 + 本次兜底再写，repository 唯一键 + COALESCE 保证仅 1 行，
              终态为后到值，且回调已回填的 signal_trade_date / *_east8 不被空覆盖（§6.5）。
        返回：本次补采遍历到的明细总条数（成交 + 委托），供调用方 / 日志统计 missing_recovered。
        边界：query_* 返回空列表时不报错，返回 0；单条规整经 normalize getattr 容错，缺字段落 None。
        """
        recovered = 0
        # 1) 成交全量兜底：成交是唯一事实源，必须补齐。
        for t in self._trader.query_stock_trades(self._account):
            rec = normalize.normalize_trade(
                t,
                account_id=self._account_id,
                trade_date=trade_date,
                data_source=DataSource.QUERY_BACKFILL,
                side_resolver=self._side_resolver,
            )
            self._dw.upsert_trade(rec)
            recovered += 1
        # 2) 委托全量兜底：含已报 / 部成 / 已成 / 已撤等终态，补齐当日权威委托全集。
        for o in self._trader.query_stock_orders(self._account):
            rec = normalize.normalize_order(
                o,
                account_id=self._account_id,
                trade_date=trade_date,
                data_source=DataSource.QUERY_BACKFILL,
                side_resolver=self._side_resolver,
                status_resolver=self._status_resolver,
            )
            self._dw.upsert_order(rec)
            recovered += 1
        return recovered

    # ------------------------------------------------------------------
    # 持仓快照：query_stock_positions → 指定 snapshot_type 落库
    # ------------------------------------------------------------------
    def _snapshot_positions(self, trade_date: date, snapshot_type: SnapshotType) -> int:
        """拉取当日持仓全量并按指定快照类型落库（§6.2.2 / §6.2.3）。

        业务意图：CLOSE 为持仓复盘唯一权威；INTRADAY 仅供当日页刷新；OPEN 为昨夜拥股基线。
        唯一键含 snapshot_type，故同日不同快照类型互不覆盖（§6.5）。
        返回：落库的持仓行数（供日志 / 断言）。
        """
        count = 0
        for p in self._trader.query_stock_positions(self._account):
            rec = normalize.normalize_position(
                p,
                account_id=self._account_id,
                trade_date=trade_date,
                snapshot_type=snapshot_type,
            )
            self._dw.upsert_position(rec, snapshot_type)
            count += 1
        return count

    # ------------------------------------------------------------------
    # 资产快照：query_stock_asset → 指定 snapshot_type 落库
    # ------------------------------------------------------------------
    def _snapshot_asset(self, trade_date: date, snapshot_type: SnapshotType) -> None:
        """拉取当日账户资产并按指定快照类型落库（§6.2.2 / §6.2.3）。

        业务意图：CLOSE 资产快照是净值曲线唯一来源；INTRADAY 仅供当日页刷新，不进历史净值。
        注：query_stock_asset 与 upsert 失败均在此抛出，由调用方（run_close）决定是否退避重试。
        """
        asset = self._trader.query_stock_asset(self._account)
        rec = normalize.normalize_account(
            asset,
            account_id=self._account_id,
            trade_date=trade_date,
            snapshot_type=snapshot_type,
        )
        self._dw.upsert_account_daily(rec, snapshot_type)

    # ==================================================================
    # 入口一：收盘定时全量快照兜底（§6.2.2）
    # ==================================================================
    def run_close(self, trade_date: Optional[date] = None) -> int:
        """收盘批次：明细全量兜底补全 + CLOSE 资产 / 持仓快照（§6.2.2）。

        步骤（对齐 §6.2.2 伪逻辑）：
          1) 明细全量兜底：query_stock_trades / orders → QUERY_BACKFILL upsert（→ 当日权威全集）。
          2) 快照：query_stock_asset → CLOSE（净值曲线唯一来源）；
                   query_stock_positions → CLOSE（持仓复盘唯一权威）。
          3) 失败重试（§6.2.2 第 4 步）：CLOSE 资产快照是历史还原唯一机会，隔日 API 清空不可补，
             故对资产快照写入做退避重试至多 max_retries 次；最终仍失败 → 强告警（logger.error
             snapshot_close_asset_failed），不静默吞错（§6.10）。
        返回：明细兜底补回的条数（成交 + 委托），供调用方统计 / 日志。
        边界：明细兜底 / 持仓快照若抛错，按「成交是唯一事实源、不可静默丢」语义直接向上抛出；
              资产快照单独退避重试，避免单次抖动导致 CLOSE 净值永久缺失。
        注：对账（reconcile.run）在 §6.2.2 第 3 步，按依赖分层由调度在本作业之后单独触发，不在本模块内。
        """
        td = self._resolve_trade_date(trade_date)
        # 1) 明细全量兜底补全 → 当日权威全集
        recovered = self._backfill_details(td)
        # 2a) 资产快照：CLOSE 为净值曲线唯一来源，做退避重试 + 强告警（隔日不可补）
        self._snapshot_asset_with_retry(td)
        # 2b) 持仓快照：CLOSE 为持仓复盘唯一权威
        pos_count = self._snapshot_positions(td, SnapshotType.CLOSE)
        # 收盘批次完成留痕：记补回明细数 + 落库持仓数，便于盘后核对当日权威全集是否齐备。
        self._logger.info(
            "snapshot_close_done",
            account_id=self._account_id,
            trade_date=str(td),
            details_recovered=recovered,
            positions=pos_count,
        )
        return recovered

    def _snapshot_asset_with_retry(self, trade_date: date) -> None:
        """CLOSE 资产快照写入：退避重试至多 max_retries 次，最终失败强告警（§6.2.2 第 4 步）。

        业务意图：收盘资产快照是当日净值曲线的唯一权威来源，**隔日 API 清空、不可补**。
        故任一次 query_stock_asset / upsert 抛错都按退避重试，给瞬时抖动（网络 / DB 短暂不可用）
        以恢复机会；只要任一次成功即返回。
        退避口径：第 1 次失败后等待 backoff_sleeper(1)，第 2 次 backoff_sleeper(2)……（默认 no-op）。
        重跑口径：max_retries 次全部失败 → logger.error 强告警（snapshot_close_asset_failed，
                  带累计失败次数），并向上抛出最后一次异常，交由调度告警 / 人工介入（绝不静默吞）。
        边界：max_retries<=0 时仍至少尝试 1 次（保证收盘批次必有一次落库尝试）。
        """
        # 总尝试次数 = 首次 + max_retries 次重试；max_retries<=0 时退化为仅 1 次尝试。
        total_attempts = max(self._max_retries, 0) + 1
        failures = 0
        last_exc: Optional[Exception] = None
        for attempt in range(1, total_attempts + 1):
            try:
                self._snapshot_asset(trade_date, SnapshotType.CLOSE)
                # 成功落库：若此前有过失败，留痕一条恢复信息便于排查抖动；随后正常返回。
                if failures:
                    self._logger.info(
                        "snapshot_close_asset_recovered",
                        account_id=self._account_id,
                        trade_date=str(trade_date),
                        failures=failures,
                        attempt=attempt,
                    )
                return
            except Exception as exc:  # noqa: BLE001 — CLOSE 失败须暴露，先退避重试再强告警
                failures += 1
                last_exc = exc
                # 每次失败先告警一条（便于观察重试过程），但不立即抛出——保留重试机会。
                self._logger.warn(
                    "snapshot_close_asset_retry",
                    account_id=self._account_id,
                    trade_date=str(trade_date),
                    attempt=attempt,
                    error=str(exc),
                )
                # 还有重试机会则退避后再试；已是最后一次则跳出循环走强告警。
                if attempt < total_attempts:
                    self._backoff_sleeper(attempt)
        # 所有尝试均失败：CLOSE 净值快照当日未落，隔日不可补 → 强告警并重抛（§6.2.2 第 4 步 / §6.10）。
        self._logger.error(
            "snapshot_close_asset_failed",
            account_id=self._account_id,
            trade_date=str(trade_date),
            failures=failures,
            error=str(last_exc) if last_exc is not None else "",
        )
        if last_exc is not None:
            raise last_exc

    # ==================================================================
    # 入口二：断线补采（§6.2.3）
    # ==================================================================
    def run_backfill(self, trade_date: Optional[date] = None) -> int:
        """断线重连后立即全量补采当日缺失（§6.2.3）。

        步骤（对齐 §6.2.3 伪逻辑）：
          1) query_stock_trades / orders → QUERY_BACKFILL upsert（断线期间漏掉的成交 / 委托被补回）。
          2) query_stock_asset / positions → 刷新当日 INTRADAY 快照（盘中态，不进历史净值）。
          3) logger.info("backfill_done", missing_recovered=N)。
        前提：query_* 返回当日全量，故重连后拉一次即可补齐当日缺失；但**必须当日内完成，隔日不可补**
              （隔日 API 清空当日明细，§6.2.3）。配合守护进程常驻 + 每日开盘前主动重连一次。
        返回：本次补回的明细条数 N（成交 + 委托）。
        边界：本模块只做「补采落库」；重建 session / connect / subscribe 由 connection 层完成，
              通常由 ExecCallback.on_disconnected 的钩子先重连、再回调本方法。
        """
        td = self._resolve_trade_date(trade_date)
        # 1) 明细全量补采 → 断线期间缺失被补回，标 QUERY_BACKFILL
        recovered = self._backfill_details(td)
        # 2) 资产 / 持仓刷新当日 INTRADAY（断线补采仍属盘中态，不落 CLOSE，避免污染净值曲线）
        self._snapshot_asset(td, SnapshotType.INTRADAY)
        self._snapshot_positions(td, SnapshotType.INTRADAY)
        # 3) 补采完成留痕：missing_recovered 即本次补回明细数，供监控判断断线缺口规模。
        self._logger.info(
            "backfill_done",
            account_id=self._account_id,
            trade_date=str(td),
            missing_recovered=recovered,
        )
        return recovered

    # ==================================================================
    # 入口三：开盘前昨夜拥股基线（可选，§6.2.2 末）
    # ==================================================================
    def run_open(self, trade_date: Optional[date] = None) -> int:
        """开盘前（如 9:10）落 OPEN 持仓快照作昨夜拥股基线（§6.2.2 末，可选）。

        业务意图：OPEN 快照记录开盘前的隔夜持仓，作为当日 T+1 可卖 / 续持决策的基线，
        与 INTRADAY（盘中刷新）/ CLOSE（收盘权威）按 snapshot_type 区分，互不覆盖（§6.5）。
        返回：落库的持仓行数。
        注：开盘前仅落持仓基线，不落资产 CLOSE（净值曲线只认收盘）。
        """
        td = self._resolve_trade_date(trade_date)
        pos_count = self._snapshot_positions(td, SnapshotType.OPEN)
        self._logger.info(
            "snapshot_open_done",
            account_id=self._account_id,
            trade_date=str(td),
            positions=pos_count,
        )
        return pos_count
