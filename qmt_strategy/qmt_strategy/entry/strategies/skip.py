"""放弃策略 SKIP（§4.2 第 5 类）。

业务意图：任何 setup 命中放弃条件时的统一出口。本策略恒产出 SKIP 决策，不下单但仍留痕
（供闭环归因「未下单 N−M」口径，§4.2 SKIP 留痕）。

设计取舍：SKIP 既是「无法路由到买类策略时的兜底 action」，也是 _should_skip 闸门命中后的产物。
为保持「五类 action 各一文件」的对称性，这里以策略形态实现，reason 由调用方（entry_router）透传，
本策略不重新判定条件、只负责把放弃语义落成 StrategyOutcome（limit_price=None、不挂价）。
"""

from __future__ import annotations

from ...config.settings import Settings
from ...contracts.enums import EntryAction, OrderPhase
from ...contracts.models import AuctionSnapshot, PlanRow
from .base import EntryStrategy, StrategyOutcome, register


@register(EntryAction.SKIP)
class SkipStrategy(EntryStrategy):
    """放弃：恒产出 SKIP（limit_price=None），不下单但留痕。"""

    def decide(self, plan: PlanRow, snap: AuctionSnapshot, settings: Settings) -> StrategyOutcome:
        # SKIP 不挂价、不下单；reason 由 router 在调用前组织（含命中的具体放弃原因），
        # 此处给一个兜底 reason，避免空理由导致复盘无据。
        return StrategyOutcome(
            action=EntryAction.SKIP,
            limit_price=None,
            reason="SKIP:命中放弃条件不下单(留痕)",
            order_phase=OrderPhase.OPENING,
        )
