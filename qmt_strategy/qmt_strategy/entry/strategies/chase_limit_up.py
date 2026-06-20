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
from ...contracts.enums import EntryAction
from ...contracts.models import AuctionSnapshot, PlanRow
from .base import EntryStrategy, StrategyOutcome, order_phase_for, prior_gate_reason, register


@register(EntryAction.CHASE_LIMIT_UP)
class ChaseLimitUpStrategy(EntryStrategy):
    """打板跟买：顶板封单稳 → 挂涨停价；炸板 / 封单减 → 弃。"""

    def decide(self, plan: PlanRow, snap: AuctionSnapshot, settings: Settings) -> StrategyOutcome:
        # —— 先验闸门（评审 P1#1）：龙头强度不足 / 续板概率弱 → 收手，不追高接最后一棒。——
        gate = prior_gate_reason(plan, settings)
        if gate is not None:
            return StrategyOutcome(action=EntryAction.SKIP, reason=f"CHASE_LIMIT_UP弃:{gate}")

        # —— 打板因子 E2（默认即生效·起步值，env 可覆盖；settings 阈值与 plan 因子双守卫，缺数据/老契约零误杀）——
        # 反复炸板(烂板)：open_times >= 阈值 → 弃（封板质量差、追板易接最后一棒）。
        if (
            settings.forbid_open_times_max is not None
            and plan.open_times is not None
            and plan.open_times >= settings.forbid_open_times_max
        ):
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_LIMIT_UP弃:反复炸板 open_times={plan.open_times}>={settings.forbid_open_times_max}",
            )
        # 高位规避：近 5 日涨幅 >= 阈值 → 弃（空间透支、追高风险，手册首要风险闸）。
        if (
            settings.high_return_pct_limit is not None
            and plan.return_5d_pct is not None
            and plan.return_5d_pct >= settings.high_return_pct_limit
        ):
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_LIMIT_UP弃:高位 return_5d_pct={plan.return_5d_pct}>={settings.high_return_pct_limit}",
            )

        # —— 弃条件 1：未顶板（虚拟成交价未达涨停价）→ 无板可打，放弃。——
        if not snap.is_limit_up:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_LIMIT_UP弃:未顶板 is_limit_up={snap.is_limit_up}",
            )

        # —— 弃条件 2：封单不稳（封流比有值但低于下限）→ 视为炸板 / 封单快速减小风险，放弃（评审二轮 P2#41）。——
        # 边界：封流比为 None（达涨停但买一量缺、或市值缺）时不据此否决，避免数据缺口误杀强势板。
        # 下限取 settings.seal_ratio_min（默认 0=关闭，目标机实测 bidVol 量纲后再配正阈值如 0.005，见 settings.py 注释；
        # 评审 doc/21 P2/P3/P4 复审订正：此处原注释「默认 0.005」与实际默认 0 不符，已对齐）。默认 0 时该护栏不触发。
        ratio = snap.seal_to_float_ratio
        if ratio is not None and ratio < settings.seal_ratio_min:
            return StrategyOutcome(
                action=EntryAction.SKIP,
                reason=f"CHASE_LIMIT_UP弃:封单不稳 seal_to_float_ratio={ratio} < {settings.seal_ratio_min}",
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
            # 下单时段按快照 phase 判定（评审 P1#4 / 二轮 #17）：竞价即封→AUCTION 竞价单；9:25 后盘中顶板→OPENING。
            order_phase=order_phase_for(snap),
        )
