"""Engine 编排层集成 smoke 测试（§1.5 主链路 + §7.1.6 竞价择时闸门 + 收盘对账闭环）。

用全 fake 依赖装配引擎，验证：装配不报错、盘前装载、竞价择时闸门（默认关只采集 / 开则下单）、
空仓禁开新仓、收盘批次 + 对账可跑。不连真实 xttrader / MySQL。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List

from qmt_strategy.app.main import EngineDeps, build_engine
from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import AuctionPhase, CentroidTrend, EntryAction
from qmt_strategy.contracts.models import AuctionSnapshot
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.watchlist.sources import CallableSelectedStockSource
from tests.conftest import T_BUY, T_SIGNAL, make_selected_row, utc_at_east8


class _Cal:
    def is_open(self, d):
        return d.weekday() < 5

    def next_open(self, d):
        x = d + timedelta(days=1)
        while x.weekday() >= 5:
            x += timedelta(days=1)
        return x

    def prev_open(self, d):
        x = d - timedelta(days=1)
        while x.weekday() >= 5:
            x -= timedelta(days=1)
        return x


class _FakeTrader:
    """实现 query_* / order_stock，供 Engine 装配与收盘批次跑通。"""

    def __init__(self, cash=1_000_000):
        self._cash = cash
        self.order_calls: List[tuple] = []
        self.order_prices: List[float] = []   # 与 order_calls 同序，记录每次下单限价

    def order_stock(self, account, code, otype, vol, ptype, price, sname, remark):
        self.order_calls.append((code, otype, vol))
        self.order_prices.append(price)
        return 100 + len(self.order_calls)

    def cancel_order_stock(self, account, order_id):
        return 0

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc1", cash=self._cash, frozen_cash=0,
                           market_value=0, total_asset=self._cash)

    def query_stock_positions(self, account):
        return []

    def query_stock_orders(self, account):
        return []

    def query_stock_trades(self, account):
        return []


class _StubTickSource:
    def get_full_tick(self, codes):
        return {}


def _deps(env=None, source_rows=None):
    rows = source_rows if source_rows is not None else [
        make_selected_row(
            ts_code="600036.SH", signal_close=Decimal("10.00"),
            limit_up_price=Decimal("11.00"),
            reasonable_open_high_low=Decimal("10.20"),
            reasonable_open_high_high=Decimal("10.80"),
            market_state="启动", tradable_flag=True,
            strategy="打板", role="龙头",
        )
    ]
    # 补 strategy_family / setup（路由到 CHASE_AUCTION_STRONG）。
    for r in rows:
        r.strategy_family = "打板"
        r.setup = "首板"
    src = CallableSelectedStockSource(lambda d: rows, source_name="test")
    return EngineDeps(
        settings=Settings.from_env(env or {}),
        clock=FakeClock(utc_at_east8(T_BUY, 9, 16)),
        logger=RecordingLogger(),
        calendar=_Cal(),
        trader=_FakeTrader(),
        account=FakeStockAccount("acc1"),
        account_id="acc1",
        tick_source=_StubTickSource(),
        selected_source=src,
        repository=InMemoryQmtRepository(),
    )


def _strong_auction_snap():
    """强开越竞越强快照（CHASE_AUCTION_STRONG 应判买、order_phase=AUCTION）。"""
    return AuctionSnapshot(
        ts_code="600036.SH", phase=AuctionPhase.AUCTION_CANCELABLE, ts=utc_at_east8(T_BUY, 9, 16),
        open_pct=Decimal("0.10"), auction_vol_ratio=Decimal("0.5"), auction_centroid=Decimal("10.50"),
        centroid_trend=CentroidTrend.UP, is_limit_up=False, last_price=Decimal("10.50"),
        pre_close=Decimal("10.00"),
    )


def test_engine_assembles_and_prewarms():
    eng = build_engine(_deps())
    ctx = eng.prewarm(T_BUY)
    assert ctx.is_open is True
    assert ctx.open_new_position_allowed is True       # 启动态 → 允许开新仓
    assert "600036.SH" in ctx.tradable
    assert eng.callback is not None                     # 回调对象可供连接守护注册


def test_auction_timing_disabled_collect_only():
    """§7.1.6：竞价择时默认关 → 竞价段 BUY 决策只采集留痕、不下单。"""
    deps = _deps()  # auction_timing_enabled 默认 False
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng._router_sink(_strong_auction_snap())
    assert deps.trader.order_calls == []                # 竞价段未下单（只采集）


def test_auction_timing_enabled_places_order():
    """竞价择时开启 + 非空仓 → 竞价段强开决策真正下单。"""
    deps = _deps({"QMT_AUCTION_TIMING_ENABLED": "true"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng._router_sink(_strong_auction_snap())
    assert len(deps.trader.order_calls) == 1
    code, _otype, vol = deps.trader.order_calls[0]
    assert code == "600036.SH" and vol > 0


def test_empty_position_state_blocks_open():
    """空仓日 → open_new_position_allowed=False，竞价择时即便开启也不开新仓（§2.6）。"""
    rows = [make_selected_row(
        ts_code="600036.SH", signal_close=Decimal("10.00"),
        limit_up_price=Decimal("11.00"),
        reasonable_open_high_low=Decimal("10.20"), reasonable_open_high_high=Decimal("10.80"),
        market_state="空仓", tradable_flag=True,
    )]
    deps = _deps({"QMT_AUCTION_TIMING_ENABLED": "true"}, source_rows=rows)
    eng = build_engine(deps)
    ctx = eng.prewarm(T_BUY)
    assert ctx.open_new_position_allowed is False
    eng._router_sink(_strong_auction_snap())
    assert deps.trader.order_calls == []                # 空仓禁开新仓


def test_transient_risk_block_releases_lock_allows_later_buy():
    """评审 doc/21 E1：单帧瞬时风控拒单（行情中断）应解锁幂等，使条件恢复后的后续帧能重评下单（破 fail-then-stuck）。

    原缺陷：on_auction_snapshot 在产出 BUY 的那一帧即把 ts_code 锁进 _decided，而 _open_blocked_by_risk/
    _limit_price_sane 晚于它执行；单帧瞬时拒单不解锁 → 该票当日被永久锁死、再不评估 = 该买不买。
    """
    deps = _deps({"QMT_AUCTION_TIMING_ENABLED": "true"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    # 这一帧行情中断 → risk.gate FREEZE → _open_blocked_by_risk True：拒单但应解锁。
    eng.report_market_feed(False)
    eng._router_sink(_strong_auction_snap())
    assert deps.trader.order_calls == []                       # 本帧未下单
    assert "600036.SH" not in eng._entry._decided              # 已解锁（关键：未被永久锁死）
    # 行情恢复后的后续帧 → 重评并真正下单（证明瞬时拒单未把强龙头锁死）。
    eng.report_market_feed(True)
    eng._router_sink(_strong_auction_snap())
    assert len(deps.trader.order_calls) == 1
    assert deps.trader.order_calls[0][0] == "600036.SH"


def test_persistent_risk_block_dedups_留痕_no_flood():
    """评审 doc/21 E1 复审：持久拒因下解锁重评不洪泛——每票每拒因每日只留痕一次（防 decision_log/日志洪泛）。"""
    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng._reconcile_blocked = True             # 持久拒因（对账未通过，不会因帧变化而恢复）
    for _ in range(5):
        eng._router_sink(_strong_auction_snap())
    assert deps.trader.order_calls == []      # 持续禁开新仓
    # 解锁重评 5 帧，但对账阻断内部留痕被 quiet 去抖、只记一次（否则 5 帧 ×N 票会洪泛）。
    assert deps.logger.events().count("engine_open_blocked_reconcile_unconfirmed") == 1


def test_calendar_unsafe_blocks_open():
    """doc/29 J-3：交易日历不可用(fail-closed)→ 禁开新仓（守仓/卖出不受影响）。"""
    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng._calendar_unsafe = True   # 盘前校验：日历耗尽且补取失败
    eng._router_sink(_strong_auction_snap())
    assert deps.trader.order_calls == []  # fail-closed：不开新仓
    assert "engine_open_blocked_calendar_unsafe" in deps.logger.events()


def _two_candidate_deps(env=None):
    """两只候选：600036.SH 强度 90、600000.SH 强度 30（用于验证强度加权分配）。"""
    rows = []
    for code, strength in (("600036.SH", Decimal("90")), ("600000.SH", Decimal("30"))):
        r = make_selected_row(
            ts_code=code, signal_close=Decimal("10.00"), limit_up_price=Decimal("11.00"),
            reasonable_open_high_low=Decimal("10.20"), reasonable_open_high_high=Decimal("10.80"),
            market_state="启动", tradable_flag=True, leader_strength_score=strength,
        )
        r.strategy_family = "打板"
        r.setup = "首板"
        rows.append(r)
    return _deps(env, source_rows=rows)


def test_strength_budget_fail_closed_when_position_query_fails():
    """评审 doc/19 M-2（强度加权主路径）：配单票上限 + 盘中持仓查询失败 → _strength_budget_volume fail-closed 返 0
    （与离线 _plan_volume 同口径，绝不把查询失败当无持仓而漏计隔夜持仓致跨日同票超单票上限）。"""
    deps = _two_candidate_deps(
        {"QMT_AUCTION_TIMING_ENABLED": "true", "QMT_MAX_POSITION_PER_STOCK": "1000000"}
    )
    eng = build_engine(deps)
    eng.prewarm(T_BUY)   # 正常 prewarm（trader 正常，日初权益/强度权重就绪）
    # 盘中持仓查询抖动失败：实例方法被替换为抛异常（模拟 query_stock_positions 超时）。
    def _boom(account):
        raise RuntimeError("position query timeout")
    deps.trader.query_stock_positions = _boom
    v = eng._strength_budget_volume(eng._plan_map["600036.SH"], Decimal("10.50"))
    assert v == 0        # 无法核验单票敞口 → fail-closed 拒单
    assert "engine_sizer_per_stock_held_unknown_reject" in deps.logger.events()


def test_strength_weighted_budget_allocation():
    """评审「按强度分」：两只候选，强度 90:30 → 预算/股数约 3:1，强的分得多。"""
    deps = _two_candidate_deps({"QMT_AUCTION_TIMING_ENABLED": "true"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)  # 日初权益=100万，target_position_ratio 默认 1.0
    v_strong = eng._strength_budget_volume(eng._plan_map["600036.SH"], Decimal("10.50"))
    v_weak = eng._strength_budget_volume(eng._plan_map["600000.SH"], Decimal("10.50"))
    assert v_strong > v_weak > 0
    assert v_strong == 3 * v_weak          # 90/30=3，预算成比例
    # 强票预算 ≈ 100万×0.75 = 75万 / 10.50 取整到百股（int() 对正 Decimal 截断=向下取整）
    assert v_strong == (int(Decimal("750000") / Decimal("10.50")) // 100) * 100


def _three_candidate_deps(env=None):
    """三只候选：强度 90/60/30（验证单日建仓只数上限 top-N 名额 + 强度归一）。"""
    rows = []
    for code, strength in (("600036.SH", Decimal("90")), ("600000.SH", Decimal("60")), ("000001.SZ", Decimal("30"))):
        r = make_selected_row(
            ts_code=code, signal_close=Decimal("10.00"), limit_up_price=Decimal("11.00"),
            reasonable_open_high_low=Decimal("10.20"), reasonable_open_high_high=Decimal("10.80"),
            market_state="启动", tradable_flag=True, leader_strength_score=strength,
        )
        r.strategy_family = "打板"
        r.setup = "首板"
        rows.append(r)
    return _deps(env, source_rows=rows)


def test_position_cap_restricts_budget_to_top_n_strength():
    """单日建仓只数上限 N=2：仅强度 top-2 分配额度（在 2 只内归一满额部署），第 3 只 sizer 返 0。

    评审三轮 EXEC-entry-01 复审（防优先级反转）：beyond-N 弱票【不进权重表】→ sizer 返 0，把名额留给更强的
    top-N，避免弱票按竞价触发先后抢占强龙头名额。单日建仓【只数】另由 order_executor.place 的 committed 计数闸
    动态收口（强票判弃→名额空置是正确的，不强行补买更弱替代票）。
    """
    deps = _three_candidate_deps({"QMT_AUCTION_TIMING_ENABLED": "true", "QMT_MAX_POSITIONS_PER_DAY": "2"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)  # 日初权益=100万，ratio=1.0 → ceiling=100万
    v1 = eng._strength_budget_volume(eng._plan_map["600036.SH"], Decimal("10.50"))
    v2 = eng._strength_budget_volume(eng._plan_map["600000.SH"], Decimal("10.50"))
    v3 = eng._strength_budget_volume(eng._plan_map["000001.SZ"], Decimal("10.50"))
    assert v3 == 0                                   # beyond-N 不在 top-2 名额内 → 不开仓（名额留给强票）
    assert v1 > v2 > 0
    # 权重在 top-2 内归一：90/150=0.6、60/150=0.4 → 预算 60万 / 40万（不被第 3 只摊薄、不闲置）
    assert v1 == (int(Decimal("600000") / Decimal("10.50")) // 100) * 100
    assert v2 == (int(Decimal("400000") / Decimal("10.50")) // 100) * 100


def test_position_cap_unlimited_keeps_all_candidates():
    """放宽建仓只数上限（设极大）→ 全体候选参与归一（回到「摊到所有候选」口径）。"""
    deps = _three_candidate_deps({"QMT_AUCTION_TIMING_ENABLED": "true", "QMT_MAX_POSITIONS_PER_DAY": "99"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    v3 = eng._strength_budget_volume(eng._plan_map["000001.SZ"], Decimal("10.50"))
    assert v3 > 0                                    # 第 3 只也分到额度（30/180）


def test_total_exposure_ceiling_caps_budget():
    """max_total_exposure 总敞口闸：可分配上限被压到敞口值，强度份额随之缩小。"""
    deps = _two_candidate_deps({"QMT_AUCTION_TIMING_ENABLED": "true", "QMT_MAX_TOTAL_EXPOSURE": "100000"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    # ceiling=min(100万, 10万)=10万；强票 w=0.75 → 预算 7.5万 / 10.50 取整
    v_strong = eng._strength_budget_volume(eng._plan_map["600036.SH"], Decimal("10.50"))
    assert v_strong == (int(Decimal("75000") / Decimal("10.50")) // 100) * 100


def test_buy_blocked_on_account_drawdown_breach():
    """评审 P0-B1/B2：账户日内回撤击穿 → 买入路径经 risk.gate 冻结、不开新仓。

    修复前买入完全绕过 risk.gate，账户熔断对开仓零作用；此用例证明回撤击穿能挡住新开仓。
    """
    deps = _deps({"QMT_AUCTION_TIMING_ENABLED": "true", "QMT_ACCOUNT_DRAWDOWN_LIMIT": "0.05"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)                  # 抓取日初基线 total_asset=100 万
    deps.trader._cash = 900_000         # 当前总资产跌到 90 万（回撤 10% > 5% 限）
    eng._router_sink(_strong_auction_snap())
    assert deps.trader.order_calls == []   # 回撤击穿 → 不开新仓


def test_buy_allowed_when_drawdown_within_limit():
    """回撤未击穿（小于阈值）→ 正常开新仓（证明不是无脑冻结）。"""
    deps = _deps({"QMT_AUCTION_TIMING_ENABLED": "true", "QMT_ACCOUNT_DRAWDOWN_LIMIT": "0.20"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    deps.trader._cash = 950_000         # 回撤 5% < 20% 限
    eng._router_sink(_strong_auction_snap())
    assert len(deps.trader.order_calls) == 1


def test_kill_switch_blocks_order_even_if_timing_on():
    """全局熔断：kill_switch=True → 即便竞价择时开启也不下单（§7.1.5 双保险）。"""
    deps = _deps({"QMT_AUCTION_TIMING_ENABLED": "true", "QMT_KILL_SWITCH": "true"})
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng._router_sink(_strong_auction_snap())
    assert deps.trader.order_calls == []


def test_close_batch_and_reconcile_run():
    """收盘批次 + 对账闭环可跑通（query_* 空集 → CLOSE 快照落库、对账无异常）。"""
    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng.close_batch(T_BUY)
    # 收盘资产快照已落库（FakeXtAsset → qmt_account_daily CLOSE）。
    assert deps.repository.get_account_daily("acc1", T_BUY) is not None


def test_run_sell_pass_no_sellable_units():
    """无可卖持仓（当日无昨仓）→ run_sell_pass 返回空、不下卖单。"""
    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    assert eng.run_sell_pass(T_BUY, books={}, session="intraday") == []


# ---------------------------------------------------------------------------
# 评审 P0-A2 / P0-C2：买入成交回写持仓 → 次日可卖 → 卖出 → 在途不重复卖（端到端闭环）
# ---------------------------------------------------------------------------
def test_buy_fill_writes_position_then_sellable_next_day_and_no_double_sell():
    from qmt_strategy.contracts.models import OrderBook
    from qmt_strategy.contracts.enums import PositionState
    from qmt_strategy.contracts.xt_objects import FakeXtTrade

    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)

    # (1) 买入成交回报经回调落地持仓状态机（评审 P0-A2：原实现从不回写，此前恒空集）。
    fill = FakeXtTrade(
        account_id="acc1", stock_code="600036.SH", traded_id="TR1", order_id=1,
        traded_price=11.00, traded_volume=1000, order_type=23,  # 评审三轮 EXEC-DW-09：买回报须带 order_type
    )
    eng.callback.on_stock_trade(fill)
    unit = eng._position.get_unit("acc1", "600036.SH")
    assert unit is not None and unit.volume == 1000
    # 守 T+1：买入当日不可卖（LOCKED_T1，can_use_volume=0）。
    assert unit.state == PositionState.LOCKED_T1
    assert eng.run_sell_pass(T_BUY, books={"600036.SH": OrderBook(ts_code="600036.SH", broke_board=True)}) == []

    # (2) 次日推进 → HOLDING、可卖量放开 → sellable_units 非空（此前恒空、永不卖出）。
    next_day = deps.calendar.next_open(T_BUY)
    eng._position.refresh_state(next_day)
    assert len(eng._position.sellable_units(next_day)) == 1

    # (3) 炸板盘口 → CLEAR → 下且仅下一张卖单，单元转 SELLING。
    book = OrderBook(ts_code="600036.SH", broke_board=True, is_sealed=False, last_price=Decimal("10.50"))
    sold = eng.run_sell_pass(next_day, books={"600036.SH": book})
    assert sold == ["600036.SH"]
    sell_idx = [i for i, c in enumerate(deps.trader.order_calls) if c[1] == 24]  # otype=24 卖出
    assert len(sell_idx) == 1
    # 评审 P2：卖出限价取盘口现价 10.50（而非成本价 avg_cost=11.00，挂成本价炸板卖不出）。
    assert deps.trader.order_prices[sell_idx[0]] == 10.50
    assert eng._position.get_unit("acc1", "600036.SH").state == PositionState.SELLING

    # (4) 同一在途 SELLING 单元再跑一轮 → 跳过、不重复下卖单、不抛错（评审 P0-C2）。
    sold2 = eng.run_sell_pass(next_day, books={"600036.SH": book})
    assert sold2 == []
    sell_calls2 = [c for c in deps.trader.order_calls if c[1] == 24]
    assert len(sell_calls2) == 1   # 仍只有 1 张卖单，未重复


# 口径变更（2026-06-21）：原 doc/29 B3「缺测持仓强卖」已下线——卖出完全交由执行侧 xtdata 实时盘口扳机
# 裁决，不再因信号侧缺测标记在无盘口下强制清仓。原 test_data_missing_force_clears_holding_even_without_book /
# test_data_missing_locked_t1_not_sold_even_without_book 两例随之移除（无盘口一律安全默认不卖，详见
# main._evaluate_and_sell_unit 的 book is None 分支与 test_run_sell_pass_* 系列）。


def test_run_sell_pass_skips_during_lunch():
    """评审 P1#11：午休(11:30–13:00)整轮跳过卖出决策，不向停牌时段下卖单。"""
    from qmt_strategy.common.time_utils import FakeClock
    from qmt_strategy.contracts.models import OrderBook
    from qmt_strategy.contracts.xt_objects import FakeXtTrade

    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    eng.callback.on_stock_trade(FakeXtTrade(
        account_id="acc1", stock_code="600036.SH", traded_id="B1", order_id=1,
        traded_price=11.00, traded_volume=1000, order_type=23))  # 评审三轮 EXEC-DW-09：买回报须带 order_type
    next_day = deps.calendar.next_open(T_BUY)
    eng._position.refresh_state(next_day)
    # 把引擎时钟拨到午休 12:00 → 即便炸板盘口想清仓，也整轮跳过、不下卖单。
    eng._clock = FakeClock(utc_at_east8(next_day, 12, 0, 0))
    book = OrderBook(ts_code="600036.SH", broke_board=True)
    assert eng.run_sell_pass(next_day, books={"600036.SH": book}) == []
    assert [c for c in deps.trader.order_calls if c[1] == 24] == []   # 无卖单


def test_sell_fill_reduces_position_idempotently():
    """卖出成交回报回写：扣减持仓、推进 SOLD/PART_SOLD；同一 traded_id 重投不重复扣减。"""
    from qmt_strategy.contracts.enums import PositionState, TradeSide
    from qmt_strategy.contracts.xt_objects import FakeXtTrade

    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    # 先买入建仓并推进到次日可卖。
    eng.callback.on_stock_trade(FakeXtTrade(
        account_id="acc1", stock_code="600036.SH", traded_id="B1", order_id=1,
        traded_price=11.00, traded_volume=1000, order_type=23,  # 评审三轮 EXEC-DW-09：买回报须带 order_type
    ))
    next_day = deps.calendar.next_open(T_BUY)
    eng._position.refresh_state(next_day)
    # 注：apply_sell_fill_by_trade 按 traded_id 去重+扣减，不依赖单元处于 SELLING；故无需(也不能经 get_unit
    # 深拷贝)预置 SELLING 态（评审三轮 EXEC-position-01：get_unit 返只读快照，写走原子方法）。

    # 卖出成交回报（offset_flag 触发 SELL 方向）：部分成交 600 → PART_SOLD。
    sell = FakeXtTrade(
        account_id="acc1", stock_code="600036.SH", traded_id="S1", order_id=2,
        traded_price=10.50, traded_volume=600, order_type=24, offset_flag=48,
    )
    # 强制方向为 SELL（规整层方向映射为占位实测值，这里直接断言回写按 rec.trade_side 分流）：
    # 用引擎内部分流函数直接喂一个 SELL 方向的轻量记录，避免依赖占位 side_resolver。
    class _Rec:
        ts_code = "600036.SH"; trade_side = TradeSide.SELL; traded_id = "S1"
        traded_volume = 600; traded_price = 10.50
        trade_date = next_day
    eng._apply_trade_to_position(_Rec())
    assert eng._position.get_unit("acc1", "600036.SH").volume == 400
    # 同一 traded_id 重投 → 不重复扣减。
    eng._apply_trade_to_position(_Rec())
    assert eng._position.get_unit("acc1", "600036.SH").volume == 400


# ===========================================================================
# 评审三轮 EXEC-entry-05：缺强度票保守份额 + 不挤占 top-N + 全缺退化等权
# ===========================================================================
def _mixed_strength_deps(strengths, env=None):
    """按给定 (code, strength) 列表构造候选 deps（strength 可为 None=缺强度）。"""
    rows = []
    for code, strength in strengths:
        r = make_selected_row(
            ts_code=code, signal_close=Decimal("10.00"), limit_up_price=Decimal("11.00"),
            reasonable_open_high_low=Decimal("10.20"), reasonable_open_high_high=Decimal("10.80"),
            market_state="启动", tradable_flag=True, leader_strength_score=strength,
        )
        r.strategy_family = "打板"
        r.setup = "首板"
        rows.append(r)
    return _deps(env, source_rows=rows)


def test_missing_strength_gets_conservative_share():
    """缺强度票拿保守小份额（floor×系数），远小于真实强票，且强票不被稀释到等权。"""
    deps = _mixed_strength_deps(
        [("600036.SH", Decimal("90")), ("600000.SH", None)],
        {"QMT_AUCTION_TIMING_ENABLED": "true"},
    )
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    v_real = eng._strength_budget_volume(eng._plan_map["600036.SH"], Decimal("10.50"))
    v_missing = eng._strength_budget_volume(eng._plan_map["600000.SH"], Decimal("10.50"))
    assert v_real > v_missing > 0
    # 真实强票份额 90/99≈0.909、缺强度票 9/99≈0.0909 → 真实票约 10 倍于缺强度票（非等权 0.5:0.5）。
    assert v_real >= v_missing * 9


def test_all_missing_strength_falls_back_equal_weight():
    """全部候选缺强度 → 退化等权 1/N（两只相等且均 >0）。"""
    deps = _mixed_strength_deps(
        [("600036.SH", None), ("600000.SH", None)],
        {"QMT_AUCTION_TIMING_ENABLED": "true"},
    )
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    v1 = eng._strength_budget_volume(eng._plan_map["600036.SH"], Decimal("10.50"))
    v2 = eng._strength_budget_volume(eng._plan_map["600000.SH"], Decimal("10.50"))
    assert v1 == v2 > 0


def test_missing_strength_not_preempt_top_n():
    """cap=1：top-1 名额给真实强度票（缺强度票排序键极低不挤名额），缺强度票为 beyond-N → sizer 返 0。"""
    deps = _mixed_strength_deps(
        [("600036.SH", Decimal("90")), ("600000.SH", None)],
        {"QMT_AUCTION_TIMING_ENABLED": "true", "QMT_MAX_POSITIONS_PER_DAY": "1"},
    )
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    v_real = eng._strength_budget_volume(eng._plan_map["600036.SH"], Decimal("10.50"))
    v_missing = eng._strength_budget_volume(eng._plan_map["600000.SH"], Decimal("10.50"))
    # top-1 满额给真实票（w=1.0）；缺强度票排序键极低落入 beyond-N → 不进权重表 → 返 0（不抢占强票名额）。
    assert v_real > 0
    assert v_missing == 0


def test_on_reconnect_backfill_reconciles_stuck_selling_after_rebuild():
    """评审 doc/21 P1：重连补采后在 rebuild 之后补调 _reconcile_stuck_selling，复位断线期卡死的 SELLING 单元(防整日漏卖)。

    原实现仅 prewarm 调用该对账、重连路径不调 → 断线期挂出的卖单被券商撤/废且回执丢失时,单元跨重连仍卡
    SELLING、sellable_remaining=0,止损/破位/炸板全部失效,要等次日盘前才解。本用例验证重连路径已补该调用且在 rebuild 之后。
    """
    deps = _deps()
    eng = build_engine(deps)
    eng.prewarm(T_BUY)
    order = []
    eng._rebuild_positions_from_broker = lambda today: order.append(("rebuild", today))
    eng._reconcile_stuck_selling = lambda today: order.append(("reconcile", today))
    eng.on_reconnect_backfill()
    assert [x[0] for x in order] == ["rebuild", "reconcile"]   # 补调了卡死 SELLING 对账，且在 rebuild 之后
    assert order[0][1] == order[1][1]                          # 同一交易日
