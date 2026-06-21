"""阶段0-C 卖出链接线单测（T0.1 build_sell_books + T0.2 prior_provider）。

覆盖：
- sell_book_builder.build_order_book 纯函数：NO_TICK / 有 tick 无 plan / 有 plan 顶涨停封板 / 五档 list；
- Engine.build_sell_books：可卖持仓→盘口；无 tick 票跳过；取数失败上抛(由调度置 feed 不健康);
- 接线端到端：build_sell_books → run_sell_pass，封板续持 HOLD(不卖) / 弱势非封板 CLEAR(卖);
- run._build_prior_provider：本机名单→SignalPrior、缺失→None、按日缓存、fetch 异常降级 None。
全部 fake/内存，不连真实 xtquant/MySQL。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from qmt_strategy.app.main import EngineDeps, build_engine
from qmt_strategy.auction.tick_source import FakeTickSource
from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.errors import TickSourceError
from qmt_strategy.contracts.models import PlanRow, SelectedStockRow
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset, FakeXtPosition
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.position.sell_book_builder import (
    DQ_CROSS_FRAME_ERR,
    DQ_NO_PLAN,
    DQ_NO_TICK,
    build_order_book,
)
from qmt_strategy.watchlist.sources import CallableSelectedStockSource

from conftest import utc_at_east8

T_SIGNAL = date(2026, 6, 11)
T_BUY = date(2026, 6, 12)
CODE = "600000.SH"


# ===========================================================================
# 1. build_order_book 纯函数
# ===========================================================================
def _plan(limit_up="11.00", float_mktcap="1000000000") -> PlanRow:
    return PlanRow(
        ts_code=CODE, signal_trade_date=T_SIGNAL, target_trade_date=T_BUY,
        limit_up_price=Decimal(limit_up), float_mktcap=Decimal(float_mktcap),
    )


def test_build_order_book_no_tick():
    """tick 缺失 → 空壳 OrderBook(NO_TICK)、各字段默认(调用方据此不纳入 books)。"""
    ob = build_order_book(CODE, None, _plan())
    assert ob.ts_code == CODE
    assert ob.last_price is None and ob.open_pct is None
    assert ob.is_sealed is False and ob.broke_board is False and ob.open_times == 0
    assert DQ_NO_TICK in ob.data_quality


def test_build_order_book_tick_without_plan():
    """有 tick 无 plan → 现价/高开可得,封单类降级默认(NO_PLAN);跨帧字段保守默认。"""
    tick = {"lastPrice": "10.50", "lastClose": "10.00"}
    ob = build_order_book(CODE, tick, None)
    assert ob.last_price == Decimal("10.50")
    assert ob.open_pct == Decimal("0.05")           # (10.50-10.00)/10.00
    assert ob.is_sealed is False                      # 无 plan 涨停价基准 → 不判封板
    assert ob.seal_to_float_ratio is None
    assert DQ_NO_PLAN in ob.data_quality
    # 跨帧字段保守默认（最小版未填，待 T1.2 真机）。
    assert ob.broke_board is False and ob.below_support is False and ob.volume_surge is False
    assert ob.near_close_weak is False and ob.price_volume_diverge is False and ob.open_times == 0


def test_build_order_book_sealed_limit_up_with_five_level_bid():
    """顶涨停 + 五档买一(list)封单 → is_sealed=True、封单额/封流比可得(复用 _best_level 取 best 档)。"""
    tick = {
        "lastPrice": "11.00", "lastClose": "10.00",
        "bidVol": [500000, 1000, 0, 0, 0],     # 五档买量 list，best=500000
        "bidPrice": ["11.00", "10.99", 0, 0, 0],
    }
    ob = build_order_book(CODE, tick, _plan(limit_up="11.00", float_mktcap="1000000000"))
    assert ob.last_price == Decimal("11.00")
    assert ob.is_sealed is True                       # 11.00 >= 涨停价 11.00
    assert ob.seal_amount == Decimal("500000") * Decimal("11.00")   # best 买量 × best 买价
    assert ob.seal_to_float_ratio is not None and ob.seal_to_float_ratio > 0


def test_build_order_book_below_limit_up_not_sealed():
    """未达涨停 → is_sealed=False、封单额 0(正常态,非异常)。"""
    tick = {"lastPrice": "10.40", "lastClose": "10.00", "bidVol": [100, 0, 0, 0, 0], "bidPrice": ["10.40", 0, 0, 0, 0]}
    ob = build_order_book(CODE, tick, _plan(limit_up="11.00"))
    assert ob.is_sealed is False
    assert ob.seal_amount == Decimal("0")


# ===========================================================================
# 评审 doc/19 C-3：跨帧字段接线扩展点（cross_frame_builder）。注：跨帧【数值保真】属 T1.2 真机依赖（已在
# 待办登记跳过）；这里只验证离线可做的「接线框架」——不注入时保守默认、注入后生效、异常降级。
# ===========================================================================
def test_build_order_book_cross_frame_default_when_no_builder():
    """C-3：不注入 cross_frame_builder（T0.1 现状）→ 六个跨帧字段全保守默认（False/0），无 CROSS_FRAME_ERR。"""
    tick = {"lastPrice": "11.00", "lastClose": "10.00"}
    ob = build_order_book(CODE, tick, _plan())
    assert ob.broke_board is False and ob.below_support is False and ob.volume_surge is False
    assert ob.near_close_weak is False and ob.price_volume_diverge is False and ob.open_times == 0
    assert DQ_CROSS_FRAME_ERR not in ob.data_quality


def test_build_order_book_cross_frame_builder_populates_fields():
    """C-3：注入 cross_frame_builder → 原地填充跨帧字段（炸板/开板次数等），扩展点生效（T1.2 真机实现即可让四类硬扳机触发）。"""
    def _builder(ts_code, tick, ob):
        ob.broke_board = True
        ob.open_times = 3

    tick = {"lastPrice": "11.00", "lastClose": "10.00"}
    ob = build_order_book(CODE, tick, _plan(), cross_frame_builder=_builder)
    assert ob.broke_board is True and ob.open_times == 3
    assert ob.last_price == Decimal("11.00")   # 单帧字段不受影响


def test_build_order_book_cross_frame_builder_error_degrades():
    """C-3：cross_frame_builder 抛错 → 该票跨帧字段保守默认 + 留 CROSS_FRAME_ERR，不拖垮整批构造。"""
    def _boom(ts_code, tick, ob):
        raise RuntimeError("cross frame calc boom")

    tick = {"lastPrice": "11.00", "lastClose": "10.00"}
    ob = build_order_book(CODE, tick, _plan(), cross_frame_builder=_boom)
    assert ob.broke_board is False and ob.open_times == 0
    assert DQ_CROSS_FRAME_ERR in ob.data_quality
    assert ob.last_price == Decimal("11.00")   # 单帧字段仍正常派生


# ===========================================================================
# 2 + 3. Engine.build_sell_books + 接线端到端
# ===========================================================================
class _PosTrader:
    """返回固定持仓的 fake trader,记录 order_stock(供断言卖单)。"""

    def __init__(self, positions):
        self._positions = list(positions)
        self.order_calls = []   # (stock_code, order_type, volume, price)
        self._oid = 5001

    def order_stock(self, account, stock_code, order_type, order_volume, price_type, price,
                    strategy_name="", order_remark=""):
        self._oid += 1
        self.order_calls.append((stock_code, order_type, order_volume, price))
        return self._oid

    def cancel_order_stock(self, account, order_id):
        return 0

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc1", cash=Decimal("100000"), frozen_cash=Decimal("0"),
                           market_value=Decimal("0"), total_asset=Decimal("100000"))

    def query_stock_positions(self, account):
        return list(self._positions)

    def query_stock_orders(self, account):
        return []

    def query_stock_trades(self, account):
        return []


def _engine(trader, tick_source, calendar, env=None):
    deps = EngineDeps(
        settings=Settings.from_env(env or {}),
        clock=FakeClock(utc_at_east8(T_BUY, 9, 31)),   # 盘中可卖段
        logger=RecordingLogger(),
        calendar=calendar,
        trader=trader,
        account=FakeStockAccount("acc1"),
        account_id="acc1",
        tick_source=tick_source,
        selected_source=CallableSelectedStockSource(lambda d: []),
        repository=InMemoryQmtRepository(),
    )
    return build_engine(deps)


def _held_position():
    return FakeXtPosition(stock_code=CODE, volume=1000, can_use_volume=1000, avg_price=Decimal("10.00"))


def test_build_sell_books_returns_book_for_sellable_position(calendar):
    """可卖持仓 + 有 tick + plan → build_sell_books 产出含现价/封板字段的 OrderBook。"""
    tick = {"lastPrice": "11.00", "lastClose": "10.00", "bidVol": [500000, 0, 0, 0, 0], "bidPrice": ["11.00", 0, 0, 0, 0]}
    eng = _engine(_PosTrader([_held_position()]), FakeTickSource(fixed={CODE: tick}), calendar)
    eng.prewarm(T_BUY)
    eng._plan_map = {CODE: _plan()}   # 直接置 plan_map(避免依赖 watchlist-loader 细节)

    books = eng.build_sell_books(T_BUY)
    assert CODE in books
    assert books[CODE].last_price == Decimal("11.00")
    assert books[CODE].is_sealed is True


def test_build_sell_books_skips_code_without_tick(calendar):
    """可卖持仓但无该票实时 tick → 不纳入 books(run_sell_pass 走 book is None 安全默认不卖)。"""
    eng = _engine(_PosTrader([_held_position()]), FakeTickSource(fixed={}), calendar)  # 空盘口
    eng.prewarm(T_BUY)
    eng._plan_map = {CODE: _plan()}
    assert eng.build_sell_books(T_BUY) == {}


def test_build_sell_books_propagates_tick_source_failure(calendar):
    """批量取盘口失败 → 上抛 TickSourceError(由调度置 report_market_feed(False) 保守不卖),不在此吞。"""
    eng = _engine(_PosTrader([_held_position()]),
                  FakeTickSource(responses=[TickSourceError("boom")]), calendar)
    eng.prewarm(T_BUY)
    eng._plan_map = {CODE: _plan()}
    with pytest.raises(TickSourceError):
        eng.build_sell_books(T_BUY)


def test_build_sell_books_empty_when_no_positions(calendar):
    """无可卖持仓 → 返回空 dict(不调用取数)。"""
    eng = _engine(_PosTrader([]), FakeTickSource(fixed={}), calendar)
    eng.prewarm(T_BUY)
    assert eng.build_sell_books(T_BUY) == {}


def test_wired_sell_pass_holds_sealed_board(calendar):
    """接线端到端:封板续持 → run_sell_pass(build_sell_books) 不卖(HOLD)。"""
    tick = {"lastPrice": "11.00", "lastClose": "10.00", "bidVol": [500000, 0, 0, 0, 0], "bidPrice": ["11.00", 0, 0, 0, 0]}
    trader = _PosTrader([_held_position()])
    eng = _engine(trader, FakeTickSource(fixed={CODE: tick}), calendar)
    eng.prewarm(T_BUY)
    eng._plan_map = {CODE: _plan()}

    sold = eng.run_sell_pass(T_BUY, eng.build_sell_books(T_BUY), session="intraday")
    assert sold == []                       # 封板稳 → 秒板续持 HOLD
    assert trader.order_calls == []         # 未发任何卖单


def test_known_limitation_plan_missing_sealed_is_misjudged(calendar):
    """红线/已知限制(国金对接核对 B1)：真封板隔夜票若不在今日 watchlist(plan 缺失) → is_sealed 被误判 False，
    无强先验时 run_sell_pass 兜底会误清仓。这正是「生产卖出由 QMT_SELL_PASS_LIVE 默认关门控」要防的场景；
    本用例固化该限制(plan 缺失时确会误清)，待阶段1 T1.2 隔夜持仓 is_sealed/封流比补数修复后应转为 HOLD。
    """
    # tick 顶涨停封死(真封板)，但 _plan_map 不含该票(模拟隔夜连板不在今日新名单)。
    tick = {"lastPrice": "11.00", "lastClose": "10.00", "bidVol": [500000, 0, 0, 0, 0], "bidPrice": ["11.00", 0, 0, 0, 0]}
    trader = _PosTrader([_held_position()])
    eng = _engine(trader, FakeTickSource(fixed={CODE: tick}), calendar)
    eng.prewarm(T_BUY)
    eng._plan_map = {}   # plan 缺失 → is_sealed 无涨停价基准 → 误判 False

    books = eng.build_sell_books(T_BUY)
    assert books[CODE].is_sealed is False            # B1：真封板被误判非封板
    sold = eng.run_sell_pass(T_BUY, books, session="intraday")
    assert sold == [CODE]                             # 故被兜底误清(故 QMT_SELL_PASS_LIVE 默认关门控保护)


def test_wired_sell_pass_clears_weak_nonsealed(calendar):
    """接线端到端:弱势非封板 + 无续板先验 → run_sell_pass 兜底 CLEAR(卖出)。"""
    tick = {"lastPrice": "9.80", "lastClose": "10.00"}   # 低开走弱、未封板、无封单
    trader = _PosTrader([_held_position()])
    eng = _engine(trader, FakeTickSource(fixed={CODE: tick}), calendar)
    eng.prewarm(T_BUY)
    eng._plan_map = {CODE: _plan()}

    sold = eng.run_sell_pass(T_BUY, eng.build_sell_books(T_BUY), session="intraday")
    assert sold == [CODE]                    # 无续板先验 + 非封板 → 安全默认了结
    assert len(trader.order_calls) == 1
    assert trader.order_calls[0][0] == CODE  # 发出该票卖单


def test_build_sell_books_uses_injected_cross_frame_builder(calendar):
    """C-3 接线端到端：engine.set_sell_cross_frame_builder 注入后，build_sell_books 产出的盘口跨帧字段被填充
    （证明 T1.2 真机计算器注入即生效，无需改 decider/下游）。"""
    tick = {"lastPrice": "11.00", "lastClose": "10.00", "bidVol": [500000, 0, 0, 0, 0], "bidPrice": ["11.00", 0, 0, 0, 0]}
    eng = _engine(_PosTrader([_held_position()]), FakeTickSource(fixed={CODE: tick}), calendar)
    eng.prewarm(T_BUY)
    eng._plan_map = {CODE: _plan()}
    # 默认（未注入）：跨帧字段保守默认。
    assert eng.build_sell_books(T_BUY)[CODE].broke_board is False
    # 注入计算器（模拟 T1.2 真机帧历史 builder）：填充炸板字段。
    eng.set_sell_cross_frame_builder(lambda ts_code, t, ob: setattr(ob, "broke_board", True))
    assert eng.build_sell_books(T_BUY)[CODE].broke_board is True


# ===========================================================================
# 4. _build_prior_provider 续板先验闭包
# ===========================================================================
class _FakeWatchlistSource:
    def __init__(self, rows, raises=False):
        self._rows = rows
        self._raises = raises
        self.fetch_count = 0

    def fetch(self, target_trade_date):
        self.fetch_count += 1
        if self._raises:
            raise RuntimeError("db locked")
        return list(self._rows)


class _FakeStack:
    def __init__(self, watchlist_source):
        self.watchlist_source = watchlist_source


def _selected_row(ts_code=CODE, cont="0.70") -> SelectedStockRow:
    return SelectedStockRow(
        ts_code=ts_code, trade_date=T_SIGNAL, target_trade_date=T_BUY,
        continuation_prob=Decimal(cont), role="龙头", strategy="打板", market_state="高潮",
        fail_conditions=["放量炸板"],
    )


def test_prior_provider_maps_row_to_signal_prior():
    from qmt_strategy.app.run import _build_prior_provider

    src = _FakeWatchlistSource([_selected_row()])
    provider = _build_prior_provider(_FakeStack(src), RecordingLogger())
    prior = provider(CODE, T_BUY)
    assert prior is not None
    assert prior.continuation_prob == Decimal("0.70")
    assert prior.role == "龙头" and prior.strategy == "打板"
    assert prior.fail_conditions == ["放量炸板"]
    # 口径变更（2026-06-21）：SignalPrior 不再携带缺测字段（B3 持仓强卖已下线，卖出交由实时盘口）。
    assert not hasattr(prior, "data_missing")


# 口径变更（2026-06-21）：原 test_prior_provider_carries_data_missing_for_b3 随 B3 下线移除——
# 缺测标记不再透传进卖出先验 SignalPrior，仅在买入侧（SelectedStockRow→buy_prefilter）拦截。


def test_prior_provider_none_for_missing_and_caches_per_day():
    from qmt_strategy.app.run import _build_prior_provider

    src = _FakeWatchlistSource([_selected_row(ts_code=CODE)])
    provider = _build_prior_provider(_FakeStack(src), RecordingLogger())
    # 命中
    assert provider(CODE, T_BUY) is not None
    # 不在名单 → None
    assert provider("000999.SZ", T_BUY) is None
    # 同日多次调用只 fetch 一次(按日缓存)
    assert src.fetch_count == 1


def test_prior_provider_degrades_on_fetch_error():
    from qmt_strategy.app.run import _build_prior_provider

    src = _FakeWatchlistSource([], raises=True)
    logger = RecordingLogger()
    provider = _build_prior_provider(_FakeStack(src), logger)
    assert provider(CODE, T_BUY) is None        # fetch 异常 → 按空名单降级,返回 None,不抛
    assert "prior_provider_fetch_failed" in [e for _l, e, _f in logger.records]
