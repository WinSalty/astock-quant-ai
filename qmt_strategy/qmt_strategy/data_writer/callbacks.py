"""实时回调采集（§6.2.1 两条腿之一：事件驱动增量）。

业务意图：把 xttrader（XtQuantTrader）注册的各类回调统一接入回流落库写端（DataWriter）与本地
下单台账（LocalLedger）。回调路径承载两类不可替代的事实：
1) 成交回报 on_stock_trade —— 唯一事实源，绝不可丢；
2) 废单 / 拒单（on_order_error）/ 撤单失败（on_cancel_error）—— 回调独有，隔日 query_* 兜底
   无法还原历史失败态（§6.2.1 / §6.9），必须在事件发生当下落库，避免「计划单凭空消失」。

依赖分层：本模块只依赖 DataWriter / LocalLedger 协议与 normalize 纯函数，不直接 import xtquant；
真实链路把本类实例 register_callback 给 XtQuantTrader，单测用 contracts.xt_objects 的 FakeXt* 驱动。

时间口径：一律经 normalize → time_utils.qmt_ts_to_db 双写（UTC naive + 东八区原值），本模块不手工 ±8h。
data_source：回调路径产生的成交 / 委托记录一律 CALLBACK（区别于收盘兜底 / 断线补采的 QUERY_BACKFILL）。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Optional

from ..contracts.enums import DataSource, OrderState, OrderStatus, SnapshotType, TradeSide
from ..contracts.models import OrderRecord
from ..contracts.protocols import DataWriter, LocalLedger, StructLogger
from . import normalize
from .normalize import SideResolver, StatusResolver

# 回调侧需要的「交易日提供器」签名：返回当日 trade_date（东八区自然日，§6.6）。
# 用可注入的 Callable 而非直接 clock，便于上游集中口径（如盘后补采用收盘日而非系统日）。
TradeDateProvider = Callable[[], date]

# OrderStatus（落库委托终态枚举）→ OrderState（台账本地状态机）映射（§6.2.1 on_stock_order）。
# 业务意图：on_stock_order 落库用 OrderStatus，但同步台账要用 OrderState；两者成员名多有重叠，
# 仍显式建表而非按名字硬转，避免后续任一枚举增删成员时静默串态。
# 边界：REPORTED/PART_TRADED/TRADED/CANCELLED 语义一一对应；REJECTED/ERROR 均为终态失败，
#       台账侧分别落 REJECTED / ERROR（与 OrderState 终态集合一致，幂等收口后不再推进）。
_STATUS_TO_STATE = {
    OrderStatus.REPORTED: OrderState.REPORTED,
    OrderStatus.PART_TRADED: OrderState.PART_TRADED,
    OrderStatus.TRADED: OrderState.TRADED,
    OrderStatus.CANCELLED: OrderState.CANCELLED,
    OrderStatus.REJECTED: OrderState.REJECTED,
    OrderStatus.ERROR: OrderState.ERROR,
}


def _no_op() -> None:
    """on_disconnected_hook 默认空实现：未注入重连补采钩子时不做任何动作（§6.2.3 补采本身不在此）。"""
    return None


class ExecCallback:
    """xttrader 回调接入器（§6.2.1）。把各类推送规整后落库 + 同步本地台账。

    依赖注入：
      - data_writer：DataWriter，负责规整后记录的幂等 upsert（含 snapshot_type 注入）；
      - ledger：LocalLedger，回调侧只按 order_id 同步状态 / 累计成交，不新建计划单；
      - logger：StructLogger，断线等事件留痕（不写敏感信息）；
      - account_id：当前账户号（落 qmt_*.account_id）；
      - trade_date_provider：每次回调取当日 trade_date（东八区自然日），避免跨日 ID 复用串号（§6.5）；
      - on_disconnected_hook：断线时触发重连 + 补采的钩子（默认空），补采逻辑本身不在本模块；
      - side_resolver / status_resolver：方向 / 状态解析器，默认走 normalize 的实测默认表，可注入覆盖。

    幂等口径：成交 / 委托落库的幂等由 repository 唯一键 + COALESCE 保证（§6.5）；本模块只负责把
    每条回调如实规整并转交，不在此去重。台账侧 sync_status / add_fill 对未知 order_id 自身忽略，
    故回调对手工单 / 非本系统单不会越权改写台账。
    """

    def __init__(
        self,
        data_writer: DataWriter,
        ledger: LocalLedger,
        logger: StructLogger,
        *,
        account_id: str,
        trade_date_provider: TradeDateProvider,
        on_disconnected_hook: Callable[[], None] = _no_op,
        side_resolver: SideResolver = normalize.default_side_resolver,
        status_resolver: StatusResolver = normalize.default_status_resolver,
        position_sink: Optional[Callable[[Any], None]] = None,
        sell_revert_sink: Optional[Callable[[str], None]] = None,
    ):
        self._dw = data_writer
        self._ledger = ledger
        self._logger = logger
        self._account_id = account_id
        self._trade_date_provider = trade_date_provider
        self._on_disconnected_hook = on_disconnected_hook
        self._side_resolver = side_resolver
        self._status_resolver = status_resolver
        # 持仓回写下沉（评审 P0-A2 修复）：成交回报除落库 + 台账累计外，还需回写持仓状态机
        # （买入建/加仓守 T+1、卖出扣减推进状态），否则 PositionManager 永远空集、永不卖出。
        # 由 Engine 注入一个接收规整后 TradeRecord 的回调；缺省 None 表示不回写（离线/单测兼容）。
        self._position_sink = position_sink
        # 卖单终态失败复位钩子（评审二轮 P1#31）：卖单被拒/全撤且零成交时，把持仓单元从 SELLING 复位回
        # HOLDING（供下一轮重挂），否则止损/破位清仓永久失效。由 Engine 注入 (ts_code)->None；缺省不复位。
        self._sell_revert_sink = sell_revert_sink

    def set_on_disconnected_hook(self, hook: Callable[[], None]) -> None:
        """运行期回填断线钩子（评审三轮 EXEC-sched-01）。

        业务意图：解决构造期循环依赖——Engine（含本回调）先于 ConnectionGuard 装配，故构造时拿不到 guard
        引用。run.py 在 guard 构造完后回填 `lambda: guard.on_disconnected()`，使真实断线先经 guard 换新
        session 重连（成功后再由 guard 触发 engine.on_reconnect_backfill 补采），而非绕过 guard 直连补采。
        边界：hook 异常仍由 on_disconnected 的既有 try 口径处理；重跑幂等覆盖（同一 hook 多次回填无副作用）。
        """
        self._on_disconnected_hook = hook

    def _maybe_revert_sell_unit(self, order_id: Optional[int]) -> None:
        """卖单终态失败 → 复位持仓 SELLING 态（评审二轮 P1#31）。

        判据：台账据 order_id 反查到的单元是 SELL 方向、已落终态失败（CANCELLED/REJECTED/ERROR）、
        且零成交（filled_volume==0）→ 该卖单确未卖出任何量，持仓须从 SELLING 复位回 HOLDING 重挂。
        部成（filled_volume>0）由 sync_status 收口为 PART_TRADED、持仓由卖出成交回报推进，不在此复位。
        """
        if order_id is None or self._sell_revert_sink is None:
            return
        entry = self._ledger.get_by_order_id(order_id)
        if entry is None:
            return
        terminal_fail = entry.state in (
            OrderState.CANCELLED, OrderState.REJECTED, OrderState.ERROR
        )
        if entry.side == TradeSide.SELL and terminal_fail and (entry.filled_volume or 0) == 0:
            self._sell_revert_sink(entry.ts_code)

    # ------------------------------------------------------------------
    # 成交回报：唯一事实源，绝不可丢（§6.2.1 on_stock_trade）
    # ------------------------------------------------------------------
    def on_stock_trade(self, t: Any) -> None:
        """成交回报落库 + 同步台账累计成交。

        流程：
          1) normalize_trade 规整为 TradeRecord（代码归一 / 方向映射 / 时间双写 / trade_date）；
             data_source 固定 CALLBACK（成交回报来自实时推送）。
          2) data_writer.upsert_trade 幂等落库（同 traded_id 后到覆盖，§6.5）。
          3) ledger.add_fill(order_id, traded_volume, traded_price)：把本笔成交累计回台账，
             推进 PART_TRADED / TRADED。台账无该 order_id（手工单 / 非本系统单）时 add_fill 内部忽略。
        边界：order_id 缺失（getattr 取 None）时仍照常落库，仅台账侧无法关联（add_fill 对 None 忽略）。
        """
        rec = normalize.normalize_trade(
            t,
            account_id=self._account_id,
            trade_date=self._trade_date_provider(),
            data_source=DataSource.CALLBACK,
            side_resolver=self._side_resolver,
        )
        # 顺序修正（评审二轮 P2#62）：成交是唯一事实源——必须【先更新内存权威】（台账累计 + 持仓状态机），
        # 【再写 qmt_* 镜像】。原实现先 upsert_trade，一旦落库异常抛出，add_fill/持仓回写就被跳过，使同一笔
        # 成交从台账与持仓状态机双双丢失（漏卖 + 幂等失效）。台账 add_fill 是 write-behind（仅入队不阻塞、
        # 不抛 DB 异常）、持仓回写是纯内存，故二者优先且稳；qmt_trade 镜像（供对账/复盘）置后并隔离其异常，
        # 即使镜像失败，台账/持仓的权威事实已落定，缺失的 qmt_trade 行可由收盘兜底/断线补采恢复。
        order_id = getattr(t, "order_id", None)
        # 1) 台账累计成交（按 traded_id 去重，断线重连回放/券商重投不重复累计，§6.5/§4.4(4)）。
        self._ledger.add_fill(
            order_id,
            getattr(t, "traded_id", None),
            getattr(t, "traded_volume", None),
            getattr(t, "traded_price", None),
        )
        # 2) 回写持仓状态机（评审 P0-A2）：BUY 建/加仓（守 T+1）、SELL 扣减推进。
        if self._position_sink is not None:
            self._position_sink(rec)
        # 3) 最后写 qmt_trade 镜像：异常不得回滚已完成的台账/持仓权威更新，单独强告警（待兜底补采恢复）。
        try:
            self._dw.upsert_trade(rec)
        except Exception as exc:  # noqa: BLE001 镜像落库失败不丢成交事实，仅告警
            self._logger.error(
                "trade_mirror_upsert_failed",
                account_id=self._account_id,
                order_id=order_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # 委托状态变化：已报 / 部成 / 已成 / 已撤 / 废单（§6.2.1 on_stock_order）
    # ------------------------------------------------------------------
    def on_stock_order(self, o: Any) -> None:
        """委托回报落库 + 按 order_id 同步台账状态。

        流程：
          1) normalize_order 规整为 OrderRecord（含 order_status 经 status_resolver 映射）；data_source=CALLBACK。
          2) data_writer.upsert_order 幂等落库。
          3) ledger.sync_status(order_id, 映射后的 OrderState, status_msg)：把委托终态同步回台账。
        边界：order_id 缺失时不调用 sync_status（台账以 order_id 为关联键，无键无从同步）。
        """
        rec = normalize.normalize_order(
            o,
            account_id=self._account_id,
            trade_date=self._trade_date_provider(),
            data_source=DataSource.CALLBACK,
            side_resolver=self._side_resolver,
            status_resolver=self._status_resolver,
        )
        self._dw.upsert_order(rec)
        # 同步台账状态：OrderStatus → OrderState 显式映射，未知态兜底 REPORTED（在途，不臆造终态）。
        if rec.order_id is not None:
            state = _STATUS_TO_STATE.get(rec.order_status, OrderState.REPORTED)
            self._ledger.sync_status(rec.order_id, state, rec.status_msg)
            # 卖单终态失败 → 复位持仓 SELLING 态（评审二轮 P1#31）：拒单/全撤且零成交时让持仓回 HOLDING 重挂。
            self._maybe_revert_sell_unit(rec.order_id)

    # ------------------------------------------------------------------
    # 下单失败：废单 / 拒单（回调独有，§6.2.1 on_order_error）
    # ------------------------------------------------------------------
    def on_order_error(self, e: Any) -> None:
        """下单失败落库 + 同步台账 ERROR。

        业务意图：on_order_error 是回调独有事实——下单未被交易所接受（废单 / 拒单），
        隔日 query_* 不返回该失败态历史，若不落库则计划单凭空消失（§6.2.1）。
        实现：因 XtOrderError 字段稀少（通常只有 order_id / error_id / error_msg，部分版本含
        stock_code / order_remark），这里构造 OrderRecord 骨架：order_status=ERROR，
        error_id / error_msg 来自 e，其余字段缺失则 getattr 容错落 None / 0（DDL 已设可空 / 缺省）。
        幂等：同一 order_id upsert（唯一键 account_id+trade_date+order_id），不新增重复行；
              若后续 on_stock_order 带来更明确终态，由后到覆盖语义接管（§6.5）。
        边界：error_id 经 _to_int 容错；缺 stock_code 则 ts_code / qmt_stock_code 落 None 不抛错。
        """
        order_id = normalize._to_int(getattr(e, "order_id", None))
        qmt_stock_code = getattr(e, "stock_code", None)
        ts_code = normalize.identity.resolve_code(qmt_stock_code)
        # order_id 缺失的下单失败回报（评审三轮 EXEC-DW-05）：原实现照常构造 OrderRecord 喂 qmt_order
        # (order_id INTEGER NOT NULL + 唯一键含 order_id)，INSERT 触发 NOT NULL 约束失败被后台写线程静默吞，
        # 仅一条 async_write_failed 日志 → "下单失败"这一回调独有、隔日 query_* 无法还原的事实彻底丢失。
        # 这里不喂带 NOT NULL 唯一键的 qmt_order，改为强告警留痕(失败上下文齐全)，杜绝静默吞；台账无 order_id 可
        # 定位故同样不推进(同步下单失败已由 order_executor 按 biz_no 置 ERROR，见 EXEC-DW-05 核验)。
        if order_id is None:
            self._logger.error(
                "order_error_missing_order_id_not_persisted",
                account_id=self._account_id,
                ts_code=ts_code,
                error_id=normalize._to_int(getattr(e, "error_id", None)),
                error_msg=getattr(e, "error_msg", None),
                order_remark=getattr(e, "order_remark", None),
            )
            return
        rec = OrderRecord(
            account_id=self._account_id,
            trade_date=self._trade_date_provider(),
            ts_code=ts_code,                 # 多数版本 XtOrderError 不含 stock_code → None
            qmt_stock_code=qmt_stock_code,
            order_id=order_id,
            trade_side=self._side_resolver(
                getattr(e, "order_type", None), getattr(e, "offset_flag", None)
            ),
            order_volume=normalize._to_int(getattr(e, "order_volume", None)) or 0,
            order_status=OrderStatus.ERROR,  # 回调独有：下单失败终态
            error_id=normalize._to_int(getattr(e, "error_id", None)),
            error_msg=getattr(e, "error_msg", None),
            order_remark=getattr(e, "order_remark", None),  # 部分版本含，缺则 None
            data_source=DataSource.CALLBACK,
        )
        self._dw.upsert_order(rec)
        # 同步台账：把对应计划单推进 ERROR 终态（台账无该 order_id 则 sync_status 自身忽略）。
        if order_id is not None:
            self._ledger.sync_status(order_id, OrderState.ERROR, getattr(e, "error_msg", None))
            # 卖单废单/拒单 → 复位持仓 SELLING 态（评审二轮 P1#31）。
            self._maybe_revert_sell_unit(order_id)

    # ------------------------------------------------------------------
    # 撤单失败：留痕（回调独有，§6.2.1 on_cancel_error）
    # ------------------------------------------------------------------
    def on_cancel_error(self, e: Any) -> None:
        """撤单失败留痕。

        业务意图：撤单失败也是回调独有事实——仅在既有委托行追加 cancel_failed=1 + error_*，
        绝不改 order_status 终态、不新增重复行（§6.2.1）。委托当前真实终态仍由 on_stock_order 决定。
        边界：error_id 经 _to_int 容错；缺字段落 None。
        """
        order_id = normalize._to_int(getattr(e, "order_id", None))
        error_id = normalize._to_int(getattr(e, "error_id", None))
        error_msg = getattr(e, "error_msg", None)
        # 委托唯一键以 order_id 关联，缺 order_id 无从定位既有行，记 warn 后返回（不臆造新行）。
        if order_id is None:
            self._logger.warn("on_cancel_error_missing_order_id", account_id=self._account_id)
            return
        self._dw.mark_cancel_failed(self._account_id, order_id, error_id, error_msg)

    # ------------------------------------------------------------------
    # 盘中资产 / 持仓快照（§6.2.1 仅刷新当日 INTRADAY 行，不进历史净值）
    # ------------------------------------------------------------------
    def on_stock_asset(self, a: Any) -> None:
        """盘中资产回报落库 INTRADAY（§6.2.1 on_stock_asset）。

        业务意图：盘中仅刷新当日 INTRADAY 行，CLOSE 才是净值曲线唯一来源（§6.2.2）；
        故 snapshot_type 固定 INTRADAY，data_source 由 normalize 默认 QUERY 改写为 CALLBACK。
        """
        rec = normalize.normalize_account(
            a,
            account_id=self._account_id,
            trade_date=self._trade_date_provider(),
            snapshot_type=SnapshotType.INTRADAY,
            data_source=DataSource.CALLBACK,
        )
        self._dw.upsert_account_daily(rec, SnapshotType.INTRADAY)

    def on_stock_position(self, p: Any) -> None:
        """盘中持仓回报落库 INTRADAY（§6.2.1 on_stock_position）。

        业务意图：盘中仅刷新当日 INTRADAY 行；持仓复盘只认 CLOSE（§6.2.2）。
        """
        rec = normalize.normalize_position(
            p,
            account_id=self._account_id,
            trade_date=self._trade_date_provider(),
            snapshot_type=SnapshotType.INTRADAY,
            data_source=DataSource.CALLBACK,
        )
        self._dw.upsert_position(rec, SnapshotType.INTRADAY)

    # ------------------------------------------------------------------
    # 断线检测：驱动重连 + 补采（§6.2.1 on_disconnected）
    # ------------------------------------------------------------------
    def on_disconnected(self) -> None:
        """断线回报：告警并触发重连补采钩子（补采逻辑本身不在本模块，§6.2.3）。

        业务意图：xttrader 断开不自动重连且退出即丢推送（§2.2 强约束），断线期间缺失的明细要靠
        重连后的 query_* 全量补采（data_source=QUERY_BACKFILL）。本模块只负责告警 + 触发钩子，
        实际补采由 on_disconnected_hook 注入的实现完成。
        边界：钩子异常不应吞掉断线告警，但也不在此重试——交由钩子实现自行退避。
        """
        self._logger.warn("disconnected", account_id=self._account_id)
        self._on_disconnected_hook()
