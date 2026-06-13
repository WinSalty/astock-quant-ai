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

    # ------------------------------------------------------------------
    # 动作分发
    # ------------------------------------------------------------------
    def dispatch(self, action: Action, today: date) -> None:
        """执行一个动作。一次性动作执行后登记 fired；INTRADAY 为周期动作不登记。"""
        if action == Action.IDLE:
            return
        if action == Action.PREWARM:
            if self._guard is not None:
                self._guard.connect_and_subscribe()
            # 先盘前拉取当日 watchlist 落本机 SQLite（失败不抛、降级无名单，详见 WatchlistPrefetcher），
            # 再让 engine.prewarm 从本机库装载——保证 prewarm 读到的是当日最新名单。
            if self._watchlist_prefetch is not None:
                try:
                    saved = self._watchlist_prefetch(today)
                    self._logger.info("scheduler_watchlist_prefetched", trade_date=str(today), saved=saved)
                except Exception as exc:  # noqa: BLE001 prefetch 内部已兜底，这里再保险一层不拖垮 PREWARM
                    self._logger.error(
                        "scheduler_watchlist_prefetch_error", trade_date=str(today), error=repr(exc)
                    )
            self._engine.prewarm(today)
            self._mark(today, Action.PREWARM)
            self._logger.info("scheduler_prewarmed", trade_date=str(today))
        elif action == Action.RUN_AUCTION:
            self._mark(today, Action.RUN_AUCTION)  # 先登记再跑（run_auction 阻塞至 09:30，避免重入）
            self._logger.info("scheduler_run_auction", trade_date=str(today))
            self._engine.run_auction()
        elif action == Action.INTRADAY:
            self._engine.sweep_ttl()  # 超时撤单巡检（无需盘口）
            if self._sell_books_provider is not None:
                # 盘中卖出决策（需实时盘口源；缺省不注入则跳过，属 TODO(实测)）。
                books = self._sell_books_provider(today)
                self._engine.run_sell_pass(today, books, session="intraday")
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
