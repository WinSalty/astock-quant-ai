"""均线低吸策略 DIP_BUY_MA（§4.2 第 3 类）。

业务意图：低吸战法（setup=均线粘合 / 回踩支撑），竞价 / 开盘小幅高开或回踩时挂限价低吸。
买 / 弃口径（§4.2）：
  买：open_pct 落在低吸档 [lowbuy_low, lowbuy_high]（对接 3.2.2 区间）、回踩不破关键均线、
      market_state 允许（上游 _should_skip 已挡）→ 挂限价低吸(limit_price 取合理下沿 / 末帧价)。
  弃：高开超 Y%（open_pct > overheat_pct，追高风险）或跌破支撑 → 放弃。
"""

from __future__ import annotations

from decimal import Decimal

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
        # —— 打板因子 E2（默认关，配阈值才生效；双守卫缺数据零误杀）：反复炸板(烂板) → 弃。——
        # 低吸虽不忌高位，但仍不接「反复开板的烂板」（封板质量差）；return_5d_pct 高位闸不接（低吸取低位、高位非其禁忌）。
        if (
            settings.forbid_open_times_max is not None
            and plan.open_times is not None
            and plan.open_times >= settings.forbid_open_times_max
        ):
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"DIP_BUY_MA弃:反复炸板 open_times={plan.open_times}>={settings.forbid_open_times_max}",
            )
        thr = resolve_thresholds(plan, snap, settings)
        op = snap.open_pct

        # —— 弃条件 0：低吸档阈值未显式配置 → fail-closed 不开仓（评审 F18）。——
        # 低吸档 [lowbuy_low, lowbuy_high] 语义是「平开/微跌回踩区间」，与信号侧「合理高开区间」相反、绝不可
        # 互相套用；缺显式配置时 resolve_thresholds 留 None（不再臆造成高开区间）。此处宁可不开仓，也绝不在
        # 缺低吸档时用错区间把真低吸点弃掉、反在高开位追买（方向反）。需用 QMT_AUCTION_LOWBUY_PCT_LOW/HIGH 显式配。
        if thr.lowbuy_low is None or thr.lowbuy_high is None:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason="DIP_BUY_MA弃:低吸档阈值未配置(QMT_AUCTION_LOWBUY_PCT_LOW/HIGH)，fail-closed 不臆造",
            )

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

        # 低吸限价取「末帧虚拟成交价 last_price」与「合理下沿 reasonable_open_low」中的【较低者】（评审三轮
        # EXEC-entry-04）：低吸=不追价。原 `reasonable_open_low or last_price` 优先取下沿，当下沿高于现价时
        # 挂限价=下沿=高于现价=追价买入，与低吸意图相悖、且与 leader_pullback 的 last_price 优先口径不一致。
        # 取两者较小者：二者皆有取小（只在更低处低吸、不追高）；其一为 None 取另一个；皆 None 由下方守卫 SKIP。
        # 脏 tick 防护（评审三轮 EXEC-entry-04 复审）：last_price 仅在【正值且不显著低于合理下沿】时才纳入取小——
        # 正常低吸价与合理下沿仅差几个点，若 last_price 跌穿下沿 10% 以上（如 0.01 的坏帧），取小会挂出永不成交、
        # 白占名额到 TTL 的死单；此时视为坏帧弃用 last_price、回退合理下沿，仍不臆造价位。
        _floor = (plan.reasonable_open_low * Decimal("0.9")) if plan.reasonable_open_low is not None else None
        _last_ok = (
            snap.last_price is not None
            and snap.last_price > 0
            and (_floor is None or snap.last_price >= _floor)
        )
        _prices = [p for p in (snap.last_price if _last_ok else None, plan.reasonable_open_low) if p is not None]
        limit = min(_prices) if _prices else None
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
