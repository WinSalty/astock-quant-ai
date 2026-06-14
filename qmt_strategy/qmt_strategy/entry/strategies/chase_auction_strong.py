"""竞价强开追策略 CHASE_AUCTION_STRONG（§4.2 第 2 类 / §4.6 降级）。

业务意图：强预期票（setup=首板或连板，竞价定方向），竞价越竞越强时 9:20 前挂竞价买单。
买 / 弃口径（§4.2）：
  买：open_pct ≥ 强开档（overheat_pct，对接高开阈值）、auction_vol_ratio 达标、centroid_trend=UP
      → 9:20 前挂竞价单(order_phase=AUCTION，limit_price 取末帧虚拟成交价或合理上沿)。
  弃：弱开（open_pct 弱于 abandon_pct）或 centroid_trend=DOWN（高开走低 / 诱多）→ 放弃。
降级（§4.6）：竞价数据整体不可得（snap 标降级 B，data_quality 含 NO_TICK）→ 自动改判 OPENING
      （开盘后追，等 9:30 后开盘行情确认再下单），不在竞价段下竞价单。
"""

from __future__ import annotations

from decimal import Decimal

from ...config.settings import Settings
from ...contracts.enums import AuctionPhase, CentroidTrend, EntryAction, OrderPhase
from ...contracts.models import AuctionSnapshot, PlanRow
from .base import (
    EntryStrategy,
    StrategyOutcome,
    is_auction_degraded,
    prior_gate_reason,
    register,
    resolve_thresholds,
)

# 竞价决策时点：9:20（AUCTION_LOCKED 起）后不可撤，竞价单必须在此前定（§3.3）。
# 据此判定「降级 B 真伪」：9:20 前的单帧 NO_TICK 视为瞬时丢帧（defer 等后续帧），
# 9:20 及以后仍整体不可得才确认降级 B → 改判开盘后追（§4.6 / 评审 medium#4）。
_DECISION_DEADLINE_PHASES = (AuctionPhase.PRE_AUCTION, AuctionPhase.AUCTION_CANCELABLE)

# 竞价量能比达标下限（低保真占位，待回测标定，评审 F3）。
# 口径说明：量能比 = 竞价段累计量 / first_board_vol(信号日全天量)。竞价 10 分钟的量天然只占全天的
# 1–5%，故下限应在 ~0.02–0.05 量级（原 0.1=要求竞价量达全天 10%，几乎永不达标、会把强票全弃）。
# 终态：分钟数据到位后分母改用"首板日竞价量"(量纲对等)再重定阈值（见 doc/archive/09）。
_VOL_RATIO_MIN = Decimal("0.02")


def _cap_to_limit_up(price, limit_up_price):
    """买入限价以涨停价封顶（评审 P1#3）。

    竞价末帧虚拟成交价 last_price 在「越竞越强 / 脏数据跳变」时可能 ≥涨停价；若直接当限价挂单，
    竞价单(9:20 后不可撤)会以超涨停价成交（异常）或被交易所判废单。故对 buy 限价统一取
    min(候选价, 涨停价)。涨停价缺失时无可信上界，沿用候选价（不臆造上界）。
    """
    if price is None:
        return None
    if limit_up_price is not None and price > limit_up_price:
        return limit_up_price
    return price


@register(EntryAction.CHASE_AUCTION_STRONG)
class ChaseAuctionStrongStrategy(EntryStrategy):
    """竞价强开追：强开 + 放量 + 越竞越强 → 9:20 前竞价单；弱开 / 走低 → 弃；降级 B → 改判 OPENING。"""

    def decide(self, plan: PlanRow, snap: AuctionSnapshot, settings: Settings) -> StrategyOutcome:
        # —— 先验闸门（评审 P1#1）：龙头强度不足 / 续板概率弱 → 收手（含降级 B 路径前置）。——
        gate = prior_gate_reason(plan, settings)
        if gate is not None:
            return StrategyOutcome(action=EntryAction.SKIP, reason=f"CHASE_AUCTION_STRONG弃:{gate}")

        # —— 降级 B 改判（§4.6 / 评审 medium#4）：竞价整体不可得 → 不下竞价单，退化为开盘后追。——
        # 关键：NO_TICK 是逐帧标记，既可能是降级 B（整体长期不可得），也可能是某一轮瞬时丢帧。
        # 故只有「已到 9:20 决策时点（phase >= AUCTION_LOCKED）仍整体不可得」才确认降级 B、改判 OPENING；
        # 9:20 前的单帧 NO_TICK 一律 defer（不决策、不锁幂等），等后续帧拿到真实竞价因子再评，
        # 避免竞价早期一次抖动就永久提交「开盘后无脑追」的当日 BUY。
        if is_auction_degraded(snap):
            if snap.phase in _DECISION_DEADLINE_PHASES:
                return StrategyOutcome(
                    action=EntryAction.SKIP,
                    reason="CHASE_AUCTION_STRONG:竞价数据暂缺(单帧NO_TICK)，推迟决策等后续帧",
                    defer=True,
                )
            # 9:20 及以后仍整体不可得 → 确认降级 B，改判开盘后追（限价用涨停价/合理上沿兜底）。
            limit = plan.limit_up_price if plan.limit_up_price is not None else plan.reasonable_open_high
            return StrategyOutcome(
                action=EntryAction.CHASE_AUCTION_STRONG,
                limit_price=limit,
                reason="CHASE_AUCTION_STRONG降级B:9:20后竞价仍不可得→改判开盘后追(OPENING)",
                order_phase=OrderPhase.OPENING,
            )

        thr = resolve_thresholds(plan, snap, settings)
        op = snap.open_pct

        # —— 弃条件 1：弱开（open_pct 弱于放弃线）→ 竞价不强，放弃。——
        # 边界：open_pct 为 None（昨收缺等）时无法判强弱，不在竞价段冒进 → 走弃（不臆造方向）。
        if op is None:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason="CHASE_AUCTION_STRONG弃:open_pct不可得无法判竞价方向",
            )
        if thr.abandon_pct is not None and op < thr.abandon_pct:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_AUCTION_STRONG弃:弱开 open_pct={op} < abandon={thr.abandon_pct}",
            )

        # —— 弃条件 2：高开走低 / 诱多（重心趋势向下）→ 放弃。——
        if snap.centroid_trend == CentroidTrend.DOWN:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_AUCTION_STRONG弃:高开走低诱多 centroid_trend={snap.centroid_trend}",
            )

        # —— 弃条件 3：未达强开档（open_pct 未超 overheat 高开线）→ 不算强开，放弃。——
        if thr.overheat_pct is not None and op < thr.overheat_pct:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_AUCTION_STRONG弃:未达强开档 open_pct={op} < overheat={thr.overheat_pct}",
            )

        # —— 弃条件 4：竞价量能比未达标 → 承接不足，放弃。——
        ratio = snap.auction_vol_ratio
        if ratio is None or ratio < _VOL_RATIO_MIN:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_AUCTION_STRONG弃:量能不足 auction_vol_ratio={ratio}",
            )

        # —— 弃条件 5：越竞越强需 centroid_trend=UP，否则（FLAT/数据不足）不追。——
        if snap.centroid_trend != CentroidTrend.UP:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_AUCTION_STRONG弃:非越竞越强 centroid_trend={snap.centroid_trend}",
            )

        # —— 买条件全满足：强开 + 放量 + 越竞越强 → 9:20 前挂竞价买单。——
        # 限价取末帧虚拟成交价（竞价价），缺失则回退合理上沿，再缺回退涨停价（均不臆造）；
        # 并以涨停价封顶（评审 P1#3）：避免脏数据/逼近涨停时挂出超涨停价的废单/异常成交。
        limit = _cap_to_limit_up(
            snap.last_price or plan.reasonable_open_high or plan.limit_up_price,
            plan.limit_up_price,
        )
        return StrategyOutcome(
            action=EntryAction.CHASE_AUCTION_STRONG,
            limit_price=limit,
            reason=(
                f"CHASE_AUCTION_STRONG买:强开越竞越强 open_pct={op}≥overheat={thr.overheat_pct} "
                f"vol_ratio={ratio} centroid_trend=UP 竞价价={limit}"
            ),
            order_phase=OrderPhase.AUCTION,
        )
