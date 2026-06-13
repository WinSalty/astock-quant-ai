"""建仓决策路由 entry_router 单测（§4.9 路由 / 降级 / 幂等 + 五类 action 买 / 弃）。

全部用 fake / 内存实现，不连真实 xttrader / xtdata / MySQL：
  - 时钟用 conftest 的 utc_at_east8 构造固定东八区时刻（竞价段 09:16 / 09:18 等）。
  - 计划行用 conftest.make_plan_row 构造，按用例改 strategy_family / setup / 阈值相关字段。
  - 快照用本文件 _snap() 直接构造 AuctionSnapshot（不经 auction_poller，聚焦路由逻辑）。
  - 决策留痕用 list（decision_log）+ RecordingLogger 双重断言「SKIP 也留痕」。

覆盖（与任务「单测必须覆盖」逐条对应）：
  1. 路由：market_state=退潮 → 任意 setup 出 SKIP、不下单但留痕。
  2. 竞价强开命中 → CHASE_AUCTION_STRONG 且 order_phase=AUCTION（9:20 前）。
  3. 竞价不可得（snap 标降级 B / NO_TICK）→ CHASE_AUCTION_STRONG 改 order_phase=OPENING。
  4. 幂等：同 ts_code 已 BUY 后再推帧 → on_auction_snapshot 返回 None。
  5. 五类 action 至少各一条买 / 弃断言。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import List, Optional

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import (
    AuctionPhase,
    CentroidTrend,
    EntryAction,
    OrderPhase,
)
from qmt_strategy.contracts.models import AuctionSnapshot, EntryDecision, PlanRow
from qmt_strategy.entry.entry_router import EntryRouter
from qmt_strategy.entry.strategies.base import DQ_NO_TICK
from tests.conftest import T_BUY, T_SIGNAL, make_plan_row, utc_at_east8


# ---------------------------------------------------------------------------
# 辅助：构造快照 / 路由器
# ---------------------------------------------------------------------------
def _snap(
    ts_code: str = "600000.SH",
    *,
    phase: AuctionPhase = AuctionPhase.AUCTION_CANCELABLE,
    open_pct: Optional[Decimal] = None,
    auction_vol_ratio: Optional[Decimal] = None,
    centroid_trend: CentroidTrend = CentroidTrend.FLAT,
    seal_to_float_ratio: Optional[Decimal] = None,
    is_limit_up: bool = False,
    last_price: Optional[Decimal] = None,
    pre_close: Optional[Decimal] = Decimal("10.00"),
    data_quality: Optional[List[str]] = None,
) -> AuctionSnapshot:
    """构造一帧 AuctionSnapshot（ts 固定在 T+1 买入日 09:16 东八区，竞价可撤段）。"""
    return AuctionSnapshot(
        ts_code=ts_code,
        phase=phase,
        ts=utc_at_east8(T_BUY, 9, 16, 0),
        open_pct=open_pct,
        auction_vol_ratio=auction_vol_ratio,
        centroid_trend=centroid_trend,
        seal_to_float_ratio=seal_to_float_ratio,
        is_limit_up=is_limit_up,
        last_price=last_price,
        pre_close=pre_close,
        data_quality=list(data_quality or []),
    )


def _settings(**overrides) -> Settings:
    """构造一个显式给定竞价阈值的 Settings（避免依赖回退路径，断言更确定）。

    默认：弱开线 +1%、低吸档 [-2%, +1%]、高开超 / 强开下沿 +5%、龙头强度下限 0.5。
    """
    base = dict(
        auction_abandon_pct=Decimal("0.01"),
        auction_lowbuy_pct_low=Decimal("-0.02"),
        auction_lowbuy_pct_high=Decimal("0.01"),
        auction_overheat_pct=Decimal("0.05"),
        leader_strength_min=Decimal("0.5"),
    )
    base.update(overrides)
    return Settings(**base)


def _router(decision_log: Optional[List[EntryDecision]] = None, position_sizer=None, **set_kw):
    """组装 EntryRouter（固定 09:16 竞价段时钟 + RecordingLogger），返回 (router, log, logger)。"""
    clock = FakeClock(utc_at_east8(T_BUY, 9, 16, 0))
    logger = RecordingLogger()
    log: List[EntryDecision] = decision_log if decision_log is not None else []
    router = EntryRouter(
        settings=_settings(**set_kw),
        clock=clock,
        logger=logger,
        decision_log=log,
        position_sizer=position_sizer,
    )
    return router, log, logger


# ---------------------------------------------------------------------------
# 1. 路由闸门：退潮 → 任意 setup 出 SKIP、不下单但留痕
# ---------------------------------------------------------------------------
def test_market_state_ebb_routes_skip_with_trace():
    """market_state=退潮 → 无论何种 setup 都 SKIP，limit_price=None（不下单），但落决策台账 + 日志。"""
    router, log, logger = _router()
    # 即便给一个会触发打板跟买买条件的强势顶板快照，退潮闸门也优先判 SKIP。
    plan = make_plan_row(strategy_family="打板", setup="连板接力", market_state="退潮")
    snap = _snap(is_limit_up=True, last_price=Decimal("11.00"), seal_to_float_ratio=Decimal("0.05"))

    decision = router.on_auction_snapshot(snap, plan)

    assert decision is not None
    assert decision.action == EntryAction.SKIP
    assert decision.limit_price is None          # 不下单：无限价
    assert decision.plan_volume is None
    assert "退潮" in decision.reason
    # 留痕：decision_log 收到一条，日志事件为 skip。
    assert len(log) == 1 and log[0].action == EntryAction.SKIP
    assert "entry_decision_skip" in logger.events()


def test_skip_also_traced_when_no_decision_log():
    """未传 decision_log 时，SKIP 仍走 logger 留痕（§4.2 留痕不依赖台账）。"""
    clock = FakeClock(utc_at_east8(T_BUY, 9, 16, 0))
    logger = RecordingLogger()
    router = EntryRouter(settings=_settings(), clock=clock, logger=logger)
    plan = make_plan_row(market_state="冰点")
    decision = router.route(plan, _snap())
    router._record(decision)  # route 不自动 record，这里显式验证 _record 留痕
    assert decision.action == EntryAction.SKIP
    assert "entry_decision_skip" in logger.events()


# ---------------------------------------------------------------------------
# 2. 竞价强开命中 → CHASE_AUCTION_STRONG 且 order_phase=AUCTION
# ---------------------------------------------------------------------------
def test_chase_auction_strong_buy_auction_phase():
    """强开 + 放量 + 越竞越强 → CHASE_AUCTION_STRONG 买、order_phase=AUCTION（9:20 前竞价单）。"""
    router, log, logger = _router()
    # setup=首板 → 打板族走竞价强开追。
    plan = make_plan_row(strategy_family="打板", setup="首板", market_state="启动")
    snap = _snap(
        open_pct=Decimal("0.06"),            # ≥ overheat 0.05，强开
        auction_vol_ratio=Decimal("0.5"),    # 量能达标
        centroid_trend=CentroidTrend.UP,     # 越竞越强
        last_price=Decimal("10.60"),
    )
    decision = router.on_auction_snapshot(snap, plan)
    assert decision.action == EntryAction.CHASE_AUCTION_STRONG
    assert decision.order_phase == OrderPhase.AUCTION
    assert decision.limit_price == Decimal("10.60")
    assert "entry_decision_buy" in logger.events()


def test_chase_auction_strong_abandon_weak_open():
    """弱开（open_pct 弱于放弃线）→ CHASE_AUCTION_STRONG 弃（SKIP）。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="打板", setup="首板")
    snap = _snap(open_pct=Decimal("0.005"), auction_vol_ratio=Decimal("0.5"), centroid_trend=CentroidTrend.UP)
    decision = router.route(plan, snap)
    assert decision.action == EntryAction.SKIP
    assert "弱开" in decision.reason


def test_chase_auction_strong_abandon_centroid_down():
    """高开但重心走低（诱多）→ CHASE_AUCTION_STRONG 弃。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="打板", setup="首板")
    snap = _snap(open_pct=Decimal("0.06"), auction_vol_ratio=Decimal("0.5"), centroid_trend=CentroidTrend.DOWN)
    decision = router.route(plan, snap)
    assert decision.action == EntryAction.SKIP
    assert "诱多" in decision.reason or "走低" in decision.reason


# ---------------------------------------------------------------------------
# 3. 竞价不可得（降级 B / NO_TICK）→ CHASE_AUCTION_STRONG 改 OPENING
# ---------------------------------------------------------------------------
def test_chase_auction_strong_degraded_b_switch_opening_after_920():
    """9:20 及以后竞价仍整体不可得（NO_TICK）→ 确认降级 B：action 仍 CHASE_AUCTION_STRONG、order_phase 改 OPENING（评审 medium#4）。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="打板", setup="首板", market_state="启动")
    # 降级 B：整帧 tick 缺失，竞价因子全 None，仅标 NO_TICK；phase 已到 9:20 决策时点（LOCKED）。
    snap = _snap(
        phase=AuctionPhase.AUCTION_LOCKED,
        open_pct=None,
        auction_vol_ratio=None,
        centroid_trend=CentroidTrend.FLAT,
        last_price=None,
        pre_close=None,
        data_quality=[DQ_NO_TICK],
    )
    decision = router.on_auction_snapshot(snap, plan)
    assert decision.action == EntryAction.CHASE_AUCTION_STRONG
    assert decision.order_phase == OrderPhase.OPENING   # 不在竞价段下竞价单
    # 限价回退涨停价（plan.limit_up_price 默认 11.00）。
    assert decision.limit_price == Decimal("11.00")
    assert "降级B" in decision.reason or "OPENING" in decision.reason


def test_chase_auction_strong_transient_no_tick_before_920_defers():
    """9:20 前单帧 NO_TICK（瞬时丢帧）→ defer：on_auction_snapshot 返回 None、不留痕、不锁幂等（评审 medium#4）。

    随后该票在 9:20 前拿到真实强开因子 → 仍能正常产出 AUCTION 竞价单（证明未被早期抖动永久污染）。
    """
    router, log, _ = _router()
    plan = make_plan_row(strategy_family="打板", setup="首板", market_state="启动")
    # 9:15 段瞬时丢帧。
    transient = _snap(
        phase=AuctionPhase.AUCTION_CANCELABLE,
        open_pct=None, auction_vol_ratio=None, centroid_trend=CentroidTrend.FLAT,
        last_price=None, pre_close=None, data_quality=[DQ_NO_TICK],
    )
    assert router.on_auction_snapshot(transient, plan) is None      # defer，不决策
    assert len([d for d in log if d.ts_code == plan.ts_code]) == 0   # 不留痕、未锁幂等
    # 后续帧拿到真实强开因子 → 正常竞价单（未被早期 NO_TICK 锁死）。
    strong = _snap(
        phase=AuctionPhase.AUCTION_CANCELABLE,
        open_pct=Decimal("0.06"), auction_vol_ratio=Decimal("0.5"),
        centroid_trend=CentroidTrend.UP, last_price=Decimal("10.60"),
    )
    decision = router.on_auction_snapshot(strong, plan)
    assert decision is not None
    assert decision.action == EntryAction.CHASE_AUCTION_STRONG
    assert decision.order_phase == OrderPhase.AUCTION


# ---------------------------------------------------------------------------
# 4. 幂等：已 BUY 后再推帧 → on_auction_snapshot 返回 None
# ---------------------------------------------------------------------------
def test_idempotent_buy_then_none():
    """同一 ts_code 第一帧产出 BUY 后，第二帧（即便仍命中买条件）→ on_auction_snapshot 返回 None。"""
    router, log, _ = _router()
    plan = make_plan_row(strategy_family="打板", setup="首板", market_state="启动")
    snap = _snap(open_pct=Decimal("0.06"), auction_vol_ratio=Decimal("0.5"),
                 centroid_trend=CentroidTrend.UP, last_price=Decimal("10.60"))
    first = router.on_auction_snapshot(snap, plan)
    assert first is not None and first.action == EntryAction.CHASE_AUCTION_STRONG
    # 再推一帧（同票），幂等短路。
    second = router.on_auction_snapshot(snap, plan)
    assert second is None
    # decision_log 只追加了第一条 BUY。
    assert len([d for d in log if d.ts_code == plan.ts_code]) == 1


def test_skip_not_marked_decided_allows_later_buy():
    """SKIP 不登记幂等集合：先弱开 SKIP，后续帧转强仍可再路由出 BUY（§4.2 末仅 BUY 幂等）。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="打板", setup="首板", market_state="启动")
    weak = _snap(open_pct=Decimal("0.005"), auction_vol_ratio=Decimal("0.5"), centroid_trend=CentroidTrend.UP)
    d1 = router.on_auction_snapshot(weak, plan)
    assert d1.action == EntryAction.SKIP
    strong = _snap(open_pct=Decimal("0.06"), auction_vol_ratio=Decimal("0.5"),
                   centroid_trend=CentroidTrend.UP, last_price=Decimal("10.60"))
    d2 = router.on_auction_snapshot(strong, plan)
    assert d2 is not None and d2.action == EntryAction.CHASE_AUCTION_STRONG


# ---------------------------------------------------------------------------
# 5. 五类 action 各买 / 弃断言
# ---------------------------------------------------------------------------
def test_chase_limit_up_buy():
    """CHASE_LIMIT_UP 买：顶板封单稳 → 挂涨停价、order_phase=AUCTION。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="打板", setup="连板接力", market_state="启动",
                         limit_up_price=Decimal("11.00"))
    snap = _snap(is_limit_up=True, last_price=Decimal("11.00"),
                 seal_to_float_ratio=Decimal("0.08"), centroid_trend=CentroidTrend.UP)
    decision = router.route(plan, snap)
    assert decision.action == EntryAction.CHASE_LIMIT_UP
    assert decision.limit_price == Decimal("11.00")
    assert decision.order_phase == OrderPhase.AUCTION


def test_chase_limit_up_abandon_not_sealed():
    """CHASE_LIMIT_UP 弃：未顶板（炸板 / 未封）→ SKIP。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="打板", setup="连板接力", market_state="启动")
    # 给一个开盘正向、重心向上的快照避免触发「因子全面走弱」闸门，但 is_limit_up=False → 策略判弃。
    snap = _snap(is_limit_up=False, open_pct=Decimal("0.03"), centroid_trend=CentroidTrend.UP,
                 last_price=Decimal("10.30"))
    decision = router.route(plan, snap)
    assert decision.action == EntryAction.SKIP
    assert "未顶板" in decision.reason


def test_dip_buy_ma_buy():
    """DIP_BUY_MA 买：open_pct 落低吸档 → 限价低吸。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="低吸", setup="均线粘合", market_state="震荡",
                         reasonable_open_low=Decimal("9.90"))
    # open_pct=-0.5% 落 [-2%, +1%] 低吸档（>0 避开「全面走弱」闸门用 last_price 正常即可，
    # 注：open_pct<=0 且未顶板且非UP 会触发闸门，故给重心 UP 规避，专测策略买条件）。
    snap = _snap(open_pct=Decimal("-0.005"), centroid_trend=CentroidTrend.UP, last_price=Decimal("9.95"))
    decision = router.route(plan, snap)
    assert decision.action == EntryAction.DIP_BUY_MA
    assert decision.limit_price == Decimal("9.90")     # 合理下沿低吸价
    assert decision.order_phase == OrderPhase.OPENING


def test_dip_buy_ma_abandon_overheat():
    """DIP_BUY_MA 弃：高开超 Y%（追高风险）→ SKIP。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="低吸", setup="均线粘合", market_state="震荡")
    snap = _snap(open_pct=Decimal("0.08"), centroid_trend=CentroidTrend.UP, last_price=Decimal("10.80"))
    decision = router.route(plan, snap)
    assert decision.action == EntryAction.SKIP
    assert "高开超" in decision.reason


def test_leader_pullback_buy():
    """LEADER_PULLBACK 买：龙头强度高 + 续板不弱 → 限价吸。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="龙回头", setup="高位回踩", market_state="启动",
                         leader_strength_score=Decimal("0.8"), continuation_prob=Decimal("0.6"),
                         reasonable_open_low=Decimal("9.80"))
    snap = _snap(open_pct=Decimal("0.02"), centroid_trend=CentroidTrend.UP, last_price=Decimal("10.20"))
    decision = router.route(plan, snap)
    assert decision.action == EntryAction.LEADER_PULLBACK
    assert decision.limit_price == Decimal("10.20")
    assert decision.order_phase == OrderPhase.OPENING


def test_leader_pullback_abandon_weak_strength():
    """LEADER_PULLBACK 弃：龙头强度不足（被新龙头取代）→ SKIP。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="龙回头", setup="高位回踩", market_state="启动",
                         leader_strength_score=Decimal("0.2"), continuation_prob=Decimal("0.6"))
    snap = _snap(open_pct=Decimal("0.02"), centroid_trend=CentroidTrend.UP, last_price=Decimal("10.20"))
    decision = router.route(plan, snap)
    assert decision.action == EntryAction.SKIP
    assert "强度不足" in decision.reason


def test_skip_action_unmatched_family():
    """SKIP（弃）：未匹配任何战法（family 不识别）→ SKIP，不下单。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="未知战法", setup="未知形态", market_state="启动")
    snap = _snap(open_pct=Decimal("0.02"), centroid_trend=CentroidTrend.UP, last_price=Decimal("10.20"))
    decision = router.route(plan, snap)
    assert decision.action == EntryAction.SKIP
    assert decision.limit_price is None


def test_skip_action_traded_via_strategy_registry():
    """SKIP（买/弃对称的「买」侧不存在）：SKIP 策略实例恒产 SKIP（验证注册表覆盖第五类）。"""
    from qmt_strategy.entry.strategies import base as sb
    strat = sb.get_strategy(EntryAction.SKIP)
    out = strat.decide(make_plan_row(), _snap(), _settings())
    assert out.action == EntryAction.SKIP
    assert out.limit_price is None


# ---------------------------------------------------------------------------
# 附加：position_sizer / factors_snapshot / decided_at 口径
# ---------------------------------------------------------------------------
def test_position_sizer_fills_plan_volume():
    """提供 position_sizer → BUY 决策填 plan_volume；SKIP 不调用 sizer。"""
    calls = []

    def sizer(plan: PlanRow, price: Decimal) -> int:
        calls.append((plan.ts_code, price))
        return 1000

    router, _, _ = _router(position_sizer=sizer)
    plan = make_plan_row(strategy_family="打板", setup="连板接力", market_state="启动",
                         limit_up_price=Decimal("11.00"))
    snap = _snap(is_limit_up=True, last_price=Decimal("11.00"), seal_to_float_ratio=Decimal("0.08"))
    decision = router.route(plan, snap)
    assert decision.action == EntryAction.CHASE_LIMIT_UP
    assert decision.plan_volume == 1000
    assert calls == [(plan.ts_code, Decimal("11.00"))]


def test_factors_snapshot_and_decided_at_utc():
    """factors_snapshot 留痕关键因子；decided_at 取注入时钟 UTC naive（无 tzinfo、不 ±8h）。"""
    router, _, _ = _router()
    plan = make_plan_row(strategy_family="打板", setup="首板", market_state="启动")
    snap = _snap(open_pct=Decimal("0.06"), auction_vol_ratio=Decimal("0.5"),
                 centroid_trend=CentroidTrend.UP, last_price=Decimal("10.60"))
    decision = router.route(plan, snap)
    # decided_at 为 UTC naive（09:16 东八区 = 01:16 UTC）。
    assert decision.decided_at.tzinfo is None
    assert decision.decided_at == utc_at_east8(T_BUY, 9, 16, 0)
    fs = decision.factors_snapshot
    assert fs["open_pct"] == "0.06"            # Decimal 转 str 保精度
    assert fs["centroid_trend"] == "UP"
    assert fs["is_limit_up"] is False
    assert decision.signal_trade_date == T_SIGNAL
    assert decision.target_trade_date == T_BUY
