"""集合竞价轮询主循环（§3.2 / §3.6 / §3.7）。

业务意图：在 9:15–9:25 集合竞价窗口内自写定时器主动轮询 get_full_tick(codes)，把每帧
原始 tick 经 auction_factors 加工成 AuctionSnapshot 推给下游 entry_router（router_sink）。
本模块只采集与计算，不做买 / 弃决策、不下单。

关键设计口径（强约束）：
- 【不依赖 tick 回调】：竞价撮合不连续，xtdata 的 tick 回调在 9:15–9:25 不保证触发，
  故一律自写定时器轮询，poll_once 完全由定时器驱动，绝不引用任何 on_xxx tick 回调（§3.2）。
- 时段判定复用 common.auction_window.resolve_phase（按东八区钟点，禁手工 ±8h，§3.3）。
- 降级 B（§3.7）：get_full_tick 整体失败 → 本轮无完整 tick，对每个 code 产出仅含可得字段
  （tick=None → 仅空壳，或仅 last_price）的 snapshot 并标 data_quality，进程不崩、仍持续产帧。
- 临近 9:20 / 9:25 关键时点加密轮询（interval ≤ 1.0s），保证定盘前观测密度。
"""

from __future__ import annotations

import time as _time
from datetime import datetime, time as dtime
from typing import Callable, Dict, List, Optional

from ..common.auction_window import resolve_phase
from ..common.time_utils import east8_time_of
from ..config.settings import Settings
from ..contracts.enums import AuctionPhase
from ..contracts.errors import TickSourceError
from ..contracts.models import AuctionSnapshot, PlanRow
from ..contracts.protocols import Clock, RouterSink, StructLogger, TickSource
from .auction_factors import compute_auction_factors

# plan_provider 返回当日计划行映射：{ts_code: PlanRow}（§3.5 codes 来源）。
PlanProvider = Callable[[], Dict[str, PlanRow]]

# 临近关键时点（东八区）：9:20 前 30 秒 / 9:25 前 30 秒触发加密轮询。
_KEYPOINT_0919_30 = dtime(9, 19, 30)
_KEYPOINT_0924_30 = dtime(9, 24, 30)
_KEYPOINT_0920 = dtime(9, 20, 0)
_KEYPOINT_0925 = dtime(9, 25, 0)
# 加密后的轮询上限间隔（秒）。
_DENSE_INTERVAL = 1.0


class AuctionPoller:
    """竞价轮询器（§3.6 主循环）。

    依赖全部经协议 / 注入，便于单测用 fake：
      - tick_source：TickSource，封装 get_full_tick（失败抛 TickSourceError）。
      - plan_provider：无参回调，返回当日 {ts_code: PlanRow}（每轮取最新，支持盘前热更）。
      - router_sink：RouterSink，把每帧 AuctionSnapshot 推给下游 entry_router（进程内回调）。
      - settings：取轮询周期 auction_poll_interval_sec。
      - clock：取 now_utc（禁直接 datetime.now）。
      - logger：结构化留痕（取数失败 / 降级）。
    """

    def __init__(
        self,
        tick_source: TickSource,
        plan_provider: PlanProvider,
        router_sink: RouterSink,
        settings: Settings,
        clock: Clock,
        logger: StructLogger,
        feed_health_sink: Optional[Callable[[bool], None]] = None,
    ):
        self._tick_source = tick_source
        self._plan_provider = plan_provider
        self._router_sink = router_sink
        self._settings = settings
        self._clock = clock
        self._logger = logger
        # 行情健康上报（评审二轮 P1#15）：每轮 get_full_tick 成功=True / 整体失败(降级)=False，
        # 经此回调通知 Engine 置 _market_feed_ok，使行情断流时 risk.gate 走 FREEZE 安全默认。缺省不上报。
        self._feed_health_sink = feed_health_sink
        # 每只股票的累积帧序列（算重心用增量），key=ts_code。
        # 仅保留「成功取到的完整 tick」入历史，降级帧（tick=None）不污染重心计算。
        self._history: Dict[str, List[dict]] = {}
        # run 主循环的停止标志，供外部注入式打断（除 max_loops / CLOSED_WINDOW 外的兜底）。
        self._stop = False

    def resolve_phase(self, now_utc: datetime) -> AuctionPhase:
        """复用 common.auction_window.resolve_phase（按东八区时段映射，§3.3）。"""
        return resolve_phase(now_utc)

    def stop(self) -> None:
        """请求停止主循环（下一轮检查时退出）。供测试 / 外部信号打断常驻进程。"""
        self._stop = True

    def poll_once(self, now_utc: datetime) -> List[AuctionSnapshot]:
        """单轮轮询：批量取 tick → 逐 code 算四因子 → push 下游，返回本轮 snapshot 列表（§3.6）。

        完全由定时器驱动（调用方传入 now_utc），不引用任何 tick 回调，证明不依赖回调（§3.2）。
        降级 B（§3.7）：get_full_tick 抛 TickSourceError → 本轮拿不到任何完整 tick，
        对每个 code 用 tick=None 产出空壳 snapshot（标 NO_TICK），进程不崩、仍持续产帧。
        """
        phase = self.resolve_phase(now_utc)
        plans = self._plan_provider()
        codes: List[str] = list(plans.keys())
        snapshots: List[AuctionSnapshot] = []

        # —— 批量取 tick（一次取全部，避免逐股放大请求数，§3.2）——
        ticks: Dict[str, dict]
        degraded = False
        try:
            ticks = self._tick_source.get_full_tick(codes)
        except TickSourceError as exc:
            # 降级 B：整体取数失败，本轮无完整 tick；记告警，对每 code 仍产出降级帧。
            degraded = True
            ticks = {}
            self._logger.warn(
                "auction_tick_fetch_failed",
                phase=str(phase),
                codes=len(codes),
                error=str(exc),
            )

        # 行情健康上报（评审二轮 P1#15）：本轮取数成败决定 _market_feed_ok。整体降级 → 行情断流 FREEZE。
        if self._feed_health_sink is not None and codes:
            try:
                self._feed_health_sink(not degraded)
            except Exception:  # noqa: BLE001 健康上报失败不得影响竞价采集
                pass

        # —— 逐 code 计算因子并推下游 ——
        # 历史累积延后到整轮成功后统一提交（评审复审 P2#46）：原实现在循环内逐 code append 历史、紧接着调
        # _router_sink（可能抛错）。若某 code 处抛错被上层(run)吞掉，已 append 的前序 code 历史会残留，下轮
        # 重跑同一轮 tick 时再次 append → _history 重复累积、污染后续帧 Δvol/重心增量。改为本地缓冲，循环
        # 全部跑完(无异常)才统一并入 _history；半轮失败则整轮历史不提交，下轮干净重跑。
        pending_history: List[tuple] = []
        for code in codes:
            plan = plans[code]
            tick = ticks.get(code)  # 可能为 None（该 code 本轮无 tick，或整体降级）
            snap = compute_auction_factors(
                ts_code=code,
                tick=tick,
                prev_ticks=self._history.get(code, []),
                phase=phase,
                plan=plan,
                now_utc=now_utc,
            )
            # 仅把成功取到的完整 tick 缓冲（供整轮成功后入历史）；降级帧（tick 缺失）不入历史，避免污染 Δvol。
            if tick is not None:
                pending_history.append((code, tick))
            # push 给下游 entry_router 观测，本模块不在此下单（若抛错，pending_history 整轮丢弃）。
            self._router_sink(snap)
            snapshots.append(snap)
        # 整轮无异常 → 统一提交历史（半轮失败上面会在抛出前丢弃 pending_history）。
        for code, tick in pending_history:
            self._history.setdefault(code, []).append(tick)

        if degraded:
            # 留痕本轮降级帧数，便于复盘确认「竞价不可得 → 退化为开盘后确认」。
            self._logger.info(
                "auction_poll_degraded", phase=str(phase), produced=len(snapshots)
            )
        return snapshots

    def _next_interval(self, now_utc: datetime) -> float:
        """计算下一轮 sleep 间隔（秒）：默认取配置周期，临近 9:20 / 9:25 加密（§3.6）。

        业务意图：定盘前观测密度要够，故在 9:19:30–9:20 与 9:24:30–9:25 两个窗口把间隔压到 ≤1s。
        """
        base = float(self._settings.auction_poll_interval_sec)
        t = east8_time_of(now_utc)
        # 临近关键时点窗口（左闭右开，落在加密窗口内即压缩间隔）。
        near = (_KEYPOINT_0919_30 <= t < _KEYPOINT_0920) or (
            _KEYPOINT_0924_30 <= t < _KEYPOINT_0925
        )
        if near:
            return min(base, _DENSE_INTERVAL)
        return base

    def run(
        self,
        sleep_fn: Callable[[float], None] = _time.sleep,
        max_loops: Optional[int] = None,
    ) -> None:
        """主循环（§3.6）：按周期轮询，PRE_AUCTION/CLOSED_WINDOW 跳过，达窗口结束 / max_loops 退出。

        可测性：sleep_fn 与 max_loops 均可注入，单测用 FakeClock + 计数 sleep_fn 驱动有限轮次，
        不依赖真实 wall-clock，也不依赖任何 tick 回调。

        退出条件（任一满足即停）：
          - 时段进入 CLOSED_WINDOW（≥9:30，窗口结束）。
          - 达到 max_loops（测试用，防无限循环）。
          - 外部调用 stop()。
        时段处理：
          - PRE_AUCTION（<9:15）：预热段，不取 tick，仅 sleep 后继续（等开窗）。
          - CLOSED_WINDOW（≥9:30）：窗口结束，直接退出。
          - CANCELABLE / LOCKED / SETTLED：正常 poll_once。
        """
        loops = 0
        while not self._stop:
            now_utc = self._clock.now_utc()
            phase = self.resolve_phase(now_utc)

            # 窗口结束：退出主循环（9:25–9:30 SETTLED 仍采集定盘帧，≥9:30 才收）。
            # 注意：CLOSED_WINDOW 立即 break、不计入 loops、不 sleep（窗口已过无需等待）。
            if phase == AuctionPhase.CLOSED_WINDOW:
                self._logger.info("auction_poll_closed", phase=str(phase))
                break

            if phase == AuctionPhase.PRE_AUCTION:
                # 预热段：尚未开窗，不取 tick，只等待（避免空转打满 CPU），下一轮再判时段。
                pass
            else:
                # CANCELABLE / LOCKED / SETTLED：正常采集一轮。
                # 单轮异常隔离（评审二轮 P2#46）：调度层 RUN_AUCTION 先标 fired 再跑本循环（防重入阻塞），若
                # poll_once 某轮抛错(因子计算/router_sink/脏 tick)逸出，整段竞价窗口会被放弃且当日永不重试。
                # 这里把单轮异常就地吞掉(已留痕)、继续下一轮，保证竞价窗口不因一轮抖动整体崩。
                try:
                    self.poll_once(now_utc)
                except Exception as exc:  # noqa: BLE001 单轮异常不放弃整段竞价
                    self._logger.error("auction_poll_once_failed", phase=str(phase), error=repr(exc))

            # 一轮工作完成后 sleep（间隔临近 9:20/9:25 自动加密），再计数判退出。
            # 口径：max_loops 计「完整迭代次数」，每轮含一次 sleep，便于测试用有限轮次驱动。
            sleep_fn(self._next_interval(now_utc))
            loops += 1
            if max_loops is not None and loops >= max_loops:
                break
