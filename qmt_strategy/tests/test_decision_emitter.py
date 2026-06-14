"""DecisionEmitter 单测：非阻塞 / 满即丢 / 异常全吞 / EntryDecision 映射 / no-op 降级 / 线程投递。

口径：只验证「采集器自身的安全不变量」——绝不抛错、满即丢、线程崩不了、行结构正确；不接真机/HTTP。
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from decimal import Decimal

from qmt_strategy.decision.decision_emitter import DecisionEmitter


class _Clock:
    """固定 UTC 时钟：2026-06-15 01:31:00Z（东八区 09:31）。"""

    def now_utc(self) -> datetime:
        return datetime(2026, 6, 15, 1, 31, 0)


class _Logger:
    def info(self, *a, **k) -> None:  # noqa: D401
        pass

    def warn(self, *a, **k) -> None:
        pass


def _em(sink=None, enabled=True, **kw) -> DecisionEmitter:
    return DecisionEmitter("ACC", _Clock(), _Logger(), sink=sink, enabled=enabled, **kw)


def test_disabled_is_noop() -> None:
    """enabled=False → emit 不入队、start 不起线程。"""
    em = _em(sink=lambda rows: None, enabled=False)
    em.emit(decision_type="BUY_SUBMIT", ts_code="600000.SH")
    assert em._q.qsize() == 0
    em.start()
    assert em._started is False


def test_no_sink_is_noop() -> None:
    """缺 sink → 等价 no-op（无处可回流）。"""
    em = _em(sink=None, enabled=True)
    em.emit(decision_type="BUY_SUBMIT", ts_code="600000.SH")
    assert em._q.qsize() == 0


def test_emit_builds_row() -> None:
    """emit 行结构 + 双写时间 + Decimal→str + JSON 因子。"""
    em = _em(sink=lambda rows: None)
    em.emit(
        decision_type="BUY_SUBMIT", ts_code="600000.SH", trade_date=date(2026, 6, 15),
        signal_trade_date=date(2026, 6, 12), strategy_family="DABAN", order_id=1001,
        limit_price=Decimal("10.50"), plan_volume=1000, reason="挂涨停价买入",
        reason_code="order_submitted", factors={"open_pct": 4.1},
    )
    row = em._q.get_nowait()
    assert row["account_id"] == "ACC"
    assert row["decision_type"] == "BUY_SUBMIT"
    assert row["ts_code"] == "600000.SH"
    assert row["trade_date"] == "2026-06-15"
    assert row["signal_trade_date"] == "2026-06-12"
    assert row["limit_price"] == "10.50"
    assert row["plan_volume"] == 1000
    assert row["order_id"] == 1001
    assert row["factors_snapshot"] == {"open_pct": 4.1}
    assert row["decided_time"] == "2026-06-15T01:31:00"
    assert row["decided_time_east8"] == "2026-06-15T09:31:00"  # UTC+8，不盲目 ±8h（源是 UTC）
    assert row["decision_id"]
    assert row["data_source"] == "EMITTER"


def test_trade_date_defaults_to_east8_date() -> None:
    """不传 trade_date → 取决策时刻东八区自然日。"""
    em = _em(sink=lambda rows: None)
    em.emit(decision_type="SKIP_ORDER", ts_code="600000.SH")
    assert em._q.get_nowait()["trade_date"] == "2026-06-15"


def test_queue_full_drops_no_raise() -> None:
    """队列满 → 丢弃 + 计数，绝不抛错（非阻塞核心）。"""
    em = _em(sink=lambda rows: None, queue_size=2)
    for _ in range(10):
        em.emit(decision_type="X", ts_code="A")  # 不得抛
    assert em._q.qsize() == 2
    assert em._dropped >= 8


def test_emit_never_raises_on_bad_input() -> None:
    """构造异常（decided_at 非 datetime）被吞，不入队、不抛错。"""
    em = _em(sink=lambda rows: None)
    em.emit(decision_type="X", decided_at="not-a-datetime")
    assert em._q.qsize() == 0


def test_append_entry_decision_mapping() -> None:
    """append(EntryDecision)：SKIP→SKIP_STRATEGY，非 SKIP→SIGNAL_QUALIFIED。"""
    em = _em(sink=lambda rows: None)

    class _Act:
        value = "SKIP"

    class _Dec:
        action = _Act()
        ts_code = "600000.SH"
        signal_trade_date = date(2026, 6, 12)
        target_trade_date = date(2026, 6, 15)
        strategy_family = "DABAN"
        order_phase = None
        reason = "竞价弱开放弃"
        factors_snapshot = {"open_pct": -1.0}
        limit_price = None
        plan_volume = None
        decided_at = datetime(2026, 6, 15, 1, 25, 0)

    em.append(_Dec())
    row = em._q.get_nowait()
    assert row["decision_type"] == "SKIP_STRATEGY"
    assert row["reason"] == "竞价弱开放弃"
    assert row["signal_trade_date"] == "2026-06-12"

    class _Act2:
        value = "CHASE_LIMIT_UP"

    d2 = _Dec()
    d2.action = _Act2()
    em.append(d2)
    assert em._q.get_nowait()["decision_type"] == "SIGNAL_QUALIFIED"


def test_append_never_raises_on_garbage() -> None:
    """append 传入非法对象不抛错（采集绝不影响交易）。"""
    em = _em(sink=lambda rows: None)
    em.append(object())  # 缺字段，getattr 兜底 None；不得抛
    em.append(None)


def test_worker_thread_delivers() -> None:
    """启动线程 → emit 的行被 sink 收到。"""
    got: list = []
    ev = threading.Event()

    def sink(rows):
        got.extend(rows)
        ev.set()

    em = _em(sink=sink, poll_interval=0.05)
    em.start()
    try:
        em.emit(decision_type="BUY_SUBMIT", ts_code="600000.SH")
        assert ev.wait(2.0), "worker 未在超时内投递"
        assert got and got[0]["decision_type"] == "BUY_SUBMIT"
    finally:
        em.stop()


def test_worker_swallows_sink_error() -> None:
    """sink 抛错 → 消费线程吞掉不崩，后续仍能投递。"""
    calls = {"n": 0}
    ev = threading.Event()

    def sink(rows):
        calls["n"] += 1
        ev.set()
        raise RuntimeError("signal down")

    em = _em(sink=sink, poll_interval=0.05)
    em.start()
    try:
        em.emit(decision_type="X", ts_code="A")
        assert ev.wait(2.0)
        em.emit(decision_type="Y", ts_code="B")  # 线程未崩，仍接受
    finally:
        em.stop()
    assert calls["n"] >= 1
