"""连接守护：xttrader 连接生命周期管理（§2.2 时序）。

业务意图：把 QMT XtQuantTrader 的「构造 → 注册回调 → start → connect(判返 0) →
subscribe → run_forever 常驻」固定时序与「断线重建（换新 session_id）」收口到单点，
避免下单链路各处自行管理连接状态导致口径漂移。

关键口径（与设计文档 §2.2 / §2.6 / §2.8 一致）：
- 就绪判定**只读 ``connect()`` 返回值**（0 即连上），QMT 无 ``on_connected`` 回调；本模块
  代码中不得引用 on_connected，防止误把就绪态绑定到不存在的回调上。
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

from typing import Any, Callable, Optional

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

        # 运行期状态：就绪标记 + 当前 session_id + 当前 trader 句柄。
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

    # ------------------------------------------------------------------
    # 连接 + 订阅（核心时序）
    # ------------------------------------------------------------------
    def connect_and_subscribe(self) -> bool:
        """执行固定启动时序并返回是否就绪（§2.2）。

        严格顺序：session_id = provider() → trader = factory(session_id) →
        register_callback(callback) → start() → rc = connect()。
        - rc != 0：log.warn + ready=False + 返回 False，**不调 subscribe / 不进就绪态 /
          不调 run_forever**（由 run() 决定是否常驻，本方法只判就绪）。
        - sub_rc = subscribe(account)，sub_rc != 0：ready=False + 返回 False。
        - 两步均 0：ready=True + 返回 True。
        无论成败都保存 current_session_id / current_trader，便于断线重建对比新旧 session。
        """
        # 1) 现取新 session_id 并按其造 trader（重连复用本方法，保证每次都是新 session）。
        session_id = self._session_id_provider()
        trader = self._trader_factory(session_id)
        self._current_session_id = session_id
        self._current_trader = trader

        # 2) 固定时序：注册回调 → 启动交易线程 → connect。顺序不可调换。
        trader.register_callback(self._callback)
        trader.start()
        rc = trader.connect()

        # 3) connect 判返 0：非 0 即连接失败，直接收口为未就绪，绝不继续 subscribe。
        if rc != 0:
            self._ready = False
            self._logger.warn(
                "connect_failed",
                session_id=session_id,
                rc=rc,
                # 断线/连接事件时间统一按东八区展示（盘口口径），落库时刻另由各写端走 time_utils。
                at_east8=str(east8_now_from_utc(self._clock.now_utc())),
            )
            return False

        # 4) 订阅账户推送：sub_rc != 0 视为未就绪（订阅失败盘中收不到回报，不能进开新仓态）。
        sub_rc = trader.subscribe(self._account)
        if sub_rc != 0:
            self._ready = False
            self._logger.warn(
                "subscribe_failed",
                session_id=session_id,
                sub_rc=sub_rc,
                at_east8=str(east8_now_from_utc(self._clock.now_utc())),
            )
            return False

        # 5) connect + subscribe 均成功 → 就绪。盘中推送开始进回调。
        self._ready = True
        self._logger.info("connect_ready", session_id=session_id)
        return True

    # ------------------------------------------------------------------
    # 断线兜底
    # ------------------------------------------------------------------
    def on_disconnected(self) -> None:
        """xttrader 主动断线回调（§2.2）：先置未就绪并告警，再触发盘中兜底重建。

        QMT 不自动重连，本回调是盘中兜底重连入口（与任务计划盘前重建叠加）。
        断线后须立刻 ready=False，确保在重连成功前盘中不发起任何新开仓委托（§2.6）。
        """
        self._ready = False
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
          这里额外断言新 session_id 必须 != 旧值（旧 session 不复用，否则订阅可能失效无回报）。
        - 重连成功（ready=True）后**恰调用一次** on_reconnect_backfill()，串联当日缺口补采；
          重连失败不补采（连接都没就绪，补采也取不到权威数据）。
        返回是否重连就绪。
        """
        prev_session_id = self._current_session_id

        ok = self.connect_and_subscribe()

        # 防御性校验：重连后的 session_id 不得与断开前相同（设计硬口径）。
        # provider 由外部保证每次新值；这里做一道断言式日志，便于落地排查 provider 误配。
        if self._current_session_id == prev_session_id:
            self._logger.error(
                "reconnect_session_id_reused",
                prev_session_id=prev_session_id,
                new_session_id=self._current_session_id,
            )

        if ok:
            # 重连就绪：触发一次当日补采入口（写表细节归回流模块，本处只负责触发串联）。
            self._logger.info(
                "reconnected",
                new_session_id=self._current_session_id,
                prev_session_id=prev_session_id,
            )
            self._on_reconnect_backfill()
        return ok

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
