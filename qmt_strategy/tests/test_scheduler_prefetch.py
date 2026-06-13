"""调度器 PREWARM 的 watchlist 拉取钩子单测：先 prefetch 后 engine.prewarm、失败不拖垮 PREWARM。"""

from __future__ import annotations

from datetime import date

from qmt_strategy.app.scheduler import Action, DailyScheduler


class _FakeLogger:
    def info(self, e, **f):
        pass

    def warn(self, e, **f):
        pass

    def error(self, e, **f):
        pass


class _FakeGuard:
    def __init__(self):
        self.connected = False

    def connect_and_subscribe(self):
        self.connected = True


class _FakeEngine:
    def __init__(self, order):
        self._order = order
        self.prewarmed = []

    def prewarm(self, today):
        self._order.append("prewarm")
        self.prewarmed.append(today)


def _make_scheduler(calendar, engine, prefetch):
    return DailyScheduler(
        engine, _FakeGuard(), None, None, _FakeLogger(),
        calendar=calendar, watchlist_prefetch=prefetch,
    )


def test_prewarm_runs_prefetch_before_engine_prewarm(calendar):
    order = []

    def prefetch(today):
        order.append("prefetch")
        return 3

    engine = _FakeEngine(order)
    sched = _make_scheduler(calendar, engine, prefetch)
    sched.dispatch(Action.PREWARM, date(2026, 6, 12))

    assert order == ["prefetch", "prewarm"]  # 先拉名单后装载
    assert engine.prewarmed == [date(2026, 6, 12)]


def test_prewarm_survives_prefetch_error(calendar):
    def prefetch(today):
        raise RuntimeError("net down")

    order = []
    engine = _FakeEngine(order)
    sched = _make_scheduler(calendar, engine, prefetch)
    # prefetch 抛错也不应阻断 engine.prewarm（调度器内层兜底）
    sched.dispatch(Action.PREWARM, date(2026, 6, 12))
    assert engine.prewarmed == [date(2026, 6, 12)]


def test_prewarm_without_prefetch_hook_still_prewarms(calendar):
    order = []
    engine = _FakeEngine(order)
    sched = _make_scheduler(calendar, engine, None)  # 未配钩子（向后兼容）
    sched.dispatch(Action.PREWARM, date(2026, 6, 12))
    assert engine.prewarmed == [date(2026, 6, 12)]
