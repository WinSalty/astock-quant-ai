"""order_executor 单测（§4.9 幂等 / 撤单 / 部分成交 / 成交确认 / 一字秒封 / 账户隔离 / 下单失败）。

全部用 fake/内存实现，不连真实 xttrader / MySQL：
  - FakeTrader 记录 order_stock / cancel_order_stock 调用次数与入参；query_stock_asset 返回 FakeXtAsset；
  - InMemoryLocalLedger 为锁定的真实台账实现；
  - FakeClock 注入固定东八区时刻（经 utc_at_east8 换算为 UTC naive）。
所有时间一律 UTC naive，时段判定经 common.auction_window（禁手工 ±8h）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import EntryAction, OrderPhase, OrderState, TradeSide
from qmt_strategy.contracts.models import EntryDecision
from qmt_strategy.contracts.xt_objects import FakeStockAccount, FakeXtAsset
from qmt_strategy.order.local_ledger import InMemoryLocalLedger
from qmt_strategy.order.order_executor import OrderExecutor

from conftest import utc_at_east8

T_SIGNAL = date(2026, 6, 11)
T_BUY = date(2026, 6, 12)


# ---------------------------------------------------------------------------
# fake trader：唯一记录 order_stock / cancel_order_stock 调用的桩
# ---------------------------------------------------------------------------
class FakeTrader:
    """模拟 XtTraderLike：记录下单/撤单调用，便于断言「调用次数」与「未调用」。"""

    def __init__(self, cash="1000000", next_order_id=1001):
        self.order_calls = []          # list of dict：每次 order_stock 的入参
        self.cancel_calls = []         # list of order_id
        self._cash = Decimal(str(cash))
        self._next_order_id = next_order_id

    def order_stock(self, account, stock_code, order_type, order_volume, price_type, price, strategy_name="", order_remark=""):
        oid = self._next_order_id
        self._next_order_id += 1
        self.order_calls.append(
            dict(
                account=account,
                stock_code=stock_code,
                order_type=order_type,
                order_volume=order_volume,
                price_type=price_type,
                price=price,
                strategy_name=strategy_name,
                order_remark=order_remark,
            )
        )
        return oid

    def cancel_order_stock(self, account, order_id):
        self.cancel_calls.append(order_id)
        return 0

    def query_stock_asset(self, account):
        return FakeXtAsset(account_id="acc", cash=self._cash, frozen_cash=Decimal("0"),
                           market_value=Decimal("0"), total_asset=self._cash)


def _decision(
    ts_code="600036.SH",
    action=EntryAction.CHASE_LIMIT_UP,
    limit_price=Decimal("11.00"),
    plan_volume=1000,
    order_phase=OrderPhase.AUCTION,
    strategy_family="打板",
    next_best=(),
    factors=None,
    signal_trade_date=T_SIGNAL,
):
    return EntryDecision(
        ts_code=ts_code,
        signal_trade_date=signal_trade_date,
        target_trade_date=T_BUY,
        strategy_family=strategy_family,
        setup="连板接力",
        action=action,
        decided_at=datetime(2026, 6, 12, 1, 16, 0),  # UTC naive
        reason="test",
        limit_price=limit_price,
        plan_volume=plan_volume,
        order_phase=order_phase,
        factors_snapshot=factors or {},
        next_best=next_best,
    )


def _executor(trader, clock, account_id="acc1", ledger=None, settings=None, logger=None):
    return OrderExecutor(
        trader=trader,
        account=FakeStockAccount(account_id=account_id),
        account_id=account_id,
        ledger=ledger or InMemoryLocalLedger(),
        settings=settings or Settings(),
        clock=clock,
        logger=logger or RecordingLogger(),
    )


@pytest.fixture
def clock_0916():
    """T+1 09:16 东八区（竞价可撤段）。"""
    return FakeClock(utc_at_east8(T_BUY, 9, 16, 0))


@pytest.fixture
def clock_0922():
    """T+1 09:22 东八区（9:20–9:25 锁定段，禁撤）。"""
    return FakeClock(utc_at_east8(T_BUY, 9, 22, 0))


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------
def test_idempotent_same_decision_places_once(clock_0916):
    """同一 EntryDecision 连续 place 两次 → order_stock 只调一次，第二次返回既有 biz_order_no。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    d = _decision()
    biz1 = ex.place(d)
    biz2 = ex.place(d)
    assert biz1 is not None
    assert biz1 == biz2                       # 第二次返回既有单号
    assert len(trader.order_calls) == 1       # 只下单一次


def test_idempotent_preexisting_active_ledger(clock_0916):
    """台账已有 active 单（模拟重启重放）→ 不重下，返回既有单号。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    d = _decision()
    ex.place(d)                               # 先建一单
    trader.order_calls.clear()
    biz2 = ex.place(d)                        # 重放
    assert biz2 is not None
    assert len(trader.order_calls) == 0       # 不重下


# ---------------------------------------------------------------------------
# 撤单（含不可撤段）
# ---------------------------------------------------------------------------
def test_ttl_cancel_in_cancelable_phase(clock_0916):
    """可撤段(9:16) + state=REPORTED + TTL 到期 → 调 cancel_order_stock。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    biz = ex.place(_decision())
    led = ledger.get(biz)
    # 模拟 on_stock_order 已报回报推进到 REPORTED。
    ledger.sync_status(led.order_id, OrderState.REPORTED)
    ex.on_ttl_expired(biz)
    assert trader.cancel_calls == [led.order_id]
    assert ledger.get(biz).state == OrderState.CANCELLING


def test_ttl_no_cancel_in_locked_phase(clock_0922):
    """锁定段(9:22) TTL 到期 → 断言【未】调 cancel_order_stock，等定盘后处理。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0922, ledger=ledger)
    biz = ex.place(_decision())
    led = ledger.get(biz)
    ledger.sync_status(led.order_id, OrderState.REPORTED)
    ex.on_ttl_expired(biz)
    assert trader.cancel_calls == []                       # 锁定段不撤
    assert ledger.get(biz).state == OrderState.REPORTED    # 状态不被改


def test_locked_phase_place_marks_non_cancelable(clock_0922):
    """锁定段下单 → 台账 cancelable=False（9:20–9:25 段标记不可撤，§4.5）。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0922, ledger=ledger)
    biz = ex.place(_decision())
    assert ledger.get(biz).cancelable is False


# ---------------------------------------------------------------------------
# 部分成交
# ---------------------------------------------------------------------------
def test_partial_fill_then_ttl_cancel(clock_0916):
    """plan_volume=1000，外部 add_fill 累计 600 后 TTL 到（可撤段）→ 撤未成 400 → 终态 PART_TRADED，已成 600。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    biz = ex.place(_decision(plan_volume=1000))
    led = ledger.get(biz)
    # 外部回报：on_stock_order 已报 → on_stock_trade 累计 600（部成）。
    ledger.sync_status(led.order_id, OrderState.REPORTED)
    ledger.add_fill(led.order_id, "tr-part", 600, Decimal("11.00"))
    assert ledger.get(biz).state == OrderState.PART_TRADED
    ex.on_ttl_expired(biz)
    assert trader.cancel_calls == [led.order_id]          # 撤未成 400
    final = ledger.get(biz)
    assert final.state == OrderState.CANCELLING            # 已发撤单
    assert final.filled_volume == 600                      # 已成 600 计成交


# ---------------------------------------------------------------------------
# 成交确认口径：place 后无 add_fill → 停在 SUBMITTED（不计 TRADED）
# ---------------------------------------------------------------------------
def test_no_fill_stays_submitted(clock_0916):
    """place 后无任何成交回报 → 台账状态停在 SUBMITTED，不自判建仓成功（§4.4(4)）。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    biz = ex.place(_decision())
    led = ledger.get(biz)
    assert led.state == OrderState.SUBMITTED
    assert led.filled_volume == 0
    assert led.order_id is not None


# ---------------------------------------------------------------------------
# 一字 / 秒封：filled=0 且 TTL 到（可撤段）→ cancel + miss_reason 被标
# ---------------------------------------------------------------------------
def test_one_word_board_miss_reason(clock_0916):
    """一字（filled=0、factors.is_one_word）TTL 到 → 撤单 + miss_reason=一字未成。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    biz = ex.place(_decision(factors={"is_one_word": True}))
    led = ledger.get(biz)
    ledger.sync_status(led.order_id, OrderState.REPORTED)
    ex.on_ttl_expired(biz)
    assert trader.cancel_calls == [led.order_id]
    assert ledger.get(biz).miss_reason == "一字未成"


def test_second_seal_miss_reason(clock_0916):
    """秒封（factors.is_second_seal）filled=0 TTL 到 → miss_reason=秒封未成。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    biz = ex.place(_decision(factors={"is_second_seal": True}))
    led = ledger.get(biz)
    ledger.sync_status(led.order_id, OrderState.REPORTED)
    ex.on_ttl_expired(biz)
    assert ledger.get(biz).miss_reason == "秒封未成"


def test_plain_queue_miss_reason(clock_0916):
    """普通排队未成（无特殊因子）→ miss_reason=排队未成。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    biz = ex.place(_decision())
    led = ledger.get(biz)
    ledger.sync_status(led.order_id, OrderState.REPORTED)
    ex.on_ttl_expired(biz)
    assert ledger.get(biz).miss_reason == "排队未成"


# ---------------------------------------------------------------------------
# order_remark / signal_trade_date
# ---------------------------------------------------------------------------
def test_order_remark_contains_correct_t(clock_0916):
    """build_order_remark 含正确 T 日，格式 LUP|<T>|<ts_code>。"""
    trader = FakeTrader()
    ex = _executor(trader, clock_0916)
    remark = ex.build_order_remark(_decision())
    assert remark == "LUP|2026-06-11|600036.SH"
    # 透传到下单入参。
    biz = ex.place(_decision())
    assert trader.order_calls[0]["order_remark"] == "LUP|2026-06-11|600036.SH"
    assert biz is not None


def test_order_remark_truncates_keep_t_segment(clock_0916):
    """超长 ts_code → 截断仍保 LUP|<T>| 段（丢 ts_code 尾部）。"""
    trader = FakeTrader()
    ex = _executor(trader, clock_0916)
    long_code = "X" * 400                       # 构造超长代码
    remark = ex.build_order_remark(_decision(ts_code=long_code))
    head = "LUP|2026-06-11|"
    assert len(remark) == 255                   # 截断到上限
    assert remark.startswith(head)              # T 段完整保留
    assert remark[len(head):] == "X" * (255 - len(head))


def test_signal_trade_date_persisted_in_ledger(clock_0916):
    """台账 signal_trade_date 落入 = T。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    biz = ex.place(_decision())
    assert ledger.get(biz).signal_trade_date == T_SIGNAL


# ---------------------------------------------------------------------------
# 账户隔离
# ---------------------------------------------------------------------------
def test_account_isolation(clock_0916):
    """两个 account 各一个 executor + 各自 ledger，同股同战法各自独立 place，互不串。"""
    trader_a = FakeTrader()
    trader_b = FakeTrader()
    ledger_a = InMemoryLocalLedger()
    ledger_b = InMemoryLocalLedger()
    ex_a = _executor(trader_a, clock_0916, account_id="accA", ledger=ledger_a)
    ex_b = _executor(trader_b, clock_0916, account_id="accB", ledger=ledger_b)
    d = _decision()
    biz_a = ex_a.place(d)
    biz_b = ex_b.place(d)
    # 各自都下了单（互不幂等串）：A 的 executor 不查 B 的台账，故都触发 order_stock。
    assert len(trader_a.order_calls) == 1
    assert len(trader_b.order_calls) == 1
    # 每条台账行只归属各自 account_id（所有键含 account_id，物理隔离，§4.4(6)）。
    assert ledger_a.get(biz_a).account_id == "accA"
    assert ledger_b.get(biz_b).account_id == "accB"
    # A 的台账只有自己的单，不含 B 写的任何行（两份 ledger 互不可见）。
    assert all(e.account_id == "accA" for e in ledger_a.all())
    assert all(e.account_id == "accB" for e in ledger_b.all())
    # A 在 B 的 ledger 撤单/操作不会影响：B 台账中该 biz_no 仍是 accB 自己的计划。
    assert ledger_b.get(biz_b).account_id == "accB"


# ---------------------------------------------------------------------------
# 下单失败：handle_order_error → ERROR；不静默；next_best 存在时转次优
# ---------------------------------------------------------------------------
def test_handle_order_error_marks_error(clock_0916):
    """handle_order_error → 台账 ERROR + error_id/msg 落账，不静默。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    logger = RecordingLogger()
    ex = _executor(trader, clock_0916, ledger=ledger, logger=logger)
    biz = ex.place(_decision())
    oid = ledger.get(biz).order_id
    ex.handle_order_error(oid, error_id=42, msg="资金不足")
    led = ledger.get(biz)
    assert led.state == OrderState.ERROR
    assert led.error_id == 42
    assert led.error_msg == "资金不足"
    assert "order_error" in logger.events()      # 不静默：有错误日志


def test_handle_order_error_falls_to_next_best(clock_0916):
    """下单失败且 decision 携带 next_best → 转次优（次优走完整 place）。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    nb = _decision(ts_code="000001.SZ")
    d = _decision(next_best=(nb,))
    biz = ex.place(d)
    oid = ledger.get(biz).order_id
    trader.order_calls.clear()
    next_biz = ex.handle_order_error(oid, error_id=1, msg="废单", decision=d)
    assert next_biz is not None
    assert len(trader.order_calls) == 1                 # 次优下了单
    assert trader.order_calls[0]["stock_code"] == "000001.SZ"


# ---------------------------------------------------------------------------
# 转次优：主标的买不进后 try_next_best 走完整 place
# ---------------------------------------------------------------------------
def test_try_next_best_places_first_candidate(clock_0916):
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    nb = _decision(ts_code="000002.SZ")
    biz = ex.try_next_best(_decision(next_best=(nb,)))
    assert biz is not None
    assert trader.order_calls[0]["stock_code"] == "000002.SZ"


def test_try_next_best_empty_gives_up(clock_0916):
    trader = FakeTrader()
    ex = _executor(trader, clock_0916)
    assert ex.try_next_best(_decision(next_best=())) is None
    assert len(trader.order_calls) == 0


def test_ttl_unfilled_triggers_next_best(clock_0916):
    """可撤段 filled=0 TTL 到 → 撤单后自动转次优（§4.4(7)）。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    nb = _decision(ts_code="000003.SZ")
    biz = ex.place(_decision(next_best=(nb,)))
    led = ledger.get(biz)
    ledger.sync_status(led.order_id, OrderState.REPORTED)
    trader.order_calls.clear()
    ex.on_ttl_expired(biz)
    assert trader.cancel_calls == [led.order_id]            # 主标的撤单
    assert len(trader.order_calls) == 1                      # 转次优下单
    assert trader.order_calls[0]["stock_code"] == "000003.SZ"


# ---------------------------------------------------------------------------
# kill_switch / SKIP
# ---------------------------------------------------------------------------
def test_kill_switch_blocks_order(clock_0916):
    """kill_switch=True → place 返回 None 且 order_stock 未被调用（§7.1.5）。"""
    trader = FakeTrader()
    ex = _executor(trader, clock_0916, settings=Settings(kill_switch=True))
    assert ex.place(_decision()) is None
    assert len(trader.order_calls) == 0


def test_skip_action_no_order(clock_0916):
    """action=SKIP → 落决策台账留痕、不下单、返回 None。"""
    trader = FakeTrader()
    logger = RecordingLogger()
    ex = _executor(trader, clock_0916, logger=logger)
    assert ex.place(_decision(action=EntryAction.SKIP)) is None
    assert len(trader.order_calls) == 0
    assert "order_skip_decision" in logger.events()


def test_max_orders_per_day_blocks_overtrading(clock_0916):
    """评审 P0-B3：单日下单次数达上限 → 后续新开仓被拦、不再调用 order_stock。"""
    trader = FakeTrader()
    logger = RecordingLogger()
    ex = _executor(trader, clock_0916, settings=Settings(max_orders_per_day=2), logger=logger)
    # 三个不同标的（避开同标的幂等短路），上限 2。
    assert ex.place(_decision(ts_code="600036.SH")) is not None
    assert ex.place(_decision(ts_code="600000.SH")) is not None
    assert len(trader.order_calls) == 2
    # 第 3 个标的超限 → 拦截、不下单、留痕。
    assert ex.place(_decision(ts_code="000001.SZ")) is None
    assert len(trader.order_calls) == 2
    assert "order_max_orders_per_day_block" in logger.events()


def test_max_orders_per_day_idempotent_hit_not_counted(clock_0916):
    """幂等命中（重复推同一计划）不占下单次数配额：上限 1 时同标的二次推仍返回既有单。"""
    trader = FakeTrader()
    ex = _executor(trader, clock_0916, settings=Settings(max_orders_per_day=1))
    biz1 = ex.place(_decision(ts_code="600036.SH"))
    assert biz1 is not None and len(trader.order_calls) == 1
    # 同标的同计划重复推 → 幂等命中返回既有单号，不新下单、不占配额。
    biz2 = ex.place(_decision(ts_code="600036.SH"))
    assert biz2 == biz1 and len(trader.order_calls) == 1


# ---------------------------------------------------------------------------
# 计划股数现算 + 资金不足
# ---------------------------------------------------------------------------
def test_plan_volume_computed_from_cash(clock_0916):
    """decision.plan_volume=None → 按可用资金现算，向下取整到 100 股。"""
    # cash=23560，限价 11.00 → 23560/11=2141.8 → 向下取整到 2100 股。
    trader = FakeTrader(cash="23560")
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    biz = ex.place(_decision(plan_volume=None, limit_price=Decimal("11.00")))
    assert ledger.get(biz).plan_volume == 2100
    assert trader.order_calls[0]["order_volume"] == 2100


def test_insufficient_cash_no_order(clock_0916):
    """可用资金不足一手 → 算出 0 股 → 不下单 + 返回 None。"""
    trader = FakeTrader(cash="500")              # 500/11 < 100 股
    logger = RecordingLogger()
    ex = _executor(trader, clock_0916, logger=logger)
    assert ex.place(_decision(plan_volume=None, limit_price=Decimal("11.00"))) is None
    assert len(trader.order_calls) == 0
    assert "order_zero_volume_skip" in logger.events()


def test_per_order_max_amount_caps_volume(clock_0916):
    """per_order_max_amount 收紧单笔预算 → 限制计划股数。"""
    trader = FakeTrader(cash="1000000")
    ledger = InMemoryLocalLedger()
    settings = Settings(per_order_max_amount=Decimal("11000"))   # 11000/11=1000 股
    ex = _executor(trader, clock_0916, ledger=ledger, settings=settings)
    biz = ex.place(_decision(plan_volume=None, limit_price=Decimal("11.00")))
    assert ledger.get(biz).plan_volume == 1000


# ---------------------------------------------------------------------------
# biz_order_no 格式与序号
# ---------------------------------------------------------------------------
def test_biz_order_no_format_and_seq(clock_0916):
    """biz_order_no = {date}_{ts}_{family}_{seq:03d}；同维度序号自增。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    d = _decision()
    biz1 = ex.build_biz_order_no(d)
    biz2 = ex.build_biz_order_no(d)
    assert biz1 == "20260612_600036.SH_打板_001"
    assert biz2 == "20260612_600036.SH_打板_002"
    # 不同战法各自计数。
    biz_other = ex.build_biz_order_no(_decision(strategy_family="低吸"))
    assert biz_other == "20260612_600036.SH_低吸_001"


def test_order_stock_uses_limit_price_type(clock_0916):
    """下单走限价类型（不挂市价）+ 买入方向 + 限价 float。"""
    from qmt_strategy.order.order_executor import XT_ORDER_TYPE_BUY, XT_PRICE_TYPE_FIX

    trader = FakeTrader()
    ex = _executor(trader, clock_0916)
    ex.place(_decision(limit_price=Decimal("11.00")))
    call = trader.order_calls[0]
    assert call["order_type"] == XT_ORDER_TYPE_BUY
    assert call["price_type"] == XT_PRICE_TYPE_FIX
    assert call["price"] == 11.0
