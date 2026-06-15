"""A1 门槛集成验收（评审三轮 EXEC-sched-01/02/03）：盘中断线→自动换 session 重连→新句柄补采→解冻。

串联 ConnectionGuard + Engine + TraderHolder + ExecCallback，复现 P0-1/P0-2 的真实回归洞：
- 断线回调先经 guard.on_disconnected → reconnect（换新 session，connect/subscribe 成功）；
- 解冻权威源绑定「连接就绪事件」(report_trade_conn)，而非一次注定失败的补采；
- 补采用重连后的【新】trader 句柄（holder 已 set 新实例）。
"""

from __future__ import annotations

from decimal import Decimal

from qmt_strategy.adapters.xt_real import TraderHolder
from qmt_strategy.app.main import EngineDeps, build_engine
from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.connection.connection_guard import ConnectionGuard
from qmt_strategy.contracts.enums import RiskVerdict
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.watchlist.sources import CallableSelectedStockSource
from tests.conftest import T_BUY, utc_at_east8


class _Cal:
    def is_open(self, d):
        return d.weekday() < 5

    def next_open(self, d):
        from datetime import timedelta
        x = d + timedelta(days=1)
        while x.weekday() >= 5:
            x += timedelta(days=1)
        return x

    def prev_open(self, d):
        from datetime import timedelta
        x = d - timedelta(days=1)
        while x.weekday() >= 5:
            x -= timedelta(days=1)
        return x


class _StubTick:
    def get_full_tick(self, codes):
        return {}


class _RecoTrader:
    """A1 集成用 fake trader：connect_rc 可注入；记录是否被补采查询（证明用新句柄补采）。"""

    def __init__(self, *, connect_rc: int = 0):
        self._connect_rc = connect_rc
        self.position_queries = 0

    def register_callback(self, cb):
        pass

    def start(self):
        pass

    def connect(self):
        return self._connect_rc

    def subscribe(self, account):
        return 0

    def run_forever(self):
        pass

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc1", cash=Decimal("1000000"), frozen_cash=Decimal("0"),
                           market_value=Decimal("0"), total_asset=Decimal("1000000"))

    def query_stock_positions(self, account):
        self.position_queries += 1
        return []

    def query_stock_orders(self, account):
        return []

    def query_stock_trades(self, account):
        return []


def _build(reconnect_connect_rc: int = 0):
    """装配 engine + guard + holder（trader_factory 换新 trader 时 set 进 holder，引擎无感知）。"""
    holder = TraderHolder()
    logger = RecordingLogger()
    created = []

    def trader_factory(session_id):
        # 初连用成功 trader；之后（重连）用注入 connect_rc 的 trader。
        rc = 0 if not created else reconnect_connect_rc
        t = _RecoTrader(connect_rc=rc)
        t.session_id = session_id
        holder.set(t)
        created.append(t)
        return t

    deps = EngineDeps(
        settings=Settings.from_env({}),
        clock=FakeClock(utc_at_east8(T_BUY, 9, 35)),
        logger=logger,
        calendar=_Cal(),
        trader=holder,
        account=FakeStockAccount("acc1"),
        account_id="acc1",
        tick_source=_StubTick(),
        selected_source=CallableSelectedStockSource(lambda d: []),
        repository=InMemoryQmtRepository(),
    )
    engine = build_engine(deps)

    sids = iter([1001, 2002, 3003, 4004])
    guard = ConnectionGuard(
        trader_factory=trader_factory,
        account=deps.account,
        callback=engine.callback,
        clock=deps.clock,
        logger=logger,
        session_id_provider=lambda: next(sids),
        on_reconnect_backfill=engine.on_reconnect_backfill,
        on_connection_state=engine.report_trade_conn,
    )
    # 延迟回填断线钩子（与 run.build_real_engine 一致）。
    engine.callback.set_on_disconnected_hook(guard.on_disconnected)
    return engine, guard, holder, created, logger


def test_intraday_disconnect_auto_reconnects_and_unfreezes():
    """盘中断线 → 自动换 session 重连 → 新句柄补采 → _trade_conn_ok 恢复 True（卖出不被永久冻结）。"""
    engine, guard, holder, created, _logger = _build(reconnect_connect_rc=0)

    # 初连就绪：连接就绪事件已解冻下单闸。
    assert guard.connect_and_subscribe() is True
    assert engine._trade_conn_ok is True
    engine.prewarm(T_BUY)
    first_session = guard.current_session_id
    first_trader = holder.current

    # 模拟盘中掉线冻结态（旧 P0 末态：补采用死句柄必败、_trade_conn_ok 永久 False）。
    engine.report_trade_conn(False)
    assert engine._trade_conn_ok is False

    # 触发真实断线回调链：ExecCallback.on_disconnected → guard.on_disconnected → reconnect。
    engine.callback.on_disconnected()

    # 重连就绪：换了新 session、guard.ready、_trade_conn_ok 恢复 True（解冻）。
    assert guard.ready is True
    assert guard.current_session_id != first_session
    assert engine._trade_conn_ok is True
    # 补采用的是重连后的【新】trader（holder 已换新实例）。
    assert holder.current is not first_trader
    assert holder.current.position_queries >= 1   # 新句柄被 on_reconnect_backfill 的持仓重建查询过
    # 解冻是连接就绪事件驱动的（gate 输入 _trade_conn_ok 已 True，卖出不再被通道冻结）。
    assert engine._risk.gate(
        market_state=None, market_feed_ok=True, trade_conn_ok=engine._trade_conn_ok,
    ).verdict != RiskVerdict.FREEZE


def test_disconnect_keeps_frozen_until_reconnect_ready():
    """重连 connect 失败 → 保持未就绪 + _trade_conn_ok 仍 False（fail-closed，不误解冻）。"""
    engine, guard, _holder, _created, _logger = _build(reconnect_connect_rc=-9)

    assert guard.connect_and_subscribe() is True
    engine.prewarm(T_BUY)

    # 断线 → 重连 connect 失败：未就绪 + 通道冻结保持。
    engine.callback.on_disconnected()
    assert guard.ready is False
    assert engine._trade_conn_ok is False
    # 通道冻结 → risk.gate FREEZE（卖出/开仓在重连真正就绪前不放行）。
    assert engine._risk.gate(
        market_state=None, market_feed_ok=True, trade_conn_ok=engine._trade_conn_ok,
    ).verdict == RiskVerdict.FREEZE
