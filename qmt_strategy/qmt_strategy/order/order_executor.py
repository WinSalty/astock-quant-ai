"""下单执行引擎（§4.4 / §4.5 / §4.7）。

业务意图：``OrderExecutor`` 是整条链路中【唯一】调用 ``trader.order_stock`` /
``trader.cancel_order_stock`` 的地方（§4.4 总则）。它承接 ``entry_router`` 产出的
``EntryDecision(BUY)``，负责：
  (1) 生成业务唯一单号 biz_order_no（去重键 + 透传 order_remark，§4.4(1)）；
  (2) 下单前查本地台账做业务级幂等防重（§4.4(2)）；
  (3) 限价排队 + 最长存活时限 TTL 撤单，且 9:20–9:25 禁撤（§4.4(3)/§4.5）；
  (4) 只认 xttrader 成交回报才算建仓——本类下单后状态最多到 SUBMITTED，
      成交由外部 callbacks 调 ledger.add_fill 推进，本类绝不自判成交成功（§4.4(4)）；
  (5) order_remark 携带「信号日 T + ts_code」，供回流回填 signal_trade_date（§4.4(5)）；
  (6) 账户隔离——本类持有自己的 account_id，不同账户用不同 executor/ledger 实例（§4.4(6)）；
  (7) 一字 / 秒封「买不进」：超时未成撤单 → 标 miss_reason → 转次优 / 放弃（§4.4(7)）。

强约束：
  - 绝不 import xtquant，只依赖 contracts 的 XtTraderLike Protocol；
  - 价位一律 Decimal，下单调用 trader 时才转 float（QMT 接口要 float）；
  - 时间一律经 clock.now_utc()（UTC naive）+ common.time_utils / auction_window，禁手工 ±8h；
  - TTL 截止时刻在本类内存维护（_ttl_deadline），不污染锁定的台账契约。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Dict, Optional, Tuple

from ..common import buy_prefilter
from ..common.auction_window import is_cancel_forbidden, is_lunch_break
from ..common.time_utils import east8_now_from_utc
from ..config.settings import Settings
from ..contracts.enums import EntryAction, OrderPhase, OrderState, OrderStatus, TradeSide
from ..contracts.models import EntryDecision, LedgerEntry
from ..contracts.protocols import Clock, LocalLedger, StructLogger, XtTraderLike

# QMT order_stock 入参整型常量。
# 业务说明：真实 xtconstant 取值须在实盘 Windows miniQMT 用 vars(xtconstant) 实测确认（§6.0），
# 此处按设计文档建议先用占位值（STOCK_BUY=23 为常见取值，FIX_PRICE 限价=11 为常见取值），
# 落地前以实测为准；本类只通过这两个常量与 trader 交互，改动只需调这里。
XT_ORDER_TYPE_BUY = 23          # 买入方向（占位，实测以 xtconstant.STOCK_BUY 为准）
XT_ORDER_TYPE_SELL = 24         # 卖出方向（占位，实测以 xtconstant.STOCK_SELL 为准）
XT_PRICE_TYPE_FIX = 11          # 限价委托（占位，实测以 xtconstant.FIX_PRICE 为准）

# 一字 / 秒封场景下的「整手」单位：A 股最小买入单位 100 股。
_BOARD_LOT = 100

# order_remark 长度上限（对齐 qmt_order.order_remark VARCHAR(255)，§4.4(5)）。
_REMARK_MAX_LEN = 255


class OrderExecutor:
    """下单执行引擎。一个实例绑定一个 account_id + 一份 ledger（账户物理隔离，§4.4(6)）。"""

    def __init__(
        self,
        trader: XtTraderLike,
        account: Any,
        account_id: str,
        ledger: LocalLedger,
        settings: Settings,
        clock: Clock,
        logger: StructLogger,
        decision_emitter: Optional[Any] = None,
        conn_health_sink: Optional[Callable[[bool], None]] = None,
    ) -> None:
        # 依赖注入：trader 是唯一下单/撤单出口；account 为下单/查询入参对象（StockAccount）。
        self._trader = trader
        # 下单通道健康回馈（评审三轮 EXEC-risk-05）：order_stock 同步失败/异常 → sink(False) 冻结下单闸；
        # 成功 → sink(True)。缺省 None（离线/单测兼容，无 sink 即旧行为）。Engine 装配时注入 report_trade_conn。
        self._conn_health_sink = conn_health_sink
        self._account = account
        self._account_id = account_id
        self._ledger = ledger
        self._settings = settings
        self._clock = clock
        self._logger = logger
        # 决策采集器（可选）：注入则在下单/未成/卖出/拦截各点 best-effort 采集；与下单热路径物理隔离。
        self._decision_emitter = decision_emitter
        # biz_order_no 序号自增器：键 = (target_trade_date, ts_code, strategy_family)，
        # 保证同一计划维度下的序号单调递增（§4.4(1) 序号段）。
        self._seq_counter: Dict[Tuple[Any, str, str], int] = {}
        # TTL 截止时刻表：biz_order_no → UTC naive 截止时刻。
        # 业务意图：每单设最长存活时限，超时未成由 on_ttl_expired 撤单（§4.4(3)）。
        # 放在内存而非台账，避免改动锁定的 LedgerEntry 契约。
        self._ttl_deadline: Dict[str, datetime] = {}
        # 决策因子快照表：biz_order_no → decision.factors_snapshot 与 next_best。
        # 业务意图：on_ttl_expired 只收到 biz_no，需据此回查原决策因子判定「买不进」归因 + 转次优；
        # 同样放内存，避免改动锁定的台账契约。
        self._decision_by_biz: Dict[str, EntryDecision] = {}
        # 单日下单次数计数（评审 P0-B3）：key=target_trade_date → 当日已真实发出的下单次数。
        # 业务意图：信号异常放量 / 重复推送 / 转次优链失控时，限制单日开仓次数，防止超频下单耗尽资金。
        # 只计真实 order_stock 发出（含同步失败，二者都占「下单次数」配额），幂等命中不计。
        self._orders_count_by_date: Dict[Any, int] = {}

    def _sync_persist_before_order(self) -> None:
        """发单后同步落盘 order_id 等关键台账行（评审 P0-C3）。

        write-behind 下 update 仅入队即返回；用于【发单成功后】把 SUBMITTED+order_id 行落盘，堵"券商有单、
        台账只认 PLANNED（无 order_id）"的孤儿窗口。此处落盘失败不可再放弃下单（委托已发），仅强告警。
        内存台账（无写队列/已即时持久）无 flush_pending → no-op。
        """
        flush = getattr(self._ledger, "flush_pending", None)
        if callable(flush):
            ok = flush()
            if ok is False:
                # 落盘超时：委托已发，不可回退，仅强告警——崩溃窗口未被完全堵住，需排查写线程。
                self._logger.warn("order_ledger_flush_timeout_after_order", account_id=self._account_id)

    def _persist_critical_before_order(self, *, kind: str, biz_no: str) -> bool:
        """发单【前】关键落盘 fail-closed（评审三轮 EXEC-storage-01）。

        业务意图：原实现 flush_pending 返回 False 一律只 warn 后继续 order_stock——在"磁盘满/写线程死/
        commit 失败"叠加崩溃重启时，券商已收委托但本机无 PLANNED 行 → 重复下单（P0-C3 要堵的窗口本身）。
        这里区分两类 False：
        - 写线程死亡/commit 失败（is_healthy()==False，确定性写丢失）→ 返回 False，调用方在 order_stock
          之前 fail-closed 放弃下单（写队列内部已自带 on_failure→engine.on_storage_failure 停开新仓告警）；
        - 纯超时（线程健康/无 is_healthy）→ 返回 True，保留"继续下单 + 强告警"（不漏信号，队列拥堵非确定故障）。
        内存台账（无 flush_pending）→ no-op 返回 True（无 write-behind 崩溃窗口）。
        """
        flush = getattr(self._ledger, "flush_pending", None)
        if not callable(flush):
            return True
        if flush() is not False:
            return True
        # flush 返回 False：用 is_healthy 区分确定性故障 vs 纯超时。
        health = getattr(self._ledger, "is_healthy", None)
        if callable(health) and health() is False:
            self._logger.error(
                "order_ledger_persist_failed_fail_closed",
                kind=kind, biz_order_no=biz_no, account_id=self._account_id,
            )
            return False  # 写线程死/commit 失败 → 拒发单（堵重复下单窗口）
        # 纯超时（线程仍健康/无 is_healthy）：继续下单但强告警。
        self._logger.warn(
            "order_ledger_flush_timeout_before_order",
            kind=kind, biz_order_no=biz_no, account_id=self._account_id,
        )
        return True

    def _report_conn(self, ok: bool) -> None:
        """向下单通道健康源回馈（评审三轮 EXEC-risk-05）；全程吞异常，绝不影响下单热路径。"""
        if self._conn_health_sink is None:
            return
        try:
            self._conn_health_sink(ok)
        except Exception:  # noqa: BLE001 健康回馈绝不影响下单
            pass

    def _safe_cancel(self, order_id: int, biz_no: str) -> bool:
        """撤单容错（国金对接核对）：吞 cancel_order_stock 异常并留痕，返回是否受理成功。

        业务意图：cancel_order_stock 对【已成交/已撤/不存在的 order_id】在部分券商/版本可能【抛异常】
        （待 §A9 真机实测确认国金行为）。on_ttl_expired 在 sweep_expired 的循环里逐单撤单，若撤单异常
        直接冒泡，会打断【整轮】sweep → 同批其它到期单当轮漏撤、TTL 兜底链中断（资金/名额被占）。
        这里把单次撤单异常隔离为「留痕 + 返回 False」，由调用方续二次截止下轮重试，绝不拖垮整轮巡检。
        """
        try:
            self._trader.cancel_order_stock(self._account, order_id)
            return True
        except Exception as exc:  # noqa: BLE001 撤单异常不得打断整轮 sweep
            self._logger.error(
                "order_cancel_failed",
                biz_order_no=biz_no,
                account_id=self._account_id,
                order_id=order_id,
                error=repr(exc),
            )
            return False

    # ------------------------------------------------------------------
    # 重启幂等：从台账重建 biz 序号计数器（评审 P0-C4）
    # ------------------------------------------------------------------
    def rebuild_seq_counter(self) -> None:
        """进程重启后按已重建的台账重置 biz_order_no 序号计数器。

        背景：load_from_db 重建了台账，但本类 _seq_counter 仍从 0 起算。若某 (target,ts_code,family)
        当日唯一一笔是终态失败（CANCELLED/REJECTED/ERROR，不在 active 集），重启后该计划被再次推送时
        find_active 返回 None → 继续 place，build_biz_order_no 又产出 _001 与磁盘失败单同号 →
        INSERT OR REPLACE 覆盖原失败留痕、甚至重复下单。这里按台账已存在的最大 seq 重建计数器，
        保证新单序号严格大于历史，杜绝同号覆盖。
        口径：seq 取 biz_order_no 最后一个 `_` 段（格式 {date}_{ts_code}_{family}_{seq:03d}，
        family 可能含下划线如 EntryAction.XXX，故用 rsplit 取末段最稳）。
        """
        counter: Dict[Tuple[Any, str, str], int] = {}
        for e in self._ledger.all():
            try:
                seq = int(e.biz_order_no.rsplit("_", 1)[1])
            except (ValueError, IndexError, AttributeError):
                continue  # 非本类格式的单号（手工/历史脏数据）跳过，不影响计数
            key = (e.target_trade_date, e.ts_code, e.strategy_family)
            if seq > counter.get(key, 0):
                counter[key] = seq
        self._seq_counter = counter
        self._logger.info("order_seq_counter_rebuilt", keys=len(counter))

    def rebuild_runtime_state(self, now: Optional[datetime] = None) -> None:
        """进程重启后从已重建的台账重建内存运行态：TTL 截止表 + 单日下单次数计数器
        （评审二轮 P1#28 / P2#32 / P2#40 / P2#69）。

        背景：_ttl_deadline / _orders_count_by_date 是纯内存，重启清零：
        - #28：崩溃前 SUBMITTED/REPORTED/PART_TRADED 的在途买单重启后无 TTL 截止 → 永不再被 sweep 撤单，
          资金/名额永久占用。这里给在途单重建一个"自重启起 order_ttl_seconds"的截止（无法恢复原始截止，
          取保守新 TTL：重启后给一段宽限再撤，避免刚重启就撤掉可能马上成交的单）。
        - #32/#40/#69：单日下单次数计数器重启清零 → max_orders_per_day 硬闸被绕过、可超频下单。
          这里按台账"已真实发出 order_stock"（state 越过 PLANNED）的条数，按 target_trade_date 重建计数。

        注：_decision_by_biz（原决策因子/next_best）无法重建（决策未持久化）——重启后到期单的 miss 归因
        退化为"排队未成"、转次优链不可用（next_best 本就为死代码，见 #68），不影响"超时必被撤"这一资金安全主目标。
        """
        now = now if now is not None else self._clock.now_utc()
        ttl = timedelta(seconds=self._settings.order_ttl_seconds)
        # CANCELLING 纳入重建（评审三轮 EXEC-order-08）：重启后卡在 CANCELLING 的单也须重建 TTL 截止，
        # 否则撤单回执丢失 + 重启 = 永远不再被 sweep 盯防、永久占名额占预算。
        in_flight = (
            OrderState.SUBMITTED, OrderState.REPORTED, OrderState.PART_TRADED, OrderState.CANCELLING,
        )
        ttl_rebuilt = 0
        counter: Dict[Any, int] = {}
        for e in self._ledger.all():
            # 计数器：凡"已越过 PLANNED"（真实发出过 order_stock，含同步失败 ERROR）的单都占当日下单次数配额。
            if e.state != OrderState.PLANNED:
                counter[e.target_trade_date] = counter.get(e.target_trade_date, 0) + 1
            # TTL：仅对在途且有 order_id 的单重建截止（PLANNED 无 order_id 无从撤；终态无需 TTL）。
            if e.state in in_flight and e.order_id is not None and e.biz_order_no not in self._ttl_deadline:
                self._ttl_deadline[e.biz_order_no] = now + ttl
                ttl_rebuilt += 1
        self._orders_count_by_date = counter
        self._logger.info(
            "order_runtime_state_rebuilt",
            ttl_rebuilt=ttl_rebuilt,
            order_count_days=len(counter),
        )

    # ------------------------------------------------------------------
    # (1) 业务唯一单号
    # ------------------------------------------------------------------
    def build_biz_order_no(self, decision: EntryDecision) -> str:
        """生成业务唯一单号：``{target:%Y%m%d}_{ts_code}_{family or action}_{seq:03d}``（§4.4(1)）。

        业务意图：日期 + 标的 + 战法 + 序号，既是幂等去重的可读键，也透传到 order_remark 供对账。
        边界：strategy_family 缺省时退化用 action 值（保证段不为空）；序号按
        (target_trade_date, ts_code, family) 维度自增，跨维度互不影响（同股不同战法各自计数）。
        """
        family = decision.strategy_family or str(decision.action)
        key = (decision.target_trade_date, decision.ts_code, family)
        # 维度内自增：首次取 1，之后逐次 +1。
        seq = self._seq_counter.get(key, 0) + 1
        self._seq_counter[key] = seq
        date_part = decision.target_trade_date.strftime("%Y%m%d")
        return f"{date_part}_{decision.ts_code}_{family}_{seq:03d}"

    # ------------------------------------------------------------------
    # (5) order_remark
    # ------------------------------------------------------------------
    def build_order_remark(self, decision: EntryDecision) -> str:
        """构造 order_remark：``LUP|{signal_trade_date:%Y-%m-%d}|{ts_code}``（§4.4(5)）。

        业务意图：透传信号日 T，供回流侧据此回填 signal_trade_date，无需反推交易日历。
        边界：本地台账列 ≤255（_REMARK_MAX_LEN，对齐 qmt_order VARCHAR(255)）；超长时【优先保 LUP|<T> 段】，
        从 ts_code 尾部截断，保证回流解析关键的 signal_trade_date 不丢失（§4.4(5) 截断口径）。
        注（国金对接核对 §A10）：xtquant/券商侧 order_remark 官方建议 ≤20 字符，本格式
        `LUP|YYYY-MM-DD|600000.SH` 约 24 字符，可能被券商进一步截断；因头部 `LUP|<日期>|` 在前，即便券商
        硬截断 signal_trade_date 仍可解析。真机确认券商实际上限（§A10）后，可据此把 _REMARK_MAX_LEN 收紧。
        """
        head = f"LUP|{decision.signal_trade_date.strftime('%Y-%m-%d')}|"
        remark = f"{head}{decision.ts_code}"
        if len(remark) <= _REMARK_MAX_LEN:
            return remark
        # 超长：保住 head（LUP|<T>| 段），ts_code 尾部按剩余预算截断。
        budget = _REMARK_MAX_LEN - len(head)
        if budget <= 0:
            # 极端：head 自身已超限（理论上不会，T 段定长），直接硬截断保前缀。
            return head[:_REMARK_MAX_LEN]
        return head + decision.ts_code[:budget]

    # ------------------------------------------------------------------
    # place：核心下单流程（§4.5）
    # ------------------------------------------------------------------
    def _emit_decision(self, **fields: Any) -> None:
        """向决策采集器发一条下单/未成/拦截决策事件（全程吞异常，绝不影响下单热路径）。"""
        em = self._decision_emitter
        if em is None:
            return
        try:
            em.emit(**fields)
        except Exception:  # noqa: BLE001 决策采集绝不影响交易
            pass

    @staticmethod
    def _is_position_committed(entry: LedgerEntry) -> bool:
        """该买单是否占用「建仓只数」名额。

        口径：已有成交（filled_volume>0，含部成/全成）→ 占名额；零成交但仍在途（PLANNED/SUBMITTED/
        REPORTED/PART_TRADED/CANCELLING）→ 占名额（结果未定，保守占位防超买）；终态零成交
        （CANCELLED/REJECTED/ERROR 且未成交=一字/秒封买不进、拒单、错误）→ 没买进，释放名额。
        """
        if (entry.filled_volume or 0) > 0:
            return True
        return entry.state not in (OrderState.CANCELLED, OrderState.REJECTED, OrderState.ERROR)

    def place(self, decision: EntryDecision) -> Optional[str]:
        """承接一条建仓决策并下单，返回 biz_order_no；不下单时返回 None（§4.5）。

        分支与边界：
          - action==SKIP：落决策台账留痕、不下单，返回 None（§4.10 SKIP 也留痕）；
          - kill_switch==True：全局熔断，只采集不下单，log 后返回 None（§7.1.5）；
          - 幂等防重：同 (target_trade_date, ts_code, strategy_family) 已有活跃单 →
            直接返回既有 biz_order_no，不重复下单（§4.4(2)，防重复推/重启重放/断线补发）；
          - plan_volume：decision 给则用，否则按账户可用资金 + 仓位上限现算，向下取整到 100 股；
            算出 0 → 不下单 + log（资金不足）；
          - 下单成功后状态推进到 SUBMITTED；成交确认走外部回报（本类不自判 TRADED）。
        """
        # —— SKIP：仅落决策台账留痕，不触发任何下单 ——
        if decision.action == EntryAction.SKIP:
            self._logger.info(
                "order_skip_decision",
                ts_code=decision.ts_code,
                account_id=self._account_id,
                reason=decision.reason,
            )
            return None

        # —— 买入前置过滤层最终硬保证（doc/18 第 3 层，唯一下单点）：决策命中任一禁买硬规则
        #    （ST/退市整理 / 四板及以上 / 数据缺测）→ 一律拒发买单。所有买入（含 try_next_best 转次优）都汇聚到本方法，
        #    是「绝不买入」的最后一道、也是最强一道闸——即便 loader/entry_router 两层被绕过或漏判，这里仍绝不下单（仅留痕）。
        #    is_st 由 _build_decision 用统一口径算定锚到决策（权威），故 CandidateView 只传 is_st 不传 name 即可；
        #    四板及以上看 decision.board_level/tier；数据缺测看 decision.data_missing（doc/29 B2，_build_decision 从 plan 透传）。
        #    阈值取 settings.forbid_board_level_min（与前两层同一口径）。
        prefilter_verdict = buy_prefilter.evaluate(
            buy_prefilter.CandidateView(
                ts_code=decision.ts_code,
                is_st=getattr(decision, "is_st", None),
                board_level=getattr(decision, "board_level", None),
                tier=getattr(decision, "tier", None),
                data_missing=getattr(decision, "data_missing", False),
            ),
            high_board_min_level=self._settings.forbid_board_level_min,
        )
        if not prefilter_verdict.allowed:
            self._logger.error(
                "order_buy_forbidden_block",
                ts_code=decision.ts_code,
                account_id=self._account_id,
                rule_code=prefilter_verdict.rule_code,
                note=f"禁买硬规则拦截，拒发买单：{prefilter_verdict.reason}",
            )
            self._emit_decision(
                decision_type="SKIP_ORDER", decision_stage="ORDER", action="SKIP",
                ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                trade_date=decision.target_trade_date, strategy_family=decision.strategy_family,
                order_phase=decision.order_phase, reason=prefilter_verdict.reason,
                reason_code=f"{prefilter_verdict.rule_code}_forbidden", factors=decision.factors_snapshot,
            )
            return None

        # —— kill_switch 全局熔断：只采集不下单（§7.1.5）——
        if self._settings.kill_switch:
            self._logger.warn(
                "order_kill_switch_block",
                ts_code=decision.ts_code,
                account_id=self._account_id,
            )
            self._emit_decision(
                decision_type="SKIP_ORDER", decision_stage="ORDER", action="SKIP",
                ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                trade_date=decision.target_trade_date, strategy_family=decision.strategy_family,
                order_phase=decision.order_phase, reason="全局熔断 kill_switch，只采集不下单",
                reason_code="kill_switch", factors=decision.factors_snapshot,
            )
            return None

        family = decision.strategy_family or str(decision.action)

        # —— (2) 幂等防重：下单前查台账，已有活跃单则返回既有单号 ——
        # 注意：必须先查台账再 build_biz_order_no，避免无谓自增序号（同一计划重复推时序号漂移）。
        existing = self._ledger.find_active(decision.target_trade_date, decision.ts_code, family)
        if existing is not None:
            self._logger.info(
                "order_idempotent_hit",
                ts_code=decision.ts_code,
                account_id=self._account_id,
                biz_order_no=existing.biz_order_no,
            )
            return existing.biz_order_no

        # —— 单日下单次数上限（评审 P0-B3）：超限不再开新仓，只留痕 ——
        # 放在幂等命中之后：幂等命中（重复推/重启重放）返回既有单，不占配额；只有真正会新发的单才计数。
        max_orders = self._settings.max_orders_per_day
        if max_orders is not None:
            placed_today = self._orders_count_by_date.get(decision.target_trade_date, 0)
            if placed_today >= max_orders:
                self._logger.warn(
                    "order_max_orders_per_day_block",
                    ts_code=decision.ts_code,
                    account_id=self._account_id,
                    placed_today=placed_today,
                    limit=max_orders,
                )
                self._emit_decision(
                    decision_type="SKIP_ORDER", decision_stage="ORDER", action="SKIP",
                    ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                    trade_date=decision.target_trade_date, strategy_family=decision.strategy_family,
                    order_phase=decision.order_phase, reason=f"超单日下单上限({placed_today}/{max_orders})",
                    reason_code="max_orders", factors=decision.factors_snapshot,
                )
                return None

        # —— 单日建仓「只数」上限：最多 N 只不同标的被买入（区别于上面的下单「次数」上限）——
        # 放在幂等命中之后：同一标的重复推不新占名额。占名额口径：当日 BUY 台账中「在途/部成/已成」的
        # 不同标的数（终态零成交=没买进，不占名额）。从台账实时统计 → 重启后仍准（台账已 load_from_db 重建）。
        max_positions = self._settings.max_positions_per_day
        if max_positions is not None and max_positions > 0:
            committed_codes = {
                e.ts_code
                for e in self._ledger.all_for_date(decision.target_trade_date)
                if e.side == TradeSide.BUY and self._is_position_committed(e)
            }
            if decision.ts_code not in committed_codes and len(committed_codes) >= max_positions:
                self._logger.warn(
                    "order_max_positions_per_day_block",
                    ts_code=decision.ts_code,
                    account_id=self._account_id,
                    distinct=len(committed_codes),
                    limit=max_positions,
                )
                self._emit_decision(
                    decision_type="SKIP_ORDER", decision_stage="ORDER", action="SKIP",
                    ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                    trade_date=decision.target_trade_date, strategy_family=family,
                    order_phase=decision.order_phase,
                    reason=f"超单日建仓只数上限({len(committed_codes)}/{max_positions})",
                    reason_code="max_positions", factors=decision.factors_snapshot,
                )
                return None

        limit_price = decision.limit_price

        # —— 计划股数：优先用决策给定 ——
        # fail-closed（评审三轮 EXEC-entry-02）：真实 BUY 的仓位必须由 EntryRouter 的强度加权 position_sizer
        # 预填 plan_volume。plan_volume 缺失说明装配/限价异常（如 chase 降级 B 限价为 None 致 entry_router 跳过
        # sizer），此时【拒单留痕】而非静默回退到【不含强度份额、绕过 top-N 名额约束】的 _plan_volume——后者会
        # 悄悄丢掉强度份额、构成隐性失效路径。仅当显式开启 allow_plan_volume_fallback（离线/无 sizer 兼容场景）
        # 才走 _plan_volume 现算。
        plan_volume = decision.plan_volume
        if plan_volume is None:
            if not self._settings.allow_plan_volume_fallback:
                self._logger.error(
                    "order_missing_plan_volume_fail_closed",
                    ts_code=decision.ts_code,
                    account_id=self._account_id,
                )
                self._emit_decision(
                    decision_type="SKIP_ORDER", decision_stage="ORDER", action="SKIP",
                    ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                    trade_date=decision.target_trade_date, strategy_family=decision.strategy_family,
                    order_phase=decision.order_phase,
                    reason="BUY 计划股数缺失（sizer 未预填，疑装配/限价异常），fail-closed 拒单",
                    reason_code="missing_plan_volume", limit_price=limit_price,
                    factors=decision.factors_snapshot,
                )
                return None
            plan_volume = self._plan_volume(decision, limit_price)
        if not plan_volume or plan_volume <= 0:
            # 资金不足 / 算出 0 股：不下单并留痕，等价于该计划放弃（§4.4(6) 资金口径）。
            self._logger.warn(
                "order_zero_volume_skip",
                ts_code=decision.ts_code,
                account_id=self._account_id,
            )
            self._emit_decision(
                decision_type="SKIP_ORDER", decision_stage="ORDER", action="SKIP",
                ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                trade_date=decision.target_trade_date, strategy_family=decision.strategy_family,
                order_phase=decision.order_phase, reason="资金不足/算出 0 股，放弃", reason_code="zero_volume",
                limit_price=limit_price, factors=decision.factors_snapshot,
            )
            return None

        # —— (1) 生成业务单号 + (5) order_remark ——
        biz_no = self.build_biz_order_no(decision)
        remark = self.build_order_remark(decision)
        now = self._clock.now_utc()

        # —— 写本地台账（state=PLANNED）：先落盘再发单，保证可对账 ——
        # cancelable：9:20–9:25 段下单标记不可撤（§4.5 mark_non_cancelable）。
        entry = LedgerEntry(
            biz_order_no=biz_no,
            account_id=self._account_id,
            target_trade_date=decision.target_trade_date,
            ts_code=decision.ts_code,
            strategy_family=family,
            side=TradeSide.BUY,
            plan_volume=plan_volume,
            plan_price=limit_price,
            order_remark=remark,
            signal_trade_date=decision.signal_trade_date,
            state=OrderState.PLANNED,
            order_phase=decision.order_phase,
            cancelable=not is_cancel_forbidden(now),
            created_at=now,
            updated_at=now,
        )
        self._ledger.insert(entry)
        # 发单前关键落盘 fail-closed（评审三轮 EXEC-storage-01）：保证"磁盘有计划单"先于"券商收到委托"。
        # 写线程死/commit 失败（确定性写丢失）→ 不发委托、台账置 ERROR 留痕 + 留痕决策，堵崩溃后重复下单。
        if not self._persist_critical_before_order(kind="buy_planned", biz_no=biz_no):
            self._ledger.update(
                biz_no, state=OrderState.ERROR,
                error_msg="发单前关键落盘失败(写线程死/commit失败),fail-closed 放弃下单",
                updated_at=self._clock.now_utc(),
            )
            self._emit_decision(
                decision_type="SKIP_ORDER", decision_stage="ORDER", action="SKIP",
                ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                trade_date=decision.target_trade_date, strategy_family=family,
                order_phase=decision.order_phase,
                reason="发单前关键落盘失败(写线程死/commit失败),fail-closed 拒发单", reason_code="persist_fail_closed",
                biz_order_no=biz_no, limit_price=limit_price, plan_volume=plan_volume,
            )
            return None

        # —— 唯一下单点：限价单（不挂市价，避免滑点失控，§4.4(3)）——
        # price 转 float 仅在调用 trader 边界，Decimal 留作台账/计算口径。
        # 下单通道健康回馈（评审三轮 EXEC-risk-05）：order_stock 抛异常 → sink(False) 后原样上抛。
        try:
            order_id = self._trader.order_stock(
                self._account,
                decision.ts_code,
                XT_ORDER_TYPE_BUY,
                plan_volume,
                XT_PRICE_TYPE_FIX,
                float(limit_price) if limit_price is not None else 0.0,
                decision.strategy_family or "",
                remark,
            )
        except Exception:  # noqa: BLE001 下单通道异常先反馈不健康再上抛（不吞异常）
            self._report_conn(False)
            raise

        # 单日下单次数 +1（评审 P0-B3）：真实发出 order_stock 即占配额（含同步失败，下同），
        # 防止转次优链 / 重复推送导致超频下单；幂等命中不会走到这里，故不重复计。
        self._orders_count_by_date[decision.target_trade_date] = (
            self._orders_count_by_date.get(decision.target_trade_date, 0) + 1
        )

        # —— 同步下单失败（order_stock 返回 <0 / None）：标 ERROR，绝不置 SUBMITTED（评审 medium#9）。——
        # 业务意图：XtTraderLike.order_stock 约定 <0 为同步失败；若仍置 SUBMITTED，对账会把它误判为
        # 「漏单凭空消失」（missing_report），污染 §6.10 告警语义。这里落 ERROR 终态 + 留痕 + 转次优。
        if order_id is None or order_id < 0:
            self._report_conn(False)  # 同步下单失败 = 通道可能不可用，反馈不健康（连续失败由 heartbeat/gate 冻结）
            # 不把负哨兵 order_id 写入台账（评审 doc/21 B2）：order_stock 同步失败返回 <0 是【失败哨兵】、非真实
            # 委托号；若写入 order_id 字段会被 _index_order_id 注册进两级反查索引（该索引只跳过 None、不拦负值），
            # 污染索引（多笔失败在 (-1,today) 互相覆盖、留痕被串），并使任何携负 order_id 的回报经 _resolve_biz(-1)
            # 误改写该 ERROR 行。故保持 order_id=None（不传该字段），失败码留在 error_msg 供审计；_resolve_biz 永不命中负值。
            self._ledger.update(
                biz_no, state=OrderState.ERROR,
                error_msg=f"order_stock 同步下单失败(返回{order_id})", updated_at=self._clock.now_utc(),
            )
            self._logger.error(
                "order_submit_failed_sync",
                ts_code=decision.ts_code,
                account_id=self._account_id,
                biz_order_no=biz_no,
                order_id=order_id,
            )
            self._emit_decision(
                decision_type="SKIP_ORDER", decision_stage="ORDER", action="SKIP",
                ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
                trade_date=decision.target_trade_date, strategy_family=family,
                order_phase=decision.order_phase, reason="同步下单失败(order_stock 返回<0)",
                reason_code="submit_failed", order_id=order_id, biz_order_no=biz_no,
                limit_price=limit_price, plan_volume=plan_volume,
            )
            # 不静默：主标的同步下单失败也不放弃整批，尝试次优（§4.4(7)/§4.7）。
            self.try_next_best(decision)
            return biz_no

        # —— 回填 order_id + 推进到 SUBMITTED（已发 order_stock，待回报）——
        # 注意：到此为止状态最多 SUBMITTED；REPORTED/TRADED 由外部回报推进（§4.4(4)）。
        self._report_conn(True)  # 同步下单成功 = 通道可用，反馈健康（清零 heartbeat 失败计数）
        self._ledger.update(biz_no, order_id=order_id, state=OrderState.SUBMITTED, updated_at=self._clock.now_utc())
        # 发单成功后【同步落盘 order_id】（评审二轮 P1#7）：原实现 order_id 仅异步镜像，崩溃窗口会产生
        # "券商有单、台账只认 PLANNED（无 order_id）"的孤儿单——重启后无法按 order_id 关联回报/撤单，
        # 名额永占、可能漏卖。这里阻塞确认 SUBMITTED+order_id 行落盘后再返回（非热路径，可阻塞）。
        self._sync_persist_before_order()

        # —— TTL 截止：竞价单到 9:25 定盘；开盘单 now + order_ttl_seconds（§4.4(3)）——
        # 买入开盘单 extend_to_open=True（评审复审 P2-3）：开盘前下的买单 TTL 顺延至开盘起算；卖单不顺延。
        self._ttl_deadline[biz_no] = self._compute_ttl_deadline(
            now, decision.order_phase, extend_to_open=True
        )
        # 保留原决策句柄：供 on_ttl_expired 判定 miss_reason 与转次优（§4.4(7)）。
        self._decision_by_biz[biz_no] = decision

        self._logger.info(
            "order_submitted",
            ts_code=decision.ts_code,
            account_id=self._account_id,
            biz_order_no=biz_no,
            order_id=order_id,
            plan_volume=plan_volume,
        )
        # 决策采集：买入已提交（锚定 order_id/biz_order_no，闭环串联 qmt_order/qmt_trade）。
        self._emit_decision(
            decision_type="BUY_SUBMIT", decision_stage="ORDER", action=decision.action,
            ts_code=decision.ts_code, signal_trade_date=decision.signal_trade_date,
            trade_date=decision.target_trade_date, strategy_family=family,
            order_phase=decision.order_phase, reason=decision.reason, reason_code="order_submitted",
            limit_price=limit_price, plan_volume=plan_volume, order_id=order_id, biz_order_no=biz_no,
            factors=decision.factors_snapshot,
        )
        return biz_no

    # ------------------------------------------------------------------
    # 卖出唯一下单点（评审 P0-C1）
    # ------------------------------------------------------------------
    def _build_sell_biz_no(self, ts_code: str, target_trade_date: Any) -> str:
        """卖出业务单号：``{date}_{ts_code}_SELL_{seq:03d}``。

        与买入单号同构、family 段固定 SELL，便于回流/对账区分买卖。序号按 (date, ts_code, SELL) 维度
        自增——同票当日多次卖出（如先 REDUCE 后 CLEAR）各自计数、各落一条卖单台账。
        """
        key = (target_trade_date, ts_code, "SELL")
        seq = self._seq_counter.get(key, 0) + 1
        self._seq_counter[key] = seq
        return f"{target_trade_date.strftime('%Y%m%d')}_{ts_code}_SELL_{seq:03d}"

    def place_sell(
        self,
        ts_code: str,
        target_trade_date: Any,
        signal_trade_date: Any,
        sell_vol: int,
        price: Optional[Decimal],
        reason: str,
        *,
        order_phase: OrderPhase = OrderPhase.OPENING,
    ) -> Optional[str]:
        """卖出唯一下单点（评审 P0-C1）：生成 biz 单号 + 落本地台账 + 经唯一出口下卖单。

        背景：原实现卖出在编排层直连 trader.order_stock，绕过唯一下单点与台账——卖单无 biz_order_no、
        不落台账、不可对账、无 TTL。这里让卖出与买入共用 trader.order_stock 唯一出口与本地台账，使
        卖单同样有业务单号、可对账、留痕。
        幂等口径：卖出幂等以持仓状态机 SELLING 态为主闸（run_sell_pass 已跳过在途单元），本方法不再
        用 find_active 造台账级幂等——因 TRADED/PART_TRADED 仍属 active，台账级幂等会误挡同票后续
        合法卖出（先 REDUCE 后 CLEAR）。
        返回 biz_order_no；kill_switch 或卖量<=0 时返回 None（不下单）。成交累计由外部 add_fill 按
        order_id 推进本台账行（买卖共用）。
        """
        # —— kill_switch 全局熔断：只采集不下单（§7.1.5）——
        if self._settings.kill_switch:
            self._logger.warn("order_sell_kill_switch_block", ts_code=ts_code, account_id=self._account_id)
            self._emit_decision(
                decision_type="SKIP_ORDER", decision_stage="SELL", action="SKIP",
                ts_code=ts_code, signal_trade_date=signal_trade_date, trade_date=target_trade_date,
                strategy_family="SELL", order_phase=order_phase, reason=f"全局熔断,卖出未下单({reason})",
                reason_code="kill_switch", plan_volume=sell_vol, limit_price=price,
            )
            return None
        if sell_vol is None or sell_vol <= 0:
            return None

        biz_no = self._build_sell_biz_no(ts_code, target_trade_date)
        now = self._clock.now_utc()
        # SELL remark 为自由文本 reason，截到台账列上限 _REMARK_MAX_LEN(255)。注（§A10）：券商侧 order_remark
        # 官方建议 ≤20 字符，可能进一步截断；SELL remark 不参与回流关键解析(signal_trade_date 来自 BUY remark)，
        # 故截断仅影响留痕可读性、不丢关键数据。
        remark = f"SELL|{reason}"[:_REMARK_MAX_LEN]
        # 先落台账（state=PLANNED）再发单，保证可对账（与买入同口径）。
        entry = LedgerEntry(
            biz_order_no=biz_no,
            account_id=self._account_id,
            target_trade_date=target_trade_date,
            ts_code=ts_code,
            strategy_family="SELL",
            side=TradeSide.SELL,
            plan_volume=sell_vol,
            plan_price=price,
            order_remark=remark,
            signal_trade_date=signal_trade_date,
            state=OrderState.PLANNED,
            order_phase=order_phase,
            cancelable=not is_cancel_forbidden(now),
            created_at=now,
            updated_at=now,
        )
        self._ledger.insert(entry)
        # 发单前关键落盘 fail-closed（评审三轮 EXEC-storage-01）：卖单同样保证"磁盘有台账"先于"券商收到委托"。
        # 写线程死/commit 失败 → 不发卖单、台账置 ERROR 留痕（下一轮卖出巡检可重挂；不留孤儿卖单）。
        if not self._persist_critical_before_order(kind="sell_planned", biz_no=biz_no):
            self._ledger.update(
                biz_no, state=OrderState.ERROR,
                error_msg="发单前关键落盘失败(写线程死/commit失败),fail-closed 放弃卖单",
                updated_at=self._clock.now_utc(),
            )
            self._logger.error(
                "order_sell_persist_fail_closed",
                ts_code=ts_code, account_id=self._account_id, biz_order_no=biz_no,
            )
            return None

        # —— 唯一下单点：限价卖单 ——
        try:
            order_id = self._trader.order_stock(
                self._account,
                ts_code,
                XT_ORDER_TYPE_SELL,
                sell_vol,
                XT_PRICE_TYPE_FIX,
                float(price) if price is not None else 0.0,
                "SELL",
                remark,
            )
        except Exception:  # noqa: BLE001 下单通道异常先反馈不健康再上抛
            self._report_conn(False)
            raise

        if order_id is None or order_id < 0:
            self._report_conn(False)
            # 不把负哨兵 order_id 写入台账（评审 doc/21 B2，与买入同口径）：保持 order_id=None，避免污染反查索引。
            self._ledger.update(
                biz_no, state=OrderState.ERROR,
                error_msg=f"sell order_stock 同步下单失败(返回{order_id})", updated_at=self._clock.now_utc(),
            )
            self._logger.error(
                "order_sell_submit_failed_sync",
                ts_code=ts_code, account_id=self._account_id, biz_order_no=biz_no, order_id=order_id,
            )
            # 同步失败返回 None（评审二轮 P1#11）：原实现返回 biz_no，编排层据非 None 调 mark_selling 把单元
            # 置 SELLING，但根本没有活的卖单 → 单元永久卡死无法再卖。返回 None 让编排层不置 SELLING（台账已留
            # ERROR 痕迹供对账），下一轮卖出巡检可对该单元重新下卖单。
            return None

        self._report_conn(True)  # 卖单同步下单成功 = 通道可用
        # 单日下单次数 +1：只在【真正被券商受理(order_id 有效)】时计数（执行-1 修正 2026-06-22）。
        # 旧实现在 order_stock 返回后无论成败都先 +1，再判失败返回——一只持续卖不出的票(跌停/拒单)每轮卖出巡检
        # 都失败一次却仍吃掉一个配额，而买卖共用 QMT_MAX_ORDERS_PER_DAY，会把全天下单配额耗光、连累买入侧被
        # 「超单日上限」拦住买不进。同步失败不产生活单、不应占配额；对持续失败的刹车由连接健康承担：每次失败
        # 已 _report_conn(False)，连续失败达 QMT_TRADE_CONN_HEARTBEAT_FAIL_THRESHOLD 即 FREEZE 停发新单。
        self._orders_count_by_date[target_trade_date] = (
            self._orders_count_by_date.get(target_trade_date, 0) + 1
        )
        self._ledger.update(biz_no, order_id=order_id, state=OrderState.SUBMITTED, updated_at=self._clock.now_utc())
        # 卖单同样同步落盘 order_id（评审二轮 P1#7）：保证"券商有单"先于台账只认 PLANNED 的孤儿窗口被堵。
        self._sync_persist_before_order()
        # 卖单登记 TTL 截止（评审二轮 P1#12）：原实现卖单无 TTL/撤改，挂不到价即全天卡在 SELLING 漏卖。
        # 这里给卖单同样的存活时限，sweep_expired 到期会撤单（→ CANCELLED 回报触发持仓 revert_selling 重挂）。
        self._ttl_deadline[biz_no] = self._compute_ttl_deadline(now, order_phase)
        self._logger.info(
            "order_sell_submitted",
            ts_code=ts_code, account_id=self._account_id, biz_order_no=biz_no,
            order_id=order_id, sell_vol=sell_vol,
        )
        # 决策采集：卖出已提交（锚定 order_id，闭环串联成交事实）。
        self._emit_decision(
            decision_type="SELL_SUBMIT", decision_stage="SELL", action="SELL_SUBMIT",
            ts_code=ts_code, signal_trade_date=signal_trade_date, trade_date=target_trade_date,
            strategy_family="SELL", order_phase=order_phase, reason=reason, reason_code="sell_submitted",
            limit_price=price, plan_volume=sell_vol, order_id=order_id, biz_order_no=biz_no,
        )
        return biz_no

    # ------------------------------------------------------------------
    # 已承诺买入资金（在途 + 已成）—— 供资金分配/总敞口闸扣减（评审 2.3余/B3余）
    # ------------------------------------------------------------------
    def committed_amount(self, target_trade_date: Any) -> Decimal:
        """当日已承诺买入资金估算 = Σ 活跃买单 plan_volume×plan_price（读台账，权威且含重启重建）。

        业务意图：发单前从可用预算里扣减已承诺，避免券商 frozen_cash 未及时刷新时多只票各按"全额现金"
        测算而超额下单废单；并作为总敞口闸 max_total_exposure 的已用额度。
        口径：只计 BUY 方向且处于活跃态（OrderState.active()：PLANNED/SUBMITTED/REPORTED/PART_TRADED/
        TRADED/CANCELLING/PART_CANCELLED）的单；纯终态失败（CANCELLED/REJECTED/ERROR，零成交）不占用资金故不计。
        其中 PART_CANCELLED（部成撤单终态，评审 doc/21 B1）虽属 active 集合（保 find_active 幂等），但其未成
        remaining 已撤死、只计真实已成 filled 段（见下方 remaining 三元口径）。同一计划当日多单各自计入（与下单事实一致）。
        """
        active = OrderState.active()
        total = Decimal("0")
        for e in self._ledger.all_for_date(target_trade_date):
            if not (e.side == TradeSide.BUY and e.state in active and e.plan_price is not None and e.plan_volume):
                continue
            # 已承诺金额按"实际已成 + 剩余在途按计划价"计（评审二轮 P3#92）：
            # 原实现对部成/已成单仍按 plan_volume×plan_price 全额计入——已成部分应按真实成交均价、未成部分才按
            # 计划价占用，否则会高估已承诺、误判预算耗尽而少买。filled_volume 钳到 plan_volume 防回报超量污染。
            filled = min(e.filled_volume or 0, e.plan_volume)
            # 部成撤单单 remaining 已死、不占承诺（评审 doc/21 B1）：PART_CANCELLED 的未成部分已撤、冻结现金已释放，
            # 绝不再按 plan_price 计入已承诺（否则死量永久超额承诺、挤占其它龙头预算），只计真实已成 filled 段。
            remaining = 0 if e.state == OrderState.PART_CANCELLED else (e.plan_volume - filled)
            filled_amt = (e.avg_filled_price or e.plan_price) * Decimal(filled)
            remaining_amt = e.plan_price * Decimal(remaining)
            total += filled_amt + remaining_amt
        return total

    def _held_value(self, ts_code: str) -> Tuple[Decimal, bool]:
        """该 ts_code 现有持仓市值 + 查询是否成功（评审三轮 EXEC-risk-03 / 评审 doc/19 M-2）。

        业务意图：max_position_per_stock 原只当「单笔预算上限」、不计已有持仓，跨日对隔夜持有的同票再
        满额下单会累计突破单票上限（连板/打板龙头连续上榜正是本系统核心打法）。这里查 QMT 权威持仓取该票
        市值。口径：market_value 优先，缺则 volume×(avg_price/open_price) 兜底，再缺记 0。
        返回 (市值, held_known)（M-2）：
        - held_known=True → 查询成功，市值为权威值（含 0=确实无持仓）；
        - held_known=False → 持仓查询失败，市值【不可知】（返回的 0 仅占位）。
          原实现查询失败【静默返 0】，被单票上限净额校验当成「该票无持仓」→ 跨日复买同票时漏计隔夜持仓、
          单票敞口可累计逼近 2×cap。改为显式回传失败标志，由单票上限消费点 fail-closed 拒单（不臆造无持仓）。
        边界：仍不抛异常（不拖垮下单主链）；是否拒单由上层据 held_known 决定。
        """
        from ..common.identity import resolve_code

        target = resolve_code(ts_code)
        try:
            rows = self._trader.query_stock_positions(self._account)
        except Exception as exc:  # noqa: BLE001 持仓查询失败不臆造市值，回传 held_known=False 由上层 fail-closed
            self._logger.warn("order_held_value_query_failed", ts_code=ts_code, error=str(exc))
            return Decimal("0"), False
        total = Decimal("0")
        for r in rows or []:
            code = resolve_code(getattr(r, "stock_code", None) or getattr(r, "ts_code", None))
            if code is None or code != target:
                continue
            mv = getattr(r, "market_value", None)
            if mv is not None:
                total += Decimal(str(mv))
                continue
            vol = getattr(r, "volume", None) or 0
            px = getattr(r, "avg_price", None) or getattr(r, "open_price", None)
            if vol and px is not None:
                total += Decimal(str(vol)) * Decimal(str(px))
        return total, True

    def _committed_for_code(self, target_trade_date: Any, ts_code: str) -> Decimal:
        """当日对该 ts_code 已承诺的活跃买入额（评审三轮 EXEC-risk-03）：单票上限须含当日在途/已成承诺。"""
        from ..common.identity import resolve_code

        target = resolve_code(ts_code)
        active = OrderState.active()
        total = Decimal("0")
        for e in self._ledger.all_for_date(target_trade_date):
            if not (e.side == TradeSide.BUY and e.state in active and e.plan_price is not None and e.plan_volume):
                continue
            if resolve_code(e.ts_code) != target:
                continue
            filled = min(e.filled_volume or 0, e.plan_volume)
            # 部成撤单单 remaining 已死、不占单票承诺（评审 doc/21 B1）：同 committed_amount 口径。
            remaining = 0 if e.state == OrderState.PART_CANCELLED else (e.plan_volume - filled)
            total += (e.avg_filled_price or e.plan_price) * Decimal(filled) + e.plan_price * Decimal(remaining)
        return total

    def _committed_remaining_for_code(self, target_trade_date: Any, ts_code: str) -> Decimal:
        """当日对该 ts_code 活跃买单的【未成在途】计划额（评审 F03/F13）：仅计 remaining，**不含已成 filled**。

        业务意图：单票【敞口】口径 = 该票现有持仓市值(_held_value，券商权威，已含当日已成买入) + 当日未成在途买单
        计划额。原 _committed_for_code 同时计入 filled+remaining，与 _held_value 的 filled 部分【重复计入】
        （F13：已成买单被券商持仓与台账承诺双重扣减、单票后续加仓被系统性少买）。本方法只取未成 remaining 段，
        与 _held_value 拼成「持仓 + 未成在途」的单计敞口口径，供单票金额上限净额校验单计无重复。
        """
        from ..common.identity import resolve_code

        target = resolve_code(ts_code)
        active = OrderState.active()
        total = Decimal("0")
        for e in self._ledger.all_for_date(target_trade_date):
            if not (e.side == TradeSide.BUY and e.state in active and e.plan_price is not None and e.plan_volume):
                continue
            if resolve_code(e.ts_code) != target:
                continue
            filled = min(e.filled_volume or 0, e.plan_volume)
            # 部成撤单单未成段已死、不计在途敞口（评审 doc/21 B1）：PART_CANCELLED 的 remaining 已撤、非在途，置 0。
            remaining = 0 if e.state == OrderState.PART_CANCELLED else (e.plan_volume - filled)
            # 只计未成在途段（已成 filled 已体现在 _held_value 的持仓市值里，不再重复计入，堵 F13 双重扣减）。
            total += e.plan_price * Decimal(remaining)
        return total

    def exposure_for_code(self, target_trade_date: Any, ts_code: str) -> Decimal:
        """该 ts_code 的单票【敞口】单计口径（评审 F03/F13）：现有持仓市值 + 当日未成在途买单计划额。

        供资金分配主链（main._strength_budget_volume）与离线兜底（_plan_volume）共用，保证单票金额上限
        net 校验在两条路径口径一致、且对当日已成买入【单计不重复】（已成体现在持仓市值、未成体现在在途计划额）。
        best-effort 口径：持仓查询失败时持仓段按 0 计（held_known 在此被丢弃）；要据查询成败 fail-closed
        的单票上限消费点须改用 exposure_for_code_checked（评审 doc/19 M-2）。本方法保留供单测/最佳努力读数。
        """
        held, _held_known = self._held_value(ts_code)
        return held + self._committed_remaining_for_code(target_trade_date, ts_code)

    def exposure_for_code_checked(
        self, target_trade_date: Any, ts_code: str
    ) -> Tuple[Decimal, bool]:
        """单票敞口 + 持仓是否可知（评审 doc/19 M-2）：单票金额上限净额校验须用此版。

        返回 (敞口, held_known)：held_known=False 表示持仓查询失败、敞口不含可信的隔夜持仓段，
        消费点（_plan_volume / main._strength_budget_volume）应据此 fail-closed 拒单——绝不把「查询失败」
        当作「持仓为 0」而漏计隔夜持仓、致跨日同票超单票上限加仓。敞口口径与 exposure_for_code 完全一致
        （持仓市值 + 未成在途计划额，对当日已成单计不重复，堵 F13 双重扣减）。
        """
        held, held_known = self._held_value(ts_code)
        return held + self._committed_remaining_for_code(target_trade_date, ts_code), held_known

    # ------------------------------------------------------------------
    # 计划股数现算（§4.4(6) 资金口径）
    # ------------------------------------------------------------------
    def _plan_volume(self, decision: EntryDecision, limit_price: Optional[Decimal]) -> int:
        """按账户可用资金 + 仓位上限算计划股数，向下取整到 100 股整手。

        ⚠️ 不含强度份额（评审三轮 EXEC-entry-02）：本现算口径只按 cash/总敞口/单票单笔上限算，
        【不】乘 EntryRouter 强度加权 position_sizer 的 w，也不参与 top-N 名额优选。故【不得用于强度加权
        BUY 主链】——主链 plan_volume 缺失应 fail-closed 拒单（见 place）。本方法仅供离线/无 sizer 的兼容
        场景，且需 settings.allow_plan_volume_fallback=True 显式开启才会被 place 调用。

        业务意图：资金读 trader.query_stock_asset(account).cash（当日可用现金，§4.4(6)）；
        单票投入上限取 settings.per_order_max_amount / max_position_per_stock 的较小值（若配置）。
        边界：limit_price 缺失或 ≤0、可用预算 < 一手 → 返回 0（由 place 据此放弃下单）。
        """
        if limit_price is None or limit_price <= 0:
            return 0
        asset = self._trader.query_stock_asset(self._account)
        cash = getattr(asset, "cash", None)
        if cash is None:
            return 0
        budget = Decimal(str(cash))
        # 总敞口闸 + 在途扣减（评审 2.3余/B3余）：可用预算再受「总敞口上限 − 已承诺(在途+已成)」约束，
        # 避免券商 frozen_cash 未及时刷新时多单各看全额现金而超额下单废单。
        committed = self.committed_amount(decision.target_trade_date)
        if self._settings.max_total_exposure is not None:
            remaining_total = Decimal(str(self._settings.max_total_exposure)) - committed
            if remaining_total < budget:
                budget = remaining_total
        # 单笔金额上限：单笔预算硬上限（不减持仓，None 视为不限制）。
        if self._settings.per_order_max_amount is not None:
            cap_order = Decimal(str(self._settings.per_order_max_amount))
            if cap_order < budget:
                budget = cap_order
        # 单票金额上限（评审三轮 EXEC-risk-03 + F13 单计修正）：净额校验须含「该票现有持仓市值 + 当日未成在途
        # 该票计划额」，单计不重复——exposure_for_code = _held_value(已含当日已成买入) + 未成在途计划额；原实现用
        # _committed_for_code(filled+remaining) 与 _held_value 的 filled 段重复扣减(F13)，使同票后续加仓被系统性少买。
        # effective_cap = cap - exposure；<=0 则该票已达/超上限直接返 0 不再加仓（跨日同样生效）。
        if self._settings.max_position_per_stock is not None:
            cap_stock = Decimal(str(self._settings.max_position_per_stock))
            exposure, held_known = self.exposure_for_code_checked(
                decision.target_trade_date, decision.ts_code
            )
            # 持仓查询失败 fail-closed（评审 doc/19 M-2）：无法核验隔夜持仓时，绝不把「查询失败」当「无持仓」
            # 而满额加仓（会使单票敞口逼近 2×cap）；本轮该票拒单 + 强告警，待下轮重试或人工排查 QMT 持仓查询。
            if not held_known:
                self._logger.error(
                    "order_per_stock_cap_held_unknown_reject",
                    ts_code=decision.ts_code, account_id=self._account_id,
                    note="持仓查询失败无法核验单票敞口，单票上限净额校验 fail-closed 拒单(M-2)",
                )
                return 0
            effective_cap = cap_stock - exposure
            if effective_cap <= 0:
                self._logger.info(
                    "order_per_stock_cap_reached",
                    ts_code=decision.ts_code, account_id=self._account_id,
                    cap=str(cap_stock), held=str(exposure),
                )
                return 0
            if effective_cap < budget:
                budget = effective_cap
        if budget <= 0:
            return 0
        # 可买股数 = 预算 / 限价，向下取整到 100 股整手。
        raw_shares = (budget / limit_price).to_integral_value(rounding=ROUND_DOWN)
        lots = int(raw_shares) // _BOARD_LOT
        return lots * _BOARD_LOT

    def _compute_ttl_deadline(
        self, now: datetime, order_phase: OrderPhase, *, extend_to_open: bool = False
    ) -> datetime:
        """计算 TTL 截止时刻（UTC naive）。

        竞价单（AUCTION）：存活到 9:25 定盘（东八区），不用相对秒数。
        开盘单（OPENING）：now + settings.order_ttl_seconds。
        实现：竞价单先在东八区把当日 9:25 拼好，再回到「相对 now 的偏移」以保持 UTC naive 口径。

        extend_to_open（仅【买入】开盘单，评审二轮 P1#16/#17 + 复审 P2-3）：开盘前(<9:30)下的买入 OPENING 单
        TTL 从 9:30 开盘起算，保证存活到开盘成交而非开盘前(连续竞价未开)就过期被撤成废单。卖单不传此参数
        （extend_to_open=False）——卖单需独立 TTL 以便挂不到价尽快撤改重挂(#12)，绝不可顺延到开盘后才撤。
        """
        if order_phase == OrderPhase.AUCTION:
            east8_now = east8_now_from_utc(now)
            east8_925 = east8_now.replace(hour=9, minute=25, second=0, microsecond=0)
            offset = east8_925 - east8_now
            # 9:25 后下/重投的竞价单防秒撤（评审三轮 EXEC-order-04）：offset<=0（已过定盘点，如 9:25 后补发/
            # try_next_best 重投陈旧 AUCTION 候选/调度抖动）时不存在"到定盘撤"语义，退化为相对 TTL，绝不返回
            # <=now 的截止（否则单一提交即被同轮 sweep_expired 秒判到期撤成废单，系统性买不进最强标的）。
            if offset.total_seconds() <= 0:
                self._logger.warn(
                    "order_auction_ttl_after_0925_fallback_relative",
                    account_id=self._account_id,
                    note="9:25 后竞价单 TTL 退化为相对存活时限，杜绝提交即秒撤",
                )
                return now + timedelta(seconds=self._settings.order_ttl_seconds)
            # 截止偏移 = 东八区(9:25 - now)，加到 UTC naive 的 now 上（不手工 ±8h，仅取差值）。
            return now + offset
        base = now
        if extend_to_open:
            east8_now = east8_now_from_utc(now)
            east8_open = east8_now.replace(hour=9, minute=30, second=0, microsecond=0)
            if east8_now < east8_open:
                base = now + (east8_open - east8_now)  # 推到开盘时刻再加 TTL（仅买单）
        return base + timedelta(seconds=self._settings.order_ttl_seconds)

    # ------------------------------------------------------------------
    # TTL 到期处理（§4.5 ttl 到期 / §4.4(7)）
    # ------------------------------------------------------------------
    def sweep_expired(self, now: Optional[datetime] = None) -> list:
        """TTL 到期巡检驱动（§4.5 / 评审 low#3）：扫描已到期单并触发 on_ttl_expired。

        业务意图：place() 只记录每单 TTL 截止时刻，需有主循环周期性调用本方法才会真正触发
        「超时未成 → 撤单 → 转次优 / 放弃」。app 编排层应在盘中按秒级 / 分钟级周期调用本方法。
        边界：
        - 9:20–9:25 锁定段：on_ttl_expired 内部已守「不撤」；本方法对锁定段的到期单【不清理】截止表，
          等定盘后下一轮巡检再处理（避免锁定段丢失该单的 TTL 而永不撤）。
        - 处理过（非锁定段）的到期单从 _ttl_deadline 移除，避免重复触发。
        返回本轮已处理（已触发 on_ttl_expired）的 biz_order_no 列表。
        """
        now = now if now is not None else self._clock.now_utc()
        # 锁定段整体跳过：此段不撤单，保留所有 TTL 截止待定盘后处理。
        if is_cancel_forbidden(now):
            return []
        # 午休停牌整体跳过（评审 P1#11）：午休撮合停止，撤单无法成交，保留 TTL 待午后复牌再处理。
        if is_lunch_break(now):
            return []
        handled = []
        # 先收集到期 biz（避免遍历中修改字典）。
        due = [biz for biz, deadline in self._ttl_deadline.items() if now >= deadline]
        for biz in due:
            # 先移除当前截止再处理（评审三轮 EXEC-order-08）：让 on_ttl_expired 内部对 CANCELLING/卡死单
            # 续写的「二次截止」生效不被本轮 pop 反向覆盖——撤单回执丢失时该 CANCELLING 单仍会被下轮 sweep
            # 重新盯防（幂等重发 cancel），而非永久卡 CANCELLING 占名额占预算。
            self._ttl_deadline.pop(biz, None)
            self.on_ttl_expired(biz)
            handled.append(biz)
        return handled

    def on_ttl_expired(self, biz_no: str) -> None:
        """单存活超时处理：可撤段撤未成单，9:20–9:25 锁定段不撤等定盘（§4.5）。

        分支与边界：
          - 台账无该单 / 无 order_id：忽略（可能尚未发单或已清理）；
          - is_cancel_forbidden(now)（9:20–9:25）：【不撤】直接 return，等定盘后再处理（§4.4(3)）；
          - state ∈ {REPORTED, PART_TRADED}：发 cancel_order_stock → 置 CANCELLING；
          - 撤后若 filled_volume==0（一字 / 秒封未成）：标 miss_reason → 转次优或放弃（§4.4(7)）。
        """
        entry = self._ledger.get(biz_no)
        if entry is None or entry.order_id is None:
            return
        now = self._clock.now_utc()

        # —— 9:20–9:25 锁定段禁撤：撤单必废，等定盘后处理（§4.4(3)/§3.3）——
        if is_cancel_forbidden(now):
            self._logger.info(
                "order_ttl_in_locked_phase_skip_cancel",
                biz_order_no=biz_no,
                account_id=self._account_id,
            )
            return

        # —— 对「在途」状态发撤单：已报 / 部成 / 已提交（评审二轮 P1#8）——
        # 原实现只撤 REPORTED/PART_TRADED，把 SUBMITTED-到期单丢给 else 分支只留痕、不撤；而 sweep_expired
        # 仍把它移出 TTL 截止表 → 超时 SUBMITTED 单永不再被撤、资金/名额永久占用。这里把 SUBMITTED 纳入撤单：
        # 已发 order_stock 拿到 order_id 但久无 on_stock_order 回报本身即异常（卡单/丢回报），且 cancel 携 order_id
        # 对券商安全（无此单则券商自行拒绝），故按 TTL 一并撤单，避免永久占用。
        if entry.state in (OrderState.SUBMITTED, OrderState.REPORTED, OrderState.PART_TRADED):
            # 撤单容错（国金对接核对）：cancel 抛异常不打断整轮 sweep；失败则不进 CANCELLING、不做
            # miss/next_best，续二次截止下轮重试（订单仍在途，下轮再撤）。
            if not self._safe_cancel(entry.order_id, biz_no):
                grace = timedelta(seconds=getattr(self._settings, "cancel_grace_seconds", 30))
                self._ttl_deadline[biz_no] = now + grace
                return
            self._ledger.update(biz_no, state=OrderState.CANCELLING, updated_at=now)
            self._logger.info(
                "order_ttl_cancel_sent",
                biz_order_no=biz_no,
                account_id=self._account_id,
                order_id=entry.order_id,
                filled_volume=entry.filled_volume,
                from_state=str(entry.state),
            )
            # —— 撤后若一手未成（filled==0）：一字 / 秒封「买不进」归因 + 转次优（§4.4(7)）——
            # 仅买单适用（评审二轮 P1#12）：卖单到期撤单后无"买不进/转次优"语义，撤单回报会触发持仓 revert_selling
            # 让下一轮重挂，故卖单不走 miss_reason/try_next_best 分支。
            if entry.side == TradeSide.BUY and entry.filled_volume == 0:
                miss_reason = self._infer_miss_reason(biz_no)
                self._ledger.update(biz_no, miss_reason=miss_reason, updated_at=now)
                self._logger.warn(
                    "order_miss_unfilled",
                    biz_order_no=biz_no,
                    account_id=self._account_id,
                    miss_reason=miss_reason,
                )
                # 决策采集：买不进（一字/秒封未成撤单），「为什么没买进」的事实源。
                self._emit_decision(
                    decision_type="BUY_MISS", decision_stage="ORDER", action="SKIP",
                    ts_code=entry.ts_code, signal_trade_date=entry.signal_trade_date,
                    trade_date=entry.target_trade_date, strategy_family=entry.strategy_family,
                    order_phase=entry.order_phase, reason=f"未成交撤单({miss_reason})",
                    reason_code="miss_unfilled", order_id=entry.order_id, biz_order_no=biz_no,
                    plan_volume=entry.plan_volume, limit_price=entry.plan_price,
                )
                # 取原决策尝试次优（主标的买不进不放弃整批，§4.4(7)）。
                decision = self._decision_by_biz.get(biz_no)
                if decision is not None:
                    self.try_next_best(decision)
            # 续二次截止（评审三轮 EXEC-order-08 + 评审复核 P0）：本单首次撤单进入 CANCELLING，sweep_expired
            # 已先 pop 掉它的 deadline；若不在此续写，撤单回执丢失时该 CANCELLING 单下轮永不再被 sweep 触达、
            # grace 重发 cancel 永不触发 = 永久卡名额/预算。故首次进 CANCELLING 即续 now+grace，与下面 CANCELLING
            # 分支同口径，使其下一轮仍被盯防；CANCELLED 回报到达后单元进终态，再下一轮 sweep 走 else 分支自然清理。
            grace = timedelta(seconds=getattr(self._settings, "cancel_grace_seconds", 30))
            self._ttl_deadline[biz_no] = now + grace
        elif entry.state == OrderState.CANCELLING:
            # 撤单回执丢失兜底（评审三轮 EXEC-order-08）：已发撤单待回执的单若 CANCELLED 回报因断线/丢包
            # 永不到达，会永久卡 CANCELLING（仍占建仓名额 + committed 预算，sweep 又已移出截止表）。这里：
            # 超过宽限期仍 CANCELLING → 幂等重发 cancel（券商无此活动委托会自行拒绝），并续二次截止待下轮再盯；
            # 未到宽限 → 只留痕、同样续二次截止，避免被永久遗忘。
            grace = timedelta(seconds=getattr(self._settings, "cancel_grace_seconds", 30))
            elapsed = now - (entry.updated_at or now)
            if elapsed >= grace:
                # 撤单容错（国金对接核对）：重发 cancel 抛异常不打断整轮 sweep。仅在受理成功时重置
                # updated_at（限制重发频率为每宽限期一次）；失败则不重置 updated_at → 下个宽限期到点时
                # elapsed 仍 >= grace、即再次重撤（每宽限期一次的节奏，不会 busy 重撤风暴）。
                if self._safe_cancel(entry.order_id, biz_no):
                    self._ledger.update(biz_no, updated_at=now)
                    self._logger.warn(
                        "order_cancelling_recancel",
                        biz_order_no=biz_no, account_id=self._account_id, order_id=entry.order_id,
                    )
            else:
                self._logger.info(
                    "order_cancelling_within_grace",
                    biz_order_no=biz_no, account_id=self._account_id, order_id=entry.order_id,
                )
            # 续二次截止：sweep 已先 pop 本项，这里重新写入使下轮继续盯防该 CANCELLING 单。
            self._ttl_deadline[biz_no] = now + grace
        else:
            # 终态（CANCELLED/TRADED/REJECTED/ERROR/...）无需再撤，仅留痕。
            self._logger.info(
                "order_ttl_state_not_cancelable",
                biz_order_no=biz_no,
                account_id=self._account_id,
                state=str(entry.state),
            )

    def _infer_miss_reason(self, biz_no: str) -> str:
        """据原决策因子推断买不进归因（§4.4(7)）。

        业务意图：一字板 / 秒封 / 普通排队未成在复盘归因里分类不同，单列「未成交机会成本」。
        判定口径（取 decision.factors_snapshot）：
          - is_one_word=True → 「一字未成」（开盘 low==high==涨停价，竞价即虚拟封死）；
          - is_second_seal=True 或顶涨停大封流比（is_limit_up）→ 「秒封未成」；
          - 否则 → 「排队未成」（普通限价排队未排到）。
        边界：无原决策句柄时保守归「排队未成」。
        """
        decision = self._decision_by_biz.get(biz_no)
        factors = decision.factors_snapshot if decision is not None else {}
        if factors.get("is_one_word"):
            return "一字未成"
        if factors.get("is_second_seal") or factors.get("is_limit_up"):
            return "秒封未成"
        return "排队未成"

    # ------------------------------------------------------------------
    # 转次优（§4.4(7) / §4.7）
    # ------------------------------------------------------------------
    def try_next_best(self, decision: EntryDecision) -> Optional[str]:
        """主标的买不进 → 按 next_best 序列尝试次优（仍走完整 place 流程，§4.4(7)）。

        业务意图：主标的一字 / 秒封 / 下单失败时不放弃整批，转向次优候选。
        边界：next_best 为空 → 返回 None（放弃，不追高破板）；非空 → 对首个候选走 place
        （仍幂等 / 限价 / 成交确认）。次优本身的 next_best 链可继续承接（由其 place 后再判）。
        """
        if not decision.next_best:
            self._logger.info(
                "order_next_best_empty_give_up",
                ts_code=decision.ts_code,
                account_id=self._account_id,
            )
            return None
        nxt = decision.next_best[0]
        self._logger.info(
            "order_try_next_best",
            from_ts_code=decision.ts_code,
            to_ts_code=nxt.ts_code,
            account_id=self._account_id,
        )
        return self.place(nxt)

    # ------------------------------------------------------------------
    # 下单失败（§4.4(4) / §4.7：不静默）
    # ------------------------------------------------------------------
    def handle_order_error(
        self, order_id: int, error_id: Optional[int], msg: Optional[str], decision: Optional[EntryDecision] = None
    ) -> Optional[str]:
        """on_order_error：标 ERROR、绝不静默；可转次优（§4.4(4)/§4.7）。

        业务意图：xttrader 回报下单失败时，该委托标 ERROR 终态并留痕；若调用方提供原 decision，
        则尝试转次优（主标的下单失败也不放弃整批）。
        边界：台账无该 order_id 时 sync_status 内部忽略（防越权改写）；error_id/msg 一并落账。
        """
        # sync_status 按 order_id 推进状态 + 落 error_msg（台账无此 order_id 时内部静默忽略）。
        self._ledger.sync_status(order_id, OrderState.ERROR, msg)
        # 补落 error_id（sync_status 只落 msg）。
        led = self._ledger.get_by_order_id(order_id)
        if led is not None and error_id is not None:
            self._ledger.update(led.biz_order_no, error_id=error_id, updated_at=self._clock.now_utc())
        self._logger.error(
            "order_error",
            order_id=order_id,
            account_id=self._account_id,
            error_id=error_id,
            error_msg=msg,
        )
        # 不静默：如有原决策则转次优。
        if decision is not None:
            return self.try_next_best(decision)
        return None
