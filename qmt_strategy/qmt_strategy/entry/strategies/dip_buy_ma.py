"""均线低吸策略 DIP_BUY_MA（§4.2 第 3 类）。

业务意图：低吸战法（setup=均线粘合 / 回踩支撑），竞价 / 开盘小幅高开或回踩时挂限价低吸。
买 / 弃口径（§4.2）：
  买：open_pct 落在低吸档 [lowbuy_low, lowbuy_high]（对接 3.2.2 区间）、回踩不破关键均线、
      market_state 允许（上游 _should_skip 已挡）→ 挂限价低吸(limit_price 取合理下沿 / 末帧价)。
  弃：高开超 Y%（open_pct > overheat_pct，追高风险）或跌破支撑 → 放弃。
"""

from __future__ import annotations

from ...config.settings import Settings
from ...contracts.enums import EntryAction, OrderPhase
from ...contracts.models import AuctionSnapshot, PlanRow
from .base import EntryStrategy, StrategyOutcome, prior_gate_reason, register, resolve_thresholds


@register(EntryAction.DIP_BUY_MA)
class DipBuyMaStrategy(EntryStrategy):
    """均线低吸：open_pct 落低吸档 → 限价低吸；高开超 / 跌破支撑 → 弃。"""

    def decide(self, plan: PlanRow, snap: AuctionSnapshot, settings: Settings) -> StrategyOutcome:
        # —— 先验闸门（评审二轮 P3#75）：低吸（含高位连板低吸）原完全不读龙头强度/续板先验、无强度把关即挂单。
        # 这里与追买类同口径消费先验：leader_strength_score/continuation_prob【有值且低于阈值】→ 收手，
        # 不去低吸一个强度已塌/续板预期弱的票（接下跌）。缺先验时不误杀，由下方盘口档位条件把关。
        gate = prior_gate_reason(plan, settings)
        if gate is not None:
            return StrategyOutcome(action=EntryAction.SKIP, reason=f"DIP_BUY_MA弃:{gate}")
        thr = resolve_thresholds(plan, snap, settings)
        op = snap.open_pct

        # —— 弃条件 1：open_pct 不可得 → 无法判低吸档位置，放弃（不臆造）。——
        if op is None:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason="DIP_BUY_MA弃:open_pct不可得无法判低吸档",
            )

        # —— 弃条件 2：高开超 Y%（追高风险，3.2.2「高开超 Y% 警惕」）→ 放弃。——
        if thr.overheat_pct is not None and op > thr.overheat_pct:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"DIP_BUY_MA弃:高开超追高风险 open_pct={op} > overheat={thr.overheat_pct}",
            )

        # —— 弃条件 3：跌破支撑（弱于低吸档下沿，等同跌破回踩支撑位）→ 放弃。——
        # 口径：低吸档下沿即关键支撑参考；弱于下沿视为破位，不接下跌。
        if thr.lowbuy_low is not None and op < thr.lowbuy_low:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"DIP_BUY_MA弃:跌破支撑 open_pct={op} < lowbuy_low={thr.lowbuy_low}",
            )

        # —— 买条件：open_pct 落在低吸档区间内 [lowbuy_low, lowbuy_high] → 挂限价低吸。——
        # 上沿存在则需不超上沿；上沿缺失时仅以「不破下沿且未高开超」约束。
        if thr.lowbuy_high is not None and op > thr.lowbuy_high:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"DIP_BUY_MA弃:高于低吸档上沿 open_pct={op} > lowbuy_high={thr.lowbuy_high}",
            )

        # 限价取合理下沿（低吸目标价），缺失则回退末帧虚拟成交价（均不臆造价位）。
        limit = plan.reasonable_open_low or snap.last_price
        if limit is None:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason="DIP_BUY_MA弃:低吸目标价缺失无法挂价",
            )
        return StrategyOutcome(
            action=EntryAction.DIP_BUY_MA,
            limit_price=limit,
            reason=(
                f"DIP_BUY_MA买:落低吸档 open_pct={op}∈[{thr.lowbuy_low},{thr.lowbuy_high}] "
                f"低吸价={limit}"
            ),
            # 低吸一般等开盘后回踩确认再挂限价，按 OPENING 处理（可被 router 沿用）。
            order_phase=OrderPhase.OPENING,
        )
