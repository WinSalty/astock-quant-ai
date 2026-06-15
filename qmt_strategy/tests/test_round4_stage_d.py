"""第二轮评审修复 · 阶段D 回归锁定测试（供数与建仓闭环 · 执行侧）。

覆盖：竞价强开定盘段改 OPENING(#17)、半路首板不误路由竞价强开(#43)、行情先订阅(#44)、
封单不稳护栏生效(#41)、竞价择时关时解锁幂等(#16)、开盘前 OPENING 单 TTL 顺延至开盘(#16/#17)。
"""

from __future__ import annotations

from decimal import Decimal

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import AuctionPhase, CentroidTrend, EntryAction, OrderPhase
from qmt_strategy.contracts.models import AuctionSnapshot
from qmt_strategy.entry.entry_router import EntryRouter
from qmt_strategy.entry.strategies.chase_auction_strong import ChaseAuctionStrongStrategy

from conftest import T_BUY, make_plan_row, utc_at_east8


def _snap(phase, **kw):
    # pre_close 留 None：与 test_entry_router 同口径，使竞价三档阈值走"未配置不约束"，强开买条件可达。
    base = dict(
        ts_code="600000.SH", phase=phase, ts=utc_at_east8(T_BUY, 9, 16),
        open_pct=Decimal("0.06"), auction_vol_ratio=Decimal("0.5"),
        centroid_trend=CentroidTrend.UP, last_price=Decimal("10.60"),
    )
    base.update(kw)
    return AuctionSnapshot(**base)


# ===========================================================================
# #17 竞价强开追：定盘段(SETTLED) → OPENING（不再硬编码 AUCTION 致 TTL 立即过期）
# ===========================================================================
def test_chase_auction_strong_settled_phase_opening():
    strat = ChaseAuctionStrongStrategy()
    s = Settings()
    # 可撤段 → AUCTION 竞价单。
    out_cancelable = strat.decide(make_plan_row(limit_up_price=Decimal("11.00")),
                                  _snap(AuctionPhase.AUCTION_CANCELABLE), s)
    assert out_cancelable.action == EntryAction.CHASE_AUCTION_STRONG
    assert out_cancelable.order_phase == OrderPhase.AUCTION
    # 定盘段(9:25-9:30, SETTLED) → OPENING 开盘后单（避免竞价单 TTL 立即过期成废单）。
    out_settled = strat.decide(make_plan_row(limit_up_price=Decimal("11.00")),
                               _snap(AuctionPhase.SETTLED), s)
    assert out_settled.action == EntryAction.CHASE_AUCTION_STRONG
    assert out_settled.order_phase == OrderPhase.OPENING


# ===========================================================================
# #43 半路首板不误路由竞价强开追
# ===========================================================================
def _router():
    return EntryRouter(Settings(), FakeClock(utc_at_east8(T_BUY, 9, 16)), RecordingLogger())


def test_banlu_first_board_routes_chase_limit_up_not_strong():
    """BANLU(半路) + 首板半路 → 打板跟买(CHASE_LIMIT_UP)，不挂竞价单(#43)。"""
    router = _router()
    plan = make_plan_row(strategy_family="BANLU", setup="首板半路", market_state="启动",
                         limit_up_price=Decimal("11.00"))
    snap = _snap(AuctionPhase.AUCTION_CANCELABLE, is_limit_up=True, last_price=Decimal("11.00"),
                 seal_to_float_ratio=Decimal("0.08"))
    assert router.route(plan, snap).action == EntryAction.CHASE_LIMIT_UP


def test_daban_first_board_still_chase_auction_strong():
    """DABAN(打板) + 首板打板 → 竞价强开追（#43 修复不误伤打板族）。"""
    router = _router()
    plan = make_plan_row(strategy_family="DABAN", setup="首板打板", market_state="启动")
    snap = _snap(AuctionPhase.AUCTION_CANCELABLE)
    assert router.route(plan, snap).action == EntryAction.CHASE_AUCTION_STRONG


# ===========================================================================
# #41 封单不稳护栏真正生效
# ===========================================================================
def test_chase_limit_up_seal_guard_configurable():
    """#41：封单不稳护栏【可配置】生效（复审 P2-1：默认 0 关闭、配 >0 启用；原硬编码 0.0 恒不触发）。"""
    plan = make_plan_row(strategy_family="打板", setup="连板接力", market_state="启动",
                         limit_up_price=Decimal("11.00"))
    thin = _snap(AuctionPhase.AUCTION_CANCELABLE, is_limit_up=True, last_price=Decimal("11.00"),
                 seal_to_float_ratio=Decimal("0.001"))
    # (a) 默认（seal_ratio_min=0，待量纲实测前关闭）→ 不因封流比小而弃（不误杀）。
    router_off = EntryRouter(Settings(), FakeClock(utc_at_east8(T_BUY, 9, 16)), RecordingLogger())
    assert router_off.route(plan, thin).action == EntryAction.CHASE_LIMIT_UP
    # (b) 配 QMT_SEAL_RATIO_MIN=0.005 启用 → 封流比 0.001 < 0.005 判封单不稳放弃。
    s_on = Settings.from_env({"QMT_SEAL_RATIO_MIN": "0.005"})
    router_on = EntryRouter(s_on, FakeClock(utc_at_east8(T_BUY, 9, 16)), RecordingLogger())
    assert router_on.route(plan, thin).action == EntryAction.SKIP
    # 配 0 是显式关闭（不被默认覆盖）。
    assert Settings.from_env({"QMT_SEAL_RATIO_MIN": "0"}).seal_ratio_min == Decimal("0")


# ===========================================================================
# #16 竞价择时关时解锁幂等
# ===========================================================================
def test_entry_router_release_unlocks_idempotency():
    router = _router()
    plan = make_plan_row(strategy_family="DABAN", setup="首板打板", market_state="启动")
    snap = _snap(AuctionPhase.AUCTION_CANCELABLE)
    d1 = router.on_auction_snapshot(snap, plan)
    assert d1 is not None and d1.action == EntryAction.CHASE_AUCTION_STRONG
    # 已锁幂等：再推同票返回 None。
    assert router.on_auction_snapshot(snap, plan) is None
    # 解锁后可重新路由（评审二轮 P1#16）。
    router.release(plan.ts_code)
    assert router.on_auction_snapshot(snap, plan) is not None


# ===========================================================================
# #44 行情先订阅再取 tick
# ===========================================================================
def test_tick_source_subscribes_before_get():
    from qmt_strategy.auction.tick_source import XtdataTickSource

    class _FakeXtdata:
        def __init__(self):
            self.subscribed = []

        def subscribe_quote(self, code, period="tick"):
            self.subscribed.append(code)
            return 0

        def get_full_tick(self, codes):
            return {c: {"lastPrice": 10.0} for c in codes}

    xt = _FakeXtdata()
    src = XtdataTickSource(xt)
    src.get_full_tick(["600000.SH", "000001.SZ"])
    assert set(xt.subscribed) == {"600000.SH", "000001.SZ"}  # 取数前已订阅
    # 二次取数不重复订阅（幂等）。
    src.get_full_tick(["600000.SH"])
    assert xt.subscribed.count("600000.SH") == 1


# ===========================================================================
# #16/#17 开盘前 OPENING 单 TTL 顺延至开盘
# ===========================================================================
def test_opening_ttl_extends_to_open_when_before_930():
    from qmt_strategy.common.time_utils import east8_now_from_utc
    from qmt_strategy.order.order_executor import OrderExecutor
    from qmt_strategy.contracts.xt_objects import FakeStockAccount
    from qmt_strategy.order.local_ledger import InMemoryLocalLedger

    class _T:
        def order_stock(self, *a, **k):
            return 1

        def cancel_order_stock(self, *a, **k):
            return 0

    clock = FakeClock(utc_at_east8(T_BUY, 9, 26))  # 定盘段(开盘前)
    ex = OrderExecutor(_T(), FakeStockAccount("acc1"), "acc1", InMemoryLocalLedger(),
                       Settings(), clock, RecordingLogger())
    # 买入开盘单(extend_to_open=True)：截止应 >= 9:30（从开盘起算 TTL），不在开盘前过期。
    buy_deadline = ex._compute_ttl_deadline(clock.now_utc(), OrderPhase.OPENING, extend_to_open=True)
    assert east8_now_from_utc(buy_deadline).hour * 60 + east8_now_from_utc(buy_deadline).minute >= 9 * 60 + 30
    # 卖单开盘单(extend_to_open=False，复审 P2-3)：不顺延，从 now 起算 → 9:27，仍在开盘前。
    sell_deadline = ex._compute_ttl_deadline(clock.now_utc(), OrderPhase.OPENING)
    sd = east8_now_from_utc(sell_deadline)
    assert sd.hour * 60 + sd.minute < 9 * 60 + 30
