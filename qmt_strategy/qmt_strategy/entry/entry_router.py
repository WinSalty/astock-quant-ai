"""建仓决策路由 EntryRouter（§4.2 / §4.6 / §4.8）。

业务意图：把每只计划股按 (strategy_family, setup) 路由到一种建仓策略，结合 auction_poller
推来的 AuctionSnapshot 因子做「如果…就买 / 弃」，产出建仓决策 EntryDecision。
**只出决策、不下单**（不碰 xttrader）；SKIP 也落决策台账 / 日志留痕（§4.2）。

幂等（§4.2 末）：每只票在建仓窗口内只产一次有效 BUY；已 BUY 后再推帧 → on_auction_snapshot
返回 None，不重复路由（撤改归 order_executor）。

阈值统一对接 settings（弱开 / 低吸 / 高开超），缺省回退 plan.reasonable_open_low/high，
不在执行侧臆造（口径见 strategies.base.resolve_thresholds）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..common import buy_prefilter
from ..common.universe_filter import is_st_stock
from ..config.settings import Settings
from ..contracts.enums import EntryAction, OrderPhase, TradeSide
from ..contracts.models import AuctionSnapshot, EntryDecision, PlanRow
from ..contracts.protocols import Clock, StructLogger
from .strategies import base as strategy_base

# —— 触发各策略注册：import 即把五类策略类登记进 strategies.base._REGISTRY（§4.1 注册机制）——
# 仅为副作用导入（注册），不直接引用类名，故标注 noqa 语义：保留 import 不可删。
from .strategies import chase_limit_up as _chase_limit_up  # noqa: F401
from .strategies import chase_auction_strong as _chase_auction_strong  # noqa: F401
from .strategies import dip_buy_ma as _dip_buy_ma  # noqa: F401
from .strategies import leader_pullback as _leader_pullback  # noqa: F401
from .strategies import skip as _skip  # noqa: F401

# 仓位测算回调：给定 (plan, limit_price) 返回计划股数；为 None 则交由 order_executor 按账户算。
PositionSizer = Callable[[PlanRow, Decimal], int]

# —— 信号侧 strategy_family 英文枚举 → 中文战法词 归一表（评审 P0-A1 修复）——
# 业务背景：信号侧 limit_up_leader_scoring_service 产出的 strategy_family 是英文枚举
# DABAN/BANLU/DIXI（原样经 HTTP 契约透传到执行侧），而本路由历史实现只按「中文关键词包含」匹配。
# 二者取值域不一致 → 英文枚举对任何中文关键词都不命中 → 所有计划票一律落 SKIP、永不开新仓
# （执行侧建仓闭环空转）。这里把英文枚举先归一为中文战法词，再与 setup 内嵌的中文战法词
# （信号侧 setup 形如「首板打板/连板半路/高位低吸」）统一口径后路由。
# 边界：半路族(BANLU)当前无独立执行策略，归入「打板族」由 chase 策略的顶板/强开买条件二次把关
#       （未顶板/未强开则各策略自行判弃，故归类保守、不会凭空追买）；如需独立半路战法须新增策略并在此调整。
_FAMILY_EN_TO_CN = {
    "DABAN": "打板",
    "BANLU": "半路",
    "DIXI": "低吸",
}


def _normalize_family(family_raw: str) -> str:
    """把信号侧英文枚举 strategy_family 归一为中文战法词；非枚举值（中文/自由文本）原样返回。

    口径：仅对精确等于 DABAN/BANLU/DIXI（忽略大小写与首尾空白）的值做翻译，避免误伤含这些子串的
    自由文本；中文输入（旧口径/单测）原样透传，保证向后兼容。
    """
    key = (family_raw or "").strip().upper()
    return _FAMILY_EN_TO_CN.get(key, family_raw)


# —— (strategy_family, setup) → action 路由表（§4.2 路由维度）——
# 业务口径：先按战法大类 + 形态精确匹配；未命中再按战法大类粗匹配（_FAMILY_DEFAULT）。
# 键用「关键词包含」匹配（信号侧用语可能带前后缀），故存为关键词对而非精确等值。
def _family_of(plan: PlanRow) -> str:
    """战法大类：优先 strategy_family，缺则回退 strategy 原值（信号侧未归类时的兜底）。"""
    return (plan.strategy_family or plan.strategy or "").strip()


def _setup_of(plan: PlanRow) -> str:
    """技术形态：取 setup 原值（可能为空，由路由表按家族缺省兜底）。"""
    return (plan.setup or "").strip()


class EntryRouter:
    """建仓决策路由器（§4.8 方法签名）。

    依赖全部经协议 / 注入，便于单测用 fake：
      - settings：竞价阈值与 market_state_block 闸门。
      - clock：取 decided_at（UTC naive，禁直接 datetime.now）。
      - logger：SKIP / BUY 决策留痕（decision_log 缺省时的留痕出口）。
      - decision_log：本地决策台账（可选）；提供则 append 每条决策（含 SKIP），供闭环归因对照。
      - position_sizer：仓位测算回调（可选）；提供则填 plan_volume，否则 None（交 order_executor）。
    """

    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        logger: StructLogger,
        decision_log: Optional[List[EntryDecision]] = None,
        position_sizer: Optional[PositionSizer] = None,
    ):
        self._settings = settings
        self._clock = clock
        self._logger = logger
        # decision_log：外部可传入一个有 append 的容器（list 或自定义台账）；缺省 None 时只走 logger。
        self._decision_log = decision_log
        self._position_sizer = position_sizer
        # 已产出有效 BUY 的 ts_code 集合（幂等：同票不再重复路由，§4.2 末）。
        self._decided: Set[str] = set()
        # 上次留痕的 action（按 ts_code）：竞价段每秒一帧、且 SKIP/竞价择时关-release 的票每帧重路由，
        # 若每帧都 _record 会让 decision_log 对同一票同一决策重复写数百行、污染闭环归因（复审 P2-2）。
        # 仅当某票的决策 action 相对上一帧【发生变化】时才留痕，去抖重复帧。
        self._last_recorded_action: Dict[str, EntryAction] = {}

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def on_auction_snapshot(self, snap: AuctionSnapshot, plan: PlanRow) -> Optional[EntryDecision]:
        """接收 auction_poller 每帧快照（§3.6 push_to_router → 本入口）。

        幂等：该 ts_code 已产出有效 BUY → 返回 None，不重复路由（§4.2 末）。
        否则走 route 得决策；BUY 决策把 ts_code 记入 _decided（后续帧不再路由）。
        """
        # —— 幂等闸门：已 BUY 的票直接短路，不再消费后续帧。——
        if plan.ts_code in self._decided:
            return None

        decision = self.route(plan, snap)
        # defer：策略本帧推迟决策（如竞价早期瞬时丢帧）→ 不留痕、不锁幂等，等后续帧重评（评审 medium#4）。
        if decision is None:
            return None
        # 落决策留痕（BUY 与 SKIP 都留），供闭环归因对照 qmt_order / qmt_trade。
        self._record(decision)
        # 仅「有效 BUY」（非 SKIP）才登记幂等集合；SKIP 不登记，允许后续帧因子转强后再判。
        if decision.action != EntryAction.SKIP:
            self._decided.add(plan.ts_code)
        return decision

    def release(self, ts_code: str) -> None:
        """解除某票的幂等锁（评审二轮 P1#16）。

        背景：竞价择时默认关时，竞价段(AUCTION)产出的 BUY 决策被编排层「只采集不下单」整类丢弃，但此票已被
        on_auction_snapshot 锁进 _decided → 后续帧（尤其定盘段 SETTLED）不再路由 → 最强龙头(秒封一字/竞价强开)
        在默认生产配置下永远买不进。编排层在「丢弃竞价决策」后调用本方法解锁，使后续帧能重新评估为 OPENING
        决策（定盘/开盘后追）真正成交。
        """
        self._decided.discard(ts_code)

    def route(self, plan: PlanRow, snap: AuctionSnapshot) -> Optional[EntryDecision]:
        """路由主逻辑（§4.8）：先闸门 _should_skip，否则按 (family, setup) 选策略得决策。

        返回 None 表示「本帧推迟决策」（defer，策略要求等后续帧），由调用方不留痕、不锁幂等。
        """
        # —— 第一道闸门：market_state（含缺失保守）/ tradable_flag → SKIP（§4.2 第 5 类）——
        skip, skip_reason = self._should_skip(plan, snap)
        if skip:
            return self._build_decision(
                plan, snap, action=EntryAction.SKIP, limit_price=None,
                reason=skip_reason, order_phase=OrderPhase.OPENING,
            )

        # —— 选策略：按 (family, setup) 推 action。——
        action = self._select_action(plan)

        # —— 因子全面走弱闸门：仅对「打板 / 竞价强开」族适用（依赖高开 + 顶板）。——
        # 低吸（DIP_BUY_MA）的合法入场恰是平开 / 微跌回踩（open_pct 可能 ≤0、不顶板、重心非 UP），
        # 龙回头同理，故全局「全面走弱即 SKIP」会误杀低吸 / 龙回头候选（评审 medium#5）；
        # 据 §4.2 第 5 类「且策略要求收手」口径，该闸门只对强势追买族生效，低吸 / 龙回头由各自策略判弃。
        if action in (EntryAction.CHASE_LIMIT_UP, EntryAction.CHASE_AUCTION_STRONG):
            weak, weak_reason = self._all_factors_weak(snap)
            if weak:
                return self._build_decision(
                    plan, snap, action=EntryAction.SKIP, limit_price=None,
                    reason=weak_reason, order_phase=OrderPhase.OPENING,
                )

        strategy = strategy_base.get_strategy(action)
        outcome = strategy.decide(plan, snap, self._settings)

        # defer：策略推迟决策（如竞价早期瞬时丢帧）→ 返回 None，调用方据此不留痕、不锁幂等（medium#4）。
        if getattr(outcome, "defer", False):
            return None

        # 组装 EntryDecision：策略产物 + 计划行透传 + 仓位测算。
        return self._build_decision(
            plan, snap,
            action=outcome.action,
            limit_price=outcome.limit_price,
            reason=outcome.reason,
            order_phase=outcome.order_phase,
        )

    def _should_skip(self, plan: PlanRow, snap: AuctionSnapshot) -> Tuple[bool, str]:
        """全局建仓闸门（§4.8）：命中即放弃、不下单只留痕。与具体战法无关的硬闸门。

        闸门项（§4.2 第 5 类 / §4.4 风控）：
          - market_state ∈ settings.market_state_block（退潮 / 冰点 / 空仓）→ skip；
            market_state 缺失（None）→ 按空仓保守处理 skip（与 watchlist_loader._resolve_open_gate
            同口径，避免缺失态被当作允许开仓，评审 medium#6）。
          - tradable_flag 明确为 False（不可参与）→ skip。
        说明：「因子全面走弱」不在此全局闸门，已下放到 route() 中仅对强势追买族生效（评审 medium#5）。
        """
        # —— 闸门 0：买入前置过滤层（doc/18 第 2 层，优先级最高）——
        # 统一委托 buy_prefilter 跑禁买硬规则集（ST/退市整理 + 四板及以上）：命中任一即 SKIP，绝不产 BUY 决策。
        # ST 口径不变（显式 is_st=True 或证券名含 ST/退）；四板及以上按 board_level>=阈值 或 tier==HIGH_BOARD 兜底。
        # 与 loader（第 1 层前置过滤）、order_executor.place（第 3 层）叠加，确保 ST 与四板及以上标的零买单。
        verdict = buy_prefilter.evaluate(
            buy_prefilter.CandidateView(
                ts_code=plan.ts_code,
                name=plan.name,
                is_st=plan.is_st,
                board_level=plan.board_level,
                tier=plan.tier,
            ),
            high_board_min_level=self._settings.forbid_board_level_min,
        )
        if not verdict.allowed:
            return True, f"SKIP闸门:{verdict.reason}"

        # —— 闸门 1：情绪周期禁开仓（对接信号侧「退潮 / 冰点不得激进接力」）；缺失按空仓保守禁开。——
        block = set(self._settings.market_state_block or [])
        if plan.market_state is None:
            return True, "SKIP闸门:market_state缺失→按空仓保守禁开新仓"
        if plan.market_state in block:
            return True, f"SKIP闸门:market_state={plan.market_state}∈禁开仓集合{sorted(block)}"

        # —— 闸门 2：可成交性标记为 False（信号侧标不可参与）——
        if plan.tradable_flag is False:
            return True, "SKIP闸门:tradable_flag=False不可参与"

        return False, ""

    def _all_factors_weak(self, snap: AuctionSnapshot) -> Tuple[bool, str]:
        """因子全面走弱判定（仅供强势追买族 route 调用，§4.2 第 5 类「策略要求收手」）。

        全面走弱 = 无竞价高开正幅（open_pct ≤ 0）且未顶板（非一字/秒封）且重心非向上。
        边界：open_pct 缺失（降级）不单独触发（降级由各策略自行改判 OPENING），仅三项均明确走弱才判。
        """
        op_weak = snap.open_pct is not None and snap.open_pct <= 0
        not_limit_up = not snap.is_limit_up
        trend_not_up = snap.centroid_trend.value != "UP"
        if op_weak and not_limit_up and trend_not_up:
            return True, (
                f"SKIP闸门:因子全面走弱 open_pct={snap.open_pct} "
                f"is_limit_up={snap.is_limit_up} centroid_trend={snap.centroid_trend}"
            )
        return False, ""

    # ------------------------------------------------------------------
    # 内部：路由表 / 决策组装 / 留痕
    # ------------------------------------------------------------------
    def _select_action(self, plan: PlanRow) -> EntryAction:
        """按 (family, setup) 推建仓 action（§4.2 路由维度）。

        口径（关键词包含匹配，兼容信号侧用语带前后缀）：
          - 打板 / 半路 / 连板接力 → CHASE_LIMIT_UP；打板 + 竞价 / 首板（竞价定方向）→ CHASE_AUCTION_STRONG。
          - 低吸 / 均线 / 回踩 → DIP_BUY_MA。
          - 龙回头 / 龙头 + 高位回踩 / 分歧转一致 → LEADER_PULLBACK。
          - 未匹配任何战法 → SKIP（不臆造动作）。

        评审修复：
          - P0-A1：family 先经 _normalize_family 把英文枚举(DABAN/BANLU/DIXI)归一为中文战法词，
            否则信号侧英文枚举对中文关键词全不命中 → 全部 SKIP；同时把 setup 内嵌的中文战法词
            （首板打板/连板半路/高位低吸…）并入匹配源 hay，使 family 与 setup 互为兜底。
          - P2(路由「板」过宽)：去掉裸 `"板" in family` 兜底，改用受控词表(打板/连板/半路)，
            避免「板块轮动」等含「板」自由文本被误路由到打板跟买。
        """
        # family 归一：英文枚举→中文战法词；中文/自由文本原样保留（向后兼容旧口径与单测）。
        family = _normalize_family(_family_of(plan))
        setup = _setup_of(plan)
        # 匹配源：归一后的战法词 + setup（信号侧 setup 已含中文战法词，二者互为补充/兜底）。
        hay = f"{family} {setup}"

        # —— 低吸族 ——
        if ("低吸" in hay) or ("均线" in setup) or ("回踩" in setup and "龙" not in hay):
            return EntryAction.DIP_BUY_MA

        # —— 龙回头族 ——（角色类，按 family/ setup 的龙头语义匹配）
        if ("龙回头" in family) or ("龙" in family and ("高位回踩" in setup or "分歧" in setup)):
            return EntryAction.LEADER_PULLBACK

        # —— 打板 / 半路族：竞价定方向（首板 / 竞价）走竞价强开追，连板接力 / 半路走打板跟买。——
        # 受控词表（不含裸「板」）：打板 / 连板 / 半路。
        if ("打板" in hay) or ("连板" in hay) or ("半路" in hay):
            # 竞价强开追(挂竞价单)仅适用【打板/连板族】的竞价定方向(首板/竞价)；半路族(BANLU)的「首板半路」
            # 不应被误路由到竞价强开追挂竞价单(评审二轮 P2#43：超出半路战法语义)，半路一律走盘中打板跟买。
            is_daban = ("打板" in family) or ("打板" in setup) or ("连板" in hay)
            if is_daban and ("竞价" in setup or "首板" in setup):
                return EntryAction.CHASE_AUCTION_STRONG
            # 连板接力 / 半路 / 其余打板形态 → 打板跟买。
            return EntryAction.CHASE_LIMIT_UP

        # —— 未匹配战法：不臆造动作，直接放弃。——
        return EntryAction.SKIP

    def _build_decision(
        self,
        plan: PlanRow,
        snap: AuctionSnapshot,
        *,
        action: EntryAction,
        limit_price: Optional[Decimal],
        reason: str,
        order_phase: OrderPhase,
    ) -> EntryDecision:
        """组装 EntryDecision（§4.3）：补 ts_code / 日期 / plan_volume / decided_at / factors_snapshot。"""
        # 计划股数：position_sizer 给则用（需有限价才能按金额折股数），否则 None（交 order_executor）。
        plan_volume: Optional[int] = None
        if action != EntryAction.SKIP and self._position_sizer is not None and limit_price is not None:
            plan_volume = self._position_sizer(plan, limit_price)

        # decided_at 一律取注入时钟的 UTC naive（禁直接 datetime.now，§6.6）。
        decided_at = self._clock.now_utc()

        return EntryDecision(
            ts_code=plan.ts_code,
            signal_trade_date=plan.signal_trade_date,
            target_trade_date=plan.target_trade_date,
            strategy_family=_family_of(plan) or "UNKNOWN",
            setup=_setup_of(plan) or "UNKNOWN",
            action=action,
            decided_at=decided_at,
            reason=reason,
            side=TradeSide.BUY,
            limit_price=limit_price,
            plan_volume=plan_volume,
            order_phase=order_phase,
            factors_snapshot=self._snapshot_factors(snap),
            # 禁买 ST 硬规则第 3 层供数：把统一口径 ST 判定算定并锚到决策上，供唯一下单点 place 做最终拒单兜底
            # （即便决策因某路径未经本 _should_skip 而携 BUY，order_executor 仍据此拒发买单）。
            is_st=is_st_stock(plan.is_st, plan.name),
            # 禁买四板及以上硬规则第 3 层供数（doc/18）：把连板维度锚到决策上，供 place 经 buy_prefilter 复核四板及以上。
            board_level=plan.board_level,
            tier=plan.tier,
        )

    def _snapshot_factors(self, snap: AuctionSnapshot) -> Dict[str, object]:
        """决策时的关键因子留痕（§4.3 factors_snapshot）。

        只存复盘归因需要的关键字段（价位转 str 保留 Decimal 精度，避免 JSON 落库丢精度）。
        """
        def _s(v: object) -> object:
            return str(v) if isinstance(v, Decimal) else v

        return {
            "phase": snap.phase.value,
            "ts": snap.ts.isoformat() if snap.ts is not None else None,
            "open_pct": _s(snap.open_pct),
            "auction_vol_ratio": _s(snap.auction_vol_ratio),
            "auction_centroid": _s(snap.auction_centroid),
            "centroid_trend": snap.centroid_trend.value,
            "virtual_seal_amount": _s(snap.virtual_seal_amount),
            "seal_to_float_ratio": _s(snap.seal_to_float_ratio),
            "is_limit_up": snap.is_limit_up,
            "last_price": _s(snap.last_price),
            "pre_close": _s(snap.pre_close),
            "data_quality": list(snap.data_quality or []),
            "tick_seq": snap.tick_seq,
        }

    def _record(self, decision: EntryDecision) -> None:
        """落决策留痕（§4.2 SKIP / BUY 均留痕）。

        decision_log 提供则 append（供闭环归因「为何买 / 弃」事实源）；
        无论是否有 decision_log，都写一条结构化日志（不写敏感信息）。
        去抖（复审 P2-2）：同一票相对上一帧 action 未变（如竞价段连续多帧同为 SKIP / 同一 BUY 被反复
        release 重路由）→ 不重复留痕，避免 decision_log 对同一决策写入数百行污染归因；action 变化才记。
        """
        if self._last_recorded_action.get(decision.ts_code) == decision.action:
            return
        self._last_recorded_action[decision.ts_code] = decision.action
        if self._decision_log is not None:
            self._decision_log.append(decision)
        # 日志事件：BUY 与 SKIP 分流，便于检索「未下单 N−M」口径。
        event = "entry_decision_skip" if decision.action == EntryAction.SKIP else "entry_decision_buy"
        self._logger.info(
            event,
            ts_code=decision.ts_code,
            action=decision.action.value,
            order_phase=decision.order_phase.value,
            limit_price=str(decision.limit_price) if decision.limit_price is not None else None,
            reason=decision.reason,
        )
