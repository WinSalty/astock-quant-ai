"""本地下单台账单测（§4.4 幂等 / 成交累计 / 状态推进）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from qmt_strategy.contracts.enums import OrderState, TradeSide
from qmt_strategy.contracts.models import LedgerEntry
from qmt_strategy.order.local_ledger import InMemoryLocalLedger

T_BUY = date(2026, 6, 12)


def _broker_order(order_id, traded_volume):
    """构造券商 query_stock_orders 委托对象（仅需 order_id + traded_volume 两属性）。"""
    return SimpleNamespace(order_id=order_id, traded_volume=traded_volume)


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


def test_reconcile_filled_from_broker_corrects_fragile_key_undercount():
    """E-6（doc/29）：缺 traded_id 时同价同量碎单被合成键误去重→少计；以券商 traded_volume 权威收口校正。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 7
    led.insert(e)
    # 缺 traded_id 的两笔同价同量碎单：合成键 (order_id|vol|price) 相同 → 第二笔被误去重 → 少计为 500。
    led.add_fill(7, None, 500, Decimal("11.00"))
    led.add_fill(7, None, 500, Decimal("11.00"))
    assert led.get_by_order_id(7).filled_volume == 500  # 脆弱键误丢第二笔
    # 券商权威累计成交量=1000 → 收口校正、状态推进 TRADED。
    touched = led.reconcile_filled_from_broker_orders([_broker_order(7, 1000)])
    got = led.get_by_order_id(7)
    assert got.filled_volume == 1000
    assert got.state == OrderState.TRADED
    assert touched == [e.biz_order_no]


def test_reconcile_filled_from_broker_noop_and_guards():
    """E-6（整体评审修复后：单调向上收口）：值一致/券商更低不动；未知委托/脏 traded_volume 跳过；绝不下修。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 8
    led.insert(e)
    led.add_fill(8, "tr1", 600, Decimal("11.00"))  # filled=600, PART_TRADED
    # (a) 券商与本地一致 → 不动、不计入 touched
    assert led.reconcile_filled_from_broker_orders([_broker_order(8, 600)]) == []
    # (b) 未知委托(order_id 不在台账) / 脏 traded_volume(None/负/非数) → 跳过，不抛
    assert led.reconcile_filled_from_broker_orders([_broker_order(999, 100)]) == []
    assert led.reconcile_filled_from_broker_orders([_broker_order(8, None)]) == []
    assert led.reconcile_filled_from_broker_orders([_broker_order(8, -5)]) == []
    assert led.reconcile_filled_from_broker_orders([_broker_order(8, "x")]) == []
    assert led.get_by_order_id(8).filled_volume == 600  # 全程未被脏值污染

    # (c) 券商量【低于】本地累计（重连 stale 快照 / 迟到回调）→ 单调向上：绝不下修，filled 保持本地、不计入 touched。
    #     下修会欠计 filled → committed/exposure 偏小 → 过度承诺/漏卖（危险方向），故只升不降（评审 #2/#3）。
    e2 = _entry(biz="b2", plan_volume=1000)
    e2.order_id = 10
    led.insert(e2)
    led.add_fill(10, "x1", 1000, Decimal("11.00"))  # TRADED, filled=1000
    assert led.reconcile_filled_from_broker_orders([_broker_order(10, 800)]) == []  # 800<1000 → 跳过
    got = led.get_by_order_id(10)
    assert got.filled_volume == 1000          # 不下修
    assert got.state == OrderState.TRADED

    # (d) 重启 reconcile_fills_from_detail 不撤销券商收口（评审 #2）：明细 sum 低于已收口 filled 时取 max、不下修。
    e3 = _entry(biz="b3", plan_volume=2000)
    e3.order_id = 12
    led.insert(e3)
    led.add_fill(12, None, 500, Decimal("11.00"))  # 碎单误去重少计：明细仅 500
    led.reconcile_filled_from_broker_orders([_broker_order(12, 1000)])  # 券商权威 1000 → 上修
    assert led.get_by_order_id(12).filled_volume == 1000
    # 模拟重启按明细(500)重算：取 max(500, 已持久 1000)=1000，收口不被撤销。
    led.reconcile_fills_from_detail("b3", [("noid1", 500, Decimal("11.00"))])
    assert led.get_by_order_id(12).filled_volume == 1000  # 仍 1000，未被明细 500 下修


def test_add_fill_dedup_int_then_str_traded_id():
    """评审 P1#7：traded_id int 与其 str 形态视为同一笔（持久化统一 str，重启 round-trip 后仍去重）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 11
    led.insert(e)
    led.add_fill(11, 12345, 600, Decimal("11.00"))     # 内存态 int（实测 miniQMT 常为 int）
    led.add_fill(11, "12345", 600, Decimal("11.00"))   # 重启后回读为 str，同一笔
    got = led.get_by_order_id(11)
    assert got.filled_volume == 600                    # 不翻倍


def test_add_fill_dedup_when_traded_id_missing():
    """评审 P1#8：traded_id 缺失时用合成键 (order_id|vol|price) 兜底去重，重投不翻倍。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 12
    led.insert(e)
    led.add_fill(12, None, 600, Decimal("11.00"))
    led.add_fill(12, None, 600, Decimal("11.00"))      # 同形态重投
    got = led.get_by_order_id(12)
    assert got.filled_volume == 600                    # 不翻倍


def test_add_fill_ignores_non_positive_volume():
    """异常/撤单回报带 traded_volume<=0 时忽略，不污染累计量（评审 low#2）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 10
    led.insert(e)
    led.add_fill(10, "z0", 0, Decimal("11.00"))
    led.add_fill(10, "zneg", -100, Decimal("11.00"))
    assert led.get_by_order_id(10).filled_volume == 0


def test_sync_status_fill_aware_cancel_settles_part_cancelled():
    """部成单撤单后 QMT 报 CANCELLED，已有成交 → 终态收口为 PART_CANCELLED（评审 doc/21 B1，原 PART_TRADED）。

    改用独立终态 PART_CANCELLED：既不被改写为 CANCELLED（保留真实建仓事实），又与「部成且仍在途」的
    PART_TRADED 区分，使其退出 on_ttl_expired 可撤集合、未成 remaining 不再占预算（见 B1 闭环测试）。
    """
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 11
    led.insert(e)
    led.add_fill(11, "f1", 600, Decimal("11.00"))  # 部成 600
    led.sync_status(11, OrderState.CANCELLED)       # 撤剩余 400 → QMT 报已撤
    got = led.get_by_order_id(11)
    assert got.state == OrderState.PART_CANCELLED    # 不被改写为 CANCELLED，且与 PART_TRADED 区分
    assert got.filled_volume == 600


def test_part_cancelled_is_sticky_terminal_no_downgrade():
    """PART_CANCELLED 终态粘滞（评审 doc/21 B1）：迟到委托态/成交回报都不得把它降级回在途/部成态。

    若放行降级，一笔已撤的部成单会重新落入 on_ttl_expired 可撤集合 → 无界重复撤单复活，正是 B1 要根治的死循环。
    """
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 13
    led.insert(e)
    led.add_fill(13, "f1", 600, Decimal("11.00"))  # 部成 600
    led.sync_status(13, OrderState.CANCELLED)       # 撤剩余 → PART_CANCELLED
    assert led.get_by_order_id(13).state == OrderState.PART_CANCELLED
    # 迟到委托态（部成/已报）回报：不得降级
    led.sync_status(13, OrderState.PART_TRADED)
    assert led.get_by_order_id(13).state == OrderState.PART_CANCELLED
    led.sync_status(13, OrderState.REPORTED)
    assert led.get_by_order_id(13).state == OrderState.PART_CANCELLED
    # 撤单瞬间在途的迟到成交：filled 仍累计（真实成交须计入），但状态保持 PART_CANCELLED 不复活
    led.add_fill(13, "f2", 100, Decimal("11.00"))
    got = led.get_by_order_id(13)
    assert got.state == OrderState.PART_CANCELLED
    assert got.filled_volume == 700


def test_sync_status_cancel_without_fill_is_cancelled():
    """完全未成单撤单 → 终态 CANCELLED（无成交时不收口为 PART_TRADED）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 12
    led.insert(e)
    led.sync_status(12, OrderState.CANCELLED)
    assert led.get_by_order_id(12).state == OrderState.CANCELLED


# ---------------------------------------------------------------------------
# 评审 doc/19 H-1：成交先到置 TRADED 后，迟到的 on_stock_order 委托态不得降级已成单
# ---------------------------------------------------------------------------
def test_sync_status_no_downgrade_from_traded_by_reported():
    """乱序：add_fill 全成置 TRADED 后，迟到 on_stock_order(REPORTED) 不降级（H-1）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 21
    led.insert(e)
    led.add_fill(21, "f1", 1000, Decimal("11.00"))   # 成交回报先到 → TRADED
    assert led.get_by_order_id(21).state == OrderState.TRADED
    led.sync_status(21, OrderState.REPORTED)          # 迟到委托态(已报)
    assert led.get_by_order_id(21).state == OrderState.TRADED  # 不被降级


def test_sync_status_no_downgrade_from_traded_by_part_traded():
    """乱序：全成 TRADED 后，迟到 on_stock_order(部成) 不降级（H-1 关键：PART_TRADED 属 terminal() 仍须拦）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 22
    led.insert(e)
    led.add_fill(22, "f1", 1000, Decimal("11.00"))   # → TRADED
    led.sync_status(22, OrderState.PART_TRADED)       # 迟到部成回报
    got = led.get_by_order_id(22)
    assert got.state == OrderState.TRADED             # 仍 TRADED，绝不退回 PART_TRADED
    assert got.filled_volume == 1000


def test_sync_status_traded_records_msg_without_state_change():
    """已 TRADED 时 sync_status 仍可记 error_msg，但不改状态（H-1 边界）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=500)
    e.order_id = 23
    led.insert(e)
    led.add_fill(23, "f1", 500, Decimal("11.00"))     # → TRADED
    led.sync_status(23, OrderState.REPORTED, msg="late order report")
    got = led.get_by_order_id(23)
    assert got.state == OrderState.TRADED
    assert got.error_msg == "late order report"


def test_sync_status_part_traded_still_advances_before_traded():
    """未达 TRADED 时委托态正常推进（不被 H-1 守卫误拦）：部成单可被后续委托态更新。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 24
    led.insert(e)
    led.add_fill(24, "f1", 600, Decimal("11.00"))     # 部成 → PART_TRADED（非 TRADED）
    led.sync_status(24, OrderState.REPORTED)           # 仍可被委托态更新
    assert led.get_by_order_id(24).state == OrderState.REPORTED


def test_reconcile_fills_traded_not_downgraded_by_partial_details():
    """评审 doc/19 H-1 对称漏洞：已 TRADED 单在启动重建按(崩溃窗口不全的)明细重算时不被降级为 PART_TRADED。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000)
    e.order_id = 31
    led.insert(e)
    led.add_fill(31, "f1", 1000, Decimal("11.00"))   # → TRADED（全成）
    assert led.get_by_order_id(31).state == OrderState.TRADED
    # 模拟崩溃窗口 local_order_fill 明细未全量落盘：只重算出 600（< plan 1000）。
    led.reconcile_fills_from_detail(e.biz_order_no, [("f1", 600, Decimal("11.00"))])
    got = led.get(e.biz_order_no)
    assert got.state == OrderState.TRADED            # 不被降级（守卫生效）
    assert got.filled_volume == 1000                  # 保留全成快照，不被(不全的)明细下修


def test_reconcile_fills_still_upgrades_part_traded_to_traded():
    """对照：未到 TRADED 的单仍按明细正常重算/收口（守卫只拦『已 TRADED 不回退』，不影响向上推进）。"""
    led = InMemoryLocalLedger()
    e = _entry(plan_volume=1000, state=OrderState.PART_TRADED)
    e.order_id = 32
    led.insert(e)
    # 明细重算累计达计划量 → 收口 TRADED（验证守卫不误拦正常推进）。
    led.reconcile_fills_from_detail(
        e.biz_order_no, [("f1", 600, Decimal("11.00")), ("f2", 400, Decimal("11.00"))]
    )
    got = led.get(e.biz_order_no)
    assert got.state == OrderState.TRADED
    assert got.filled_volume == 1000


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


# ===========================================================================
# 评审三轮 EXEC-order-06 / storage-05：跨日 order_id 反查 + 活跃单索引一致性
# ===========================================================================
from qmt_strategy.common.logger import RecordingLogger  # noqa: E402


def _entry_full(biz, td, *, order_id=None, ts_code="600036.SH", family="打板",
                state=OrderState.SUBMITTED, plan_volume=1000):
    return LedgerEntry(
        biz_order_no=biz, account_id="acc1", target_trade_date=td, ts_code=ts_code,
        strategy_family=family, side=TradeSide.BUY, plan_volume=plan_volume,
        plan_price=Decimal("11.00"), order_remark="r", signal_trade_date=td,
        state=state, order_id=order_id,
    )


def test_order_id_reuse_across_days_not_crossed():
    """EXEC-order-06：跨日复用同 order_id，当日回报只更新当日单，不串改历史单。"""
    led = InMemoryLocalLedger()
    d11, d12 = date(2026, 6, 11), date(2026, 6, 12)
    led.insert(_entry_full("bizA", d11, order_id=5))
    led.insert(_entry_full("bizB", d12, order_id=5))
    # order_id=5 当日(D12)回报 → 反查取最近交易日那条(bizB)，只 B 被更新
    led.add_fill(5, "t1", 1000, Decimal("11.00"))
    assert led.get("bizB").filled_volume == 1000
    assert led.get("bizA").filled_volume == 0          # 历史单不被串改
    assert led.get_by_order_id(5).biz_order_no == "bizB"


def test_insert_order_id_collision_warns():
    """EXEC-order-06：同日同 order_id 不同 biz → 告警 ledger_order_id_collision。"""
    logger = RecordingLogger()
    led = InMemoryLocalLedger(logger=logger)
    d = date(2026, 6, 12)
    led.insert(_entry_full("bizA", d, order_id=7))
    led.insert(_entry_full("bizB", d, order_id=7))     # 同日同 order_id 不同 biz → 异常
    assert "ledger_order_id_collision" in logger.events()
    # 跨日同 order_id 不告警（预期复用）
    logger2 = RecordingLogger()
    led2 = InMemoryLocalLedger(logger=logger2)
    led2.insert(_entry_full("bizC", date(2026, 6, 11), order_id=9))
    led2.insert(_entry_full("bizD", date(2026, 6, 12), order_id=9))
    assert "ledger_order_id_collision" not in logger2.events()


def test_find_active_index_consistent_with_state_collapse():
    """EXEC-storage-05：find_active 经活跃索引按键定位，且与权威态一致——收口终态后该键不再活跃。"""
    led = InMemoryLocalLedger()
    d = date(2026, 6, 12)
    led.insert(_entry_full("bizA", d, order_id=5, state=OrderState.SUBMITTED))
    assert led.has_active(d, "600036.SH", "打板") is True
    assert led.find_active(d, "600036.SH", "打板").biz_order_no == "bizA"
    # 无成交撤单收口 CANCELLED → 索引虽残留 bizA，find_active 复核 state 后判非活跃
    led.sync_status(5, OrderState.CANCELLED)
    assert led.has_active(d, "600036.SH", "打板") is False
    assert led.find_active(d, "600036.SH", "打板") is None
    # 不同键不串：另一 ts_code 活跃单不影响本键
    led.insert(_entry_full("bizE", d, order_id=6, ts_code="600000.SH"))
    assert led.has_active(d, "600000.SH", "打板") is True
    assert led.has_active(d, "600036.SH", "打板") is False
