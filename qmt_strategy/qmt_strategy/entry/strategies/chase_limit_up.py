"""打板跟买策略 CHASE_LIMIT_UP（§4.2 第 1 类）。

业务意图：强势连板 / 龙头（setup=连板接力），盘中或竞价即封顶板时挂涨停价排队跟买。
买 / 弃口径（§4.2）：
  买：顶板且封单稳（is_limit_up=True 且封流比未塌缩）、market_state 非退潮 / 冰点、未触一字
      → 挂涨停价(limit_price=plan.limit_up_price)。
  弃：炸板 / 封单快速减小（封流比过小视为封单不稳）/ market_state 退潮 / 冰点 → 放弃。
注：一字板另走 §4.7（执行侧 order_executor 处理排队 / 买不进）；此处「未触一字」仅作软校验，
    上游 _should_skip 已挡退潮 / 冰点，这里再做策略级二次确认。
"""

from __future__ import annotations

from decimal import Decimal

from ...config.settings import Settings
from ...contracts.enums import AuctionPhase, EntryAction, OrderPhase
from ...contracts.models import AuctionSnapshot, PlanRow
from .base import EntryStrategy, StrategyOutcome, register

# 封单稳的封流比下限（占位经验阈值，真实落地以实测为准）：封流比低于此值视为封单偏薄 / 不稳。
# 注：封流比 None（未达涨停或数据缺）时不据此判稳，由 is_limit_up 主导。
_SEAL_RATIO_MIN = Decimal("0.0")

# 竞价段（9:25 定盘前）：挂竞价单 AUCTION；定盘/盘中（>=9:25）：挂开盘后单 OPENING。
_AUCTION_PHASES = (AuctionPhase.PRE_AUCTION, AuctionPhase.AUCTION_CANCELABLE, AuctionPhase.AUCTION_LOCKED)


def _order_phase_for(snap: AuctionSnapshot) -> OrderPhase:
    """按快照所处时段判定下单时段（评审 P1#4）。

    原实现硬编码 order_phase=AUCTION：竞价择时默认关时，连板打板跟买的 AUCTION 决策会被编排层
    「只留痕不下单」整类丢弃（核心战法静默失效）；且 9:25 后的盘中顶板跟买本应是 OPENING 也被当
    竞价单。这里据 snap.phase 区分：定盘前→AUCTION，定盘/盘中→OPENING。
    """
    return OrderPhase.AUCTION if snap.phase in _AUCTION_PHASES else OrderPhase.OPENING


@register(EntryAction.CHASE_LIMIT_UP)
class ChaseLimitUpStrategy(EntryStrategy):
    """打板跟买：顶板封单稳 → 挂涨停价；炸板 / 封单减 → 弃。"""

    def decide(self, plan: PlanRow, snap: AuctionSnapshot, settings: Settings) -> StrategyOutcome:
        # —— 弃条件 1：未顶板（虚拟成交价未达涨停价）→ 无板可打，放弃。——
        if not snap.is_limit_up:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_LIMIT_UP弃:未顶板 is_limit_up={snap.is_limit_up}",
            )

        # —— 弃条件 2：封单不稳（封流比有值但低于下限）→ 视为炸板 / 封单快速减小风险，放弃。——
        # 边界：封流比为 None（达涨停但买一量缺、或市值缺）时不据此否决，避免数据缺口误杀强势板。
        ratio = snap.seal_to_float_ratio
        if ratio is not None and ratio < _SEAL_RATIO_MIN:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_LIMIT_UP弃:封单不稳 seal_to_float_ratio={ratio}",
            )

        # —— 买条件：顶板且封单稳 → 挂涨停价排队跟买。——
        # 限价取 plan.limit_up_price（涨停价）；缺失则无法挂价，降级为放弃（不臆造价位）。
        if plan.limit_up_price is None:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason="CHASE_LIMIT_UP弃:涨停价缺失无法挂价",
            )

        return StrategyOutcome(
            action=EntryAction.CHASE_LIMIT_UP,
            limit_price=plan.limit_up_price,
            reason=(
                f"CHASE_LIMIT_UP买:顶板封单稳 is_limit_up=True "
                f"seal_to_float_ratio={ratio} 挂涨停价={plan.limit_up_price}"
            ),
            # 下单时段按快照 phase 判定（评审 P1#4）：竞价即封→AUCTION 竞价单；9:25 后盘中顶板→OPENING。
            order_phase=_order_phase_for(snap),
        )
