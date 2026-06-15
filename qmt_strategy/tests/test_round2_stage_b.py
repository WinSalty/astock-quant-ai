"""第二轮评审修复 · 阶段B 回归锁定测试（风控护栏与价位口径真正生效）。

覆盖：风控配置(#33)、行情/下单中断 FREEZE(#14#15)、单票止损(#39)、限价合法性(#67)、
账户回撤 fail-closed(#70)、ST 5% 与价位口径(#5#18#45)、name 透传(#63)、ST 过滤(#10)、
对账阻断次日开仓(#9)、对账同向偏差/弱关联(#34#35#36)。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from qmt_strategy.common import board_rules
from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import (
    Board,
    OrderState,
    OrderStatus,
    PriceSource,
    SellActionType,
    TradeSide,
)
from qmt_strategy.contracts.models import LedgerEntry, OrderBook, OrderRecord, SelectedStockRow
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset, FakeXtPosition
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.order.local_ledger import InMemoryLocalLedger
from qmt_strategy.reconcile.reconcile import Reconcile
from qmt_strategy.storage import mappers
from qmt_strategy.watchlist.remote_watchlist import watchlist_item_to_selected

from conftest import T_BUY, T_SELL, T_SIGNAL, utc_at_east8

T2 = T_BUY


# ===========================================================================
# #33 settings：target_position_ratio=0 不被静默改满仓
# ===========================================================================
def test_target_position_ratio_zero_preserved():
    assert Settings.from_env({"QMT_TARGET_POSITION_RATIO": "0"}).target_position_ratio == Decimal("0")
    assert Settings.from_env({}).target_position_ratio == Decimal("1.0")  # 未配置→默认 1.0
    assert Settings.from_env({"QMT_TARGET_POSITION_RATIO": "0.8"}).target_position_ratio == Decimal("0.8")


# ===========================================================================
# #5#18#45 board_rules：ST 5%、signal_close<=0→MISSING、板块口径
# ===========================================================================
def _row(ts_code="600000.SH", close="10.00", name=None):
    return SelectedStockRow(
        ts_code=ts_code, trade_date=T_SIGNAL, target_trade_date=T_BUY,
        signal_close=Decimal(close) if close is not None else None, name=name,
    )


def test_signal_close_zero_is_missing():
    pb = board_rules.budget_prices(_row(close="0"))
    assert pb.price_source == PriceSource.MISSING


def test_signal_close_negative_is_missing():
    pb = board_rules.budget_prices(_row(close="-1"))
    assert pb.price_source == PriceSource.MISSING


def test_non_st_main_board_10pct():
    pb = board_rules.budget_prices(_row(ts_code="600000.SH", close="10.00", name="贵州茅台"))
    assert pb.limit_up_price == Decimal("11.00") and pb.board == Board.MAIN


def test_st_main_board_5pct():
    pb = board_rules.budget_prices(_row(ts_code="600000.SH", close="10.00", name="ST华业"))
    assert pb.limit_up_price == Decimal("10.50")  # 主板 ST 涨停 5%


def test_chinext_st_still_20pct():
    pb = board_rules.budget_prices(_row(ts_code="300001.SZ", close="10.00", name="*ST创业"))
    assert pb.limit_up_price == Decimal("12.00")  # 创业板 ST 仍 20%（无 5% 制度）


# ===========================================================================
# #63 name 透传：契约 → SelectedStockRow → SQLite 行 round-trip
# ===========================================================================
def test_name_plumbed_through_contract_and_mappers():
    item = {"ts_code": "600000.SH", "trade_date": "2026-06-11", "close": "10.00",
            "name": "ST华业", "tradable_flag": "TRADABLE"}
    row = watchlist_item_to_selected(item, T_BUY)
    assert row.name == "ST华业"
    d = mappers.selected_to_row(row)
    assert d["name"] == "ST华业"
    assert mappers.row_to_selected(d).name == "ST华业"


# ===========================================================================
# #10 ST 过滤：loader 把 ST 票转观察名单（不下单）
# ===========================================================================
def test_loader_st_to_watch_only(calendar):
    from qmt_strategy.watchlist.sources import CallableSelectedStockSource
    from qmt_strategy.watchlist.watchlist_loader import WatchlistLoader

    rows = [
        SelectedStockRow(ts_code="600000.SH", trade_date=T_SIGNAL, target_trade_date=T_BUY,
                         signal_close=Decimal("10.00"), name="ST华业", market_state="参与", tradable_flag=True),
        SelectedStockRow(ts_code="600036.SH", trade_date=T_SIGNAL, target_trade_date=T_BUY,
                         signal_close=Decimal("10.00"), name="招商银行", market_state="参与", tradable_flag=True),
    ]
    src = CallableSelectedStockSource(lambda d: rows, source_name="t")
    loader = WatchlistLoader(src, calendar, RecordingLogger(), Settings())
    ctx = loader.load(T_BUY)
    assert "600000.SH" not in ctx.tradable  # ST 不进可交易
    assert "600036.SH" in ctx.tradable
    assert any(e.norm_code == "600000.SH" for e in ctx.watch_only)


# ===========================================================================
# #34#35#36 reconcile：同向偏差告警、卖单弱关联、买卖方向校验
# ===========================================================================
class _ReconRepo(InMemoryQmtRepository):
    """提供 get_orders/get_trades，且可注入 cash 变动用于资产对账。"""

    def __init__(self, account_change=None):
        super().__init__()
        self._account_change = account_change

    def get_account_change(self, account_id, trade_date):
        return self._account_change


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


def _led_buy(order_id=None, ts="600000.SH"):
    return LedgerEntry(
        biz_order_no="b1", account_id="acc1", target_trade_date=T_BUY, ts_code=ts,
        strategy_family="打板", side=TradeSide.BUY, plan_volume=1000, plan_price=Decimal("11"),
        order_remark=f"LUP|{T_SIGNAL.isoformat()}|{ts}", signal_trade_date=T_SIGNAL,
        state=OrderState.SUBMITTED, order_id=order_id,
    )


def _led_sell(order_id=None, ts="600000.SH"):
    return LedgerEntry(
        biz_order_no="s1", account_id="acc1", target_trade_date=T_BUY, ts_code=ts,
        strategy_family="SELL", side=TradeSide.SELL, plan_volume=1000, plan_price=Decimal("12"),
        order_remark="SELL|止损", signal_trade_date=T_SIGNAL, state=OrderState.SUBMITTED, order_id=order_id,
    )


def _ord(order_id, side, ts="600000.SH", status=OrderStatus.TRADED, remark=None):
    return OrderRecord(
        account_id="acc1", trade_date=T_BUY, ts_code=ts, qmt_stock_code=ts, order_id=order_id,
        trade_side=side, order_volume=1000, order_status=status, traded_volume=1000,
        order_remark=remark,
    )


def _reconcile(ledger_rows, order_rows, account_change=None):
    led = InMemoryLocalLedger()
    for e in ledger_rows:
        led.insert(e)
    repo = _ReconRepo(account_change=account_change)
    for o in order_rows:
        repo.upsert_order(o)
    rc = Reconcile(led, repo, RecordingLogger(), _Cal(), account_id="acc1")
    return rc.run(T_BUY)


def test_sell_weak_match_no_false_missing():
    """#35：卖单(remark=SELL|…、无 order_id)按归一 ts_code+方向弱关联，不被误报漏单。"""
    rep = _reconcile([_led_sell(order_id=None)], [_ord(50, TradeSide.SELL, remark="SELL|止损")])
    assert rep.matched_orders == 1
    assert not any(d.kind == "missing_report" for d in rep.order_discrepancies)


def test_weak_match_side_mismatch_not_matched():
    """#36：买单台账不应错配卖单回报（方向不一致）。"""
    rep = _reconcile([_led_buy(order_id=None)], [_ord(50, TradeSide.SELL, remark="LUP|2026-06-11|600000.SH")])
    assert any(d.kind == "missing_report" for d in rep.order_discrepancies)  # 方向不符→不弱配→漏单


def test_asset_same_direction_shortfall_alerts():
    """#34：同向但金额超额偏差（漏采一笔买入）也告警，不再只在方向相反时报。"""
    # 台账/回报无关此项；构造成交净流出 1万，但账户现金少了 5万（漏采 4万买入，方向同为流出）。
    trades_repo_change = Decimal("-50000")
    # 用一笔成交制造 net_flow = -10000（买 1000股×10）。
    from qmt_strategy.contracts.models import TradeRecord
    led = InMemoryLocalLedger()
    repo = _ReconRepo(account_change=trades_repo_change)
    repo.upsert_trade(TradeRecord(
        account_id="acc1", trade_date=T_BUY, ts_code="600000.SH", qmt_stock_code="600000.SH",
        traded_id="t1", trade_side=TradeSide.BUY, traded_price=Decimal("10"), traded_volume=1000,
        traded_amount=Decimal("10000"),
    ))
    rc = Reconcile(led, repo, RecordingLogger(), _Cal(), account_id="acc1")
    rep = rc.run(T_BUY)
    assert rep.asset_discrepancy is not None  # 同向超额偏差被告警


# ===========================================================================
# Engine 级：止损/限价闸/回撤 fail-closed/通道&行情冻结/对账阻断
# ===========================================================================
class _StubTick:
    def get_full_tick(self, codes):
        return {}


class _CfgTrader:
    def __init__(self, total_asset=1_000_000, positions=None):
        self._ta = Decimal(str(total_asset))
        self._positions = positions or []
        self.order_calls = []

    def order_stock(self, account, code, otype, vol, ptype, price, sname="", remark=""):
        self.order_calls.append((code, otype, vol, price))
        return 100 + len(self.order_calls)

    def cancel_order_stock(self, account, oid):
        return 0

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc1", cash=self._ta, frozen_cash=Decimal("0"),
                           market_value=Decimal("0"), total_asset=self._ta)

    def query_stock_positions(self, account):
        return self._positions

    def query_stock_orders(self, account):
        return []

    def query_stock_trades(self, account):
        return []


def _engine(trader, env=None, repository=None):
    from qmt_strategy.app.main import EngineDeps, build_engine
    from qmt_strategy.watchlist.sources import CallableSelectedStockSource

    repo = repository if repository is not None else InMemoryQmtRepository()
    deps = EngineDeps(
        settings=Settings.from_env(env or {}),
        clock=FakeClock(utc_at_east8(T_BUY, 9, 0)),
        logger=RecordingLogger(),
        calendar=_Cal(),
        trader=trader,
        account=FakeStockAccount("acc1"),
        account_id="acc1",
        tick_source=_StubTick(),
        selected_source=CallableSelectedStockSource(lambda d: []),
        repository=repo,
    )
    return build_engine(deps), repo


def test_unit_stop_loss_triggers_clear():
    """#39：单票浮亏击穿 stock_float_loss_limit → 强制清仓（止损真正生效）。"""
    pos = FakeXtPosition(stock_code="600000.SH", volume=1000, can_use_volume=1000, avg_price=Decimal("10.00"))
    trader = _CfgTrader(positions=[pos])
    eng, _ = _engine(trader, env={"QMT_STOCK_FLOAT_LOSS_LIMIT": "0.05"})
    eng.prewarm(T_BUY)
    book = OrderBook(ts_code="600000.SH", last_price=Decimal("9.40"))  # 浮亏 6% > 5%
    sold = eng.run_sell_pass(T_BUY, {"600000.SH": book}, session="intraday")
    assert sold == ["600000.SH"]
    assert trader.order_calls[-1][2] == 1000  # CLEAR 全部可用


def test_unit_stop_loss_not_triggered_within_limit():
    """#39：浮亏未达阈值不强制止损（由常规决策处理）。"""
    pos = FakeXtPosition(stock_code="600000.SH", volume=1000, can_use_volume=1000, avg_price=Decimal("10.00"))
    eng, _ = _engine(_CfgTrader(positions=[pos]), env={"QMT_STOCK_FLOAT_LOSS_LIMIT": "0.10"})
    eng.prewarm(T_BUY)
    unit = eng._position.get_unit("acc1", "600000.SH")
    book = OrderBook(ts_code="600000.SH", last_price=Decimal("9.70"))  # 浮亏 3% < 10%
    assert eng._unit_stop_loss_breached(unit, book) is False


def test_limit_price_guard_rejects_above_cap():
    """#67：限价超法定涨停价上界 → 拒发。"""
    from qmt_strategy.contracts.enums import AuctionPhase, EntryAction, OrderPhase
    from qmt_strategy.contracts.models import AuctionSnapshot, EntryDecision, PlanRow

    eng, _ = _engine(_CfgTrader())
    plan = PlanRow(ts_code="600000.SH", signal_trade_date=T_SIGNAL, target_trade_date=T_BUY,
                   limit_up_price=Decimal("11.00"))
    snap = AuctionSnapshot(ts_code="600000.SH", phase=AuctionPhase.AUCTION_CANCELABLE,
                           ts=utc_at_east8(T_BUY, 9, 16), last_price=Decimal("11.00"))
    dec = EntryDecision(ts_code="600000.SH", signal_trade_date=T_SIGNAL, target_trade_date=T_BUY,
                        strategy_family="打板", setup="连板", action=EntryAction.CHASE_LIMIT_UP,
                        decided_at=utc_at_east8(T_BUY, 9, 16), reason="t",
                        limit_price=Decimal("11.50"), order_phase=OrderPhase.OPENING)  # 超 11.00 上界
    ok, why = eng._limit_price_sane(dec, plan, snap)
    assert ok is False and "超法定涨停价" in why


def test_limit_price_guard_rejects_deviation():
    """#67：限价偏离盘口现价超 guard → 拒发。"""
    from qmt_strategy.contracts.enums import AuctionPhase, EntryAction, OrderPhase
    from qmt_strategy.contracts.models import AuctionSnapshot, EntryDecision, PlanRow

    eng, _ = _engine(_CfgTrader(), env={"QMT_PRICE_DEVIATION_GUARD_PCT": "0.05"})
    plan = PlanRow(ts_code="600000.SH", signal_trade_date=T_SIGNAL, target_trade_date=T_BUY,
                   limit_up_price=Decimal("11.00"))
    snap = AuctionSnapshot(ts_code="600000.SH", phase=AuctionPhase.AUCTION_CANCELABLE,
                           ts=utc_at_east8(T_BUY, 9, 16), last_price=Decimal("10.00"))  # 现价 10
    dec = EntryDecision(ts_code="600000.SH", signal_trade_date=T_SIGNAL, target_trade_date=T_BUY,
                        strategy_family="打板", setup="连板", action=EntryAction.CHASE_LIMIT_UP,
                        decided_at=utc_at_east8(T_BUY, 9, 16), reason="t",
                        limit_price=Decimal("11.00"), order_phase=OrderPhase.OPENING)  # 偏离 10% > 5%
    ok, why = eng._limit_price_sane(dec, plan, snap)
    assert ok is False and "偏离" in why


def test_open_blocked_when_drawdown_unknown():
    """#70：有日初基线但当前资产查询失败 → 禁开新仓（fail-closed，不再放行）。"""
    class _FlakyTrader(_CfgTrader):
        def __init__(self):
            super().__init__(1_000_000)
            self._asset_calls = 0

        def query_stock_asset(self, account):
            self._asset_calls += 1
            if self._asset_calls == 1:
                return FakeXtAsset(account_id="acc1", cash=Decimal("1000000"), frozen_cash=Decimal("0"),
                                   market_value=Decimal("0"), total_asset=Decimal("1000000"))
            raise RuntimeError("asset query down")

    eng, _ = _engine(_FlakyTrader())
    eng.prewarm(T_BUY)  # 基线已抓(1M)
    assert eng._open_blocked_by_risk("600000.SH") is True


def test_reconnect_backfill_does_not_freeze_on_failure():
    """评审三轮 EXEC-sched-02：on_reconnect_backfill 不再独占管理 _trade_conn_ok。

    解冻/冻结权威源已迁到「连接就绪事件」(report_trade_conn)；补采失败仅强告警、不再据此把
    _trade_conn_ok 翻成 False 永久冻结卖出（连接已由 guard 就绪解冻，持仓由下次 prewarm/收盘快照校准）。
    """
    eng, _ = _engine(_CfgTrader())
    eng.prewarm(T_BUY)
    eng._trade_conn_ok = True  # 连接已就绪（guard 已 report_trade_conn(True)）
    eng._snapshot.run_backfill = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("conn down"))
    eng.on_reconnect_backfill()
    assert eng._trade_conn_ok is True  # 补采失败不再据此冻结
    assert "engine_reconnect_backfill_failed" in eng._logger.events()


def test_reconnect_backfill_does_not_self_unfreeze():
    """评审三轮 EXEC-sched-02：补采成功也不擅自解冻——解冻是 report_trade_conn(True) 的职责。"""
    eng, _ = _engine(_CfgTrader())
    eng.prewarm(T_BUY)
    eng._trade_conn_ok = False  # 模拟断线冻结态
    eng.on_reconnect_backfill()  # 补采成功，但不触碰 _trade_conn_ok
    assert eng._trade_conn_ok is False


def test_report_trade_conn_toggles_flag():
    """评审三轮 EXEC-sched-02：report_trade_conn 真正切换 _trade_conn_ok 并清零探活失败计数。"""
    eng, _ = _engine(_CfgTrader())
    eng._trade_conn_fail_streak = 2
    eng.report_trade_conn(False)
    assert eng._trade_conn_ok is False
    eng.report_trade_conn(True)
    assert eng._trade_conn_ok is True
    assert eng._trade_conn_fail_streak == 0  # 解冻同时清零失败计数


def test_trade_conn_heartbeat_marks_down_after_n_failures():
    """评审三轮 EXEC-risk-05：盘中主动探活连续失败达阈值才 FREEZE，单次成功清零、不自动解冻。"""
    class _DeadAssetTrader(_CfgTrader):
        def query_stock_asset(self, account):
            raise RuntimeError("rpc timeout")

    eng, _ = _engine(_DeadAssetTrader(), env={"QMT_TRADE_CONN_HEARTBEAT_FAIL_THRESHOLD": "3"})
    eng.trade_conn_heartbeat()
    assert eng._trade_conn_ok is True   # 1 次失败未达阈值
    eng.trade_conn_heartbeat()
    assert eng._trade_conn_ok is True   # 2 次仍未达
    eng.trade_conn_heartbeat()
    assert eng._trade_conn_ok is False  # 第 3 次达阈值 → FREEZE

    # 心跳恢复成功：清零失败计数，但不擅自解冻（解冻是 report_trade_conn(True) 的职责）。
    eng_ok, _ = _engine(_CfgTrader())
    eng_ok._trade_conn_fail_streak = 1
    eng_ok.trade_conn_heartbeat()  # query_stock_asset 成功
    assert eng_ok._trade_conn_fail_streak == 0
    assert eng_ok._trade_conn_ok is True


def test_trade_conn_heartbeat_requests_reconnect_once_on_freeze():
    """评审三轮 P1-1：心跳跨阈值冻结时请求一次重连（把静默死亡当断线，让连接就绪事件接管解冻）。"""
    class _DeadAssetTrader(_CfgTrader):
        def query_stock_asset(self, account):
            raise RuntimeError("rpc timeout")

    eng, _ = _engine(_DeadAssetTrader(), env={"QMT_TRADE_CONN_HEARTBEAT_FAIL_THRESHOLD": "2"})
    reconnects = {"n": 0}
    eng.set_reconnect_requester(lambda: reconnects.__setitem__("n", reconnects["n"] + 1))
    eng.trade_conn_heartbeat()            # 1 次失败
    assert reconnects["n"] == 0
    eng.trade_conn_heartbeat()            # 2 次达阈值 → 首次冻结 → 请求 1 次重连
    assert eng._trade_conn_ok is False
    assert reconnects["n"] == 1
    eng.trade_conn_heartbeat()            # 已冻结，后续失败不再重复请求（避免重连风暴，由主循环退避重连）
    assert reconnects["n"] == 1


def test_report_market_feed_toggles_flag():
    """#15：行情健康上报真正切换 _market_feed_ok（非死代码）。"""
    eng, _ = _engine(_CfgTrader())
    eng.report_market_feed(False)
    assert eng._market_feed_ok is False
    eng.report_market_feed(True)
    assert eng._market_feed_ok is True


def test_auction_poller_reports_feed_down_on_tick_failure():
    """#15：竞价 tick 整体失败 → feed_health_sink(False)。"""
    from qmt_strategy.auction.auction_poller import AuctionPoller
    from qmt_strategy.contracts.errors import TickSourceError
    from conftest import make_plan_row

    class _BadTick:
        def get_full_tick(self, codes):
            raise TickSourceError("down")

    health = []
    poller = AuctionPoller(
        _BadTick(), lambda: {"600000.SH": make_plan_row()}, lambda snap: None,
        Settings(), FakeClock(utc_at_east8(T_BUY, 9, 16)), RecordingLogger(),
        feed_health_sink=lambda ok: health.append(ok),
    )
    poller.poll_once(utc_at_east8(T_BUY, 9, 16))
    assert health == [False]


def test_reconcile_block_persists_and_blocks_next_day_open():
    """#9：对账未通过标记持久化，次日新进程 prewarm 读到即阻断开仓。"""
    repo = InMemoryQmtRepository()
    eng1, _ = _engine(_CfgTrader(), repository=repo)
    eng1._set_reconcile_block(T_BUY, 1, 0)
    assert eng1._reconcile_blocked is True
    assert repo.get_flag("reconcile_blocked") == T_BUY.isoformat()
    eng2, _ = _engine(_CfgTrader(), repository=repo)
    eng2.prewarm(T_SELL)
    assert eng2._reconcile_blocked is True
    assert eng2._open_blocked_by_risk("600000.SH") is True


def test_close_batch_sets_reconcile_block_on_discrepancy():
    """#9：close_batch 对账检出漏单 → 置持久阻断标记。"""
    repo = InMemoryQmtRepository()
    eng, _ = _engine(_CfgTrader(), repository=repo)
    eng._ledger.insert(_led_buy(order_id=999))  # 台账有计划单但 qmt_order 无 → missing_report
    eng.close_batch(T_BUY)
    assert repo.get_flag("reconcile_blocked") == T_BUY.isoformat()
    assert eng._reconcile_blocked is True


def test_close_batch_no_block_on_benign_order_failed():
    """复审 P1-1：台账 ERROR（下单失败、本就无回报）是良性态，不触发阻断（避免高频误禁开）。"""
    repo = InMemoryQmtRepository()
    eng, _ = _engine(_CfgTrader(), repository=repo)
    e = _led_buy(order_id=None)
    e.state = OrderState.ERROR  # 同步下单失败留痕
    eng._ledger.insert(e)
    eng.close_batch(T_BUY)
    assert repo.get_flag("reconcile_blocked") is None  # order_failed 不阻断
    assert eng._reconcile_blocked is False


def test_stop_loss_ignores_nonpositive_price():
    """复审 P1-2：0/非正现价不凭空触发止损（坏 tick 防御）。"""
    pos = FakeXtPosition(stock_code="600000.SH", volume=1000, can_use_volume=1000, avg_price=Decimal("10.00"))
    eng, _ = _engine(_CfgTrader(positions=[pos]), env={"QMT_STOCK_FLOAT_LOSS_LIMIT": "0.05"})
    eng.prewarm(T_BUY)
    unit = eng._position.get_unit("acc1", "600000.SH")
    assert eng._unit_stop_loss_breached(unit, OrderBook(ts_code="600000.SH", last_price=Decimal("0"))) is False
    assert eng._unit_stop_loss_breached(unit, OrderBook(ts_code="600000.SH", last_price=None)) is False
    # 正常浮亏 6% > 5% → 仍正确触发
    assert eng._unit_stop_loss_breached(unit, OrderBook(ts_code="600000.SH", last_price=Decimal("9.40"))) is True


def test_sell_weak_match_one_to_one():
    """复审 P2-1：同标的多笔卖单（无 order_id）与多条回报一对一关联，不全配到同一条。"""
    e1 = _led_sell(order_id=None)
    e1.biz_order_no = "s1"
    e2 = _led_sell(order_id=None)
    e2.biz_order_no = "s2"
    o1 = _ord(50, TradeSide.SELL, remark="SELL|减仓")
    o2 = _ord(51, TradeSide.SELL, remark="SELL|清仓")
    rep = _reconcile([e1, e2], [o1, o2])
    assert rep.matched_orders == 2  # 两笔各配一条，无一对多
    assert not any(d.kind == "manual_order" for d in rep.order_discrepancies)
