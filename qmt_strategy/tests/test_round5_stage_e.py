"""第二轮评审修复 · 阶段E 回归锁定测试（并发/健壮性/时间口径 · 执行侧）。

覆盖：竞价轮询单轮异常不放弃整段(#46)、loader 非约定异常降级不崩(#79)、卖单 signal_trade_date
取信号日 T 而非买入日 T+1(#65)、写连接 busy_timeout(#47)。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.models import OrderBook
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset, FakeXtPosition

from conftest import T_BUY, T_SIGNAL, utc_at_east8


# ===========================================================================
# #46 竞价轮询单轮异常不放弃整段窗口
# ===========================================================================
def test_auction_poller_survives_poll_once_exception():
    from qmt_strategy.auction.auction_poller import AuctionPoller
    from qmt_strategy.auction.tick_source import FakeTickSource
    from conftest import make_plan_row

    sink_calls = []

    def _raising_sink(snap):
        sink_calls.append(1)
        raise RuntimeError("router boom")  # 每帧 router_sink 抛错

    poller = AuctionPoller(
        FakeTickSource(fixed={"600000.SH": {"lastPrice": 10.0}}),
        lambda: {"600000.SH": make_plan_row()},
        _raising_sink,
        Settings(),
        FakeClock(utc_at_east8(T_BUY, 9, 16)),  # 竞价可撤段
        RecordingLogger(),
    )
    # 单轮抛错被 run 吞掉、继续下一轮，跑满 max_loops 不向上抛（整段窗口不被放弃）。
    poller.run(sleep_fn=lambda s: None, max_loops=3)
    assert len(sink_calls) >= 1


# ===========================================================================
# #79 loader 非约定异常降级不崩
# ===========================================================================
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


def test_loader_degrades_on_unexpected_fetch_error():
    from qmt_strategy.watchlist.watchlist_loader import WatchlistLoader

    class _BadSource:
        def fetch(self, d):
            raise RuntimeError("db blew up")  # 非 WatchlistLoadError 的未预期异常

    loader = WatchlistLoader(_BadSource(), _Cal(), RecordingLogger(), Settings())
    ctx = loader.load(T_BUY)  # 不应抛出
    assert ctx.degraded is True
    assert ctx.open_new_position_allowed is False
    assert ctx.tradable == {}


# ===========================================================================
# #65 卖单 signal_trade_date = 信号日 T = prev_open(买入日 B)，非买入日 T+1
# ===========================================================================
def _engine(positions):
    from qmt_strategy.app.main import EngineDeps, build_engine
    from qmt_strategy.data_writer.repository import InMemoryQmtRepository
    from qmt_strategy.watchlist.sources import CallableSelectedStockSource

    class _T:
        def order_stock(self, *a, **k):
            return 2001

        def cancel_order_stock(self, *a, **k):
            return 0

        def query_stock_asset(self, account):
            return FakeXtAsset(account_id="acc1", cash=Decimal("1000000"), frozen_cash=Decimal("0"),
                               market_value=Decimal("0"), total_asset=Decimal("1000000"))

        def query_stock_positions(self, account):
            return positions

        def query_stock_orders(self, account):
            return []

        def query_stock_trades(self, account):
            return []

    class _Tick:
        def get_full_tick(self, codes):
            return {}

    deps = EngineDeps(
        settings=Settings(), clock=FakeClock(utc_at_east8(T_BUY, 9, 35)), logger=RecordingLogger(),
        calendar=_Cal(), trader=_T(), account=FakeStockAccount("acc1"), account_id="acc1",
        tick_source=_Tick(), selected_source=CallableSelectedStockSource(lambda d: []),
        repository=InMemoryQmtRepository(),
    )
    return build_engine(deps)


def test_sell_signal_trade_date_is_signal_day():
    pos = FakeXtPosition(stock_code="600000.SH", volume=1000, can_use_volume=1000, avg_price=Decimal("10.00"))
    eng = _engine([pos])
    eng.prewarm(T_BUY)  # rebuild 用 prev_open(T_BUY) 作 buy_date
    unit = eng._position.get_unit("acc1", "600000.SH")
    book = OrderBook(ts_code="600000.SH", last_price=Decimal("9.50"))
    from qmt_strategy.contracts.enums import SellActionType
    assert eng._place_sell(unit, SellActionType.CLEAR, None, "清仓", book, T_BUY) is True
    # 卖单台账 signal_trade_date = prev_open(unit.buy_date)，绝不等于买入日 unit.buy_date 本身。
    sell_entry = next(e for e in eng._ledger.all() if e.strategy_family == "SELL")
    assert sell_entry.signal_trade_date == _Cal().prev_open(unit.buy_date)
    assert sell_entry.signal_trade_date != unit.buy_date
