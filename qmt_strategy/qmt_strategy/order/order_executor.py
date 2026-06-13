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
from typing import Any, Dict, Optional, Tuple

from ..common.auction_window import is_cancel_forbidden
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
    ) -> None:
        # 依赖注入：trader 是唯一下单/撤单出口；account 为下单/查询入参对象（StockAccount）。
        self._trader = trader
        self._account = account
        self._account_id = account_id
        self._ledger = ledger
        self._settings = settings
        self._clock = clock
        self._logger = logger
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
        边界：长度受 QMT 限制（≤255）；超长时【优先保 LUP|<T> 段】，从 ts_code 尾部截断，
        保证回流解析关键的 signal_trade_date 不丢失（§4.4(5) 截断口径）。
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

        # —— kill_switch 全局熔断：只采集不下单（§7.1.5）——
        if self._settings.kill_switch:
            self._logger.warn(
                "order_kill_switch_block",
                ts_code=decision.ts_code,
                account_id=self._account_id,
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

        limit_price = decision.limit_price

        # —— 计划股数：优先用决策给定，否则按账户可用资金现算 ——
        plan_volume = decision.plan_volume
        if plan_volume is None:
            plan_volume = self._plan_volume(decision, limit_price)
        if not plan_volume or plan_volume <= 0:
            # 资金不足 / 算出 0 股：不下单并留痕，等价于该计划放弃（§4.4(6) 资金口径）。
            self._logger.warn(
                "order_zero_volume_skip",
                ts_code=decision.ts_code,
                account_id=self._account_id,
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

        # —— 唯一下单点：限价单（不挂市价，避免滑点失控，§4.4(3)）——
        # price 转 float 仅在调用 trader 边界，Decimal 留作台账/计算口径。
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

        # —— 同步下单失败（order_stock 返回 <0 / None）：标 ERROR，绝不置 SUBMITTED（评审 medium#9）。——
        # 业务意图：XtTraderLike.order_stock 约定 <0 为同步失败；若仍置 SUBMITTED，对账会把它误判为
        # 「漏单凭空消失」（missing_report），污染 §6.10 告警语义。这里落 ERROR 终态 + 留痕 + 转次优。
        if order_id is None or order_id < 0:
            self._ledger.update(
                biz_no, order_id=order_id, state=OrderState.ERROR,
                error_msg="order_stock 同步下单失败(返回<0)", updated_at=self._clock.now_utc(),
            )
            self._logger.error(
                "order_submit_failed_sync",
                ts_code=decision.ts_code,
                account_id=self._account_id,
                biz_order_no=biz_no,
                order_id=order_id,
            )
            # 不静默：主标的同步下单失败也不放弃整批，尝试次优（§4.4(7)/§4.7）。
            self.try_next_best(decision)
            return biz_no

        # —— 回填 order_id + 推进到 SUBMITTED（已发 order_stock，待回报）——
        # 注意：到此为止状态最多 SUBMITTED；REPORTED/TRADED 由外部回报推进（§4.4(4)）。
        self._ledger.update(biz_no, order_id=order_id, state=OrderState.SUBMITTED, updated_at=self._clock.now_utc())

        # —— TTL 截止：竞价单到 9:25 定盘；开盘单 now + order_ttl_seconds（§4.4(3)）——
        self._ttl_deadline[biz_no] = self._compute_ttl_deadline(now, decision.order_phase)
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
        return biz_no

    # ------------------------------------------------------------------
    # 计划股数现算（§4.4(6) 资金口径）
    # ------------------------------------------------------------------
    def _plan_volume(self, decision: EntryDecision, limit_price: Optional[Decimal]) -> int:
        """按账户可用资金 + 仓位上限算计划股数，向下取整到 100 股整手。

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
        # 单票 / 单笔金额上限：取已配置项中的较小值收紧（None 视为不限制）。
        for cap in (self._settings.per_order_max_amount, self._settings.max_position_per_stock):
            if cap is not None and Decimal(str(cap)) < budget:
                budget = Decimal(str(cap))
        if budget <= 0:
            return 0
        # 可买股数 = 预算 / 限价，向下取整到 100 股整手。
        raw_shares = (budget / limit_price).to_integral_value(rounding=ROUND_DOWN)
        lots = int(raw_shares) // _BOARD_LOT
        return lots * _BOARD_LOT

    def _compute_ttl_deadline(self, now: datetime, order_phase: OrderPhase) -> datetime:
        """计算 TTL 截止时刻（UTC naive）。

        竞价单（AUCTION）：存活到 9:25 定盘（东八区），不用相对秒数。
        开盘单（OPENING）：now + settings.order_ttl_seconds。
        实现：竞价单先在东八区把当日 9:25 拼好，再回到「相对 now 的偏移」以保持 UTC naive 口径。
        """
        if order_phase == OrderPhase.AUCTION:
            east8_now = east8_now_from_utc(now)
            east8_925 = east8_now.replace(hour=9, minute=25, second=0, microsecond=0)
            # 截止偏移 = 东八区(9:25 - now)，加到 UTC naive 的 now 上（不手工 ±8h，仅取差值）。
            return now + (east8_925 - east8_now)
        return now + timedelta(seconds=self._settings.order_ttl_seconds)

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
        handled = []
        # 先收集到期 biz（避免遍历中修改字典）。
        due = [biz for biz, deadline in self._ttl_deadline.items() if now >= deadline]
        for biz in due:
            self.on_ttl_expired(biz)
            # 处理后移除截止表项，避免重复触发（已撤 / 已转次优 / 已留痕）。
            self._ttl_deadline.pop(biz, None)
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

        # —— 仅对「在途可撤」状态发撤单：已报 / 部成 ——
        if entry.state in (OrderState.REPORTED, OrderState.PART_TRADED):
            self._trader.cancel_order_stock(self._account, entry.order_id)
            self._ledger.update(biz_no, state=OrderState.CANCELLING, updated_at=now)
            self._logger.info(
                "order_ttl_cancel_sent",
                biz_order_no=biz_no,
                account_id=self._account_id,
                order_id=entry.order_id,
                filled_volume=entry.filled_volume,
            )
            # —— 撤后若一手未成（filled==0）：一字 / 秒封「买不进」归因 + 转次优（§4.4(7)）——
            if entry.filled_volume == 0:
                miss_reason = self._infer_miss_reason(biz_no)
                self._ledger.update(biz_no, miss_reason=miss_reason, updated_at=now)
                self._logger.warn(
                    "order_miss_unfilled",
                    biz_order_no=biz_no,
                    account_id=self._account_id,
                    miss_reason=miss_reason,
                )
                # 取原决策尝试次优（主标的买不进不放弃整批，§4.4(7)）。
                decision = self._decision_by_biz.get(biz_no)
                if decision is not None:
                    self.try_next_best(decision)
        else:
            # SUBMITTED 尚未收到 on_stock_order 回报：保守不盲撤（避免对未确认委托发撤），仅留痕。
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
