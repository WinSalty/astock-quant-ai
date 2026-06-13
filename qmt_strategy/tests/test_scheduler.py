"""DailyScheduler 单测：decide() 按东八区钟点决策 + run() 驱动整日闭环（app/scheduler.py）。"""

from __future__ import annotations

from datetime import date, time as dtime

from qmt_strategy.app.scheduler import Action, DailyScheduler
from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from tests.conftest import utc_at_east8

TRADING_DAY = date(2026, 6, 12)   # 周五
WEEKEND = date(2026, 6, 13)       # 周六


class _FakeEngine:
    def __init__(self):
        self.calls = []
    def prewarm(self, today): self.calls.append(("prewarm", today))
    def run_auction(self): self.calls.append(("run_auction",))
    def sweep_ttl(self): self.calls.append(("sweep",))
    def run_sell_pass(self, today, books, session="intraday"): self.calls.append(("sell", session))
    def close_batch(self, today): self.calls.append(("close", today))


class _FakeGuard:
    def __init__(self): self.connects = 0
    def connect_and_subscribe(self): self.connects += 1; return True


class _FakeStack:
    def __init__(self): self.synced = []
    def sync_to_remote(self, today): self.synced.append(today)


class _Cal:
    def is_open(self, d): return d.weekday() < 5


def _sched(clock, **kw):
    eng = _FakeEngine(); guard = _FakeGuard(); stack = _FakeStack()
    s = DailyScheduler(eng, guard, stack, clock, RecordingLogger(), calendar=_Cal(), **kw)
    return s, eng, guard, stack


def _count(eng, name):
    return sum(1 for c in eng.calls if c[0] == name)


# ===========================================================================
# decide() 边界决策
# ===========================================================================
def test_decide_before_premarket_idle():
    s, *_ = _sched(FakeClock(utc_at_east8(TRADING_DAY, 8, 0)))
    assert s.decide(utc_at_east8(TRADING_DAY, 8, 0)) == Action.IDLE


def test_decide_premarket_prewarm():
    s, *_ = _sched(FakeClock(utc_at_east8(TRADING_DAY, 9, 0)))
    assert s.decide(utc_at_east8(TRADING_DAY, 9, 0)) == Action.PREWARM


def test_decide_auction_after_prewarm():
    s, *_ = _sched(FakeClock(utc_at_east8(TRADING_DAY, 9, 16)))
    s.mark_prewarmed(TRADING_DAY)
    assert s.decide(utc_at_east8(TRADING_DAY, 9, 16)) == Action.RUN_AUCTION


def test_decide_auction_requires_prewarm_first():
    # 9:16 但还没装载 → 先 PREWARM（不会直接进竞价，保证装载在前）。
    s, *_ = _sched(FakeClock(utc_at_east8(TRADING_DAY, 9, 16)))
    assert s.decide(utc_at_east8(TRADING_DAY, 9, 16)) == Action.PREWARM


def test_decide_intraday():
    s, *_ = _sched(FakeClock(utc_at_east8(TRADING_DAY, 9, 35)))
    s.mark_prewarmed(TRADING_DAY)
    s._mark(TRADING_DAY, Action.RUN_AUCTION)
    assert s.decide(utc_at_east8(TRADING_DAY, 9, 35)) == Action.INTRADAY


def test_decide_close_then_sync():
    s, *_ = _sched(FakeClock(utc_at_east8(TRADING_DAY, 15, 6)))
    s.mark_prewarmed(TRADING_DAY)
    # 收盘点后未做 → CLOSE_BATCH
    assert s.decide(utc_at_east8(TRADING_DAY, 15, 6)) == Action.CLOSE_BATCH
    s._mark(TRADING_DAY, Action.CLOSE_BATCH)
    # 收盘已做、未到同步点(15:35) → IDLE
    assert s.decide(utc_at_east8(TRADING_DAY, 15, 20)) == Action.IDLE
    # 到同步点 → SYNC
    assert s.decide(utc_at_east8(TRADING_DAY, 15, 40)) == Action.SYNC


def test_decide_non_trading_day_idle():
    s, *_ = _sched(FakeClock(utc_at_east8(WEEKEND, 9, 16)))
    assert s.decide(utc_at_east8(WEEKEND, 9, 16)) == Action.IDLE


# ===========================================================================
# run() 整日闭环（注入 sleep_fn 推进时钟，有限轮次）
# ===========================================================================
def test_run_full_day_fires_each_phase():
    clock = FakeClock(utc_at_east8(TRADING_DAY, 8, 54))
    s, eng, guard, stack = _sched(clock, sell_books_provider=lambda today: {})
    # 每轮把时钟推进 10 分钟（忽略 poll 实参），覆盖盘前→竞价→盘中→收盘→同步。
    def fast_sleep(_secs):
        clock.advance(600)
    s.run(sleep_fn=fast_sleep, max_iters=45)
    assert _count(eng, "prewarm") == 1          # 盘前装载一次
    assert guard.connects == 1                   # 建连一次
    assert _count(eng, "run_auction") == 1       # 竞价一次
    assert _count(eng, "sweep") >= 1             # 盘中巡检多次
    assert _count(eng, "sell") >= 1              # 注入盘口源 → 盘中跑卖出
    assert _count(eng, "close") == 1             # 收盘一次
    assert stack.synced == [TRADING_DAY]         # 盘后同步一次


def test_run_skips_non_trading_day():
    clock = FakeClock(utc_at_east8(WEEKEND, 8, 54))
    s, eng, guard, stack = _sched(clock)
    def fast_sleep(_secs):
        clock.advance(600)
    s.run(sleep_fn=fast_sleep, max_iters=45)
    assert eng.calls == [] and guard.connects == 0 and stack.synced == []
