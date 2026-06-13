"""本地下单台账单测（§4.4 幂等 / 成交累计 / 状态推进）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from qmt_strategy.contracts.enums import OrderState, TradeSide
from qmt_strategy.contracts.models import LedgerEntry
from qmt_strategy.order.local_ledger import InMemoryLocalLedger

T_BUY = date(2026, 6, 12)


def _entry(biz="20260612_600036.SH_CHASE_LIMIT_UP_001", plan_volume=1000, state=OrderState.PLANNED):
    return LedgerEntry(
        biz_order_no=biz,
        account_id="acc1",
        target_trade_date=T_BUY,
        ts_code="600036.SH",
        strategy_family="打板",
        side=TradeSide.BUY,
        plan_volume=plan_volume,
        plan_price=Decimal("11.00"),
        order_remark="LUP|2026-06-11|600036.SH",
        signal_trade_date=date(2026, 6, 11),
        state=state,
    )


def test_has_active_after_insert():
    led = InMemoryLocalLedger()
    led.insert(_entry())
    assert led.has_active(T_BUY, "600036.SH", "打板") is True
    assert led.has_active(T_BUY, "600036.SH", "低吸") is False  # 不同战法不串


def test_cancelled_not_active():
    led = InMemoryLocalLedger()
    led.insert(_entry(state=OrderState.CANCELLED))
    assert led.has_active(T_BUY, "600036.SH", "打板") is False  # 已撤可重试/转次优


def test_add_fill_part_then_full():
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 555
    led.insert(e)
    led.add_fill(555, "tr1", 600, Decimal("11.00"))
    got = led.get_by_order_id(555)
    assert got.filled_volume == 600
    assert got.state == OrderState.PART_TRADED
    led.add_fill(555, "tr2", 400, Decimal("11.00"))
    got = led.get_by_order_id(555)
    assert got.filled_volume == 1000
    assert got.state == OrderState.TRADED
    assert got.avg_filled_price == Decimal("11.00")


def test_add_fill_dedup_by_traded_id():
    """同一 traded_id 重投（断线重连回放/券商重复推送）只计一次（§6.5/§4.4(4) 评审 low#1）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 9
    led.insert(e)
    led.add_fill(9, "dup", 600, Decimal("11.00"))
    led.add_fill(9, "dup", 600, Decimal("11.00"))  # 同 traded_id 重投
    got = led.get_by_order_id(9)
    assert got.filled_volume == 600  # 不翻倍
    assert got.state == OrderState.PART_TRADED


def test_add_fill_ignores_non_positive_volume():
    """异常/撤单回报带 traded_volume<=0 时忽略，不污染累计量（评审 low#2）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 10
    led.insert(e)
    led.add_fill(10, "z0", 0, Decimal("11.00"))
    led.add_fill(10, "zneg", -100, Decimal("11.00"))
    assert led.get_by_order_id(10).filled_volume == 0


def test_sync_status_fill_aware_cancel_keeps_part_traded():
    """部成单撤单后 QMT 报 CANCELLED，已有成交 → 终态收口为 PART_TRADED（§4.7/§4.9 评审 medium#2）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 11
    led.insert(e)
    led.add_fill(11, "f1", 600, Decimal("11.00"))  # 部成 600
    led.sync_status(11, OrderState.CANCELLED)       # 撤剩余 400 → QMT 报已撤
    got = led.get_by_order_id(11)
    assert got.state == OrderState.PART_TRADED       # 不被改写为 CANCELLED
    assert got.filled_volume == 600


def test_sync_status_cancel_without_fill_is_cancelled():
    """完全未成单撤单 → 终态 CANCELLED（无成交时不收口为 PART_TRADED）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 12
    led.insert(e)
    led.sync_status(12, OrderState.CANCELLED)
    assert led.get_by_order_id(12).state == OrderState.CANCELLED


def test_sync_status_by_order_id():
    led = InMemoryLocalLedger()
    e = _entry()
    e.order_id = 777
    led.insert(e)
    led.sync_status(777, OrderState.REPORTED)
    assert led.get_by_order_id(777).state == OrderState.REPORTED
    # 未知 order_id 不报错、不越权改写
    led.sync_status(999999, OrderState.CANCELLED)


def test_avg_filled_price_weighted():
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=300)
    e.order_id = 1
    led.insert(e)
    led.add_fill(1, "a", 100, Decimal("10.00"))
    led.add_fill(1, "b", 200, Decimal("13.00"))
    got = led.get_by_order_id(1)
    # (100*10 + 200*13) / 300 = 12.00
    assert got.avg_filled_price == Decimal("12.00")
