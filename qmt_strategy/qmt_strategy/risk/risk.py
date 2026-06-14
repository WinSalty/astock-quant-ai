"""风控护栏实现（§5.4 风控 risk）。

业务意图：执行侧下单/卖出决策前的最后一道闸门，统一在此判定
ALLOW（放行）/ FREEZE（冻结，暂停一切新决策）/ SELL_ONLY_HOLD（空仓闸门，只守仓不新开、
存量仍可卖）。把账户级 / 单票级阈值、行情/下单中断冻结态、空仓闸门联动、安全默认
（不确定宁可不交易）收敛为单一可测组件，避免各调用方各写一套口径导致风控漂移。

安全默认口径（§5.4.3）：任一关键输入不可信（行情断流 / 下单断线）→ 直接 FREEZE，
没有可信盘口就不做实时卖出决策，宁可不交易也不误卖。

依赖边界：本模块只依赖锁定契约层（enums / models）、配置（Settings）与时钟 / 日志协议，
不 import 任何 xtquant；价位 / 阈值一律用 Decimal 比较，禁用 float。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import RiskVerdict
from qmt_strategy.contracts.models import RiskDecision
from qmt_strategy.contracts.protocols import Clock, StructLogger


class Risk:
    """风控闸门组件（§5.4）。

    构造依赖：
    - settings：提供风控阈值（account_drawdown_limit / account_loss_limit /
      stock_float_loss_limit）与空仓 / 禁开仓情绪周期集合（market_state_block）。
    - clock：统一时钟（落决策日志时刻用，禁直接 datetime.now / 手工 ±8h）。
    - logger：结构化日志，风控冻结 / 放行留痕（§5.10 第 7 条要求风控冻结/恢复均落决策日志）。
    """

    def __init__(self, settings: Settings, clock: Clock, logger: StructLogger) -> None:
        self._settings = settings
        self._clock = clock
        self._logger = logger

    # ------------------------------------------------------------------
    # 主闸门：gate（§5.4）
    # ------------------------------------------------------------------
    def gate(
        self,
        *,
        market_state: Optional[str],
        market_feed_ok: bool = True,
        trade_conn_ok: bool = True,
        account_drawdown: Optional[Decimal] = None,
        account_realized_loss: Optional[Decimal] = None,
        unit_float_loss: Optional[Decimal] = None,
    ) -> RiskDecision:
        """风控闸门裁决。

        业务意图：把多层风控条件按「安全默认在前」的优先级折叠为单一裁决。命中越靠前的
        危险条件越严格——优先保证不误卖 / 不超额，再谈放行。

        优先级（从高到低，前者命中即短路返回，安全默认优先）：
          1. 关键输入不可信：market_feed_ok 或 trade_conn_ok 为 False → FREEZE
             （§5.4.1/§5.4.3：行情断流没有可信盘口、下单断线无法可靠成交，
              二者任一中断都不做实时卖出决策，宁可不交易）。
          2. 账户级击穿：account_drawdown 超 account_drawdown_limit，或
             account_realized_loss 超 account_loss_limit → FREEZE（账户级击穿，§5.4.1）。
             阈值为 None（未配置）视为不约束。
             口径澄清（评审二轮 P3#74）：账户级击穿在【开新仓】路径生效（_open_blocked_by_risk 喂
             account_drawdown → FREEZE 禁开仓）；【卖出】路径**刻意不喂** account_drawdown（run_sell_pass
             只喂行情/下单中断），以免账户回撤反而冻结必要的止损出场。即"账户击穿冻结买入、不冻结卖出"，
             由调用方按是否喂入 account_drawdown 决定，gate 本身只做无状态裁决。
          3. 单票级击穿：unit_float_loss 超 stock_float_loss_limit → FREEZE（该票冻结，
             §5.4.1）。
          4. 空仓闸门：market_state == '空仓' → SELL_ONLY_HOLD（只守仓不新开，
             存量仍可卖，§5.4.2：空仓闸门只关买入闸、不锁卖出闸，避免该出的票出不掉）。
          5. 其余 → ALLOW。

        边界：
        - 阈值「超」口径取「大于等于阈值即击穿」——亏损/回撤达到约定红线即触发，
          口径偏保守（宁可早冻结，符合安全默认）。
        - 入参亏损/回撤均以「正数表示亏损幅度」承载（与 stock_float_loss_limit 等
          正向阈值同口径）；为 None 表示该维度本次无数据，不参与判定。
        - 阈值未配置（settings 对应项为 None）时该层不约束，跳过不误冻结。
        - 裁决是无状态纯函数：同入参恒得同结果，重跑安全、可幂等复算。
        """
        # —— 第 1 层：关键输入不可信 → FREEZE（安全默认最高优先）——
        # 行情断流或下单断线，任一为 False 即冻结：没有可信盘口/通道就不做实时卖出决策。
        if not market_feed_ok or not trade_conn_ok:
            reason = "行情/下单中断：market_feed_ok={} trade_conn_ok={}（安全默认，§5.4.3）".format(
                market_feed_ok, trade_conn_ok
            )
            return self._freeze(
                reason,
                market_feed_ok=market_feed_ok,
                trade_conn_ok=trade_conn_ok,
            )

        # —— 第 2 层：账户级阈值击穿 → 全账户 FREEZE ——
        # 当日账户回撤超阈值（阈值非 None 且被击穿）。
        if self._breached(account_drawdown, self._settings.account_drawdown_limit):
            reason = "账户级回撤击穿：drawdown={} >= limit={}（§5.4.1）".format(
                account_drawdown, self._settings.account_drawdown_limit
            )
            return self._freeze(reason, account_drawdown=str(account_drawdown))
        # 当日已实现亏损超阈值。
        if self._breached(account_realized_loss, self._settings.account_loss_limit):
            reason = "账户级已实现亏损击穿：loss={} >= limit={}（§5.4.1）".format(
                account_realized_loss, self._settings.account_loss_limit
            )
            return self._freeze(reason, account_realized_loss=str(account_realized_loss))

        # —— 第 3 层：单票级浮亏击穿 → 该票 FREEZE ——
        if self._breached(unit_float_loss, self._settings.stock_float_loss_limit):
            reason = "单票浮亏击穿：float_loss={} >= limit={}（§5.4.1）".format(
                unit_float_loss, self._settings.stock_float_loss_limit
            )
            return self._freeze(reason, unit_float_loss=str(unit_float_loss))

        # —— 第 4 层：空仓闸门 → 只守仓不新开，存量仍可卖 ——
        # 注意：空仓闸门只关买入闸、不锁卖出闸，故返回 SELL_ONLY_HOLD 而非 FREEZE。
        if market_state == "空仓":
            decision = RiskDecision(
                verdict=RiskVerdict.SELL_ONLY_HOLD,
                reason="market_state=空仓：只守仓不新开，存量仍可卖（§5.4.2）",
            )
            self._logger.info(
                "risk_gate_sell_only_hold",
                verdict=str(decision.verdict),
                market_state=market_state,
            )
            return decision

        # —— 第 5 层：其余条件均未命中 → 放行 ——
        decision = RiskDecision(verdict=RiskVerdict.ALLOW, reason="风控通过：无命中冻结/空仓条件")
        self._logger.info(
            "risk_gate_allow",
            verdict=str(decision.verdict),
            market_state=market_state,
        )
        return decision

    # ------------------------------------------------------------------
    # 卖量钳制：clamp_sell_volume（§5.4.3）
    # ------------------------------------------------------------------
    def clamp_sell_volume(self, decision_vol: int, can_use_volume: int) -> int:
        """卖出量上限 = min(决策量, 可用量)，绝不下超量卖单（§5.4.3 / §5.8 守 T+1 量闸）。

        业务意图：守 T+1 的「量闸」——当日买入部分不计入 can_use_volume，
        即便决策要清仓，实际可卖量也只能取 can_use_volume，避免下超量卖单造成废单 / 对账失真。
        边界：
        - can_use_volume=0（当日全为新买入、整仓锁定）→ 钳为 0，不发卖单。
        - 入参出现负值时统一兜底为 0（不应出现负可卖量，防御性处理，绝不产出负卖量）。
        """
        # 负值兜底：理论上不会出现负的决策量/可卖量，做防御性归零，确保结果非负。
        dv = decision_vol if decision_vol > 0 else 0
        cu = can_use_volume if can_use_volume > 0 else 0
        return min(dv, cu)

    # ------------------------------------------------------------------
    # 开仓闸门判定：is_open_blocked（空仓闸门联动，§5.4.2）
    # ------------------------------------------------------------------
    def is_open_blocked(self, market_state: Optional[str]) -> bool:
        """当前情绪周期是否禁止新开仓（§5.4.2 空仓闸门 + 退潮/冰点不得激进接力）。

        业务意图：market_state 落在 settings.market_state_block（默认 退潮/冰点/空仓）时
        停止一切买入；启动 / 高潮等积极周期不拦截。
        边界：market_state 为 None（情绪周期未知）时不在禁开集合内 → 返回 False，
        不在此处兜底冻结（冻结由 gate 的关键输入不可信分支负责，本方法只做开仓闸判定）。
        """
        if market_state is None:
            return False
        return market_state in self._settings.market_state_block

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _breached(self, observed: Optional[Decimal], limit: Optional[Decimal]) -> bool:
        """阈值击穿判定：阈值非 None 且观测值非 None 且观测值 >= 阈值，视为击穿。

        业务意图：统一账户级/单票级阈值比较口径，避免三处各写一套。
        边界：阈值未配置（limit 为 None）→ 该维度不约束，返回 False；
        观测值缺失（observed 为 None）→ 本次无数据，返回 False（不凭空冻结）。
        口径：用 Decimal 直接比较（禁 float），「达到红线即击穿」偏保守。
        """
        if limit is None or observed is None:
            return False
        return observed >= limit

    def _freeze(self, reason: str, **fields) -> RiskDecision:
        """构造 FREEZE 裁决并落告警日志（风控冻结须留痕，§5.10 第 7 条）。

        统一冻结出口：所有冻结分支经此产出 RiskDecision(FREEZE) 并记录 error 级日志，
        便于人工恢复时审计（恢复需人工确认再解冻，§5.4.3）。
        """
        self._logger.error("risk_gate_freeze", verdict=str(RiskVerdict.FREEZE), reason=reason, **fields)
        return RiskDecision(verdict=RiskVerdict.FREEZE, reason=reason)
