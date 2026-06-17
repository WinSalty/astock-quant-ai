"""连接守护：xttrader 连接生命周期管理（§2.2 时序）。

业务意图：把 QMT XtQuantTrader 的「构造 → 注册回调 → start → connect(判返 0) →
subscribe → run_forever 常驻」固定时序与「断线重建（换新 session_id）」收口到单点，
避免下单链路各处自行管理连接状态导致口径漂移。

关键口径（与设计文档 §2.2 / §2.6 / §2.8 一致）：
- 就绪判定**只读 ``connect()`` 返回值**（0 即连上）：xtquant 虽有 ``on_connected`` 回调，但 QMT
  官方推荐靠 ``connect()`` 返回值判就绪，本引擎据此判定、**不依赖 on_connected**（避免把就绪态绑定到
  回调时序上）。
- ``connect()`` 或 ``subscribe()`` 任一返回非 0 → 视为未就绪：``ready=False``，且
  connect 失败时**不再调用 subscribe / 不进入就绪态 / 不调 run_forever**，盘中不发起新开仓。
- **每次重连必须使用新 session_id**（≠ 旧值），旧 session 不复用——复用已断 session 可能
  订阅失效却无回报。session_id 由外部 ``session_id_provider`` 每次现取。
- 断线（``on_disconnected``）仅作盘中兜底重建，与任务计划每日盘前重建叠加而非互斥；
  重连成功后触发一次当日补采入口 ``on_reconnect_backfill``（补采写表细节不在本模块，
  本模块只负责「触发」这一串联点）。

只依赖契约层 Protocol（XtTraderLike / Clock / StructLogger）与 time_utils 的东八区换算，
绝不 import xtquant；真实落地时由调用方注入 xtquant 实现，单测注入 fake。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional, Tuple

from qmt_strategy.common.time_utils import east8_now_from_utc
from qmt_strategy.contracts.protocols import Clock, StructLogger, XtTraderLike


class ConnectionGuard:
    """xttrader 连接守护，封装连接/订阅就绪判定与断线重建（§2.2）。

    构造参数：
        trader_factory: 按 session_id 造一个 XtTraderLike（每次重连用新 session_id 造新 trader）。
        account: 下单/订阅入参（StockAccount；单测用 FakeStockAccount）。
        callback: 回调对象，注册用，本守护不解析其内容（可为任意对象）。
        clock: 时钟抽象，取当前 UTC naive（断线日志按东八区展示，禁手工 ±8h）。
        logger: 结构化日志。
        session_id_provider: 每次调用现取一个新的 session_id（须保证重连时 != 旧值）。
        on_reconnect_backfill: 重连成功后触发的当日补采入口；默认 no-op，调用方按需注入。
    """

    def __init__(
        self,
        trader_factory: Callable[[int], XtTraderLike],
        account: Any,
        callback: Any,
        clock: Clock,
        logger: StructLogger,
        session_id_provider: Callable[[], int],
        on_reconnect_backfill: Optional[Callable[[], None]] = None,
        on_connection_state: Optional[Callable[[bool], None]] = None,
        max_session_retry: int = 5,
    ) -> None:
        self._trader_factory = trader_factory
        self._account = account
        self._callback = callback
        self._clock = clock
        self._logger = logger
        self._session_id_provider = session_id_provider
        # 默认 no-op：未注入补采入口时重连成功也不报错，仅不做补采。
        self._on_reconnect_backfill: Callable[[], None] = (
            on_reconnect_backfill if on_reconnect_backfill is not None else (lambda: None)
        )
        # 连接状态变更通知（评审三轮 EXEC-sched-02）：就绪→True、断线/连接失败→False。
        # 业务意图：把「下单通道健康权威源」从「一次注定失败的补采」迁出，直接绑定连接就绪事件——
        # 重连真正就绪（换新 session + connect/subscribe 成功）才解冻下单闸，断线即冻结，杜绝 fail-open。
        # 默认 no-op，离线/单测不注入也不报错。
        self._on_connection_state: Callable[[bool], None] = (
            on_connection_state if on_connection_state is not None else (lambda ok: None)
        )
        # session 复用退避重试上限（评审三轮 EXEC-sched-08）：provider 误配吐相同 session 时，
        # 向 provider 重取至不同值的最大次数；超限仍复用即拒绝重连（fail-closed）。
        self._max_session_retry = max_session_retry

        # 运行期状态：就绪标记 + 当前 session_id + 当前 trader 句柄。
        # 跨线程保护（评审三轮 EXEC-sched-03）：调度线程做 connect/reconnect 换 trader 与主线程读
        # (ready, current_trader) run_forever 并发，下面这把锁只保护「状态读写 + 序列化的连接换手」，
        # 绝不在持锁时调 run_forever（阻塞）。锁仅包短临界区，I/O（connect/subscribe）在锁外执行，
        # 故不会与递归的 reconnect→connect_and_subscribe 自锁死（用普通 Lock 即可，不跨调用持锁）。
        self._lock = threading.Lock()
        # 重连/建连互斥锁（评审 doc/19 H-2）：序列化整个「建连/重连窗口」（trader_factory→start→connect→
        # subscribe 全序，含 I/O），杜绝断线回调线程 / 主循环 supervise / 调度 PREWARM 三路并发各造一个 trader、
        # 各 start() 起后台线程致【句柄漂移 + 旧 trader 后台线程泄漏】。用 RLock：reconnect 内调 connect_and_subscribe
        # 同线程重入放行；对其它线程则非阻塞 try-acquire 抢不到即跳过（那一路会把连接带到就绪，不并发再建 trader）。
        # 与短临界锁 _lock 解耦：_lock 只护 (ready/session/trader) 状态读写与一致快照，绝不持 _conn_lock 时阻塞快照。
        self._conn_lock = threading.RLock()
        self._ready: bool = False
        self._current_session_id: Optional[int] = None
        self._current_trader: Optional[XtTraderLike] = None

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def ready(self) -> bool:
        """连接是否就绪（connect + subscribe 均返 0 才为 True）。盘中开新仓须先判此值。"""
        return self._ready

    @property
    def current_session_id(self) -> Optional[int]:
        """当前会话号；尚未连接过为 None。重连后会变更为新值。"""
        return self._current_session_id

    @property
    def current_trader(self) -> Optional[XtTraderLike]:
        """当前 trader 句柄；供下单层取用。未就绪时也可能非 None（connect 失败仍保留句柄）。"""
        return self._current_trader

    def snapshot(self) -> Tuple[bool, Optional[XtTraderLike]]:
        """在锁内取 (ready, current_trader) 一致快照（评审三轮 EXEC-sched-03）。

        业务意图：主循环（run.py）跨线程读 ready 与 current_trader 并据此 run_forever，必须取到
        同一时刻的一致对：避免「读到 ready=True 但 trader 还是上一个/未 start 的新实例」。
        取完即释放锁再 run_forever（绝不持锁阻塞）。
        """
        with self._lock:
            return self._ready, self._current_trader

    def _set_ready(self, ok: bool) -> None:
        """在锁内置就绪标记，并在锁外通知连接状态（解冻/冻结下单闸的权威源）。"""
        with self._lock:
            self._ready = ok
        # on_connection_state 在锁外调用：通知引擎 report_trade_conn，避免持锁回调放大临界区。
        self._on_connection_state(ok)

    # ------------------------------------------------------------------
    # 连接 + 订阅（核心时序）
    # ------------------------------------------------------------------
    def connect_and_subscribe(self, session_id: Optional[int] = None) -> bool:
        """执行固定启动时序并返回是否就绪（§2.2）。

        严格顺序：session_id = provider()（或外部指定） → trader = factory(session_id) →
        register_callback(callback) → start() → rc = connect()。
        session_id 入参（评审三轮 EXEC-sched-08/P1-2）：缺省 None 时现取 provider；reconnect 为避免
        「复用退避时每轮全量重建 trader+start+subscribe 泄漏线程」，会先廉价地从 provider 取一个与旧值
        不同的 session_id，再以其显式调用本方法【只建一次】trader。
        - rc != 0：log.warn + ready=False + 返回 False，**不调 subscribe / 不进就绪态 /
          不调 run_forever**（由 run() 决定是否常驻，本方法只判就绪）。
        - sub_rc = subscribe(account)，sub_rc != 0：ready=False + 返回 False。
        - 两步均 0：ready=True + 返回 True。
        无论成败都保存 current_session_id / current_trader，便于断线重建对比新旧 session。

        重连/建连互斥（评审 doc/19 H-2）：全程持 _conn_lock（含下面的 I/O），非阻塞 try-acquire——抢不到即
        说明已有一路建连/重连在进行，立即跳过返回 False（不并发再造一个 trader）。reconnect 内调本方法靠
        RLock 同线程重入放行。
        """
        # 非阻塞抢互斥锁：抢不到 = 已有建连/重连在进行（另一线程持锁）→ 跳过本次，绝不并发再造 trader（H-2）。
        if not self._conn_lock.acquire(blocking=False):
            self._logger.warn(
                "connect_skip_in_progress",
                note="已有建连/重连在进行，跳过本次并发建连（H-2 重连互斥，防多 trader/后台线程泄漏）",
            )
            return False
        try:
            # 0) 进入连接/重连窗口即先置未就绪（评审三轮 EXEC-sched-03）：避免重连窗口内 _ready 维持旧 True
            #    而 _current_trader 已切到尚未 start/connect 的新实例，主循环据此对半就绪句柄 run_forever。
            with self._lock:
                self._ready = False

            # 1) 现取新 session_id（外部未指定时）并按其造 trader（重连复用本方法，保证每次都是新 session）。
            #    替换前先 best-effort 停旧 trader（H-2）：回收上一个 trader 的 start() 后台线程，防反复断线下泄漏。
            if session_id is None:
                session_id = self._session_id_provider()
            old_trader = self._current_trader
            trader = self._trader_factory(session_id)
            self._stop_trader_quietly(old_trader)
            with self._lock:
                self._current_session_id = session_id
                self._current_trader = trader

            # 2) 固定时序：注册回调 → 启动交易线程 → connect。顺序不可调换。（I/O 在互斥锁内、短临界锁外执行）
            trader.register_callback(self._callback)
            trader.start()
            rc = trader.connect()

            # 3) connect 判返 0：非 0 即连接失败，直接收口为未就绪，绝不继续 subscribe。
            if rc != 0:
                self._logger.warn(
                    "connect_failed",
                    session_id=session_id,
                    rc=rc,
                    # 断线/连接事件时间统一按东八区展示（盘口口径），落库时刻另由各写端走 time_utils。
                    at_east8=str(east8_now_from_utc(self._clock.now_utc())),
                )
                self._set_ready(False)  # 未就绪并通知冻结下单闸（连接失败 = 通道不可用）
                return False

            # 4) 订阅账户推送：sub_rc != 0 视为未就绪（订阅失败盘中收不到回报，不能进开新仓态）。
            sub_rc = trader.subscribe(self._account)
            if sub_rc != 0:
                self._logger.warn(
                    "subscribe_failed",
                    session_id=session_id,
                    sub_rc=sub_rc,
                    at_east8=str(east8_now_from_utc(self._clock.now_utc())),
                )
                self._set_ready(False)
                return False

            # 5) connect + subscribe 均成功 → 就绪。盘中推送开始进回调。
            self._logger.info("connect_ready", session_id=session_id)
            self._set_ready(True)  # 就绪并通知解冻下单闸（连接确实可用）
            return True
        finally:
            self._conn_lock.release()

    def _stop_trader_quietly(self, trader: Optional[XtTraderLike]) -> None:
        """best-effort 停掉被替换的旧 trader，回收其 start() 起的后台线程（评审 doc/19 H-2）。

        业务意图：重连换新 trader 时，旧 trader（多为已断线）的后台线程若无人 stop 会持续泄漏，反复断线下
        线程/句柄无界增长。这里在替换前调旧 trader 的 stop()（若该实现暴露了）。
        边界：trader 为 None（首次建连无旧实例）跳过；stop 不存在或抛错一律吞掉——不同 xtquant 版本的 stop
        语义差异属 TODO(实测)，绝不让「停旧失败」拖垮「建新连」。
        """
        if trader is None:
            return
        stop = getattr(trader, "stop", None)
        if not callable(stop):
            return
        try:
            stop()
            self._logger.info("old_trader_stopped", note="重连替换前已停旧 trader 回收后台线程(H-2)")
        except Exception:  # noqa: BLE001 停旧 trader 失败不影响新建连（best-effort）
            self._logger.warn("old_trader_stop_failed", note="停旧 trader 失败(best-effort，不影响新建连)")

    # ------------------------------------------------------------------
    # 断线兜底
    # ------------------------------------------------------------------
    def on_disconnected(self) -> None:
        """xttrader 主动断线回调（§2.2）：先置未就绪并告警，再触发盘中兜底重建。

        QMT 不自动重连，本回调是盘中兜底重连入口（与任务计划盘前重建叠加）。
        断线后须立刻 ready=False，确保在重连成功前盘中不发起任何新开仓委托（§2.6）；
        并立即通知下单通道冻结（评审三轮 EXEC-sched-02：解冻权威源绑定连接就绪，断线即冻结）。
        """
        self._set_ready(False)
        self._logger.warn(
            "disconnected",
            session_id=self._current_session_id,
            at_east8=str(east8_now_from_utc(self._clock.now_utc())),
        )
        self.reconnect()

    def reconnect(self) -> bool:
        """断线重建：换新 session_id 重走全序，成功后触发一次当日补采（§2.2）。

        口径：
        - 通过 connect_and_subscribe() 复用启动全序——其中已现取新 session_id；
          新 session_id 必须 != 旧值（旧 session 不复用，否则订阅可能失效无回报）。
        - session 复用 fail-closed（评审三轮 EXEC-sched-08）：若 provider 误配吐相同 session，
          置未就绪并向 provider 退避重取至不同值（最多 max_session_retry 次）；超限仍复用→拒绝重连
          （返回 False、保持未就绪、强告警），绝不带复用 session 进入就绪态/触发补采。
        - 重连成功（ready=True 且 session 确为新值）后**恰调用一次** on_reconnect_backfill()；
          重连失败不补采（连接都没就绪，补采也取不到权威数据）。
        返回是否重连就绪。

        重连互斥（评审 doc/19 H-2）：非阻塞抢 _conn_lock——抢不到即说明已有建连/重连在进行，立即跳过返回
        False（不并发再起一路重连）；内部 connect_and_subscribe 靠 RLock 同线程重入放行，故 session 复用退避 +
        建连全序在同一把锁内串行，断线回调线程 / 主循环 supervise / 调度 PREWARM 三路触发也只有一路真正执行。
        """
        # 非阻塞抢互斥锁：抢不到 = 已有建连/重连在进行 → 跳过本次并发重连（H-2，防多 trader/后台线程泄漏）。
        if not self._conn_lock.acquire(blocking=False):
            self._logger.warn(
                "reconnect_skip_in_progress",
                note="已有建连/重连在进行，跳过本次并发重连（H-2 重连互斥）",
            )
            return False
        try:
            prev_session_id = self._current_session_id

            # session 复用退避（评审三轮 EXEC-sched-08 / P1-2）：先【廉价地】从 provider 取一个与旧值不同的
            # session_id（不重建 trader），最多 max_session_retry 次；拿不到不同值即 fail-closed 拒绝重连。
            new_session_id = self._session_id_provider()
            retries = 0
            while new_session_id == prev_session_id and retries < self._max_session_retry:
                self._logger.error(
                    "reconnect_session_id_reused_retry",
                    prev_session_id=prev_session_id, new_session_id=new_session_id, retry=retries + 1,
                )
                self._set_ready(False)  # 复用即未就绪，绝不进就绪态
                retries += 1
                new_session_id = self._session_id_provider()

            if new_session_id == prev_session_id:
                # 超上限仍复用 → 拒绝本次重连（保持未就绪 + 强告警），绝不带复用 session 进就绪态/补采。
                self._set_ready(False)
                self._logger.error(
                    "reconnect_aborted_session_reuse",
                    prev_session_id=prev_session_id, new_session_id=new_session_id,
                    note="provider 持续吐相同 session，旧 session 订阅可能失效无回报，拒绝带复用 session 进就绪态",
                )
                return False

            # 拿到不同的新 session_id → 用其【只建一次】trader 走全序（避免退避期重复重建+线程泄漏）。
            ok = self.connect_and_subscribe(session_id=new_session_id)

            if ok:
                # 重连就绪（且 session 确为新值）：触发一次当日补采入口（写表细节归回流模块，本处只负责触发串联）。
                self._logger.info(
                    "reconnected",
                    new_session_id=self._current_session_id,
                    prev_session_id=prev_session_id,
                )
                self._on_reconnect_backfill()
            return ok
        finally:
            self._conn_lock.release()

    # ------------------------------------------------------------------
    # 常驻入口
    # ------------------------------------------------------------------
    def run(self) -> None:
        """进程常驻入口（§2.2）：connect_and_subscribe 成功后 run_forever 阻塞常驻。

        connect/subscribe 未就绪则不进入 run_forever（由调用方按退避重试 / 告警处理），
        避免在未连上时空转阻塞。单测不调用本方法（run_forever 是阻塞调用）。
        """
        if not self.connect_and_subscribe():
            # 未就绪不常驻：交由上层（任务计划 / 守护）退避后重新拉起。
            self._logger.warn("run_aborted_not_ready", session_id=self._current_session_id)
            return
        # 就绪后阻塞常驻，进程不退出；盘中推送持续进回调。
        assert self._current_trader is not None  # connect_and_subscribe 成功必已置 trader
        self._current_trader.run_forever()
