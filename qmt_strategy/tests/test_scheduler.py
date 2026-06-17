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


# ===========================================================================
# 评审三轮 EXEC-sched-10：PREWARM 以连接就绪为前置
# ===========================================================================
class _FlakyGuard:
    """connect_and_subscribe 可注入返回值（模拟连接失败）。"""
    def __init__(self, results):
        self._results = list(results)
        self.connects = 0
    def connect_and_subscribe(self):
        self.connects += 1
        return self._results[min(self.connects - 1, len(self._results) - 1)]


def test_prewarm_not_fired_when_connect_fails_before_auction():
    # 9:00（< 9:15）连接失败 → 不标 fired（下轮 decide 仍返回 PREWARM 重做），并强告警。
    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 0))
    eng = _FakeEngine(); guard = _FlakyGuard([False, True]); stack = _FakeStack()
    s = DailyScheduler(eng, guard, stack, clock, RecordingLogger(), calendar=_Cal())
    s.dispatch(Action.PREWARM, TRADING_DAY)
    assert s.decide(clock.now_utc()) == Action.PREWARM       # 未定稿，仍需 PREWARM
    # 第二轮连接成功 → 定稿 fired。
    s.dispatch(Action.PREWARM, TRADING_DAY)
    assert s.decide(clock.now_utc()) != Action.PREWARM       # 已定稿
    assert "scheduler_prewarm_conn_not_ready_will_retry" in s._logger.events()


def test_prewarm_not_fired_after_9_15_when_conn_fails_then_recovers():
    """评审 doc/19 H-4：连接未就绪时 PREWARM 【不论时间】都不定稿（原实现过 9:15 强制定稿是不可恢复单点），
    连接恢复后才定稿（engine.prewarm 重跑补齐持仓重建/权益基线）。"""
    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 20))  # 9:20 已过竞价起点
    eng = _FakeEngine(); guard = _FlakyGuard([False, True]); stack = _FakeStack()
    s = DailyScheduler(eng, guard, stack, clock, RecordingLogger(), calendar=_Cal())
    s.dispatch(Action.PREWARM, TRADING_DAY)
    # 过 9:15 但连接失败 → 仍不定稿（H-4：连接未就绪不可定稿，可恢复）。
    assert Action.PREWARM not in s._fired.get(TRADING_DAY, set())
    assert "scheduler_prewarm_conn_not_ready_will_retry" in s._logger.events()
    # 第二轮连接恢复 → 定稿 fired，prewarm 重跑补齐持仓/基线。
    s.dispatch(Action.PREWARM, TRADING_DAY)
    assert Action.PREWARM in s._fired.get(TRADING_DAY, set())


def test_prewarm_retry_naturally_terminates_at_close():
    """H-4：连接持续失败致 PREWARM 始终不定稿，但 decide() 仅在 < 收盘 才返回 PREWARM——
    过收盘点(默认 15:05)自然转 CLOSE_BATCH，证明「连接未就绪不限时重试」天然以收盘为界、不会无限。"""
    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 20))
    eng = _FakeEngine(); guard = _FlakyGuard([False]); stack = _FakeStack()
    s = DailyScheduler(eng, guard, stack, clock, RecordingLogger(), calendar=_Cal())
    s.dispatch(Action.PREWARM, TRADING_DAY)
    assert Action.PREWARM not in s._fired.get(TRADING_DAY, set())          # 连接失败 → 不定稿
    assert s.decide(utc_at_east8(TRADING_DAY, 9, 20)) == Action.PREWARM    # 盘中仍重试
    # 推进到收盘后：即便 PREWARM 从未定稿，decide 也转 CLOSE_BATCH（重试自然终止，非无限）。
    assert s.decide(utc_at_east8(TRADING_DAY, 15, 6)) == Action.CLOSE_BATCH


def test_prewarm_skips_connect_when_guard_already_ready():
    """评审 doc/19 H-2：guard 已就绪时 PREWARM 不再调 connect_and_subscribe（不重建已连好的通道）。"""
    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 0))

    class _ReadyGuard:
        def __init__(self): self.connects = 0; self.ready = True
        def connect_and_subscribe(self): self.connects += 1; return True

    eng = _FakeEngine(); guard = _ReadyGuard(); stack = _FakeStack()
    s = DailyScheduler(eng, guard, stack, clock, RecordingLogger(), calendar=_Cal())
    s.dispatch(Action.PREWARM, TRADING_DAY)
    assert guard.connects == 0                                   # 已就绪 → 不重建
    assert Action.PREWARM in s._fired.get(TRADING_DAY, set())    # 视为已连，正常定稿


# ===========================================================================
# 评审三轮 EXEC-risk-04 / EXEC-risk-05 / EXEC-sched-05：盘中/竞价卖出 + 探活
# ===========================================================================
class _RichEngine(_FakeEngine):
    """带行情健康/通道探活记录的 fake engine。"""
    def __init__(self):
        super().__init__()
        self.feed_reports = []
        self.heartbeats = 0
    def report_market_feed(self, ok): self.feed_reports.append(ok)
    def trade_conn_heartbeat(self): self.heartbeats += 1


def _rich_sched(clock, **kw):
    eng = _RichEngine(); guard = _FakeGuard(); stack = _FakeStack()
    s = DailyScheduler(eng, guard, stack, clock, RecordingLogger(), calendar=_Cal(), **kw)
    return s, eng, guard, stack


def test_intraday_calls_heartbeat():
    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 35))
    s, eng, _g, _st = _rich_sched(clock)
    s.dispatch(Action.INTRADAY, TRADING_DAY)
    assert eng.heartbeats == 1                # 盘中主动探活下单通道


def test_intraday_no_books_provider_reports_feed_false():
    # 无盘口源 → 盘中保守置行情不健康（FREEZE），不跑 run_sell_pass。
    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 35))
    s, eng, _g, _st = _rich_sched(clock, sell_books_provider=None)
    s.dispatch(Action.INTRADAY, TRADING_DAY)
    assert eng.feed_reports == [False]
    assert _count(eng, "sell") == 0


def test_intraday_books_failure_reports_feed_false():
    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 35))
    def boom(_today):
        raise RuntimeError("xtdata down")
    s, eng, _g, _st = _rich_sched(clock, sell_books_provider=boom)
    s.dispatch(Action.INTRADAY, TRADING_DAY)
    assert eng.feed_reports == [False]
    assert "scheduler_sell_books_failed" in s._logger.events()
    assert _count(eng, "sell") == 0


def test_intraday_books_ok_reports_feed_true_and_sells():
    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 35))
    s, eng, _g, _st = _rich_sched(clock, sell_books_provider=lambda today: {"600036.SH": object()})
    s.dispatch(Action.INTRADAY, TRADING_DAY)
    assert eng.feed_reports == [True]
    assert ("sell", "intraday") in eng.calls


# ===========================================================================
# 评审 doc/19 H-4：RUN_AUCTION 成功才标 fired，异常（<9:30）可重试恢复
# ===========================================================================
def test_run_auction_exception_not_marked_fired_and_retryable():
    """run_auction 抛异常 → 不标 fired（异常由 run() 顶层吞；dispatch 直调则上抛）→ <9:30 decide 仍返回 RUN_AUCTION。"""
    import pytest

    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 16))

    class _BoomEngine(_FakeEngine):
        def run_auction(self):
            raise RuntimeError("auction entry boom")

    eng = _BoomEngine(); guard = _FakeGuard(); stack = _FakeStack()
    s = DailyScheduler(eng, guard, stack, clock, RecordingLogger(), calendar=_Cal())
    s._mark(TRADING_DAY, Action.PREWARM)                 # 前置：已 prewarm
    with pytest.raises(RuntimeError):
        s.dispatch(Action.RUN_AUCTION, TRADING_DAY)      # 异常上抛（run() 顶层会吞，这里直接断言）
    assert Action.RUN_AUCTION not in s._fired.get(TRADING_DAY, set())   # 未定稿
    assert s.decide(clock.now_utc()) == Action.RUN_AUCTION              # 9:16 仍在窗内，可重试


def test_run_auction_recovers_after_transient_exception():
    """首轮 run_auction 异常→run() 吞掉不定稿，下一轮 decide 仍 RUN_AUCTION→重试成功才定稿（整段可恢复）。"""
    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 16))

    class _FlakyAuctionEngine(_FakeEngine):
        def __init__(self):
            super().__init__(); self.auction_attempts = 0
        def run_auction(self):
            self.auction_attempts += 1
            if self.auction_attempts == 1:
                raise RuntimeError("transient auction boom")
            super().run_auction()

    eng = _FlakyAuctionEngine(); guard = _FakeGuard(); stack = _FakeStack()
    s = DailyScheduler(eng, guard, stack, clock, RecordingLogger(), calendar=_Cal())
    s.run(sleep_fn=lambda _s: None, max_iters=3)         # iter1 PREWARM / iter2 异常 / iter3 重试成功
    assert eng.auction_attempts == 2                     # 首轮异常后重试一次
    assert Action.RUN_AUCTION in s._fired.get(TRADING_DAY, set())   # 成功才定稿


def test_auction_sell_entry_runs_with_provider():
    # 评审三轮 EXEC-sched-05：竞价段有 session='auction' 的卖出调度入口（decide_auction 非死代码）。
    clock = FakeClock(utc_at_east8(TRADING_DAY, 9, 16))
    s, eng, _g, _st = _rich_sched(clock, sell_books_provider=lambda today: {"600036.SH": object()})
    s.dispatch(Action.RUN_AUCTION, TRADING_DAY)
    assert ("sell", "auction") in eng.calls   # 竞价段卖出评估入口存在
    assert _count(eng, "run_auction") == 1    # 买入侧轮询照常
    # 竞价段不改 _market_feed_ok（由买入侧 AuctionPoller 自管）。
    assert eng.feed_reports == []
