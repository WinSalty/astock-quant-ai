"""进程编排入口 Engine（§1.5 模块协作主链路 / §7.4 上线 checklist）。

业务意图：把各模块按「盘前装载 → 竞价轮询 → 自决择时/价位 → 下单 → 管仓位 → 风控 → 回流 → 对账」
的交易闭环装配为一个可常驻的引擎。本层只做【装配 + 生命周期编排 + 安全闸门 gating】，不重复任何
业务逻辑（业务都在各模块内）。

安全闸门（编排层强制，§7.1.5/§7.1.6）：
- kill_switch=True：order_executor.place 内部已熔断（只采集不下单），编排层不再额外下单；
- auction_timing_enabled=False：竞价段（order_phase=AUCTION）的买入决策【只留痕不下单】，
  仅开盘后（OPENING）决策才下单——实测通过前竞价择时不进实盘下单（§7.1.6 强约束）；
- open_new_position_allowed=False（空仓 / 降级 / 契约失败）：一律不开新仓，只守仓（§2.6）。

外部依赖（xtquant / MySQL / 信号侧取数 / 交易日历）全部经 EngineDeps 注入，真实部署由 main() 装配
真实实现，单测注入 fake，故本层在任意平台可装配、可测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from ..common.time_utils import east8_trade_date
from ..config.settings import Settings
from ..contracts.enums import (
    EntryAction,
    OrderPhase,
    RiskVerdict,
    SellActionType,
    TradeSide,
)
from ..contracts.models import (
    AuctionSnapshot,
    EntryDecision,
    OrderBook,
    PlanRow,
    SignalPrior,
    WatchlistContext,
)
from ..contracts.protocols import (
    Clock,
    QmtRepository,
    SelectedStockSource,
    StructLogger,
    TickSource,
    TradeCalendar,
    XtTraderLike,
)
from ..data_writer.callbacks import ExecCallback
from ..data_writer.data_writer import DataWriterImpl
from ..data_writer.snapshot_job import SnapshotJob
from ..entry.entry_router import EntryRouter
from ..order.local_ledger import InMemoryLocalLedger
from ..order.order_executor import OrderExecutor
from ..position.position_manager import PositionManager
from ..position.sell_decider import SellDecider
from ..reconcile.reconcile import Reconcile
from ..risk.risk import Risk
from ..auction.auction_poller import AuctionPoller
from ..watchlist.watchlist_loader import WatchlistLoader

# QMT order_stock 卖出方向常量（占位，实测以 xtconstant.STOCK_SELL 为准，与 order_executor 同口径）。
XT_ORDER_TYPE_SELL = 24
XT_PRICE_TYPE_FIX = 11


@dataclass
class EngineDeps:
    """引擎装配所需的全部外部依赖（注入，便于真实/测试两套实现切换）。"""

    settings: Settings
    clock: Clock
    logger: StructLogger
    calendar: TradeCalendar
    trader: XtTraderLike
    account: Any
    account_id: str
    tick_source: TickSource
    selected_source: SelectedStockSource
    repository: QmtRepository
    fallback_source: Optional[SelectedStockSource] = None
    # 先验取数：(ts_code, today) -> Optional[SignalPrior]，缺省无先验（纯技术退出）。
    prior_provider: Callable[[str, date], Optional[SignalPrior]] = field(
        default=lambda ts, today: None
    )
    # 本地下单台账（可注入）：本地化方案注入 PersistentLocalLedger（SQLite 持久 + 重启重建）；
    # 缺省 None → Engine 用内存台账（向后兼容，离线/单测用）。
    ledger: Optional[Any] = None
    # 持久化 flush 钩子（可注入）：本地化方案注入 LocalStorage.flush；
    # close_batch 在对账前调用它，保证「写后异步」的回流已落盘、对账读到一致数据（doc/05 关键不变量）。
    flush_hook: Callable[[], Any] = field(default=lambda: None)


class Engine:
    """打板量化 QMT 执行引擎（编排层）。"""

    def __init__(self, deps: EngineDeps):
        d = self._deps = deps
        s = self._settings = deps.settings
        self._clock = deps.clock
        self._logger = deps.logger
        self._calendar = deps.calendar
        self._account_id = deps.account_id

        # 当日交易日提供器（东八区自然日，§6.6）：回流落库 / 快照 / 对账统一口径。
        self._today_provider: Callable[[], date] = lambda: east8_trade_date(self._clock.now_utc())

        # —— 回流写端 + 本地台账 ——
        # 台账可注入（本地化方案注入 PersistentLocalLedger 以持久 + 重启重建）；缺省内存台账。
        self._ledger = deps.ledger if deps.ledger is not None else InMemoryLocalLedger()
        self._data_writer = DataWriterImpl(deps.repository, deps.logger, deps.clock)

        # —— 名单 / 持仓 / 风控 / 决策 ——
        self._watchlist = WatchlistLoader(
            deps.selected_source, deps.calendar, deps.logger, s, fallback=deps.fallback_source
        )
        self._position = PositionManager(deps.calendar, deps.clock, deps.logger, deps.prior_provider)
        self._risk = Risk(s, deps.clock, deps.logger)
        self._sell = SellDecider(s, deps.clock, deps.logger)
        self._entry = EntryRouter(s, deps.clock, deps.logger, decision_log=[])
        self._order = OrderExecutor(
            deps.trader, deps.account, deps.account_id, self._ledger, s, deps.clock, deps.logger
        )

        # —— 回调（落库 + 台账 + 持仓建仓回写 + 断线补采钩子）——
        self._callback = ExecCallback(
            self._data_writer, self._ledger, deps.logger,
            account_id=deps.account_id,
            trade_date_provider=self._today_provider,
            on_disconnected_hook=self.on_reconnect_backfill,
        )

        # —— 收盘 / 补采 + 对账 ——
        self._snapshot = SnapshotJob(
            deps.trader, deps.account, self._data_writer, deps.logger, deps.clock,
            account_id=deps.account_id, trade_date_provider=self._today_provider,
        )
        self._reconcile = Reconcile(
            self._ledger, deps.repository, deps.logger, deps.calendar, account_id=deps.account_id
        )

        # —— 竞价轮询（plan_provider 取当日可交易名单的计划行；router_sink 接决策 + 下单 gating）——
        self._poller = AuctionPoller(
            deps.tick_source, self._plan_provider, self._router_sink, s, deps.clock, deps.logger
        )

        # 运行期状态：当日 watchlist 上下文 + 计划行映射（盘中只读）。
        self._context: Optional[WatchlistContext] = None
        self._plan_map: Dict[str, PlanRow] = {}
        # 当日情绪周期（供 risk.gate 空仓判定，盘前装载时记录）。
        self._market_state: Optional[str] = None
        # 行情 / 下单通道健康标志（断线时置 False，恢复后置 True；供 risk.gate 安全默认）。
        self._market_feed_ok = True
        self._trade_conn_ok = True

    # ------------------------------------------------------------------
    # 对外暴露（供 main / 连接守护注册回调 / 调度）
    # ------------------------------------------------------------------
    @property
    def callback(self) -> ExecCallback:
        """供连接守护 register_callback 注册到 xttrader 的回调对象。"""
        return self._callback

    # ------------------------------------------------------------------
    # 盘前：装载当日 watchlist + 推进持仓状态
    # ------------------------------------------------------------------
    def prewarm(self, today: date) -> WatchlistContext:
        """盘前一次性装载（§2.3）：装 watchlist 上下文、建计划映射、推进持仓状态（守 T+1 转 HOLDING）。"""
        ctx = self._watchlist.load(today)
        self._context = ctx
        # 计划映射：仅可交易名单进竞价/建仓（观察名单只跟踪不下单，§2.3）。
        self._plan_map = {code: entry.to_plan_row() for code, entry in ctx.tradable.items()}
        # 记录当日情绪周期（从可交易/观察任一条目透传的 market_state 取，供风控空仓判定）。
        self._market_state = self._extract_market_state(ctx)
        # 推进持仓状态：跨买入日 LOCKED_T1 → HOLDING，并刷新先验挂接（§5.6）。
        self._position.refresh_state(today)
        self._logger.info(
            "engine_prewarmed",
            trade_date=str(today),
            tradable=len(self._plan_map),
            open_new_position_allowed=ctx.open_new_position_allowed,
            degraded=ctx.degraded,
        )
        return ctx

    @staticmethod
    def _extract_market_state(ctx: WatchlistContext) -> Optional[str]:
        """从上下文条目透传字段取当日 market_state（可交易优先，其次观察名单）。"""
        for entry in ctx.tradable.values():
            if entry.market_state is not None:
                return entry.market_state
        for entry in ctx.watch_only:
            if entry.market_state is not None:
                return entry.market_state
        return None

    # ------------------------------------------------------------------
    # 竞价轮询（§3 / §4 建仓链路）
    # ------------------------------------------------------------------
    def _plan_provider(self) -> Dict[str, PlanRow]:
        """auction_poller 取当日参与建仓的计划行映射（§3.5 codes 来源）。"""
        return dict(self._plan_map)

    def _router_sink(self, snap: AuctionSnapshot) -> None:
        """每帧快照入口（§3.6 push_to_router）：路由决策 + 下单 gating。

        gating（编排层强制）：
        - 无对应计划行（观察名单 / 已剔除）→ 不路由；
        - open_new_position_allowed=False（空仓 / 降级）→ 只采集留痕、不开新仓（§2.6）；
        - 竞价段决策（order_phase=AUCTION）：仅当 auction_timing_enabled=True 才下单，否则只留痕
          （§7.1.6：实测通过前竞价择时不进实盘下单）；OPENING 决策正常下单；
        - kill_switch 的最终熔断在 order_executor.place 内（双保险）。
        """
        plan = self._plan_map.get(snap.ts_code)
        if plan is None:
            return
        decision = self._entry.on_auction_snapshot(snap, plan)
        # 无决策（defer）/ SKIP（留痕已在 router 内完成）→ 不下单。
        if decision is None or decision.action == EntryAction.SKIP:
            return
        # 空仓 / 降级：禁开新仓（守仓不受影响，卖出在 run_sell_pass）。
        if self._context is None or not self._context.open_new_position_allowed:
            self._logger.info(
                "engine_skip_open_blocked",
                ts_code=decision.ts_code,
                reason="open_new_position_allowed=False",
            )
            return
        # 竞价择时闸门：竞价单仅在实测开关打开时下单，否则只采集留痕（§7.1.6）。
        if decision.order_phase == OrderPhase.AUCTION and not self._settings.auction_timing_enabled:
            self._logger.info(
                "engine_auction_timing_disabled_collect_only",
                ts_code=decision.ts_code,
                reason="QMT_AUCTION_TIMING_ENABLED=false",
            )
            return
        # 通过全部闸门 → 下单（place 内仍有 kill_switch / 幂等 / 资金校验）。
        self._order.place(decision)

    def run_auction(self, sleep_fn=None, max_loops: Optional[int] = None) -> None:
        """启动竞价轮询主循环（§3.6）。sleep_fn / max_loops 供测试注入有限轮次。"""
        if sleep_fn is None:
            self._poller.run(max_loops=max_loops)
        else:
            self._poller.run(sleep_fn=sleep_fn, max_loops=max_loops)

    def sweep_ttl(self, now: Optional[datetime] = None) -> List[str]:
        """TTL 到期巡检（§4.5 / 评审 low#3）：供盘中主循环周期调用，驱动超时撤单。"""
        return self._order.sweep_expired(now)

    # ------------------------------------------------------------------
    # 卖出链路（§5：守 T+1 + 风控闸门 + 竞价/分时定夺）
    # ------------------------------------------------------------------
    def run_sell_pass(self, today: date, books: Dict[str, OrderBook], *, session: str = "intraday") -> List[str]:
        """对当前可卖持仓跑一遍卖出决策并下单（§5.3 决策表）。

        session：'auction'（可卖日 9:15–9:25 竞价定夺）/ 'intraday'（9:30 起分时定夺）。
        流程：sellable_units → 每单元过 risk.gate（FREEZE 则跳过该单元）→ sell_decider 决策 →
        REDUCE/CLEAR 则按 clamp_sell_volume(决策量, can_use_volume) 下卖单（绝不超量，§5.4.3）。
        返回本轮发出卖单的 ts_code 列表。
        """
        sold: List[str] = []
        for unit in self._position.sellable_units(today):
            # 风控闸门：行情/下单中断 → FREEZE 跳过；空仓 SELL_ONLY_HOLD 不影响卖出。
            verdict = self._risk.gate(
                market_state=self._market_state,
                market_feed_ok=self._market_feed_ok,
                trade_conn_ok=self._trade_conn_ok,
            )
            if verdict.verdict == RiskVerdict.FREEZE:
                self._logger.warn("engine_sell_frozen", ts_code=unit.ts_code, reason=verdict.reason)
                continue
            prior = self._deps.prior_provider(unit.ts_code, today)
            book = books.get(unit.ts_code)
            if book is None:
                # 无执行侧盘口：安全默认不主动卖（§5.4.3，没有可信盘口不做实时卖出决策）。
                continue
            if session == "auction":
                action = self._sell.decide_auction(unit, prior, book, risk_verdict=verdict.verdict)
            else:
                action = self._sell.decide_intraday(unit, prior, book, risk_verdict=verdict.verdict)
            if action.action in (SellActionType.REDUCE, SellActionType.CLEAR):
                if self._place_sell(unit, action.action, action.reduce_ratio, action.reason):
                    sold.append(unit.ts_code)
        return sold

    def _place_sell(self, unit, action_type, reduce_ratio, reason) -> bool:
        """发出卖出委托（守 T+1 量闸 + kill_switch + 价位由 QMT 自定）。

        卖量 = clamp(决策量, can_use_volume)，绝不超量（§5.4.3）；CLEAR 卖全部可用，
        REDUCE 按 reduce_ratio（缺省半仓）卖部分。价位本编排用现价占位（真实由盘口/滑点控制自定，§5.3）。
        边界：kill_switch=True 只采集不下单；可卖量 0 跳过。
        """
        if self._settings.kill_switch:
            self._logger.warn("engine_sell_kill_switch", ts_code=unit.ts_code)
            return False
        if action_type == SellActionType.CLEAR:
            decision_vol = unit.can_use_volume
        else:
            ratio = reduce_ratio if reduce_ratio is not None else __import__("decimal").Decimal("0.5")
            decision_vol = int(unit.can_use_volume * ratio)
        sell_vol = self._risk.clamp_sell_volume(decision_vol, unit.can_use_volume)
        if sell_vol <= 0:
            return False
        # 价位由 QMT 执行侧自定：此处用持仓现价占位（real：结合实时盘口 + 滑点控制）。
        price = float(unit.avg_cost)
        self._deps.trader.order_stock(
            self._deps.account, unit.ts_code, XT_ORDER_TYPE_SELL, sell_vol,
            XT_PRICE_TYPE_FIX, price, "", f"SELL|{reason}",
        )
        self._position.mark_selling(unit)
        self._logger.info(
            "engine_sell_placed", ts_code=unit.ts_code, volume=sell_vol, reason=reason
        )
        return True

    # ------------------------------------------------------------------
    # 收盘 / 断线补采 / 对账
    # ------------------------------------------------------------------
    def on_reconnect_backfill(self) -> None:
        """断线重连后补采当日缺口（§6.2.3）：由连接守护 / 回调 on_disconnected 钩子触发。"""
        self._trade_conn_ok = False
        try:
            self._snapshot.run_backfill()
        finally:
            # 补采尝试后恢复通道标志（真实重连成功与否由连接守护判定，这里乐观置回）。
            self._trade_conn_ok = True

    def close_batch(self, trade_date: Optional[date] = None) -> None:
        """收盘批次（§6.2.2）：全量明细兜底 + CLOSE 资产/持仓快照，随后触发对账（§6.7）。"""
        td = trade_date or self._today_provider()
        self._snapshot.run_close(td)
        # 对账前 flush 持久化队列：收盘兜底 + 盘中回调都是「写后异步」入队，必须先落盘，
        # 否则对账（读本机 SQLite）会读到不完整数据（doc/05 关键不变量「当日完成 / 一致读」）。
        self._deps.flush_hook()
        report = self._reconcile.run(td)
        self._logger.info(
            "engine_close_done",
            trade_date=str(td),
            matched_orders=report.matched_orders,
            discrepancies=len(report.order_discrepancies),
            needs_backfill=report.needs_backfill,
        )


def build_engine(deps: EngineDeps) -> Engine:
    """装配引擎（便于 main / 测试统一入口）。"""
    return Engine(deps)
