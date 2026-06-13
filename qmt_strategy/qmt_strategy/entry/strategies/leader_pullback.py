"""龙回头策略 LEADER_PULLBACK（§4.2 第 4 类）。

业务意图：前期龙头回调后再起（setup=高位回踩 / 分歧转一致），出现承接时限价分批吸。
买 / 弃口径（§4.2）：
  买：龙头分歧日回踩后有承接（量价配合）、leader_strength_score 高（≥ settings.leader_strength_min）、
      continuation_prob 不弱 → 限价吸(limit_price 取末帧价 / 合理下沿)。
  弃：回踩无承接、放量下跌、龙头地位被取代（强度分不足或续板概率弱）→ 放弃。
"""

from __future__ import annotations

from decimal import Decimal

from ...config.settings import Settings
from ...contracts.enums import EntryAction, OrderPhase
from ...contracts.models import AuctionSnapshot, PlanRow
from .base import EntryStrategy, StrategyOutcome, register

# 续板概率「不弱」下限（占位经验阈值，真实落地以实测为准）：低于此值视为续板预期走弱。
_CONTINUATION_MIN = Decimal("0.3")
# leader_strength_min 未配置时的强度分兜底下限（不臆造高线，仅作有值校验的保守回退）。
_LEADER_STRENGTH_FALLBACK = Decimal("0")


@register(EntryAction.LEADER_PULLBACK)
class LeaderPullbackStrategy(EntryStrategy):
    """龙回头：强度高 + 续板不弱 → 限价吸；强度不足 / 续板弱 → 弃。"""

    def decide(self, plan: PlanRow, snap: AuctionSnapshot, settings: Settings) -> StrategyOutcome:
        # 龙头强度下限：优先用 settings.leader_strength_min，缺省用保守兜底（≥0 即有值即可）。
        strength_min = (
            settings.leader_strength_min
            if settings.leader_strength_min is not None
            else _LEADER_STRENGTH_FALLBACK
        )

        score = plan.leader_strength_score
        # —— 弃条件 1：龙头强度分缺失或不足（地位被新龙头取代）→ 放弃。——
        if score is None or score < strength_min:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"LEADER_PULLBACK弃:龙头强度不足 leader_strength_score={score} < {strength_min}",
            )

        # —— 弃条件 2：续板概率弱（回踩无承接预期）→ 放弃。——
        cont = plan.continuation_prob
        if cont is None or cont < _CONTINUATION_MIN:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"LEADER_PULLBACK弃:续板概率弱 continuation_prob={cont} < {_CONTINUATION_MIN}",
            )

        # —— 买条件：强度高 + 续板不弱 → 限价分批吸。——
        # 限价取末帧虚拟成交价（回踩承接价），缺失则回退合理下沿（均不臆造价位）。
        limit = snap.last_price or plan.reasonable_open_low
        if limit is None:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason="LEADER_PULLBACK弃:吸筹限价缺失无法挂价",
            )
        return StrategyOutcome(
            action=EntryAction.LEADER_PULLBACK,
            limit_price=limit,
            reason=(
                f"LEADER_PULLBACK买:龙头强度高承接到位 leader_strength_score={score} "
                f"continuation_prob={cont} 吸筹价={limit}"
            ),
            order_phase=OrderPhase.OPENING,
        )
