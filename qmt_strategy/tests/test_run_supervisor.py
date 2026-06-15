"""run.py 主循环监督 + session_id provider 单测（评审三轮 EXEC-sched-03/07/09）。

主循环 run_forever 阻塞语义属目标机 TODO 实测，故把可测的「监督一轮」逻辑抽成 _supervise_once，
单测覆盖：就绪则 run、未就绪则主动退避重连 + 计数 + 阈值告警；session_id provider 起点高位且不重叠。
"""

from __future__ import annotations

import qmt_strategy.app.run as run
from qmt_strategy.common.logger import RecordingLogger


class _FakeGuard:
    """监督单测用 fake guard：snapshot 返回 (ready, trader)，reconnect 计数（可在 N 次后转就绪）。"""

    def __init__(self, ready: bool, *, become_ready_after: int = -1):
        self._ready = ready
        self._trader = object()
        self.reconnects = 0
        self._become_ready_after = become_ready_after

    def snapshot(self):
        return (self._ready, self._trader if self._ready else None)

    def reconnect(self):
        self.reconnects += 1
        if 0 <= self._become_ready_after <= self.reconnects:
            self._ready = True
        return self._ready


# ---------------------------------------------------------------------------
# _supervise_once
# ---------------------------------------------------------------------------
def test_supervise_runs_when_ready_and_resets_streak():
    g = _FakeGuard(ready=True)
    logger = RecordingLogger()
    ready, trader, state = run._supervise_once(g, logger, {"not_ready_streak": 5})
    assert ready is True
    assert trader is g._trader
    assert state["not_ready_streak"] == 0   # 就绪清零
    assert g.reconnects == 0                 # 就绪不重连


def test_supervise_reconnects_when_not_ready():
    g = _FakeGuard(ready=False)
    logger = RecordingLogger()
    ready, trader, state = run._supervise_once(g, logger, {"not_ready_streak": 0})
    assert ready is False
    assert trader is None
    assert g.reconnects == 1                 # 未就绪主动退避重连
    assert state["not_ready_streak"] == 1


def test_supervise_alerts_after_threshold():
    g = _FakeGuard(ready=False)
    logger = RecordingLogger()
    state = {"not_ready_streak": 0}
    for _ in range(run._NOT_READY_ALERT_THRESHOLD):
        _ready, _t, state = run._supervise_once(g, logger, state)
    assert state["not_ready_streak"] == run._NOT_READY_ALERT_THRESHOLD
    assert "run_loop_persistently_disconnected" in logger.events()  # 连续未就绪强告警


def test_supervise_reconnect_exception_does_not_crash():
    class _Boom(_FakeGuard):
        def reconnect(self):
            raise RuntimeError("reconnect boom")

    g = _Boom(ready=False)
    logger = RecordingLogger()
    ready, _t, state = run._supervise_once(g, logger, {"not_ready_streak": 0})
    assert ready is False
    assert "run_supervise_reconnect_error" in logger.events()  # 重连异常不终止主循环、留痕


# ---------------------------------------------------------------------------
# _make_session_id_provider（EXEC-sched-07）
# ---------------------------------------------------------------------------
def test_session_id_provider_monotonic_and_positive_int32():
    provider = run._make_session_id_provider()
    vals = [provider() for _ in range(5)]
    # 严格单调递增。
    assert all(b > a for a, b in zip(vals, vals[1:]))
    # 收口到正 int32（< 2^31），避免被 xtquant C 层整型截断（评审三轮 P0-1）。
    assert all(0 < v < 2 ** 31 for v in vals)


def test_session_id_providers_do_not_overlap_across_restart(monkeypatch):
    """同秒不同毫秒重启的两个 provider 区间不重叠（毫秒基底 + 高位移位）。"""
    monkeypatch.setattr(run.os, "getpid", lambda: 10)
    monkeypatch.setattr(run.random, "getrandbits", lambda n: 1)
    # 进程1：1000.001s。
    monkeypatch.setattr(run.time, "time", lambda: 1000.001)
    p1 = [run._make_session_id_provider()() for _ in range(100)]
    # 进程2：1000.050s（同秒不同毫秒）。
    monkeypatch.setattr(run.time, "time", lambda: 1000.050)
    p2 = [run._make_session_id_provider()() for _ in range(100)]
    assert max(p1) < min(p2)  # 两次启动区间完全不重叠
