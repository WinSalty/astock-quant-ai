"""建仓策略接口与阈值口径（§4.1 / §4.2）。

业务意图：
- 把「按 action 判定如果…就买 / 弃」的逻辑抽象为统一策略接口 ``EntryStrategy``，每类 action
  一个实现文件（chase_limit_up / chase_auction_strong / dip_buy_ma / skip；leader_pullback 已随 J-2 弃用删除），
  便于单测与扩展（§4.1 各策略一文件）。
- 提供注册 / 选择机制（``register`` 装饰器 + ``get_strategy``），entry_router 据
  (strategy_family, setup) 推出 action 后，按 action 取对应策略实例。
- 统一竞价阈值取数口径（``resolve_thresholds``）：弱开 / 低吸档 / 高开超 三档阈值一律对接
  settings（auction_abandon_pct / auction_lowbuy_pct_low/high / auction_overheat_pct），
  缺省时回退用 plan.reasonable_open_low/high 折算的百分比；执行侧绝不臆造阈值（§4.2）。

返回口径：策略 ``decide`` 返回 ``StrategyOutcome(action, limit_price, reason, order_phase)``，
由 entry_router 组装成 EntryDecision（补 ts_code / 日期 / plan_volume / decided_at /
factors_snapshot 等透传字段）。策略本身不产 EntryDecision，便于纯函数式单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from typing import Callable, Dict, Optional, Type

from ...config.settings import Settings
from ...contracts.enums import AuctionPhase, CentroidTrend, EntryAction, OrderPhase
from ...contracts.models import AuctionSnapshot, PlanRow

# 竞价单时段（定盘前可挂竞价单）：< 9:25。定盘/盘中(>=9:25)一律走 OPENING 开盘后单。
_AUCTION_ORDER_PHASES = (
    AuctionPhase.PRE_AUCTION,
    AuctionPhase.AUCTION_CANCELABLE,
    AuctionPhase.AUCTION_LOCKED,
)


def order_phase_for(snap: AuctionSnapshot) -> OrderPhase:
    """按快照所处时段判定下单时段（评审二轮 P1#17）。

    定盘前(<9:25, PRE/CANCELABLE/LOCKED)→AUCTION 竞价单；定盘及以后(>=9:25, SETTLED/CLOSED)→OPENING 开盘后单。
    原 chase_auction_strong 买分支硬编码 order_phase=AUCTION：在定盘段(9:25–9:30)仍按竞价单提交，
    其 TTL 立即过期(竞价单 TTL 截止=9:25)→下单即被撤的废单 + 白占配额；改用本函数按时段动态判定。
    """
    return OrderPhase.AUCTION if snap.phase in _AUCTION_ORDER_PHASES else OrderPhase.OPENING

# 竞价整体不可得（降级 B）的 data_quality 标记码。
# 口径与 auction.auction_factors.DQ_NO_TICK 一致（值 "NO_TICK"）：整帧 tick 缺失即降级 B。
# 此处本地常量声明（不 import 前序 auction 层），避免跨业务模块耦合，只认这一字符串契约。
DQ_NO_TICK = "NO_TICK"


@dataclass(frozen=True)
class StrategyOutcome:
    """单个策略的判定产物（§4.2 买 / 弃）。

    - action：命中买条件→对应 BUY 类 action；命中弃条件→SKIP。
    - limit_price：BUY 的计划限价（涨停价 / 低吸价 / 竞价价）；SKIP / 缺价为 None。
    - reason：买 / 弃理由（含命中的关键因子值，供复盘归因）。
    - order_phase：AUCTION（竞价单，9:20 前）/ OPENING（开盘后追）；默认沿用策略语义。
    """

    action: EntryAction
    reason: str
    limit_price: Optional[Decimal] = None
    order_phase: OrderPhase = OrderPhase.OPENING
    # defer：本帧暂不决策（如竞价早期瞬时丢帧），entry_router 据此返回 None——不留痕、不锁幂等，
    # 等后续帧重评，避免单帧抖动永久污染当日决策（评审 medium#4）。
    defer: bool = False


@dataclass(frozen=True)
class AuctionThresholds:
    """竞价三档阈值（均为「相对昨收的涨跌幅」小数，如 0.03 表示 +3%）。

    口径（§4.2 / 信号侧竞价观察清单 3.2.2）：
    - abandon_pct：弱开放弃线，open_pct 弱于此值（低于）即视为弱开。
    - lowbuy_low / lowbuy_high：低吸档区间 [low, high]，open_pct 落区间内为低吸候选。
    - overheat_pct：高开超线，open_pct 高于此值视为高开超（追高警惕 / 强开档下沿）。

    缺省回退：settings 未配置某档时，用 plan.reasonable_open_low/high 折算昨收百分比兜底
    （reasonable_open_* 是信号侧给的合理高开绝对价位，除以昨收减 1 即得百分比口径）。
    """

    abandon_pct: Optional[Decimal] = None
    lowbuy_low: Optional[Decimal] = None
    lowbuy_high: Optional[Decimal] = None
    overheat_pct: Optional[Decimal] = None


def _pre_close_of(plan: PlanRow, snap: AuctionSnapshot) -> Optional[Decimal]:
    """取昨收基准：优先用快照 pre_close（实时帧权威），缺则无法折算回退百分比。"""
    if snap.pre_close is not None and snap.pre_close != 0:
        return snap.pre_close
    return None


def _abs_price_to_pct(price: Optional[Decimal], pre_close: Optional[Decimal]) -> Optional[Decimal]:
    """把信号侧给的合理高开绝对价位折算成相对昨收的涨跌幅百分比（兜底口径）。

    边界：价位或昨收缺失 / 昨收为 0 → 返回 None（无法折算，该档阈值视为未知）。
    """
    if price is None or pre_close is None or pre_close == 0:
        return None
    return (price - pre_close) / pre_close


def resolve_thresholds(plan: PlanRow, snap: AuctionSnapshot, settings: Settings) -> AuctionThresholds:
    """统一解析竞价三档阈值（§4.2 阈值对接，不在执行侧臆造）。

    优先级：settings 显式配置 > plan.reasonable_open_low/high 折算回退 > None（未知，下游按缺失处理）。
    - abandon_pct 缺省回退：reasonable_open_low 折算的百分比（弱于合理下沿即弱开放弃；供强势追买族用）。
    - overheat_pct 缺省回退：reasonable_open_high 折算的百分比（高开超出合理上沿即追高警惕；供强势追买族用）。
    - lowbuy_low/high：**缺省不回退**（评审 F18）。原实现把信号侧「合理高开区间」(reasonable_open_low/high，
      均高于昨收) 折算成低吸档 → 低吸档被抬成 [+2%,+8%] 高开区间：真低吸点(平开/微跌 open_pct≤0)被判「跌破支撑」
      弃、反在 +2%~+8% 高开位才买，低吸族买卖方向相反。低吸档与高开区间语义相反、绝不可互相套用——缺显式配置时
      留 None，由 dip_buy_ma fail-closed（不臆造低吸档、宁可不开仓）。
    """
    pre_close = _pre_close_of(plan, snap)
    low_pct = _abs_price_to_pct(plan.reasonable_open_low, pre_close)
    high_pct = _abs_price_to_pct(plan.reasonable_open_high, pre_close)
    return AuctionThresholds(
        # 弱开放弃线：配置优先，否则回退合理下沿折算（弱于合理下沿即弱开）。
        abandon_pct=settings.auction_abandon_pct if settings.auction_abandon_pct is not None else low_pct,
        # 低吸档下沿/上沿：仅取显式配置，缺省 None（绝不回退高开区间，见上方 F18 说明）。
        lowbuy_low=settings.auction_lowbuy_pct_low,
        lowbuy_high=settings.auction_lowbuy_pct_high,
        # 高开超线：配置优先，否则回退合理上沿折算。
        overheat_pct=settings.auction_overheat_pct if settings.auction_overheat_pct is not None else high_pct,
    )


def is_auction_degraded(snap: AuctionSnapshot) -> bool:
    """判定该帧是否处于「竞价数据整体不可得」（降级 B，§4.6）。

    口径：data_quality 含 NO_TICK（整帧 tick 缺失）→ 竞价因子整体不可得，
    CHASE_AUCTION_STRONG 据此自动改判 OPENING（开盘后追），不在竞价段下竞价单。
    """
    return DQ_NO_TICK in (snap.data_quality or [])


# 追买类策略的续板概率先验下限【默认值】（doc/29 J-1）：现已收口为可配置 settings.prior_continuation_min，
# 本常量仅作 settings 缺省。口径=信号侧续板「中/低档分界」：continuation_prob 由信号侧 _PROB_BAND_TO_DECIMAL 按
# tier 把高/中/低/极低映射为数值下发，各 tier 中档≥0.35、低档≤0.25，故 0.3 恰拒「低/极低」档、放行「中/高」档
# （显式按档位分界判定；买卖行为与历史硬编码 0.3 等价）。档值表变动时改 QMT_PRIOR_CONTINUATION_MIN 即可。
_PRIOR_CONTINUATION_MIN = Decimal("0.3")


def _parse_hms(s: Optional[str]) -> Optional[time]:
    """把 'HH:MM' 或 'HH:MM:SS' 文本解析为 time（东八区时刻，仅用于同区先后比较，不参与 UTC 下注体系）。

    用于打板因子 E2：比较 plan.first_limit_time（HH:MM:SS）与配置 deadline（HH:MM）。
    边界（fail-open）：空/None/格式非法 → 返回 None；调用方对 None 一律不触发弃，绝不因脏时刻误杀强票。
    """
    if not s:
        return None
    try:
        parts = [int(x) for x in str(s).split(":")]
    except (ValueError, TypeError):
        return None
    if len(parts) == 2:
        try:
            return time(parts[0], parts[1])
        except ValueError:
            return None
    if len(parts) >= 3:
        try:
            return time(parts[0], parts[1], parts[2])
        except ValueError:
            return None
    return None


def prior_gate_reason(plan: PlanRow, settings: Settings) -> Optional[str]:
    """追买类策略的信号侧先验闸门（评审 P1#1）。

    业务意图：让最激进的两类追买（CHASE_LIMIT_UP / CHASE_AUCTION_STRONG）真正消费信号侧龙头增强层
    产出的「强度 leader_strength_score / 续板概率 continuation_prob」先验，避免对低强度、续板预期弱
    的票照样挂涨停价追高、接最后一棒——原实现这两类策略完全不读先验，龙头增强 alpha 在最激进路径
    被旁路。
    口径（仅在先验【有值】时据其否决；缺数据不在此误杀，交由各策略盘口条件把关，与安全默认
    「不凭空冻结」一致）：
    - leader_strength_score 有值且 < settings.leader_strength_min → 收手；
    - continuation_prob 有值且 < 续板下限 → 收手。
    返回否决原因（命中即 SKIP）；无否决返回 None。

    例外（doc/29 B2）：data_missing=True 是信号侧【约定核心交易指标缺测】的显式 sentinel，须 fail-closed 直接收手
    ——与「普通降级 null（仅个别先验字段缺、按盘口把关 fail-open）」严格区分。缺测票绝不追买（最保守口径），
    且 buy_prefilter 闸门已先于本闸门拦截，这里是策略侧冗余防线。
    """
    if plan.data_missing:
        return "先验闸门:核心交易指标缺测(data_missing)→放弃买入"
    score = plan.leader_strength_score
    strength_min = settings.leader_strength_min
    if score is not None and strength_min is not None and score < strength_min:
        return f"先验闸门:龙头强度不足 leader_strength_score={score} < leader_strength_min={strength_min}"
    cont = plan.continuation_prob
    # 续板下限取 settings（doc/29 J-1，可配 QMT_PRIOR_CONTINUATION_MIN）：缺省回落 _PRIOR_CONTINUATION_MIN(0.3)，
    # 即信号侧续板「中/低档分界」——拒「低/极低」档、放行「中/高」档（显式按档位分界，买卖行为与历史 0.3 等价）。
    # is-not-None 守卫（与 settings 层同口径）：显式配 0（运维关闭本续板闸：prob 恒≥0、永不 <0）不能被 `or` 当假值
    # 吞成 0.3，否则安全阀关不掉。
    _cont_min = getattr(settings, "prior_continuation_min", None)
    cont_min = _cont_min if _cont_min is not None else _PRIOR_CONTINUATION_MIN
    if cont is not None and cont < cont_min:
        return f"先验闸门:续板概率弱(拒低/极低档) continuation_prob={cont} < {cont_min}"
    return None


class EntryStrategy:
    """建仓策略接口（§4.2）。各 action 一实现，统一 ``decide`` 签名。

    decide(plan, snap, settings) -> StrategyOutcome：
      - 命中买条件 → StrategyOutcome(action=本策略 BUY 类 action, limit_price, reason, order_phase)。
      - 命中弃条件 → StrategyOutcome(action=SKIP, reason)。
    """

    # 该策略负责的 action（子类覆盖），用于注册表与日志留痕。
    action: EntryAction

    def decide(self, plan: PlanRow, snap: AuctionSnapshot, settings: Settings) -> StrategyOutcome:
        raise NotImplementedError


# —— 策略注册表（action → 策略类）：注册 / 选择机制（§4.1）——
_REGISTRY: Dict[EntryAction, Type[EntryStrategy]] = {}


def register(action: EntryAction) -> Callable[[Type[EntryStrategy]], Type[EntryStrategy]]:
    """类装饰器：把策略类按其负责的 action 注册进全局表，供 entry_router 选择。"""

    def _wrap(cls: Type[EntryStrategy]) -> Type[EntryStrategy]:
        cls.action = action
        _REGISTRY[action] = cls
        return cls

    return _wrap


def get_strategy(action: EntryAction) -> EntryStrategy:
    """按 action 取策略实例。未注册 action 视为编码缺陷，直接抛错（不静默放过）。"""
    cls = _REGISTRY.get(action)
    if cls is None:
        raise KeyError(f"未注册的建仓策略 action={action}")
    return cls()
