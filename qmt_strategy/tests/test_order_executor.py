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


def test_part_cancelled_no_recancel_loop(clock_0916):
    """评审 doc/21 B1：部成单被撤 → CANCELLED 回报收口 PART_CANCELLED 后，后续 sweep 不再重复撤单（破死循环）。

    原缺陷：部成撤单收口回 PART_TRADED（既属 active() 又在 on_ttl_expired 可撤集合 {SUBMITTED,REPORTED,
    PART_TRADED}）→ 每个 grace 周期 sweep 都对一笔券商侧早已撤销的委托反复发 cancel，到收盘为止无界重复废撤单。
    改用独立终态 PART_CANCELLED 后退出可撤集合，首撤即终止、不再复发。
    """
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    biz = ex.place(_decision(plan_volume=1000))
    led = ledger.get(biz)
    # 部成 600 → PART_TRADED；TTL 到撤未成 → CANCELLING（首次且应是唯一一次撤单）。
    ledger.sync_status(led.order_id, OrderState.REPORTED)
    ledger.add_fill(led.order_id, "tr-part", 600, Decimal("11.00"))
    ex.on_ttl_expired(biz)
    assert trader.cancel_calls == [led.order_id]
    assert ledger.get(biz).state == OrderState.CANCELLING
    # 券商 CANCELLED 回报到达 → 收口 PART_CANCELLED（终态）。
    ledger.sync_status(led.order_id, OrderState.CANCELLED)
    assert ledger.get(biz).state == OrderState.PART_CANCELLED
    # 过 grace 后再 sweep（仍在 9:15–9:20 可撤段）：deadline 被 pop、on_ttl_expired 走终态分支 → 绝不再撤、不再续 deadline。
    clock_0916.set(utc_at_east8(T_BUY, 9, 17, 30))
    ex.sweep_expired()
    assert trader.cancel_calls == [led.order_id]          # 仍只有一次撤单，未复发（死循环已破）
    assert biz not in ex._ttl_deadline                    # 退出 TTL 盯防，不会再被 sweep 触达
    assert ledger.get(biz).state == OrderState.PART_CANCELLED


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


def test_place_sell_via_unique_point_with_ledger(clock_0916):
    """评审 P0-C1：place_sell 经唯一下单点下卖单(otype=24) + 落台账(biz单号/side=SELL/SUBMITTED)。"""
    from qmt_strategy.contracts.enums import TradeSide

    trader = FakeTrader()
    led = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=led)
    biz = ex.place_sell(
        ts_code="600036.SH", target_trade_date=T_BUY, signal_trade_date=T_SIGNAL,
        sell_vol=1000, price=Decimal("10.50"), reason="炸板CLEAR",
    )
    assert biz is not None and biz.endswith("_SELL_001")
    assert len(trader.order_calls) == 1
    assert trader.order_calls[0]["order_type"] == 24          # 卖出方向
    assert trader.order_calls[0]["price"] == 10.50            # 盘口现价限价
    e = led.get(biz)
    assert e is not None and e.side == TradeSide.SELL and e.plan_volume == 1000
    # 同票当日二次卖出(先 REDUCE 后 CLEAR)各落一条，不被台账级幂等误挡。
    biz2 = ex.place_sell("600036.SH", T_BUY, T_SIGNAL, 500, Decimal("10.40"), "CLEAR")
    assert biz2 is not None and biz2.endswith("_SELL_002")


def test_committed_amount_counts_active_buys(clock_0916):
    """评审 2.3余/B3余：committed_amount = Σ 活跃买单 plan_volume×plan_price，终态失败不计。"""
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    assert ex.committed_amount(T_BUY) == Decimal("0")
    # 下一单：1000 股 × 11.00 = 11000 占用。
    ex.place(_decision(ts_code="600036.SH", plan_volume=1000, limit_price=Decimal("11.00")))
    assert ex.committed_amount(T_BUY) == Decimal("11000")
    # 再下一只不同标的：累计 +5000×9.00=45000 → 56000。
    ex.place(_decision(ts_code="600000.SH", plan_volume=5000, limit_price=Decimal("9.00")))
    assert ex.committed_amount(T_BUY) == Decimal("56000")


def test_committed_amount_excludes_part_cancelled_dead_remaining(clock_0916):
    """评审 doc/21 B1：部成撤单单(PART_CANCELLED)的未成 remaining 已死，不再计已承诺（只计真实已成 filled）。

    原缺陷：部成撤单收口 PART_TRADED 仍属 active()，committed_amount 按 filled+remaining 全额计入，
    一笔永不会再成交的死量 remaining 永久超额承诺、挤占其它龙头预算。改 PART_CANCELLED 后 remaining 置 0。
    """
    trader = FakeTrader()
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger)
    biz = ex.place(_decision(plan_volume=1000, limit_price=Decimal("11.00")))
    led = ledger.get(biz)
    assert ex.committed_amount(T_BUY) == Decimal("11000")        # 全额在途
    ledger.add_fill(led.order_id, "f1", 600, Decimal("11.00"))   # 部成 600 → 600+400 仍 = 11000
    assert ex.committed_amount(T_BUY) == Decimal("11000")
    ex.on_ttl_expired(biz)                                       # 撤未成 → CANCELLING
    ledger.sync_status(led.order_id, OrderState.CANCELLED)       # 收口 PART_CANCELLED
    # 死量 remaining(400×11) 不再占承诺，只计已成 600×11 = 6600。
    assert ex.committed_amount(T_BUY) == Decimal("6600")


def test_place_flushes_ledger_before_order(clock_0916):
    """评审 P0-C3：发单前同步落盘——flush_pending 在 order_stock 之前被调用（堵崩溃窗口重复下单）。"""
    events = []

    class SpyLedger(InMemoryLocalLedger):
        def insert(self, entry):
            events.append("insert")
            super().insert(entry)

        def flush_pending(self, timeout=5.0):
            events.append("flush")
            return True

    class SpyTrader(FakeTrader):
        def order_stock(self, *a, **k):
            events.append("order")
            return super().order_stock(*a, **k)

    ex = _executor(SpyTrader(), clock_0916, ledger=SpyLedger())
    ex.place(_decision(plan_volume=1000))
    assert events.index("insert") < events.index("flush") < events.index("order")


def test_place_sell_kill_switch_blocks(clock_0916):
    """kill_switch=True → place_sell 不下单、返回 None。"""
    trader = FakeTrader()
    ex = _executor(trader, clock_0916, settings=Settings(kill_switch=True))
    assert ex.place_sell("600036.SH", T_BUY, T_SIGNAL, 1000, Decimal("10.50"), "x") is None
    assert len(trader.order_calls) == 0


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


def test_max_positions_per_day_caps_distinct_stocks(clock_0916):
    """单日建仓「只数」上限：达上限后新标的被拦（区别于下单「次数」上限）。"""
    trader = FakeTrader()
    logger = RecordingLogger()
    ex = _executor(trader, clock_0916, settings=Settings(max_positions_per_day=2), logger=logger)
    assert ex.place(_decision(ts_code="600036.SH")) is not None
    assert ex.place(_decision(ts_code="600000.SH")) is not None
    assert len(trader.order_calls) == 2
    # 第 3 只不同标的 → 超建仓只数上限被拦、不下单、留痕。
    assert ex.place(_decision(ts_code="000001.SZ")) is None
    assert len(trader.order_calls) == 2
    assert "order_max_positions_per_day_block" in logger.events()


def test_max_positions_unfilled_frees_slot(clock_0916):
    """买不进（终态零成交）不占建仓名额：撤单零成交后可再开新标的。"""
    from qmt_strategy.contracts.enums import OrderState

    trader = FakeTrader()
    led = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=led, settings=Settings(max_positions_per_day=1))
    biz1 = ex.place(_decision(ts_code="600036.SH"))
    assert biz1 is not None
    # 名额=1 已占 → 第 2 只此刻被拦。
    assert ex.place(_decision(ts_code="600000.SH")) is None
    # 600036 一字买不进：撤单零成交 → 释放名额。
    led.update(biz1, state=OrderState.CANCELLED, filled_volume=0)
    # 名额释放后第 2 只可开。
    assert ex.place(_decision(ts_code="600000.SH")) is not None


def test_max_positions_same_stock_not_double_counted(clock_0916):
    """同一标的重复推（幂等命中）不重复占名额：上限 1 时同标的二次推仍返回既有单。"""
    trader = FakeTrader()
    ex = _executor(trader, clock_0916, settings=Settings(max_positions_per_day=1))
    biz1 = ex.place(_decision(ts_code="600036.SH"))
    assert biz1 is not None
    assert ex.place(_decision(ts_code="600036.SH")) == biz1  # 幂等命中，未被名额闸误拦


def test_rebuild_seq_counter_avoids_same_no_after_restart(clock_0916):
    """评审 P0-C4：重启后 _seq_counter 按台账已存在的最大 seq 重建，新单号严格大于历史，
    不与磁盘上的终态失败单同号覆盖。"""
    from qmt_strategy.contracts.enums import OrderState, TradeSide
    from qmt_strategy.contracts.models import LedgerEntry

    led = InMemoryLocalLedger()
    # 模拟上一进程留下的终态失败单 _003（CANCELLED 不在 active 集，重启后会被重新推送）。
    led.insert(LedgerEntry(
        biz_order_no=f"{T_BUY:%Y%m%d}_600036.SH_打板_003",
        account_id="acc1", target_trade_date=T_BUY, ts_code="600036.SH",
        strategy_family="打板", side=TradeSide.BUY, plan_volume=1000,
        plan_price=Decimal("11.00"), order_remark="LUP|2026-06-11|600036.SH",
        signal_trade_date=T_SIGNAL, state=OrderState.CANCELLED,
    ))
    ex = _executor(FakeTrader(), clock_0916, ledger=led)
    ex.rebuild_seq_counter()
    # 新单号必须接在 _003 之后（_004），而非从 _001 起算覆盖历史失败单。
    assert ex.build_biz_order_no(_decision()).endswith("_004")


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
    """decision.plan_volume=None → 按可用资金现算，向下取整到 100 股。

    评审三轮 EXEC-entry-02：现算回退默认 fail-closed，本用例显式开启 allow_plan_volume_fallback 验证回退口径。
    """
    # cash=23560，限价 11.00 → 23560/11=2141.8 → 向下取整到 2100 股。
    trader = FakeTrader(cash="23560")
    ledger = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=ledger,
                   settings=Settings(allow_plan_volume_fallback=True))
    biz = ex.place(_decision(plan_volume=None, limit_price=Decimal("11.00")))
    assert ledger.get(biz).plan_volume == 2100
    assert trader.order_calls[0]["order_volume"] == 2100


def test_insufficient_cash_no_order(clock_0916):
    """可用资金不足一手 → 算出 0 股 → 不下单 + 返回 None。"""
    trader = FakeTrader(cash="500")              # 500/11 < 100 股
    logger = RecordingLogger()
    ex = _executor(trader, clock_0916, logger=logger,
                   settings=Settings(allow_plan_volume_fallback=True))
    assert ex.place(_decision(plan_volume=None, limit_price=Decimal("11.00"))) is None
    assert len(trader.order_calls) == 0
    assert "order_zero_volume_skip" in logger.events()


def test_per_order_max_amount_caps_volume(clock_0916):
    """per_order_max_amount 收紧单笔预算 → 限制计划股数。"""
    trader = FakeTrader(cash="1000000")
    ledger = InMemoryLocalLedger()
    settings = Settings(per_order_max_amount=Decimal("11000"), allow_plan_volume_fallback=True)   # 11000/11=1000 股
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


# ===========================================================================
# 评审三轮 批次2：发单前落盘 fail-closed / 通道健康回馈 / AUCTION TTL / CANCELLING / 单票上限含持仓
# ===========================================================================
from qmt_strategy.contracts.xt_objects import FakeXtPosition  # noqa: E402


class _PersistSpyLedger(InMemoryLocalLedger):
    """可注入 flush_pending/is_healthy 返回值的台账，用于验证发单前落盘 fail-closed（EXEC-storage-01）。"""

    def __init__(self, flush_result=True, healthy=True):
        super().__init__()
        self._flush_result = flush_result
        self._healthy = healthy
        self.flush_calls = 0

    def flush_pending(self, timeout: float = 5.0) -> bool:
        self.flush_calls += 1
        return self._flush_result

    def is_healthy(self) -> bool:
        return self._healthy


class _RcTrader(FakeTrader):
    """order_stock 返回可注入返回码（<0 模拟同步下单失败），并支持持仓查询。"""

    def __init__(self, rc=1001, positions=None, **kw):
        super().__init__(**kw)
        self._rc = rc
        self._positions = positions or []

    def order_stock(self, account, stock_code, order_type, order_volume, price_type, price,
                    strategy_name="", order_remark=""):
        super().order_stock(account, stock_code, order_type, order_volume, price_type, price,
                            strategy_name, order_remark)
        return self._rc  # 注入返回码（>0 成功 oid，<0 同步失败）

    def query_stock_positions(self, account):
        return self._positions


# —— EXEC-storage-01：发单前关键落盘 fail-closed ——
def test_place_fail_closed_when_persist_thread_dead(clock_0916):
    """写线程死/commit 失败（flush False + is_healthy False）→ 不发委托、台账 ERROR、强告警。"""
    trader = FakeTrader()
    led = _PersistSpyLedger(flush_result=False, healthy=False)
    logger = RecordingLogger()
    ex = _executor(trader, clock_0916, ledger=led, logger=logger)
    biz = ex.place(_decision())
    assert biz is None                              # fail-closed 放弃下单
    assert trader.order_calls == []                 # 未发券商委托（堵重复下单窗口）
    assert "order_ledger_persist_failed_fail_closed" in logger.events()


def test_place_continues_on_pure_flush_timeout(clock_0916):
    """纯超时（flush False 但 is_healthy True）→ 仍下单 + 强告警（不漏信号）。"""
    trader = FakeTrader()
    led = _PersistSpyLedger(flush_result=False, healthy=True)
    logger = RecordingLogger()
    ex = _executor(trader, clock_0916, ledger=led, logger=logger)
    biz = ex.place(_decision())
    assert biz is not None
    assert len(trader.order_calls) == 1             # 拥堵超时仍下单
    assert "order_ledger_flush_timeout_before_order" in logger.events()


# —— EXEC-risk-05：order_stock 同步成败回馈通道健康 ——
def test_order_stock_failure_reports_conn_down(clock_0916):
    states = []
    trader = _RcTrader(rc=-1)
    ex = _executor(trader, clock_0916)
    ex._conn_health_sink = states.append
    ex.place(_decision(next_best=()))
    assert states and states[-1] is False           # 同步失败 → 反馈不健康


def test_order_stock_success_reports_conn_up(clock_0916):
    states = []
    trader = FakeTrader()
    ex = _executor(trader, clock_0916)
    ex._conn_health_sink = states.append
    ex.place(_decision())
    assert states == [True]                          # 成功 → 反馈健康


# —— EXEC-order-04：AUCTION TTL 在 9:25 后退化为相对 TTL ——
def test_auction_ttl_after_0925_uses_relative_ttl():
    clock = FakeClock(utc_at_east8(T_BUY, 9, 26, 0))
    ex = _executor(FakeTrader(), clock, settings=Settings(order_ttl_seconds=60))
    now = clock.now_utc()
    deadline = ex._compute_ttl_deadline(now, OrderPhase.AUCTION)
    assert deadline > now                            # 绝不返回 <=now
    assert (deadline - now).total_seconds() == 60    # 退化为相对 order_ttl_seconds


def test_auction_ttl_before_0925_unchanged(clock_0916):
    ex = _executor(FakeTrader(), clock_0916)
    now = clock_0916.now_utc()
    deadline = ex._compute_ttl_deadline(now, OrderPhase.AUCTION)
    # 9:16 → 截止仍是当日 9:25 定盘（相对 now 偏移 9 分钟）。
    assert (deadline - now).total_seconds() == 9 * 60


def test_place_after_0925_not_swept_immediately():
    clock = FakeClock(utc_at_east8(T_BUY, 9, 26, 0))
    ex = _executor(FakeTrader(), clock, settings=Settings(order_ttl_seconds=60))
    biz = ex.place(_decision(order_phase=OrderPhase.AUCTION))
    handled = ex.sweep_expired(clock.now_utc())
    assert biz not in handled                        # 提交即被秒撤的回归不再发生


# —— EXEC-order-08：CANCELLING 撤单回执丢失兜底 ——
def _to_cancelling(ex, led, biz, *, aged_seconds):
    """把台账单置 CANCELLING，并把 updated_at 设到 aged_seconds 前（模拟撤单回执久未到达）。"""
    from datetime import timedelta
    past = ex._clock.now_utc() - timedelta(seconds=aged_seconds)
    led.update(biz, state=OrderState.CANCELLING, updated_at=past)


def test_cancelling_recancel_after_grace(clock_0916):
    trader = FakeTrader()
    led = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=led, settings=Settings(cancel_grace_seconds=30))
    biz = ex.place(_decision())
    oid = led.get(biz).order_id
    trader.cancel_calls.clear()
    _to_cancelling(ex, led, biz, aged_seconds=31)    # 超宽限
    ex.on_ttl_expired(biz)
    assert oid in trader.cancel_calls                # 超宽限幂等重发 cancel
    assert biz in ex._ttl_deadline                   # 续二次截止待下轮再盯


def test_cancelling_within_grace_not_recancelled(clock_0916):
    trader = FakeTrader()
    led = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=led, settings=Settings(cancel_grace_seconds=30))
    biz = ex.place(_decision())
    trader.cancel_calls.clear()
    _to_cancelling(ex, led, biz, aged_seconds=5)     # 未到宽限
    ex.on_ttl_expired(biz)
    assert trader.cancel_calls == []                 # 未到宽限不重发
    assert biz in ex._ttl_deadline                   # 但仍续截止不遗忘


def test_cancelling_not_dropped_by_sweep(clock_0916):
    trader = FakeTrader()
    led = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=led, settings=Settings(cancel_grace_seconds=30))
    biz = ex.place(_decision())
    _to_cancelling(ex, led, biz, aged_seconds=5)
    # 让 TTL 截止已过期 → sweep 处理；CANCELLING 不应被永久移出截止表。
    from datetime import timedelta
    ex._ttl_deadline[biz] = ex._clock.now_utc() - timedelta(seconds=1)
    ex.sweep_expired(ex._clock.now_utc())
    assert biz in ex._ttl_deadline                   # 续二次截止（未被 sweep 反向 pop 永久丢失）


def test_rebuild_runtime_state_includes_cancelling(clock_0916):
    trader = FakeTrader()
    led = InMemoryLocalLedger()
    ex = _executor(trader, clock_0916, ledger=led)
    biz = ex.place(_decision())
    led.update(biz, state=OrderState.CANCELLING, updated_at=clock_0916.now_utc())
    ex._ttl_deadline.clear()                          # 模拟重启清空运行态
    ex.rebuild_runtime_state()
    assert biz in ex._ttl_deadline                    # CANCELLING 单重启后重建 TTL 继续盯防


# —— EXEC-risk-03：单票金额上限计入既有持仓 ——
def test_plan_volume_zero_when_held_at_cap(clock_0916):
    # 现有持仓市值已达单票上限 → 不再加仓（_plan_volume 返 0 → place 返回 None）。
    pos = FakeXtPosition(account_id="acc", stock_code="600036.SH", volume=10000,
                         can_use_volume=10000, open_price=Decimal("11.0"),
                         market_value=Decimal("110000"))
    trader = _RcTrader(rc=1001, positions=[pos])
    ex = _executor(trader, clock_0916,
                   settings=Settings(max_position_per_stock=Decimal("100000"),
                                     allow_plan_volume_fallback=True))
    biz = ex.place(_decision(plan_volume=None))       # 触发 _plan_volume 现算
    assert biz is None
    assert trader.order_calls == []


def test_plan_volume_subtracts_held_position(clock_0916):
    # 现有持仓占一半上限 → 新单金额钳到 cap-held 以内（不突破单票上限）。
    pos = FakeXtPosition(account_id="acc", stock_code="600036.SH", volume=5000,
                         can_use_volume=5000, open_price=Decimal("11.0"),
                         market_value=Decimal("55000"))
    trader = _RcTrader(rc=1001, positions=[pos], cash="1000000")
    ex = _executor(trader, clock_0916,
                   settings=Settings(max_position_per_stock=Decimal("100000"),
                                     allow_plan_volume_fallback=True))
    biz = ex.place(_decision(plan_volume=None, limit_price=Decimal("11.00")))
    assert biz is not None
    call = trader.order_calls[0]
    # 新单金额 = vol×price <= cap-held = 100000-55000 = 45000。
    assert call["order_volume"] * call["price"] <= 45000


# —— 评审 doc/19 M-2：持仓查询失败时单票上限净额校验 fail-closed ——
class _PosFailTrader(_RcTrader):
    """query_stock_positions 抛异常（模拟券商持仓查询抖动/超时），用于 M-2 fail-closed 验证。"""

    def query_stock_positions(self, account):
        raise RuntimeError("position query timeout")


def test_plan_volume_fail_closed_when_position_query_fails(clock_0916):
    """M-2：配了单票上限且持仓查询失败 → 无法核验隔夜持仓 → fail-closed 拒单（不当作无持仓满额买）。"""
    trader = _PosFailTrader(rc=1001, cash="1000000")
    logger = RecordingLogger()
    ex = _executor(trader, clock_0916, logger=logger,
                   settings=Settings(max_position_per_stock=Decimal("100000"),
                                     allow_plan_volume_fallback=True))
    biz = ex.place(_decision(plan_volume=None, limit_price=Decimal("11.00")))
    assert biz is None                                          # 拒单
    assert trader.order_calls == []                             # 未发券商委托
    assert "order_per_stock_cap_held_unknown_reject" in logger.events()


def test_plan_volume_proceeds_when_position_query_ok_no_holding(clock_0916):
    """对照：持仓查询成功且该票无持仓 → held_known=True、敞口 0 → 正常下单（M-2 不误拒成功查询）。"""
    trader = _RcTrader(rc=1001, positions=[], cash="1000000")
    ex = _executor(trader, clock_0916,
                   settings=Settings(max_position_per_stock=Decimal("100000"),
                                     allow_plan_volume_fallback=True))
    biz = ex.place(_decision(plan_volume=None, limit_price=Decimal("11.00")))
    assert biz is not None
    assert len(trader.order_calls) == 1


def test_plan_volume_no_cap_unaffected_by_position_query_failure(clock_0916):
    """边界：未配单票上限时，持仓查询失败不影响下单（M-2 守卫只在 max_position_per_stock 配了时生效）。"""
    trader = _PosFailTrader(rc=1001, cash="1000000")
    ex = _executor(trader, clock_0916,
                   settings=Settings(allow_plan_volume_fallback=True))  # 不配单票上限
    biz = ex.place(_decision(plan_volume=None, limit_price=Decimal("11.00")))
    assert biz is not None                                      # 未配上限 → 不触发 M-2 fail-closed
    assert len(trader.order_calls) == 1


def test_exposure_for_code_checked_flags_query_failure(clock_0916):
    """exposure_for_code_checked 直接验证：查询成功 held_known=True，查询失败 held_known=False。"""
    ok_trader = _RcTrader(rc=1001, positions=[], cash="1000000")
    ex_ok = _executor(ok_trader, clock_0916)
    _exp, known = ex_ok.exposure_for_code_checked(T_BUY, "600036.SH")
    assert known is True
    fail_trader = _PosFailTrader(rc=1001, cash="1000000")
    ex_fail = _executor(fail_trader, clock_0916)
    _exp2, known2 = ex_fail.exposure_for_code_checked(T_BUY, "600036.SH")
    assert known2 is False


def test_cancelling_real_chain_recancel_after_grace():
    """评审复核 P0：走真实链路（非手工置态）——SUBMITTED 单到期撤成 CANCELLING 后仍被持续盯防，
    撤单回执丢失时超 grace 由 sweep 幂等重发 cancel（验证首次进 CANCELLING 已续 deadline）。"""
    from datetime import timedelta
    # 09:31 连续交易段（可撤、非午休），TTL 短、grace 短便于推进。
    clock = FakeClock(utc_at_east8(T_BUY, 9, 31, 0))
    trader = FakeTrader()
    led = InMemoryLocalLedger()
    ex = _executor(trader, clock, ledger=led,
                   settings=Settings(order_ttl_seconds=10, cancel_grace_seconds=30))
    biz = ex.place(_decision(order_phase=OrderPhase.OPENING))
    oid = led.get(biz).order_id
    # 推进过 TTL → sweep#1：撤单 → CANCELLING，且 deadline 仍被续上（非永久丢失）。
    clock.advance(11)
    ex.sweep_expired(clock.now_utc())
    assert led.get(biz).state == OrderState.CANCELLING
    assert biz in ex._ttl_deadline                      # 首次进 CANCELLING 已续 deadline
    assert trader.cancel_calls == [oid]
    # 模拟撤单回执丢失：推进过 grace → sweep#2 幂等重发 cancel。
    clock.advance(31)
    ex.sweep_expired(clock.now_utc())
    assert trader.cancel_calls == [oid, oid]            # 超 grace 幂等重发
    assert biz in ex._ttl_deadline                      # 仍续二次截止待回执/再撤


# —— EXEC-entry-02：真实 BUY plan_volume 缺失 → fail-closed 拒单（默认不回退 _plan_volume）——
def test_buy_missing_plan_volume_fail_closed(clock_0916):
    """plan_volume=None 且未开启回退 → 不下单、返回 None、留痕 reason_code=missing_plan_volume。"""
    trader = FakeTrader(cash="1000000")
    logger = RecordingLogger()
    ex = _executor(trader, clock_0916, logger=logger)
    biz = ex.place(_decision(plan_volume=None, limit_price=Decimal("11.00")))
    assert biz is None                      # fail-closed 拒单
    assert trader.order_calls == []         # 未真正下单
    assert "order_missing_plan_volume_fail_closed" in logger.events()
