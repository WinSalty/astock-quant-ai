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
        sell_on_road_release_sink: Optional[Callable[[str, int, Optional[int]], None]] = None,
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
        # 卖单【部成后剩余委托】终态失败的在途量精确回扣钩子（评审 F02/F16）：部成单 filled>0 时不走零成交复位，
        # 但其未成在途量须按本单未成量回扣，否则 sellable_remaining 被永久下调=漏卖。由 Engine 注入
        # (ts_code, unfilled_qty)->None；缺省不回扣（向后兼容旧装配/单测）。
        self._sell_on_road_release_sink = sell_on_road_release_sink

    def set_on_disconnected_hook(self, hook: Callable[[], None]) -> None:
        """运行期回填断线钩子（评审三轮 EXEC-sched-01）。

        业务意图：解决构造期循环依赖——Engine（含本回调）先于 ConnectionGuard 装配，故构造时拿不到 guard
        引用。run.py 在 guard 构造完后回填 `lambda: guard.on_disconnected()`，使真实断线先经 guard 换新
        session 重连（成功后再由 guard 触发 engine.on_reconnect_backfill 补采），而非绕过 guard 直连补采。
        边界：hook 异常仍由 on_disconnected 的既有 try 口径处理；重跑幂等覆盖（同一 hook 多次回填无副作用）。
        """
        self._on_disconnected_hook = hook

    def _maybe_revert_sell_unit(self, order_id: Optional[int], terminal_status: Any) -> None:
        """卖单终态失败 → 按本单未成量精确回扣在途冻结量 / 复位持仓 SELLING 态（评审二轮 P1#31 + F02/F16 + review）。

        判据修正（review F02 死路）：**用回报的原始终态状态 terminal_status 判定撤/废/拒，绝不读台账 state**——
        on_stock_order 先调 ledger.sync_status，对【CANCELLED/REJECTED 且 filled>0】的部成撤单已 fill-aware 收口为
        PART_CANCELLED（local_ledger.sync_status，评审 doc/21 B1；原收口 PART_TRADED），若仍按 entry.state∈终态集合
        判定，部成单永远进不来、on_road 当日不回扣 = 漏卖（F02 本要堵的窗口）。故改以回报原始状态判定。

        统一精确回扣（review #2）：零成交与部成【统一】按本单未成量 unfilled=plan_volume-filled 经
        sell_on_road_release_sink 精确回扣 on_road（绝不整体清零误清同单元其它在途单的冻结量）；release 内在 on_road
        归零且单元仍 SELLING 时复位 HOLDING。仅当未接 release sink（旧装配/单测只配 revert sink）时，零成交回退旧
        revert 复位路径（向后兼容）。
        边界：台账无该 order_id / 非 SELL / 非撤废拒终态 / 无未成量 → 不动。
        """
        if order_id is None:
            return
        entry = self._ledger.get_by_order_id(order_id)
        if entry is None or entry.side != TradeSide.SELL:
            return
        # 撤/废/拒终态判定取【回报原始状态】，不取台账 state（sync_status 已对部成撤单收口为 PART_CANCELLED）。
        if terminal_status not in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.ERROR):
            return
        filled = entry.filled_volume or 0
        unfilled = max(0, (entry.plan_volume or 0) - filled)
        if unfilled <= 0:
            return  # 本单全成（不该来撤/废）或无未成量，无需回扣
        if self._sell_on_road_release_sink is not None:
            # 精确按本单未成量回扣 on_road（多单在途不误清），传 order_id 供 release 按单去重（防双面回报/重投
            # 重复回扣误清兄弟在途单，review 幂等）；on_road 归零时由 release 内复位 SELLING→HOLDING。
            self._sell_on_road_release_sink(entry.ts_code, unfilled, entry.order_id)
        elif filled == 0 and self._sell_revert_sink is not None:
            # 向后兼容：仅配 revert sink 时，零成交单走旧复位路径（整体清零 on_road + 复位 HOLDING）。
            self._sell_revert_sink(entry.ts_code)

    # ------------------------------------------------------------------
    # 成交回报：唯一事实源，绝不可丢（§6.2.1 on_stock_trade）
    # ------------------------------------------------------------------
    def _warn_if_dirty(self, kind: str, key: Any, fields: dict) -> None:
        """J-4/5/6 执行侧脏行源头告警（doc/29 C）：normalize 产出的关键字段缺失/不可判时 warn 一次。

        fields 取值约定：普通字段 None 即视为缺；trade_side_unknown=True 表示方向不可判（UNKNOWN）。
        把缺失项拼成 'missing:col1,col2'（与信号侧回流 ingest_data_quality 留痕格式对称），便于监控/排查定位脏源；
        仅告警、不阻断落库（脏行仍按放宽后的可空列入库，由对账/复盘兜底）。无缺失项则不产生噪声日志。
        """
        missing = []
        for name, value in fields.items():
            if name == "trade_side_unknown":
                if value:
                    missing.append("trade_side")
            elif value is None:
                missing.append(name)
        if missing and self._logger is not None:
            self._logger.warn(
                "callback_normalize_dirty_row",
                account_id=self._account_id,
                kind=kind,
                key=key,
                data_quality="missing:" + ",".join(missing),
            )

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
        # J-4/5/6 执行侧脏行源头告警（doc/29 C）：normalize 把脏字段（归一失败 ts_code=None、方向不可判 UNKNOWN、
        # 价/量/时间缺）落 None 后照常入库（信号侧四表已放宽 NOT NULL + ingest_data_quality 留痕），但执行侧须在
        # 源头 warn 一次——使脏回报在落库前可被监控/排查定位，不被静默淹没（与信号侧回流缺测留痕对称）。
        self._warn_if_dirty(
            "trade",
            getattr(t, "traded_id", None),
            {
                "ts_code": rec.ts_code,
                "trade_side_unknown": rec.trade_side == TradeSide.UNKNOWN,
                "traded_price": rec.traded_price,
                "traded_volume": rec.traded_volume,
                "traded_time": rec.traded_time,
            },
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
        # J-4/5/6 执行侧脏行源头告警（doc/29 C）：委托归一脏字段（ts_code=None、方向 UNKNOWN、order_volume 缺）在源头 warn。
        self._warn_if_dirty(
            "order",
            getattr(o, "order_id", None),
            {
                "ts_code": rec.ts_code,
                "trade_side_unknown": rec.trade_side == TradeSide.UNKNOWN,
                "order_volume": rec.order_volume,
            },
        )
        self._dw.upsert_order(rec)
        # 同步台账状态：OrderStatus → OrderState 显式映射，未知态兜底 REPORTED（在途，不臆造终态）。
        if rec.order_id is not None:
            state = _STATUS_TO_STATE.get(rec.order_status, OrderState.REPORTED)
            self._ledger.sync_status(rec.order_id, state, rec.status_msg)
            # 卖单终态失败 → 回扣在途量 / 复位 SELLING（评审二轮 P1#31 + F02/F16 + review）：传【回报原始状态】
            # rec.order_status，而非已被 sync_status 收口的台账 state，否则部成撤单进不来 release 分支=漏卖。
            self._maybe_revert_sell_unit(rec.order_id, rec.order_status)

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
            # 卖单废单/拒单 → 回扣在途量 / 复位 SELLING（评审二轮 P1#31 + F02/F16）：on_order_error 即 ERROR 终态。
            self._maybe_revert_sell_unit(order_id, OrderStatus.ERROR)

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
