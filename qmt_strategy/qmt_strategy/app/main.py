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
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Dict, List, Optional

from ..common.auction_window import is_lunch_break
from ..common.time_utils import east8_trade_date
from ..config.settings import Settings
from ..contracts.enums import (
    DataSource,
    EntryAction,
    OrderPhase,
    OrderState,
    OrderStatus,
    PositionState,
    RiskVerdict,
    SellActionType,
    SnapshotType,
    TradeSide,
)
from ..contracts.models import (
    AccountRecord,
    AuctionSnapshot,
    EntryDecision,
    OrderBook,
    PlanRow,
    SignalPrior,
    WatchlistContext,
)
from ..data_writer import normalize
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

# 对账未通过阻断开仓的持久标志键（评审二轮 P1#9）：置位后次日 prewarm 读到即只守仓不开新仓，人工清除前持续。
_RECONCILE_BLOCK_FLAG = "reconcile_blocked"

# 缺强度/beyond-N 票的保守份额系数（评审三轮 EXEC-entry-05/01）：缺强度或超出 top-N 名额的票，其预算份额基数
# 按「最弱真实票 × 本系数」给一个远低于任一 top-N 真实份额的小额，既不挤占强票满额、也不在名额/余额富余时闲置。
# 0.1 为占位值，回测标定前任何数值都是经验占位；落地后可移入 settings 标定。
_MISSING_STRENGTH_FACTOR = Decimal("0.1")


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
    # 决策采集器（可注入，可选）：注入 DecisionEmitter 则在各决策点 best-effort 采集决策链路；
    # 缺省 None → 各组件退化为「不采集」（与历史行为一致，单测/离线无需提供）。与交易热路径物理隔离。
    decision_emitter: Optional[Any] = None


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
        # 决策采集器（可选，注入则各决策点 best-effort 采集；与下单热路径物理隔离，绝不影响交易）。
        self._decision_emitter = deps.decision_emitter
        self._sell = SellDecider(
            s, deps.clock, deps.logger, decision_emitter=self._decision_emitter
        )
        # position_sizer：按 leader_strength_score 强度加权分配买入资金（强的分得多），评审"按强度分"。
        # decision_log：注入决策采集器（实现 .append(EntryDecision)）→ 采集 SIGNAL_QUALIFIED/SKIP；
        # 缺省 []（历史行为：只 logger 留痕，不外采）。
        self._entry = EntryRouter(
            s, deps.clock, deps.logger,
            decision_log=self._decision_emitter if self._decision_emitter is not None else [],
            position_sizer=self._strength_budget_volume,
        )
        self._order = OrderExecutor(
            deps.trader, deps.account, deps.account_id, self._ledger, s, deps.clock, deps.logger,
            decision_emitter=self._decision_emitter,
            # 下单通道健康回馈（评审三轮 EXEC-risk-05）：order_stock 同步失败/异常 → report_trade_conn(False)
            # 冻结下单闸；成功 → report_trade_conn(True)。与盘中心跳互补，覆盖通道静默变坏的盲区。
            conn_health_sink=self.report_trade_conn,
        )
        # 重启幂等（评审 P0-C4）：台账已由 LocalStorage.start()→load_from_db 重建，这里据此重置
        # biz 序号计数器，保证重启后新单序号严格大于历史、不与磁盘失败单同号覆盖/重复下单。
        self._order.rebuild_seq_counter()
        # 重启重建运行态（评审二轮 P1#28/P2#32/#40/#69）：TTL 截止表 + 单日下单次数计数器从台账重建，
        # 否则崩溃前在途单永不再被 sweep 撤单、单日下单次数硬闸被清零绕过。
        self._order.rebuild_runtime_state()

        # —— 回调（落库 + 台账 + 持仓建仓回写 + 断线补采钩子）——
        # position_sink：评审 P0-A2 修复——把成交回报回写持仓状态机，否则永远空集、永不卖出。
        self._callback = ExecCallback(
            self._data_writer, self._ledger, deps.logger,
            account_id=deps.account_id,
            trade_date_provider=self._today_provider,
            # 断线钩子默认 no-op（评审三轮 EXEC-sched-01）：原实现直接接 on_reconnect_backfill，绕过
            # ConnectionGuard，导致真实掉线后永不换 session 重连、guard.ready 恒 True。改为由 run.py 在
            # guard 构造后回填 callback.set_on_disconnected_hook(lambda: guard.on_disconnected())——断线先经
            # guard 换新 session 重连，成功后再由 guard 触发 engine.on_reconnect_backfill 补采。
            position_sink=self._apply_trade_to_position,
            # 卖单终态失败复位钩子（评审二轮 P1#31）：拒单/全撤零成交 → 持仓从 SELLING 复位回 HOLDING 重挂。
            sell_revert_sink=self._revert_sell_unit,
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
        # feed_health_sink（评审二轮 P1#15）：竞价段 tick 取数成败上报 _market_feed_ok，行情断流走 FREEZE。
        self._poller = AuctionPoller(
            deps.tick_source, self._plan_provider, self._router_sink, s, deps.clock, deps.logger,
            feed_health_sink=self.report_market_feed,
        )

        # 运行期状态：当日 watchlist 上下文 + 计划行映射（盘中只读）。
        self._context: Optional[WatchlistContext] = None
        self._plan_map: Dict[str, PlanRow] = {}
        # 当日情绪周期（供 risk.gate 空仓判定，盘前装载时记录）。
        self._market_state: Optional[str] = None
        # 行情 / 下单通道健康标志（断线时置 False，恢复后置 True；供 risk.gate 安全默认）。
        self._market_feed_ok = True
        self._trade_conn_ok = True
        # 下单通道主动探活连续失败计数（评审三轮 EXEC-risk-05）：盘中心跳 query_stock_asset 连续失败累计，
        # 达阈值才置 _trade_conn_ok=False（避免单次抖动误冻）；任一次成功即清零。
        self._trade_conn_fail_streak = 0
        # 重连请求钩子（评审三轮 EXEC-risk-05 / 评审复核 P1-1）：心跳探到「静默死亡」（通道卡死但未触发
        # 断线回调）首次跨阈值冻结时，把它当断线处理触发 guard 换 session 重连重订阅，让「连接就绪事件」
        # 接管解冻——否则心跳冻结后 guard.ready 仍 True、_supervise_once 不会重连、_trade_conn_ok 永无解冻
        # 路径 = 永久冻结整日。默认 no-op；run.py 在 guard 构造后回填 guard.on_disconnected。
        self._reconnect_requester: Callable[[], None] = lambda: None
        # 日初总资产基线（评审 P0-B2）：prewarm 抓取，供 risk.gate 算账户日内回撤击穿。
        # None 表示未取到基线（查询失败/未装载）→ 回撤口径返回 None，不凭空冻结（其它闸门仍生效）。
        self._day_open_equity: Optional[Decimal] = None
        # 强度资金权重表：ts_code → 预算占比（prewarm 按当日可交易候选 leader_strength_score 归一）。
        self._strength_weights: Dict[str, Decimal] = {}
        # 存储健康标志（评审二轮 P0#2）：写线程死亡 / 关键落盘失败时由 on_storage_failure 置 False。
        # fail-closed 口径：存储不健康 → 停开新仓（持仓事实无法可靠持久 = 重启幂等/对账失效），但不冻结卖出
        # （保留风险出场能力，与 risk.gate 的"空仓不锁卖出"同口径）。装配末尾由 run.py 接线到 stack.set_on_failure。
        self._storage_ok = True
        # 当日已抓基线的交易日（评审二轮 P1#19）：防同日盘中重启 prewarm 重跑覆盖日初回撤基线。
        self._day_open_equity_date: Optional[date] = None
        # 对账未通过阻断开仓标志（评审二轮 P1#9）：prewarm 从持久标志读入；True 则今日只守仓不开新仓。
        self._reconcile_blocked = False
        # 竞价择时关时"仅采集"留痕去抖集合（复审 P2-2）：同一票每帧重复采集会洪泛日志，每票每日只留痕一次。
        self._auction_collect_logged: set = set()

    # ------------------------------------------------------------------
    # 对外暴露（供 main / 连接守护注册回调 / 调度）
    # ------------------------------------------------------------------
    @property
    def callback(self) -> ExecCallback:
        """供连接守护 register_callback 注册到 xttrader 的回调对象。"""
        return self._callback

    def _emit_decision(self, **fields: Any) -> None:
        """向决策采集器发一条编排层决策事件（全程吞异常，绝不影响编排/下单热路径）。"""
        em = self._decision_emitter
        if em is None:
            return
        try:
            em.emit(**fields)
        except Exception:  # noqa: BLE001 决策采集绝不影响交易
            pass

    # ------------------------------------------------------------------
    # 存储 fail-closed（评审二轮 P0#2）
    # ------------------------------------------------------------------
    def on_storage_failure(self, reason: str) -> None:
        """存储故障入口：写线程死亡 / 关键落盘失败时被 AsyncWriteQueue.on_failure 回调。

        置"存储不健康" → 停开新仓（_open_blocked_by_risk / _router_sink 据此拒开），并强告警。
        不冻结卖出：持久化失效时仍允许风险出场（与空仓闸门"不锁卖出"同口径，避免被迫扛单）。
        幂等：重复回调只重复告警，不改变语义。运行期不自动恢复——须人工排查写线程后重启进程。
        """
        self._storage_ok = False
        self._logger.error("engine_storage_fail_closed", reason=reason, account_id=self._account_id)

    def storage_health_tick(self) -> None:
        """供调度器周期体检调用（评审二轮 P0#2）：探测存储栈健康，不健康即 fail-closed 停开新仓。

        与 on_failure 回调互补：on_failure 是事件驱动（submit 命中死线程时触发），本方法是轮询兜底
        （写线程静默死亡且当轮无 submit 时也能被发现）。健康探测器由 run.py 装配时注入（缺省 no-op）。
        """
        checker = getattr(self, "_storage_health_checker", None)
        if checker is None:
            return
        try:
            if not checker():
                self.on_storage_failure("storage_unhealthy_on_tick")
        except Exception as exc:  # noqa: BLE001 体检异常不得拖垮调度
            self._logger.warn("engine_storage_health_tick_error", error=str(exc))

    def set_storage_health_checker(self, checker: Callable[[], bool]) -> None:
        """注入存储健康探测器（返回 True=健康）。由 run.py 接线到 stack.is_healthy。"""
        self._storage_health_checker = checker

    # ------------------------------------------------------------------
    # 行情通道健康（评审二轮 P1#15）
    # ------------------------------------------------------------------
    def report_market_feed(self, ok: bool) -> None:
        """行情源健康上报：行情断流 → ok=False 使 risk.gate 走 FREEZE 安全默认（没有可信盘口不实时卖）。

        原 `_market_feed_ok` 恒 True、无置 False 处 = 死代码（行情断流冻结分支永不触发）。这里提供置位入口：
        - 竞价段由 AuctionPoller 注入：本轮 get_full_tick 整体失败(降级) → report(False)，成功 → report(True)；
        - 盘中连续交易段由 sell_books_provider（实时盘口源，TODO实测）在取盘口失败时上报 False（落地见 run.py）。
        仅在状态翻转时留痕，避免每帧刷日志。
        """
        if self._market_feed_ok != ok:
            self._logger.warn("engine_market_feed_changed", market_feed_ok=ok)
        self._market_feed_ok = ok

    def report_trade_conn(self, ok: bool) -> None:
        """下单通道健康上报（评审三轮 EXEC-sched-02）：作为 risk.gate FREEZE 的权威源之一。

        口径迁移：原来 _trade_conn_ok 只由 on_reconnect_backfill 一次注定失败的补采管理（fail-open 风险），
        现改由「连接就绪事件」驱动——ConnectionGuard 经 on_connection_state 在重连真正就绪时 report(True)、
        断线/连接失败时 report(False)；另由主动心跳（trade_conn_heartbeat）与下单同步失败回馈补充。
        仅状态翻转时留痕，避免刷日志。置 True 时清零探活失败计数。
        """
        if ok:
            self._trade_conn_fail_streak = 0
        if self._trade_conn_ok != ok:
            self._logger.warn("engine_trade_conn_changed", trade_conn_ok=ok)
        self._trade_conn_ok = ok

    def set_reconnect_requester(self, requester: Callable[[], None]) -> None:
        """注入「请求重连」钩子（评审三轮 P1-1）。由 run.py 在 guard 构造后回填 guard.on_disconnected。"""
        self._reconnect_requester = requester

    def trade_conn_heartbeat(self) -> None:
        """盘中主动探活下单通道（评审三轮 EXEC-risk-05）：由调度 INTRADAY 周期调用。

        业务意图：_trade_conn_ok 原仅靠 on_disconnected 翻转，下单通道「静默变坏」（卡单/RPC 超时但
        未触发断线回调）时风控不冻结、仍向不可用通道发单。这里用轻量 query_stock_asset 做心跳：
        连续失败达阈值（settings.trade_conn_heartbeat_fail_threshold）才 report_trade_conn(False)，
        单次成功即清零；避免单次抖动误冻。查询本身异常被吞（探活失败即记一次失败，不外抛）。
        解冻路径（P1-1 修复）：首次跨阈值冻结时把「静默死亡」当断线，触发 guard 换 session 重连重订阅，
        由「连接就绪事件」接管解冻；否则心跳冻结后 guard.ready 仍 True、主循环不重连 = 永久冻结。
        """
        threshold = getattr(self._settings, "trade_conn_heartbeat_fail_threshold", 3)
        try:
            self._deps.trader.query_stock_asset(self._deps.account)
        except Exception as exc:  # noqa: BLE001 探活失败计一次，连续达阈值才冻结
            self._trade_conn_fail_streak += 1
            self._logger.warn(
                "engine_trade_conn_heartbeat_failed",
                streak=self._trade_conn_fail_streak,
                threshold=threshold,
                error=str(exc),
            )
            if self._trade_conn_fail_streak >= threshold:
                was_ok = self._trade_conn_ok
                self.report_trade_conn(False)
                if was_ok:
                    # 仅在「首次从健康跨入冻结」时请求一次重连：把静默死亡当断线，guard 换 session 重连
                    # 重订阅成功后经 on_connection_state(True) 解冻；若重连失败 ready=False，主循环 _supervise_once
                    # 会继续退避重连（无需在此每个 tick 重复请求，避免重连风暴）。
                    self._reconnect_requester()
            return
        # 心跳成功：清零失败计数。注意不在此擅自 report(True) 解冻——解冻权威源是「连接就绪事件」
        # （guard.on_connection_state），心跳只负责「探测到死即冻结 + 请求重连」，避免静默通道被心跳成功误解冻。
        self._trade_conn_fail_streak = 0

    # ------------------------------------------------------------------
    # 盘前：装载当日 watchlist + 推进持仓状态
    # ------------------------------------------------------------------
    def prewarm(self, today: date) -> WatchlistContext:
        """盘前一次性装载（§2.3）：装 watchlist 上下文、建计划映射、推进持仓状态（守 T+1 转 HOLDING）。"""
        # 每个交易日开盘前重置行情通道健康为 True（评审二轮 P1#15）：新一天默认行情可用，盘中由竞价轮询/
        # 盘口源在断流时置 False；避免前一日末轮的 False 状态跨日残留，导致开盘即全程冻结。
        self._market_feed_ok = True
        self._auction_collect_logged.clear()  # 新交易日重置竞价采集去抖集合（复审 P2-2）
        # 对账阻断标记（评审二轮 P1#9）：上一交易日对账未通过且未人工清除 → 今日只守仓不开新仓。
        self._reconcile_blocked = self._read_reconcile_block()
        if self._reconcile_blocked:
            self._logger.error(
                "engine_prewarm_reconcile_blocked", trade_date=str(today),
                note="对账未通过未清除,今日只守仓不开新仓;人工核查后删除 system_flags.reconcile_blocked 解除",
            )
        ctx = self._watchlist.load(today)
        self._context = ctx
        # 计划映射：仅可交易名单进竞价/建仓（观察名单只跟踪不下单，§2.3）。
        self._plan_map = {code: entry.to_plan_row() for code, entry in ctx.tradable.items()}
        # 记录当日情绪周期（从可交易/观察任一条目透传的 market_state 取，供风控空仓判定）。
        self._market_state = self._extract_market_state(ctx)
        # 推进持仓状态：跨买入日 LOCKED_T1 → HOLDING，并刷新先验挂接（§5.6）。
        self._position.refresh_state(today)
        # 盘前对账跨日卡死 SELLING 单元（评审三轮 EXEC-position-04）：隔夜/断线重启后昨日 SELLING 单元若其卖单
        # 实际已撤/废且复位回调丢失，会跨日卡 SELLING 永久漏卖；这里主动 query 券商委托终态、零成交终态则复位重挂。
        self._reconcile_stuck_selling(today)
        # 用 QMT 权威持仓重建/校准持仓状态机（评审二轮 P0#6/#29/#30/#38）：重启/断线后隔夜 T+1 持仓
        # 必定进入卖出决策，且可卖量以 QMT 为权威。须在 refresh_state 之后，保证 QMT can_use 是最终口径。
        self._rebuild_positions_from_broker(today)
        # 抓取日初总资产基线（评审 P0-B2 + 二轮 P1#19）：供盘中 risk.gate 算账户日内回撤击穿；
        # 同日盘中重启复用已持久化的 OPEN 基线，绝不把当前(已亏损)资产当日初基线。
        self._capture_day_open_equity(today)
        # 计算当日强度资金权重（按 leader_strength_score 给候选分预算占比，强的分得多）。
        self._compute_strength_weights()
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
    def _reconcile_stuck_selling(self, today: date) -> None:
        """盘前对账跨日卡死 SELLING 单元（评审三轮 EXEC-position-04）。

        查 QMT query_stock_orders 建「ts_code → 当日卖单终态」映射（归一码），交 PositionManager.reconcile_stuck_selling
        对跨日仍 SELLING 的单元裁决：零成交终态(撤/废/拒/错)→复位重挂；已成/部成/仍在途→不动；查不到→保守不动+告警。
        边界：query_stock_orders 失败/未实测口径时整体保守跳过（不复位、不误判），仅告警。
        order_type==XT_ORDER_TYPE_SELL 识别卖单方向；order_status 经 status_resolver 归一为 OrderStatus 再映射。
        """
        from ..common.identity import resolve_code
        from ..data_writer import normalize as _normalize
        from ..order.order_executor import XT_ORDER_TYPE_SELL

        try:
            orders = self._deps.trader.query_stock_orders(self._deps.account)
        except Exception as exc:  # noqa: BLE001 查询失败保守跳过（不复位、不误判），仅告警
            self._logger.warn("engine_stuck_selling_query_failed", error=str(exc))
            return
        # OrderStatus → reconcile 终态字符串（撤/废/拒/错=复位；已成=交回报；在途=不动）。
        _term = {
            OrderStatus.CANCELLED: "CANCELLED", OrderStatus.REJECTED: "REJECTED",
            OrderStatus.ERROR: "ERROR", OrderStatus.TRADED: "FILLED",
            OrderStatus.PART_TRADED: "PARTIAL", OrderStatus.REPORTED: "ACTIVE",
        }
        state_by_code: Dict[str, str] = {}
        for o in orders or []:
            try:
                if getattr(o, "order_type", None) != XT_ORDER_TYPE_SELL:
                    continue  # 只看卖单方向（买单不参与卖出卡死对账）
                code = resolve_code(getattr(o, "stock_code", None))
                if code is None:
                    continue
                status = _normalize.default_status_resolver(getattr(o, "order_status", None))
                term = _term.get(status)
                if term is not None:
                    # 同票多卖单取「更靠终态」的：已撤/废优先于在途（保证卡死能被识别复位）。
                    prev = state_by_code.get(code)
                    if prev is None or term in ("CANCELLED", "REJECTED", "ERROR"):
                        state_by_code[code] = term
            except Exception:  # noqa: BLE001 单条委托解析失败跳过，不拖垮整体对账
                continue

        def _query(account_id: str, ts_code: str):
            return state_by_code.get(ts_code)

        self._position.reconcile_stuck_selling(today, _query)

    def _rebuild_positions_from_broker(self, today: date) -> None:
        """盘前 / 重连后用 QMT 权威持仓重建 / 校准持仓状态机（评审二轮 P0#6/#29/#30/#38）。

        业务意图：持仓状态机纯内存，进程重启清空；断线期间建仓只补采落库不回写持仓——二者都会导致隔夜
        T+1 持仓永不进卖出决策（裸奔扛单）。这里查 QMT query_stock_positions（权威全集）→ 归一 →
        交 PositionManager.rebuild_from_broker_positions：重建本地缺失的持仓单元、用 QMT 可卖量校准。
        边界：查询失败不抛断 prewarm（保留现有内存态），仅强告警——重启后无法重建会漏卖，须人工关注。
        """
        try:
            raws = self._deps.trader.query_stock_positions(self._deps.account)
        except Exception as exc:  # noqa: BLE001 查询失败不阻断盘前装载，但强告警（漏卖风险）
            self._logger.error("engine_position_rebuild_query_failed", error=str(exc))
            return
        records = []
        for p in raws or []:
            try:
                rec = normalize.normalize_position(
                    p, account_id=self._account_id, trade_date=today, snapshot_type=SnapshotType.OPEN
                )
            except Exception:  # noqa: BLE001 单条规整失败跳过，不拖垮整体重建
                continue
            if rec.ts_code is None:
                continue
            records.append(rec)
        n = self._position.rebuild_from_broker_positions(records, today)
        self._logger.info("engine_positions_rebuilt", count=n, queried=len(records))

    def _capture_day_open_equity(self, today: date) -> None:
        """抓取日初总资产作回撤基线（prewarm 调用），并持久化供同日重启复用（评审 P0-B2 + 二轮 P1#19）。

        盘中崩溃重启后 prewarm 会重跑——若每次都把"当前总资产"当日初基线，已亏损时基线被悄悄抬低，
        账户日内回撤永远算不出击穿，回撤熔断形同虚设。修复：
        1) 本进程同日已抓过（_day_open_equity_date==today）→ 不重复抓；
        2) 否则先读已持久化的当日 OPEN 资产快照作基线（上一进程开盘前落的真实日初值）；
        3) 都没有才查当前资产作基线，并落 OPEN 快照（供同日后续重启复用）。
        查询失败 → None（回撤口径降级为 None，不凭空冻结，其它闸门仍生效）。
        """
        # 1) 本进程同日已抓过：直接复用，避免重跑覆盖。
        if self._day_open_equity_date == today and self._day_open_equity is not None:
            return
        # 2) 复用已持久化的当日 OPEN 基线（重启复用的关键）。
        try:
            existing = self._deps.repository.get_account_daily(
                self._account_id, today, SnapshotType.OPEN
            )
        except Exception:  # noqa: BLE001 读基线失败不致命，回落实时查询
            existing = None
        if existing is not None and existing.total_asset is not None and existing.total_asset > 0:
            self._day_open_equity = Decimal(str(existing.total_asset))
            self._day_open_equity_date = today
            self._logger.info("engine_day_open_equity_reused", trade_date=str(today))
            return
        # 3) 首次：查当前资产作基线并持久化 OPEN 快照。
        try:
            asset = self._deps.trader.query_stock_asset(self._deps.account)
            ta = getattr(asset, "total_asset", None)
            cash = getattr(asset, "cash", None)
        except Exception as exc:  # noqa: BLE001 资产查询失败不阻断盘前装载，回撤口径降级为 None
            self._day_open_equity = None
            self._logger.warn("engine_day_open_equity_failed", error=str(exc))
            return
        if ta is None:
            self._day_open_equity = None
            return
        self._day_open_equity = Decimal(str(ta))
        self._day_open_equity_date = today
        # 持久化 OPEN 资产基线（供同日盘中重启复用）：不污染净值曲线（复盘只认 CLOSE 快照）。
        try:
            rec = AccountRecord(
                account_id=self._account_id,
                trade_date=today,
                total_asset=Decimal(str(ta)),
                cash=Decimal(str(cash)) if cash is not None else Decimal("0"),
                snapshot_type=SnapshotType.OPEN,
                data_source=DataSource.QUERY,
            )
            self._data_writer.upsert_account_daily(rec, SnapshotType.OPEN)
            # 尽量 drain 落盘（prewarm 非热路径，可阻塞）：flush_hook 为普通 flush（仅等队列清空，不强确认
            # commit 成功），故只是尽力把基线尽快落盘；万一未及落盘即崩溃，重启时回落"重新抓当前资产"老口径，
            # 仅退化、不致命（回撤基线不如下单关键，无需 flush_confirm 级强保证）。
            self._deps.flush_hook()
        except Exception as exc:  # noqa: BLE001 基线持久化失败不致命，退化为本进程内存基线
            self._logger.warn("engine_day_open_equity_persist_failed", error=str(exc))

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
        # —— 存储 fail-closed（评审二轮 P0#2）：持久化失效则禁开新仓（重启幂等/对账依赖可信落盘）——
        if not self._storage_ok:
            self._logger.error("engine_open_blocked_storage_unhealthy", ts_code=ts_code)
            return True
        # —— 对账未通过阻断（评审二轮 P1#9）：上一交易日对账有漏单/串单/资产偏差且未人工清除 → 禁开新仓——
        if self._reconcile_blocked:
            self._logger.error("engine_open_blocked_reconcile_unconfirmed", ts_code=ts_code)
            return True
        # —— 账户回撤 fail-closed（评审二轮 P2#70）：有日初基线但当前资产查询失败 → 无法核验回撤 →
        # 禁开新仓（不冻结卖出）。原实现 _account_drawdown() 失败返 None → gate 当"无回撤"放行 = 账户级
        # 止损闸 fail-open。这里在有基线时把"算不出回撤"显式当作"不可放行开仓"。
        drawdown = self._account_drawdown()
        if self._day_open_equity is not None and self._day_open_equity > 0 and drawdown is None:
            self._logger.error("engine_open_blocked_drawdown_unknown", ts_code=ts_code)
            return True
        verdict = self._risk.gate(
            market_state=self._market_state,
            market_feed_ok=self._market_feed_ok,
            trade_conn_ok=self._trade_conn_ok,
            account_drawdown=drawdown,   # 复用上方已查值（评审二轮 P2#70），避免重复查资产
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
    # 强度加权资金分配（评审"按强度/优先级排序分"）
    # ------------------------------------------------------------------
    def _compute_strength_weights(self) -> None:
        """按当日可交易候选的 leader_strength_score 归一为预算【份额】权重（强的占比大）。

        口径（评审三轮 EXEC-entry-01/05 修订 + 复审防优先级反转）：
        - top-N 优先制（保护龙头名额）：按强度降序取前 N（cap=max_positions_per_day），仅 top-N 进 weights、
          在 N 只内归一（Σw=1），beyond-N 票【不进 weights】→ sizer 据 w is None 返 0，把名额留给更强的 top-N。
          这样【不会】被竞价触发先后劫持——若放开 beyond-N 让其拿小额度，弱票先触发就会先占 committed 名额，
          把后触发的强龙头挤出名额（优先级反转，背离打板"只打最强"的核心）。单日只数仍由 order_executor.place
          的 committed 计数闸动态收口（终态零成交释放名额）。注：强票全判弃→名额空置是【正确】的——策略既然
          对这些票收手，就不应强行补买更弱的替代票。
        - top-N 满额部署：强票份额不被 beyond-N 摊薄，也不把额度摊到买不到的弱票上闲置现金。
        - 缺强度票（leader_strength_score is None）：排序键置极低（-1，不挤占 top-N 名额优选）；若仍落入 top-N
          （真实强度票不足 N 只时），份额基数用 floor×_MISSING_STRENGTH_FACTOR（远低于最弱真实票，不等权稀释
          强票）；全部缺强度时退化等权(1/N)。
        权重在 prewarm 一次性算定、不随触发顺序变化——弱票即使先触发也只吃自己的小份额。
        """
        plans = list(self._plan_map.values())
        if not plans:
            self._strength_weights = {}
            return
        present = [p.leader_strength_score for p in plans if p.leader_strength_score is not None]
        floor_score = min(present) if present else Decimal("0")

        def _rank_key(p: PlanRow) -> Decimal:
            """排序键：真实强度用本身；缺强度置极低（-1，低于所有真实强度）→ 不挤占 top-N 名额优选。"""
            return p.leader_strength_score if p.leader_strength_score is not None else Decimal("-1")

        def _share_basis(p: PlanRow) -> Decimal:
            """份额基数：真实强度用本身；缺强度用 floor×保守系数（远低于最弱真实票，不等权稀释强票）。"""
            if p.leader_strength_score is not None:
                return p.leader_strength_score
            return floor_score * _MISSING_STRENGTH_FACTOR

        ranked = sorted(plans, key=_rank_key, reverse=True)
        cap = self._settings.max_positions_per_day
        top = ranked[:cap] if (cap is not None and cap > 0) else ranked
        basis: Dict[str, Decimal] = {p.ts_code: _share_basis(p) for p in top}
        total = sum(basis.values(), Decimal("0"))
        weights: Dict[str, Decimal] = {}
        if total > 0:
            for code, s in basis.items():
                weights[code] = s / total
        else:
            # 全部缺强度且 floor=0 → 退化等权 1/N（与既有口径一致）。
            eq = Decimal("1") / Decimal(len(basis))
            for code in basis:
                weights[code] = eq
        # 注意（复审 EXEC-entry-01）：beyond-N 票【刻意不进 weights】——见上方 top-N 优先制说明，避免弱票按触发
        # 先后抢占强龙头名额造成优先级反转。名额收口仍在 order_executor.place 的 committed 计数闸（动态）。
        self._strength_weights = weights
        self._logger.info(
            "engine_strength_weights", count=len(self._strength_weights), position_cap=cap
        )

    def _strength_budget_volume(self, plan: PlanRow, limit_price: Decimal) -> int:
        """按强度权重 + 可用现金 + 总敞口闸 + 在途扣减 测算单票计划股数（评审"按强度分"）。

        预算 = min(当下可用现金, 总预算上限ceiling − 已承诺committed, ceiling×强度权重w,
                   per_order_max_amount, max_position_per_stock) / 限价，向下取整到 100 股。
        ceiling = 日初权益×target_position_ratio（无基线退化为当前现金），再与 max_total_exposure 取小。
        w<=0（缺强度）时不加强度份额约束，退化为"先到先得 + 总敞口闸"。
        """
        if limit_price is None or limit_price <= 0:
            return 0
        # 目标仓位比 ≤0（空跑/降仓到 0，已在 settings 钳到非负）→ 确定性不开新仓（评审复审 P2-3）：
        # 不依赖日初基线是否查到——原实现基线查询失败时 ceiling 退化为全额现金，会绕过 ratio=0 的空跑意图。
        if self._settings.target_position_ratio <= 0:
            return 0
        try:
            asset = self._deps.trader.query_stock_asset(self._deps.account)
            cash = getattr(asset, "cash", None)
        except Exception as exc:  # noqa: BLE001 资产查询失败 → 不臆造资金，返回 0 不下单
            self._logger.warn("engine_sizer_asset_query_failed", ts_code=plan.ts_code, error=str(exc))
            return 0
        if cash is None:
            return 0
        cash = Decimal(str(cash))

        # 可分配总预算上限：日初权益×目标仓位比（无基线退化为当前现金），再与 max_total_exposure 取小。
        ceiling = cash
        if self._day_open_equity is not None and self._day_open_equity > 0:
            ceiling = self._day_open_equity * self._settings.target_position_ratio
        if self._settings.max_total_exposure is not None:
            ceiling = min(ceiling, Decimal(str(self._settings.max_total_exposure)))

        # 总敞口闸 + 在途扣减：可分配上限 − 当日已承诺(在途+已成)买入。
        committed = self._order.committed_amount(self._today_provider())
        budget = min(cash, ceiling - committed)
        # 强度份额（评审三轮 EXEC-entry-01）：仅 top-N 强票进 _strength_weights（按强度归一分资金，强的分得多）；
        # beyond-N 弱票/非候选不在表内 → w is None → 返 0，把名额留给更强的 top-N（防弱票按触发先后抢占强龙头
        # 名额的优先级反转）。单日建仓【只数】仍由 order_executor.place 的 committed 计数闸动态收口。
        w = self._strength_weights.get(plan.ts_code)
        if w is None:
            return 0
        budget = min(budget, ceiling * w)
        # 单笔 / 单票金额上限收紧。
        for cap in (self._settings.per_order_max_amount, self._settings.max_position_per_stock):
            if cap is not None and Decimal(str(cap)) < budget:
                budget = Decimal(str(cap))
        if budget <= 0:
            return 0
        raw_shares = (budget / limit_price).to_integral_value(rounding=ROUND_DOWN)
        return (int(raw_shares) // 100) * 100  # 向下取整到 100 股整手

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
                # 卖出：经原子方法一体完成「去重 + 扣减 + 状态推进」（评审三轮 EXEC-position-01+EXEC-DW-04）：
                # 原实现 get_unit 拿 live 引用后在锁外 counted_trade_ids 判重/add + apply_sell_fill 三步，去重检查
                # 与扣减之间的窗口可被另一回调线程插入导致重复扣减/丢更新；改走 apply_sell_fill_by_trade 临界区原子化。
                self._position.apply_sell_fill_by_trade(
                    self._account_id, ts_code, traded_id,
                    int(traded_volume) if traded_volume else 0,
                )
            else:
                # 方向不可判定（评审三轮 EXEC-DW-09）：UNKNOWN（order_type 未命中映射表）绝不臆造改持仓——
                # 默认当买入会凭空建仓、算反持仓与资金流向。拒绝改持仓 + 强告警（成交已落库留痕供事后核对/补判）。
                self._logger.error(
                    "position_writeback_unknown_side_rejected",
                    account_id=self._account_id, ts_code=ts_code, traded_id=traded_id,
                    note="成交方向不可判定(order_type 未命中映射表)，拒绝改持仓，需核对 normalize 映射表",
                )
        except Exception as exc:  # noqa: BLE001 回写失败不得吞掉成交落库/台账事实，仅告警
            self._logger.error(
                "position_writeback_failed",
                account_id=self._account_id,
                ts_code=getattr(rec, "ts_code", None),
                error=str(exc),
            )

    def _revert_sell_unit(self, ts_code: str) -> None:
        """卖单终态失败（拒单/全撤零成交）的持仓复位（评审二轮 P1#31，由 callbacks 经 sell_revert_sink 调用）。

        把对应 (account_id, ts_code) 持仓单元从 SELLING 复位回 HOLDING，使下一轮卖出巡检可重新挂单，
        止损/破位清仓不再因一次挂单失败而永久失效。无该单元（手工/非本系统）则忽略。
        """
        # 原子复位（评审三轮 EXEC-position-01）：不再 get_unit 拿 live 引用在锁外改，改走 revert_selling_by_code。
        self._position.revert_selling_by_code(
            self._account_id, ts_code, reason="sell_order_terminal_failed"
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
            self._emit_decision(
                decision_type="SKIP_ORCHESTRATION", decision_stage="ORCHESTRATION", action="SKIP",
                ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                trade_date=decision.target_trade_date, strategy_family=decision.strategy_family,
                order_phase=decision.order_phase, reason="空仓/降级禁开新仓", reason_code="open_blocked",
                factors=decision.factors_snapshot,
            )
            return
        # 风控总闸（评审 P0-B1/B2）：买入也必须过 risk.gate——账户日内回撤击穿 / 行情或下单中断 /
        # 空仓 / 退潮冰点 → 禁开新仓。原实现买入路径完全绕过 gate，账户熔断对开仓零作用。
        if self._open_blocked_by_risk(decision.ts_code):
            self._emit_decision(
                decision_type="SKIP_ORCHESTRATION", decision_stage="ORCHESTRATION", action="SKIP",
                ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                trade_date=decision.target_trade_date, strategy_family=decision.strategy_family,
                order_phase=decision.order_phase, reason="风控总闸禁开新仓", reason_code="risk_block",
                factors=decision.factors_snapshot,
            )
            return
        # 竞价择时闸门：竞价单仅在实测开关打开时下单，否则只采集留痕（§7.1.6）。
        if decision.order_phase == OrderPhase.AUCTION and not self._settings.auction_timing_enabled:
            # 去抖（复审 P2-2）：同一票每帧重复采集会洪泛日志/decision_log，每票每日只留痕一次。
            if decision.ts_code not in self._auction_collect_logged:
                self._auction_collect_logged.add(decision.ts_code)
                self._logger.info(
                    "engine_auction_timing_disabled_collect_only",
                    ts_code=decision.ts_code,
                    reason="QMT_AUCTION_TIMING_ENABLED=false",
                )
                self._emit_decision(
                    decision_type="SKIP_ORCHESTRATION", decision_stage="ORCHESTRATION", action="SKIP",
                    ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                    trade_date=decision.target_trade_date, strategy_family=decision.strategy_family,
                    order_phase=decision.order_phase, reason="竞价择时未开,仅采集", reason_code="auction_timing_disabled",
                    factors=decision.factors_snapshot,
                )
            # 解除幂等锁（评审二轮 P1#16）：竞价段决策被丢弃后允许后续帧（定盘段）重评为 OPENING 真正成交，
            # 否则最强龙头(秒封一字/竞价强开)在默认配置下被永久锁死买不进。
            self._entry.release(decision.ts_code)
            return
        # 限价合法性 / 偏离防护（评审二轮 P2#67）：下单前校验限价非正/超法定涨停价/偏离盘口过大则拒发，
        # 防价位口径错误（如 ST 误算成 10%、参考价兜底错位）直发废单或追高失控。
        sane, why = self._limit_price_sane(decision, plan, snap)
        if not sane:
            self._logger.warn("engine_limit_price_rejected", ts_code=decision.ts_code, reason=why)
            self._emit_decision(
                decision_type="SKIP_ORCHESTRATION", decision_stage="ORCHESTRATION", action="SKIP",
                ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                trade_date=decision.target_trade_date, strategy_family=decision.strategy_family,
                order_phase=decision.order_phase, reason=f"限价校验未过:{why}", reason_code="limit_price_guard",
                limit_price=decision.limit_price, factors=decision.factors_snapshot,
            )
            return
        # 通过全部闸门 → 下单（place 内仍有 kill_switch / 幂等 / 资金校验）。
        self._order.place(decision)

    def _limit_price_sane(self, decision: EntryDecision, plan: PlanRow, snap: AuctionSnapshot):
        """下单前限价合法性 / 偏离校验（评审二轮 P2#67）。返回 (是否通过, 原因)。

        三道校验（任一不过即拒发）：
        1) 限价非正（None/<=0）→ 拒（无效价位）；
        2) 超法定涨停价上界：限价 > plan.limit_up_price（board_rules 已按板块/ST 算）→ 拒（必废单）；
        3) 偏离防护：配了 price_deviation_guard_pct 且有盘口现价时，限价与盘口现价偏离比例 > 阈值 → 拒
           （价位口径错位 / 追高失控的兜底闸）。
        """
        lp = decision.limit_price
        if lp is None or lp <= 0:
            return False, "限价非正"
        cap = plan.limit_up_price
        if cap is not None and cap > 0 and lp > cap:
            return False, f"限价{lp}超法定涨停价{cap}"
        guard = self._settings.price_deviation_guard_pct
        ref = getattr(snap, "last_price", None)
        if guard is not None and ref is not None and ref > 0:
            dev = abs(lp - ref) / ref
            if dev > guard:
                return False, f"限价{lp}偏离盘口现价{ref}超{guard}"
        return True, ""

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
        # 午休停牌跳过（评审 P1#11）：11:30–13:00 撮合停止，向停牌时段发卖单无法成交（废单/与复牌竞态），
        # 本轮不做卖出决策，仅等午后复牌再评（盘口只读快照不受影响）。
        if is_lunch_break(self._clock.now_utc()):
            return []
        sold: List[str] = []
        for unit in self._position.sellable_units(today):
            # 单票评估/下单异常隔离（评审复审 P1 / 二轮 #90）：任一单只票的盘口/决策/下单异常只跳过该票，
            # 绝不中断整轮卖出巡检——否则一只票失败会让其余该卖的票全部漏卖（裸奔扛单）。
            try:
                if self._evaluate_and_sell_unit(unit, today, books, session):
                    sold.append(unit.ts_code)
            except Exception as exc:  # noqa: BLE001 单票失败不拖垮整轮卖出
                self._logger.error(
                    "engine_sell_unit_failed", ts_code=getattr(unit, "ts_code", None), error=str(exc)
                )
        return sold

    def _evaluate_and_sell_unit(self, unit, today, books, session) -> bool:
        """对单只可卖单元评估并（必要时）下卖单，返回是否发出卖单。

        由 run_sell_pass 以 per-unit try/except 调用，保证单票异常不拖垮整轮巡检（评审复审 P1）。
        """
        # —— 在途卖单跳过（评审 P0-C2 修复）——
        # sellable_units 含 SELLING（在途已发卖单）单元，原实现每 tick 仍对其二次决策，命中 CLEAR/REDUCE 时
        # 会在 order_stock 已发第二张卖单之后才在 mark_selling 抛错。这里在决策前显式跳过在途单元，等成交回报
        # 推进出 SELLING 后下一轮再评（卖出成交回写见 _apply_trade_to_position）。
        if unit.state == PositionState.SELLING:
            return False
        # 风控闸门：行情/下单中断 → FREEZE 跳过；空仓 SELL_ONLY_HOLD 不影响卖出。
        verdict = self._risk.gate(
            market_state=self._market_state,
            market_feed_ok=self._market_feed_ok,
            trade_conn_ok=self._trade_conn_ok,
        )
        if verdict.verdict == RiskVerdict.FREEZE:
            self._logger.warn("engine_sell_frozen", ts_code=unit.ts_code, reason=verdict.reason)
            return False
        # 先验取数异常隔离（评审二轮 P3#90）：单票 prior_provider 抛错按"无先验"处理（不拖垮整轮）。
        try:
            prior = self._deps.prior_provider(unit.ts_code, today)
        except Exception as exc:  # noqa: BLE001 单票先验失败不拖垮整轮卖出
            self._logger.warn("engine_sell_prior_failed", ts_code=unit.ts_code, error=str(exc))
            prior = None
        book = books.get(unit.ts_code)
        if book is None:
            # 无执行侧盘口：安全默认不主动卖（§5.4.3，没有可信盘口不做实时卖出决策）。
            return False
        # 单票浮亏止损（评审二轮 P2#39）：浮亏击穿 stock_float_loss_limit → 强制清仓，优先于常规卖出决策。
        # 注意口径：单票止损是"该卖"的触发器（应主动卖出），不能喂进 risk.gate 当 FREEZE（那会反而冻结卖出 =
        # 不止损）。故在此独立判定并直接 CLEAR；gate 的 unit_float_loss 层不用于卖出路径。
        if self._unit_stop_loss_breached(unit, book):
            self._logger.warn(
                "engine_unit_stop_loss_clear",
                ts_code=unit.ts_code, avg_cost=str(unit.avg_cost),
                last_price=str(getattr(book, "last_price", None)),
                limit=str(self._settings.stock_float_loss_limit),
            )
            return self._place_sell(unit, SellActionType.CLEAR, None, "单票浮亏止损", book, today)
        if session == "auction":
            action = self._sell.decide_auction(unit, prior, book, risk_verdict=verdict.verdict)
        else:
            action = self._sell.decide_intraday(unit, prior, book, risk_verdict=verdict.verdict)
        if action.action in (SellActionType.REDUCE, SellActionType.CLEAR):
            return self._place_sell(unit, action.action, action.reduce_ratio, action.reason, book, today)
        return False

    def _unit_stop_loss_breached(self, unit, book) -> bool:
        """单票浮亏是否击穿止损线（评审二轮 P2#39）：浮亏比例 = (成本 − 现价)/成本 ≥ stock_float_loss_limit。

        边界：阈值未配置 / 无现价 / 无有效成本 → 不触发（不凭空止损）。Decimal 口径，禁 float。
        """
        limit = self._settings.stock_float_loss_limit
        last = getattr(book, "last_price", None)
        # last<=0 防御（评审复审 P1）：降级/坏 tick 给出 0 价时，(成本-0)/成本=1.0 会"凭空"触发强平并挂 0 价废单。
        # 现价非正即数据不可信，按"无可信盘口不主动止损"处理（安全默认），返回 False。
        if limit is None or last is None or last <= 0 or unit.avg_cost is None or unit.avg_cost <= 0:
            return False
        float_loss = (unit.avg_cost - last) / unit.avg_cost
        return float_loss >= limit

    def _place_sell(self, unit, action_type, reduce_ratio, reason, book=None, today=None) -> bool:
        """发出卖出委托（守 T+1 量闸 + kill_switch + 价位取实时盘口）。

        卖量 = clamp(决策量, can_use_volume)，绝不超量（§5.4.3）；CLEAR 卖全部可用，
        REDUCE 按 reduce_ratio（缺省半仓）卖部分。
        价位（评审 P2 修复）：取实时盘口 book.last_price（当前市价），而非持仓成本 avg_cost——
        炸板/破位 CLEAR 时成本价往往远高于现价，挂成本价的卖单成交不了（等于下了单卖不出去），
        与 CLEAR=立即出场相悖。无盘口现价时回退 avg_cost 并告警（应尽量避免）。
        注：更优的对手价/跌停保成交价 + 滑点下保护需盘口买一价，依赖目标机 get_full_tick 实测，
        本次先用 last_price 消除"挂成本价卖不出"的硬伤。
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
        # 可卖上限扣减在途未成卖量（评审三轮 EXEC-position-03）：可卖基数用 sellable_remaining(=can_use_volume
        # - on_road_sell_volume) 而非 can_use_volume，防 REDUCE 部成后 PART_SOLD 单元相邻 tick 就「在途未成量」
        # 重复挂减仓单超挂。
        avail = self._position.sellable_remaining(unit)
        if action_type == SellActionType.CLEAR:
            # 清仓卖全部可用量（含不足整手的零股——A 股允许一次性清掉零股余额）。
            decision_vol = avail
        else:
            # 减仓须为整手（评审二轮 P2#37）：按比例算出的减仓量【向下取整到 100 股】，否则奇数股卖单被券商
            # 拒为废单（部分卖出会留下余仓，必须整手；清仓才允许零股）。不足一手则减仓量为 0、本轮不卖。
            ratio = reduce_ratio if reduce_ratio is not None else Decimal("0.5")
            decision_vol = (int(avail * ratio) // 100) * 100
        sell_vol = self._risk.clamp_sell_volume(decision_vol, avail)
        if sell_vol <= 0:
            return False
        # 价位取实时盘口现价（评审 P2）：优先 book.last_price（须 >0）；缺失/非正才回退成本价并告警。
        last = getattr(book, "last_price", None) if book is not None else None
        if last is not None and last > 0:
            price = last
        else:
            price = unit.avg_cost
            self._logger.warn(
                "engine_sell_price_fallback_avg_cost",
                ts_code=unit.ts_code,
                reason="盘口 last_price 缺失/非正，回退成本价(可能挂不出)，应排查盘口源",
            )
        # 价位非正兜底（评审复审 P1）：成本价亦非正（脏数据）则绝不下 0 价废单，放弃本次卖出并告警。
        if price is None or price <= 0:
            self._logger.error("engine_sell_invalid_price_skip", ts_code=unit.ts_code, price=str(price))
            return False
        # 卖单 signal_trade_date 取原买入信号日 T = prev_open(买入日 B)（评审二轮 P2#65）：
        # 原实现直接传 unit.buy_date(=买入日 T+1)，把 T+1 错当信号日 T，污染卖出回报的 signal_trade_date 回填与对账。
        sell_signal_date = None
        if unit.buy_date is not None:
            try:
                sell_signal_date = self._calendar.prev_open(unit.buy_date)
            except Exception:  # noqa: BLE001 日历异常不阻断卖出，signal_trade_date 留 None（回流可由对账反推）
                sell_signal_date = None
        # 卖出走唯一下单点（评审 P0-C1）：生成 biz 单号 + 落台账 + 经 OrderExecutor 唯一出口下卖单，
        # 不再编排层直连 trader.order_stock（原实现卖单无台账/无 biz 单号/不可对账）。
        biz_no = self._order.place_sell(
            ts_code=unit.ts_code,
            target_trade_date=today if today is not None else unit.buy_date,
            signal_trade_date=sell_signal_date,
            sell_vol=sell_vol,
            price=price,
            reason=reason,
        )
        if biz_no is None:
            return False
        # mark_selling 竞态防御（评审复审 P1）：place_sell 内含同步落盘确认，发单到此处之间 QMT 回调线程可能已
        # 把该单元推进（刚发卖单秒成 → PART_SOLD/SOLD，或并发推进）。仅当单元仍处可发卖态才 mark_selling，避免对
        # 已被回调推进的单元强行改写而抛 ValueError 中断整轮卖出（持仓事实以回调推进为准）。
        if unit.state in (PositionState.HOLDING, PositionState.PART_SOLD):
            # 冻结本次在途委托量（评审三轮 EXEC-position-03）：传 sell_volume 使 on_road_sell_volume += sell_vol。
            self._position.mark_selling(unit, sell_volume=sell_vol)
        else:
            self._logger.info(
                "engine_sell_unit_already_advanced", ts_code=unit.ts_code, state=str(unit.state)
            )
        self._logger.info(
            "engine_sell_placed", ts_code=unit.ts_code, volume=sell_vol, reason=reason, biz_order_no=biz_no
        )
        return True

    # ------------------------------------------------------------------
    # 收盘 / 断线补采 / 对账
    # ------------------------------------------------------------------
    def on_reconnect_backfill(self) -> None:
        """断线重连【就绪后】补采当日缺口（§6.2.3）：由 ConnectionGuard.reconnect 成功后触发。

        通道健康标志口径再修正（评审三轮 EXEC-sched-01/02）：
        - 原二轮实现用本方法独占管理 _trade_conn_ok（进入置 False、补采全成才置 True）。但因断线钩子
          被错接成本方法、从不经 guard 换 session 重连（EXEC-sched-01），且本方法用已断 trader 补采必败，
          导致 _trade_conn_ok 永久 False → 全天 FREEZE 连止损都冻结（EXEC-sched-02）。
        - 现在解冻/冻结权威源迁到「连接就绪事件」：ConnectionGuard 在 connect_and_subscribe 成功时已通过
          on_connection_state→report_trade_conn(True) 解冻；断线/连接失败时 report(False) 冻结。故本方法
          **不再触碰 _trade_conn_ok**，只负责「重连就绪后补采 + 持仓重建」；补采失败仅强告警、不再令通道
          永久冻结（连接已就绪、卖出能力须保留，持仓由下次 prewarm/收盘快照重新校准）。
        """
        try:
            self._snapshot.run_backfill()
            # 补采后用 QMT 权威持仓重建持仓状态机（评审二轮 P1#30）：断线期间发生的买入成交只被 query_*
            # 补采落库、不回写持仓状态机 → 会漏卖；这里据 QMT 当前持仓重建/校准单元，使其进入卖出决策。
            self._rebuild_positions_from_broker(self._today_provider())
        except Exception as exc:  # noqa: BLE001 补采失败仅告警：连接已就绪，不再据此永久冻结卖出
            self._logger.error("engine_reconnect_backfill_failed", error=str(exc))

    def close_batch(self, trade_date: Optional[date] = None) -> None:
        """收盘批次（§6.2.2）：全量明细兜底 + CLOSE 资产/持仓快照，随后触发对账（§6.7）。"""
        td = trade_date or self._today_provider()
        self._snapshot.run_close(td)
        # 对账前 flush 持久化队列：收盘兜底 + 盘中回调都是「写后异步」入队，必须先落盘，
        # 否则对账（读本机 SQLite）会读到不完整数据（doc/05 关键不变量「当日完成 / 一致读」）。
        # flush 成败校验（评审三轮 EXEC-DW-03）：flush_hook 返回 False（commit 失败/写线程死）说明成交镜像批
        # 可能未全落盘，对账据不完整数据会失真；强告警留痕（write-behind fail-closed 另由 on_storage_failure 兜）。
        flushed = self._deps.flush_hook()
        if flushed is False:
            self._logger.error(
                "engine_close_flush_incomplete", trade_date=str(td),
                note="收盘对账前镜像落盘未确认全 commit，对账可能读到不完整数据，须排查写线程/磁盘",
            )
        report = self._reconcile.run(td)
        # —— 对账结果阻断/补采（评审二轮 P1#9，复审 P1-1 修正）：原实现只打日志、不动作 ——
        # 1) 成交量不勾稽（needs_backfill）→ 当日内立即补采 + 持仓重建（隔日 query_* 清空不可补），
        #    补采后【重跑对账】取新 report——避免把"补采即可自愈的瞬时不勾稽"误判为需人工介入的偏差而误阻断。
        if report.needs_backfill:
            self._logger.warn("engine_reconcile_needs_backfill", trade_date=str(td))
            try:
                self._snapshot.run_backfill(td)
                self._rebuild_positions_from_broker(td)
                self._deps.flush_hook()              # 补采落盘后再读，保证重跑对账读到补全数据
                report = self._reconcile.run(td)      # 用补采后的新结果判定是否真的有残留偏差
            except Exception as exc:  # noqa: BLE001 补采失败不拖垮收盘流程，但强告警；保留补采前 report
                self._logger.error("engine_reconcile_backfill_failed", trade_date=str(td), error=str(exc))
        # 2) （补采后仍残留的）真异常 → 置持久"对账未通过"标记，阻断次日开仓直至人工清除。
        #    只对【需人工介入的真异常】阻断（评审复审 P1-1）：漏单 missing_report、手工单/串账户 manual_order、
        #    补采后仍残留的成交量不勾稽 trade_discrepancies、资产偏差 asset_discrepancy；而 order_failed（台账已
        #    ERROR、本就无回报）是良性已知态，不触发阻断，避免高频误禁开。
        blocking = (
            any(d.kind in ("missing_report", "manual_order") for d in report.order_discrepancies)
            or bool(report.trade_discrepancies)
            or report.asset_discrepancy is not None
        )
        if blocking:
            self._set_reconcile_block(
                td, len(report.order_discrepancies), len(report.trade_discrepancies)
            )
        self._logger.info(
            "engine_close_done",
            trade_date=str(td),
            matched_orders=report.matched_orders,
            discrepancies=len(report.order_discrepancies),
            needs_backfill=report.needs_backfill,
            reconcile_blocked=self._reconcile_blocked,
        )

    # ------------------------------------------------------------------
    # 对账阻断标志的持久读写（评审二轮 P1#9）
    # ------------------------------------------------------------------
    def _read_reconcile_block(self) -> bool:
        """读持久"对账未通过"标志（repository 支持则用之；不支持则视为未阻断）。"""
        getter = getattr(self._deps.repository, "get_flag", None)
        if not callable(getter):
            return False
        try:
            return bool(getter(_RECONCILE_BLOCK_FLAG))
        except Exception as exc:  # noqa: BLE001 读标志失败不致命，按未阻断处理但留痕
            self._logger.warn("engine_reconcile_flag_read_failed", error=str(exc))
            return False

    def _set_reconcile_block(self, td: date, n_order: int, n_trade: int) -> None:
        """置"对账未通过"标志（持久 + 本进程内存），并强告警。人工核查后删除标志方可恢复开仓。"""
        setter = getattr(self._deps.repository, "set_flag", None)
        if callable(setter):
            try:
                setter(_RECONCILE_BLOCK_FLAG, td.isoformat())
            except Exception as exc:  # noqa: BLE001 持久化失败仍置内存标志，至少本进程内阻断
                self._logger.error("engine_reconcile_block_persist_failed", error=str(exc))
        self._reconcile_blocked = True
        self._logger.error(
            "engine_reconcile_block_set",
            trade_date=str(td), order_discrepancies=n_order, trade_discrepancies=n_trade,
            note="对账未通过,次日只守仓不开新仓;人工核查后删除 system_flags.reconcile_blocked 解除",
        )


def build_engine(deps: EngineDeps) -> Engine:
    """装配引擎（便于 main / 测试统一入口）。"""
    return Engine(deps)
