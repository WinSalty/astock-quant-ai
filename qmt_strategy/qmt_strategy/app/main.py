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
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from ..common.time_utils import east8_trade_date
from ..config.settings import Settings
from ..contracts.enums import (
    EntryAction,
    OrderPhase,
    PositionState,
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
        # 重启幂等（评审 P0-C4）：台账已由 LocalStorage.start()→load_from_db 重建，这里据此重置
        # biz 序号计数器，保证重启后新单序号严格大于历史、不与磁盘失败单同号覆盖/重复下单。
        self._order.rebuild_seq_counter()

        # —— 回调（落库 + 台账 + 持仓建仓回写 + 断线补采钩子）——
        # position_sink：评审 P0-A2 修复——把成交回报回写持仓状态机，否则永远空集、永不卖出。
        self._callback = ExecCallback(
            self._data_writer, self._ledger, deps.logger,
            account_id=deps.account_id,
            trade_date_provider=self._today_provider,
            on_disconnected_hook=self.on_reconnect_backfill,
            position_sink=self._apply_trade_to_position,
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
        # 日初总资产基线（评审 P0-B2）：prewarm 抓取，供 risk.gate 算账户日内回撤击穿。
        # None 表示未取到基线（查询失败/未装载）→ 回撤口径返回 None，不凭空冻结（其它闸门仍生效）。
        self._day_open_equity: Optional[Decimal] = None

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
        # 抓取日初总资产基线（评审 P0-B2）：供盘中 risk.gate 算账户日内回撤击穿。
        self._capture_day_open_equity()
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
    # 账户级风控输入（评审 P0-B2：喂给 risk.gate，使账户回撤击穿真正触发 FREEZE）
    # ------------------------------------------------------------------
    def _capture_day_open_equity(self) -> None:
        """抓取日初总资产作回撤基线（prewarm 调用）。查询失败 → None（不凭空冻结，安全默认）。"""
        try:
            asset = self._deps.trader.query_stock_asset(self._deps.account)
            ta = getattr(asset, "total_asset", None)
            self._day_open_equity = Decimal(str(ta)) if ta is not None else None
        except Exception as exc:  # noqa: BLE001 资产查询失败不阻断盘前装载，回撤口径降级为 None
            self._day_open_equity = None
            self._logger.warn("engine_day_open_equity_failed", error=str(exc))

    def _account_drawdown(self) -> Optional[Decimal]:
        """账户日内回撤 = max(0, (日初总资产 − 当前总资产) / 日初总资产)，正数表示亏损幅度。

        口径：总资产(total_asset=现金+市值)较日初的回撤，综合承载已实现+浮动亏损，故 account_realized_loss
        不再单列（喂 None）。无基线 / 查询失败 → None（不凭空冻结，FREEZE 交由其它确定可信的闸门）。
        """
        if self._day_open_equity is None or self._day_open_equity <= 0:
            return None
        try:
            asset = self._deps.trader.query_stock_asset(self._deps.account)
            cur = getattr(asset, "total_asset", None)
            if cur is None:
                return None
            cur_dec = Decimal(str(cur))
        except Exception as exc:  # noqa: BLE001 查询失败 → 回撤未知，返回 None
            self._logger.warn("engine_account_drawdown_query_failed", error=str(exc))
            return None
        dd = (self._day_open_equity - cur_dec) / self._day_open_equity
        return dd if dd > 0 else Decimal("0")

    def _open_blocked_by_risk(self, ts_code: str) -> bool:
        """买入前风控总闸（评审 P0-B1/B2）：返回 True 表示禁止开新仓。

        判据（任一命中即禁开）：
        - risk.gate 非 ALLOW：行情/下单中断 FREEZE、账户回撤击穿 FREEZE、空仓 SELL_ONLY_HOLD；
        - is_open_blocked(market_state)：退潮/冰点/空仓等情绪周期禁开（市场级，§5.4.2）。
        注意：本闸门只管「开新仓」；卖出（风险减仓）不受账户回撤/空仓闸门影响，避免冻结必要的止损出场，
        故 run_sell_pass 的 gate 仍只用行情/下单中断口径（不喂账户回撤）。
        """
        verdict = self._risk.gate(
            market_state=self._market_state,
            market_feed_ok=self._market_feed_ok,
            trade_conn_ok=self._trade_conn_ok,
            account_drawdown=self._account_drawdown(),
            account_realized_loss=None,  # 由 total_asset 回撤综合承载，不单列
            unit_float_loss=None,        # 开新仓为全新标的，无既有浮亏
        )
        if verdict.verdict != RiskVerdict.ALLOW or self._risk.is_open_blocked(self._market_state):
            self._logger.info(
                "engine_open_blocked_by_risk",
                ts_code=ts_code,
                verdict=str(verdict.verdict),
                market_state=self._market_state,
                reason=verdict.reason,
            )
            return True
        return False

    # ------------------------------------------------------------------
    # 成交回报 → 持仓状态机回写（评审 P0-A2 修复）
    # ------------------------------------------------------------------
    def _apply_trade_to_position(self, rec: Any) -> None:
        """把规整后的成交回报（TradeRecord）落地到持仓状态机。

        业务意图：成交是唯一事实源，买入成交必须回写为 PositionUnit（守 T+1、跨日转 HOLDING、
        供 sellable_units 卖出），卖出成交必须扣减持仓推进状态。原实现回调只落库 + 入台账，
        从不回写持仓 → PositionManager 永远空集、run_sell_pass 永不卖出（裸奔扛单）。

        口径：
        - ts_code 用 rec.ts_code（归一码），与盘口 books / 计划行 / QMT 快照一致。
        - today/买入日 取 rec.trade_date（东八区自然日）；跨午夜补采的口径细化见评审 x-time，另行处理。
        - 幂等：BUY 由 mark_position_on_fill 按 traded_id 去重；SELL 在此按 traded_id 去重（券商重投/
          断线重放不重复扣减持仓）。
        - 防御：rec 字段缺失时不抛断回调主链（成交已落库 + 已入台账），仅记日志。
        """
        try:
            side = getattr(rec, "trade_side", None)
            ts_code = getattr(rec, "ts_code", None)
            _tid = getattr(rec, "traded_id", None)
            traded_id = str(_tid) if _tid is not None else None  # 与台账/持仓去重口径一致(评审 P1#7)
            traded_volume = getattr(rec, "traded_volume", None)
            if ts_code is None:
                return
            if side == TradeSide.BUY:
                # 买入：建/加仓（mark_position_on_fill 内部按 traded_id 去重、守 T+1）。
                self._position.mark_position_on_fill(
                    rec, rec.trade_date, account_id=self._account_id, ts_code=ts_code
                )
            elif side == TradeSide.SELL:
                # 卖出：扣减对应单元（无该单元则忽略——手工单/非本系统单）。
                unit = self._position.get_unit(self._account_id, ts_code)
                if unit is None:
                    return
                # 卖出成交按 traded_id 去重（复用单元去重集合），防券商重投/断线重放重复扣减。
                if traded_id is not None:
                    if traded_id in unit.counted_trade_ids:
                        return
                    unit.counted_trade_ids.add(traded_id)
                if traded_volume:
                    self._position.apply_sell_fill(unit, int(traded_volume))
        except Exception as exc:  # noqa: BLE001 回写失败不得吞掉成交落库/台账事实，仅告警
            self._logger.error(
                "position_writeback_failed",
                account_id=self._account_id,
                ts_code=getattr(rec, "ts_code", None),
                error=str(exc),
            )

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
        # 风控总闸（评审 P0-B1/B2）：买入也必须过 risk.gate——账户日内回撤击穿 / 行情或下单中断 /
        # 空仓 / 退潮冰点 → 禁开新仓。原实现买入路径完全绕过 gate，账户熔断对开仓零作用。
        if self._open_blocked_by_risk(decision.ts_code):
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
            # —— 在途卖单跳过（评审 P0-C2 修复）——
            # sellable_units 含 SELLING（在途已发卖单）单元，原实现每 tick 仍对其二次决策，
            # 命中 CLEAR/REDUCE 时会在 order_stock 已发第二张卖单之后才在 mark_selling 抛错，
            # 造成「重复下卖单 + 抛错中断本轮其余持仓的应卖决策」。这里在决策前显式跳过在途单元，
            # 等成交回报推进出 SELLING 后下一轮再评（卖出成交回写见 _apply_trade_to_position）。
            if unit.state == PositionState.SELLING:
                continue
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
        # —— 状态防御（评审 P0-C2）：仅对可发卖单态（HOLDING/PART_SOLD）下卖单——
        # run_sell_pass 已跳过 SELLING，这里再做一道防御：避免任何路径在 order_stock 已发出之后
        # 才在 mark_selling 抛错（先发单后抛错会造成重复卖单 / 中断本轮）。非可卖态直接不下单留痕。
        if unit.state not in (PositionState.HOLDING, PositionState.PART_SOLD):
            self._logger.info(
                "engine_sell_state_not_sellable", ts_code=unit.ts_code, state=str(unit.state)
            )
            return False
        if action_type == SellActionType.CLEAR:
            decision_vol = unit.can_use_volume
        else:
            ratio = reduce_ratio if reduce_ratio is not None else Decimal("0.5")
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
