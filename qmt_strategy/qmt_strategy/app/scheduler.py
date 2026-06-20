"""交易日生命周期调度器 DailyScheduler（doc/05 / 设计 §7.5）。

在【独立线程】按东八区钟点触发当日各阶段（主线程跑 trader.run_forever 接收回调）：
  盘前(默认 ≥08:55，且 <收盘)  → PREWARM：连接守护建连/重连 + 装载当日 watchlist（一次）
  竞价(09:15–09:30)            → RUN_AUCTION：自写定时器轮询竞价（run_auction 内部阻塞至 09:30，一次）
  盘中(09:30–收盘)             → INTRADAY：周期性 sweep_ttl（超时撤单巡检）+（注入盘口源时）run_sell_pass
  收盘(默认 15:05)             → CLOSE_BATCH：收盘快照 + 对账（一次）
  盘后(默认 15:35)             → SYNC：本机 SQLite → 远端 MySQL 幂等同步（一次）
非交易日 / 时段外：IDLE 空转。

可测性：``decide(now)`` 是纯函数（给定东八区时刻 + 当日已触发集合 + 交易日历 → 下一动作），
单测可在各边界时刻断言动作；``run()`` 用注入的 clock/sleep_fn/max_iters 驱动有限轮次。

健壮性：单个动作抛异常只记日志、不终止调度循环（盘中一个环节出错不拖垮整日调度）。
"""

from __future__ import annotations

import threading
import time as _time
from datetime import date, time as dtime
from enum import Enum
from typing import Callable, Dict, Optional, Set

from ..common.time_utils import east8_time_of, east8_trade_date


class Action(str, Enum):
    IDLE = "IDLE"
    PREWARM = "PREWARM"
    RUN_AUCTION = "RUN_AUCTION"
    INTRADAY = "INTRADAY"
    CLOSE_BATCH = "CLOSE_BATCH"
    SYNC = "SYNC"


# 各阶段东八区默认钟点（可经构造参数覆盖）。
_PREMARKET_START = dtime(8, 55)
_AUCTION_START = dtime(9, 15)
_INTRADAY_START = dtime(9, 30)
_DEFAULT_CLOSE = dtime(15, 5)
_DEFAULT_SYNC = dtime(15, 35)


def parse_hhmm(s: str, default: dtime) -> dtime:
    """把 'HH:MM' 解析为 time；非法则用 default。"""
    try:
        hh, mm = str(s).split(":")[:2]
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return default


class DailyScheduler:
    """按东八区钟点触发交易日闭环的调度器。

    依赖（全部注入，便于单测用 fake）：
    - engine：提供 prewarm/run_auction/sweep_ttl/run_sell_pass/close_batch；
    - guard：连接守护（PREWARM 时 connect_and_subscribe）；可为 None（仅离线/测试）；
    - stack：本机存储栈（SYNC 时 sync_to_remote）；可为 None；
    - clock / logger / calendar：时钟、日志、交易日历（is_open 判交易日）。
    - sell_books_provider：可选 (today)->Dict[ts_code, OrderBook]，盘中卖出决策的实时盘口源；
      缺省 None → 盘中只跑 sweep_ttl，不跑 run_sell_pass（盘口源需 xtdata 真实落地，属 TODO(实测)）。
    """

    def __init__(
        self,
        engine,
        guard,
        stack,
        clock,
        logger,
        *,
        calendar,
        close_time: Optional[dtime] = None,
        sync_time: Optional[dtime] = None,
        poll_seconds: float = 15.0,
        sell_books_provider: Optional[Callable[[date], dict]] = None,
        watchlist_prefetch: Optional[Callable[[date], int]] = None,
        calendar_refresh: Optional[Callable[[date], bool]] = None,
    ) -> None:
        self._engine = engine
        self._guard = guard
        self._stack = stack
        self._clock = clock
        self._logger = logger
        self._calendar = calendar
        self._close_time = close_time or _DEFAULT_CLOSE
        self._sync_time = sync_time or _DEFAULT_SYNC
        self._poll_seconds = poll_seconds
        self._sell_books_provider = sell_books_provider
        # 盘前 watchlist 拉取钩子（doc/07）：PREWARM 时先从信号侧 HTTP 拉当日名单落本机 SQLite，
        # 再让 engine.prewarm 从本机库装载。缺省 None → 跳过（向后兼容：engine 读本机库已有/旧数据）。
        self._watchlist_prefetch = watchlist_prefetch
        # 盘前交易日历校验/补取钩子（doc/29 J-3）：PREWARM 时先校验本地日历是否覆盖到今天，不足则拉信号侧
        # /api/internal/trade_calendar 补取并入【与 engine 共享的同一日历对象】+ 落本地文件。须在 engine.prewarm
        # 之前调用——使 prewarm 据补取后的日历重算覆盖度（仍不足则 engine fail-closed 阻断开仓）。缺省 None → 跳过。
        self._calendar_refresh = calendar_refresh
        # 每个交易日已触发的「一次性」动作集合：key=date → {Action,...}
        self._fired: Dict[date, Set[Action]] = {}
        self._stopped = False

    # ------------------------------------------------------------------
    # 纯决策（可单测）
    # ------------------------------------------------------------------
    def decide(self, now_utc) -> Action:
        """根据东八区时刻 + 当日已触发集合 + 交易日历，返回下一应触发动作。"""
        today = east8_trade_date(now_utc)
        if not self._calendar.is_open(today):
            return Action.IDLE
        t = east8_time_of(now_utc)
        fired = self._fired.get(today, set())

        # 1) 盘前装载（一次）：≥08:55 且未到收盘且尚未装载 → PREWARM（含开市后晚启动的补装载）。
        if Action.PREWARM not in fired and _PREMARKET_START <= t < self._close_time:
            return Action.PREWARM
        # 2) 收盘批次（一次）：到收盘点且未做 → CLOSE_BATCH（即使晚启动也补做收盘快照/对账）。
        if Action.CLOSE_BATCH not in fired and t >= self._close_time:
            return Action.CLOSE_BATCH
        # 3) 盘后同步（一次）：到同步点、已收盘、未同步 → SYNC。
        if Action.SYNC not in fired and Action.CLOSE_BATCH in fired and t >= self._sync_time:
            return Action.SYNC
        # 4) 竞价（一次）：09:15–09:30 且已装载 → RUN_AUCTION（run_auction 阻塞至 09:30）。
        if Action.RUN_AUCTION not in fired and Action.PREWARM in fired and _AUCTION_START <= t < _INTRADAY_START:
            return Action.RUN_AUCTION
        # 5) 盘中（周期）：09:30–收盘 且已装载 → INTRADAY（sweep_ttl + 可选卖出）。
        if Action.PREWARM in fired and _INTRADAY_START <= t < self._close_time:
            return Action.INTRADAY
        return Action.IDLE

    def _mark(self, today: date, action: Action) -> None:
        self._fired.setdefault(today, set()).add(action)

    def _run_sell_with_feed_health(self, today: date, *, session: str, manage_feed: bool) -> None:
        """取实时盘口跑卖出，并（盘中）以取盘口成败作行情健康源（评审三轮 EXEC-risk-04 / EXEC-sched-05）。

        业务意图：盘中连续交易段 _market_feed_ok 原无更新来源（竞价后 AuctionPoller 退出），行情断流
        FREEZE 安全默认在盘中形同虚设。这里把「盘中卖出取盘口的成败」作为行情健康置位源。
        manage_feed 口径：
        - True（盘中 INTRADAY）：sell_books_provider 未接线/取盘口异常 → report_market_feed(False)
          保守不卖（不残留 prewarm 重置的 True）；取到盘口（即便空）→ report_market_feed(True) 再卖。
        - False（竞价段 RUN_AUCTION）：行情健康由买入侧 AuctionPoller 自管，本路径**不触碰** _market_feed_ok，
          只在有盘口源时跑一次竞价卖出评估（提供 decide_auction 的调度入口，避免其成死代码）。
        边界：engine 缺 report_market_feed（部分离线/单测 fake）时用 getattr 守卫跳过健康置位。
        """
        report = getattr(self._engine, "report_market_feed", None) if manage_feed else None
        if self._sell_books_provider is None:
            # E-10（评审 doc/24）：卖出门控关（QMT_SELL_PASS_LIVE=false → provider=None）是「有意不接卖出」，
            # 不等于「行情断流」。原实现在此 report(False) 把全局 _market_feed_ok 置 False、污染 risk.gate 第 1 层
            # （把「卖出未接线」误编码为「行情不健康」恒 FREEZE，盘中任何依赖该标志的逻辑被语义混淆）。
            # 此处直接返回、**不触碰行情健康位**——「保守不卖」已由 provider=None（run_sell_pass 不被调用）达成；
            # 行情健康由竞价段买入侧 AuctionPoller 的 feed_health_sink 自管。
            return
        try:
            books = self._sell_books_provider(today)
        except Exception as exc:  # noqa: BLE001 取盘口失败 = 行情不可信：置 feed 不健康 + 强告警，不卖
            self._logger.error(
                "scheduler_sell_books_failed", trade_date=str(today), session=session, error=repr(exc)
            )
            if callable(report):
                report(False)
            return
        if callable(report):
            report(True)  # 成功取到盘口源（即便空）= 行情通道可用
        self._engine.run_sell_pass(today, books, session=session)

    # ------------------------------------------------------------------
    # 动作分发
    # ------------------------------------------------------------------
    def dispatch(self, action: Action, today: date) -> None:
        """执行一个动作。一次性动作执行后登记 fired；INTRADAY 为周期动作不登记。"""
        if action == Action.IDLE:
            return
        if action == Action.PREWARM:
            # 连接就绪为盘前重建前置（评审三轮 EXEC-sched-10）：检查 connect_and_subscribe 返回值，
            # 连接失败时不把 PREWARM 标 final fired（允许下轮重连重做），并强告警。
            connected = True
            if self._guard is not None:
                # 已就绪则不重建（评审 doc/19 H-2）：避免 PREWARM 每个 poll 周期都强制 connect_and_subscribe
                # 重建 trader、与 on_disconnected/supervise 的重连争用、把刚连好的通道反复推倒重建。仅未就绪才建连；
                # 此前 reconnect/supervise 已把连接带到就绪时，本轮直接视为已连（connected=True），随后定稿。
                if getattr(self._guard, "ready", False):
                    connected = True
                else:
                    connected = bool(self._guard.connect_and_subscribe())
                if not connected:
                    self._logger.warn(
                        "scheduler_prewarm_conn_not_ready_will_retry", trade_date=str(today),
                        note="连接未就绪：基于未就绪 trader 的 query_* 重建/抓基线可能失败，不定稿，下轮重连重做",
                    )
            # 先盘前拉取当日 watchlist 落本机 SQLite（失败不抛、降级无名单，详见 WatchlistPrefetcher），
            # 再让 engine.prewarm 从本机库装载——保证 prewarm 读到的是当日最新名单。
            # engine.prewarm 幂等（日初基线复用 + 持仓以 QMT 为权威重建），连接失败时重建可能不可靠但重跑安全。
            # 盘前交易日历校验/补取（doc/29 J-3）：先于 watchlist 拉取与 engine.prewarm——使日历补取并入共享对象后，
            # prewarm 据补取后的覆盖度重算 fail-closed。失败不抛（refresher 内部已兜底，仍不足则 engine fail-closed）。
            if self._calendar_refresh is not None:
                try:
                    ok = self._calendar_refresh(today)
                    if not ok:
                        self._logger.error(
                            "scheduler_calendar_coverage_insufficient", trade_date=str(today),
                            note="盘前交易日历校验/补取后仍不足，engine 将 fail-closed 只守仓不开新仓",
                        )
                except Exception as exc:  # noqa: BLE001 校验/补取已内部兜底，这里再保险一层不拖垮 PREWARM
                    self._logger.error(
                        "scheduler_calendar_refresh_error", trade_date=str(today), error=repr(exc)
                    )
            saved = 0
            if self._watchlist_prefetch is not None:
                try:
                    saved = self._watchlist_prefetch(today)
                    self._logger.info("scheduler_watchlist_prefetched", trade_date=str(today), saved=saved)
                except Exception as exc:  # noqa: BLE001 prefetch 内部已兜底，这里再保险一层不拖垮 PREWARM
                    self._logger.error(
                        "scheduler_watchlist_prefetch_error", trade_date=str(today), error=repr(exc)
                    )
            self._engine.prewarm(today)
            # PREWARM 重试与定稿（评审二轮 P2#48 + 三轮 EXEC-sched-10 + F11 + 评审 doc/19 H-4）：
            # - watchlist【真失败(saved<0)】：仅在开窗前(< 竞价 9:15)重试拉新名单；过 9:15 不再为名单延迟、用本机库
            #   已有名单定稿。合法空名单(saved==0，空仓日/报告未就绪)不重试、直接定稿，避免空仓日反复重连打满频控。
            #   （F11 口径：prefetch 返回 -1=真失败 / 0=合法空 / >0=成功。）
            # - 连接未就绪(not connected)：【不论时间，持续重试到连上或收盘，不定稿】。H-4 修复要点——原实现过 9:15
            #   一律强制定稿(`before_auction and ...`)，会把「基于未就绪 trader 重建持仓/抓日初权益基线」的残缺态
            #   定死且当日永不重做(调度层不可恢复单点)。改为连接恢复前绝不定稿：一旦 reconnect/本分支建连成功，
            #   engine.prewarm 重跑会把持仓重建/权益基线补齐(prewarm 幂等、_capture_day_open_equity 查询失败不缓存
            #   坏基线、下轮自愈)。decide() 仅在 < 收盘 才返回 PREWARM，故未连上时重试天然以收盘为界、不会无限。
            # clock 缺失（部分离线/单测注入 None）→ 不做时间窗判断（watchlist 不重试），但仍遵守「连接未就绪不定稿」。
            now_t = east8_time_of(self._clock.now_utc()) if self._clock is not None else None
            before_auction = now_t is not None and now_t < _AUCTION_START
            watchlist_hard_fail = self._watchlist_prefetch is not None and saved < 0
            should_retry = (not connected) or (before_auction and watchlist_hard_fail)
            if not should_retry:
                self._mark(today, Action.PREWARM)
                self._logger.info(
                    "scheduler_prewarmed", trade_date=str(today), saved=saved, connected=connected
                )
            else:
                self._logger.warn(
                    "scheduler_prewarm_will_retry", trade_date=str(today), saved=saved, connected=connected,
                    note="连接未就绪(持续重试至连上/收盘) 或 (未到9:15且watchlist真失败)，下轮重试",
                )
        elif action == Action.RUN_AUCTION:
            self._logger.info("scheduler_run_auction", trade_date=str(today))
            # 竞价段卖出评估入口（评审三轮 EXEC-sched-05）：可卖日 9:15–9:25 集合竞价定夺卖出，原调度从无
            # session='auction' 调 run_sell_pass 的入口、decide_auction 整条卖出分支是死代码。这里在买入侧
            # run_auction（阻塞至 9:30）之前先跑一次竞价卖出评估（需实时盘口源 sell_books_provider，缺省跳过）。
            # 注：竞价段行情健康由买入侧 AuctionPoller 自管，故此处不改 _market_feed_ok（conservative=False）。
            self._run_sell_with_feed_health(today, session="auction", manage_feed=False)
            self._engine.run_auction()  # 阻塞至 09:30（AuctionPoller 内单轮异常不放弃整段）
            # 成功跑完才登记 fired（评审 doc/19 H-4）：原实现「先 _mark 再跑」在 run_auction 入口/早期抛异常
            # （被 run() 顶层 try/except 吞）时，当日竞价已被定稿、整段放弃且不重试，与 AuctionPoller 内「单轮
            # 异常不放弃整段」的健壮性自相矛盾。改为成功返回才标记：异常则不标 → 下轮(仍 <9:30)decide 重新返回
            # RUN_AUCTION 重试，竞价相位可恢复。run_auction 阻塞期间调度线程在其内部、loop 无法重入；正常返回
            # (≈9:30)后标记，过 9:30 decide 不再返回本相位（自然以竞价窗口为界，不会无限重试）。
            self._mark(today, Action.RUN_AUCTION)
            self._logger.info("scheduler_run_auction_done", trade_date=str(today))
        elif action == Action.INTRADAY:
            # 存储健康周期体检（评审二轮 P0#2）：写线程静默死亡且当轮无 submit 触发 on_failure 时的兜底发现，
            # 不健康即 engine fail-closed 停开新仓。置于盘中动作首位，使后续 sweep/卖出在已知状态下进行。
            health_tick = getattr(self._engine, "storage_health_tick", None)
            if callable(health_tick):
                health_tick()
            # 下单通道主动探活（评审三轮 EXEC-risk-05）：盘中周期心跳 query_stock_asset，连续失败达阈值即
            # report_trade_conn(False) → risk.gate FREEZE，覆盖「通道静默变坏但未触发断线回调」的盲区。
            heartbeat = getattr(self._engine, "trade_conn_heartbeat", None)
            if callable(heartbeat):
                heartbeat()
            self._engine.sweep_ttl()  # 超时撤单巡检（无需盘口）
            # 盘中卖出决策（评审三轮 EXEC-risk-04）：以取盘口成败作行情健康源；无盘口源时保守置 feed=False
            # （盘中不卖，不残留 prewarm 重置的 True），避免「行情断流 FREEZE 安全默认」在盘中形同虚设。
            self._run_sell_with_feed_health(today, session="intraday", manage_feed=True)
        elif action == Action.CLOSE_BATCH:
            self._engine.close_batch(today)
            self._mark(today, Action.CLOSE_BATCH)
            self._logger.info("scheduler_close_batch", trade_date=str(today))
        elif action == Action.SYNC:
            if self._stack is not None:
                self._stack.sync_to_remote(today)
            self._mark(today, Action.SYNC)
            self._logger.info("scheduler_synced", trade_date=str(today))

    # ------------------------------------------------------------------
    # 主循环（独立线程运行）
    # ------------------------------------------------------------------
    def mark_prewarmed(self, today: date) -> None:
        """供 main 在启动时已手动 prewarm 后调用，避免调度器重复 PREWARM（重连）。"""
        self._mark(today, Action.PREWARM)

    def stop(self) -> None:
        self._stopped = True

    def run(self, sleep_fn: Callable[[float], None] = _time.sleep, max_iters: Optional[int] = None) -> None:
        """调度主循环：每轮 decide → dispatch → sleep。异常只记日志不退出（盘中健壮性）。

        sleep_fn / max_iters 供单测注入有限轮次；生产用默认 time.sleep 常驻。
        """
        iters = 0
        while not self._stopped:
            try:
                now = self._clock.now_utc()
                today = east8_trade_date(now)
                action = self.decide(now)
                self.dispatch(action, today)
            except Exception as e:  # noqa: BLE001 调度异常不致命：记录后继续下一轮
                self._logger.error("scheduler_tick_failed", error=repr(e))
            iters += 1
            if max_iters is not None and iters >= max_iters:
                break
            sleep_fn(self._poll_seconds)

    def start_thread(self) -> threading.Thread:
        """以 daemon 线程启动调度循环，返回线程句柄（主线程另跑 run_forever 接收回调）。"""
        t = threading.Thread(target=self.run, name="qmt-scheduler", daemon=True)
        t.start()
        return t
