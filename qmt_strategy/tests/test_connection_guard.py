"""连接守护单测（§2.2 时序 / §2.6 兜底 / §2.8 验收）。

全部用 fake/内存实现，不连真实 xttrader：
- 一个记录调用顺序的 fake trader，断言严格 register_callback→start→connect→subscribe(→run_forever)。
- connect() 非 0 → ready False、未调 subscribe、未调 run_forever。
- subscribe() 非 0 → ready False。
- 不依赖 on_connected：connect 返 0 即 ready=True，无需任何 connected 回调。
- on_disconnected → 生成新 session_id(≠旧)、重走全序、on_reconnect_backfill 恰被调一次。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.connection.connection_guard import ConnectionGuard
from qmt_strategy.contracts.xt_objects import FakeStockAccount


class RecordingFakeTrader:
    """记录调用顺序的 fake XtQuantTrader（实现 XtTraderLike 协议的子集）。

    calls：按发生顺序记录方法名，供断言严格时序；
    connect_rc / subscribe_rc：注入返回码模拟连接 / 订阅成败；
    run_forever_called：是否进入常驻（单测不触发 run，仅验证未被误调）。
    """

    def __init__(self, *, connect_rc: int = 0, subscribe_rc: int = 0) -> None:
        self.calls: List[str] = []
        self._connect_rc = connect_rc
        self._subscribe_rc = subscribe_rc
        self.registered_callback: Any = None
        self.subscribed_account: Any = None
        self.run_forever_called = False

    def register_callback(self, callback: Any) -> None:
        self.calls.append("register_callback")
        self.registered_callback = callback

    def start(self) -> None:
        self.calls.append("start")

    def connect(self) -> int:
        self.calls.append("connect")
        return self._connect_rc

    def subscribe(self, account: Any) -> int:
        self.calls.append("subscribe")
        self.subscribed_account = account
        return self._subscribe_rc

    def run_forever(self) -> None:
        self.calls.append("run_forever")
        self.run_forever_called = True


def _clock() -> FakeClock:
    # 固定 UTC naive 时刻；连接事件日志按东八区展示，不影响断言。
    return FakeClock(datetime(2026, 6, 12, 1, 16, 0))


def _make_guard(
    traders: List[RecordingFakeTrader],
    session_ids: List[int],
    *,
    on_reconnect_backfill=None,
):
    """按预置 trader 列表与 session_id 列表构造守护。

    每次 connect_and_subscribe 现取下一个 session_id 并造下一个 trader，
    便于在「初连 + 重连」场景中区分两次调用各自的 trader 与 session。
    """
    logger = RecordingLogger()
    account = FakeStockAccount("acc1", account_type=2)
    callback = object()  # 回调内容本守护不解析，用哨兵对象即可

    sid_iter = iter(session_ids)
    trader_iter = iter(traders)

    def session_id_provider() -> int:
        return next(sid_iter)

    def trader_factory(session_id: int) -> RecordingFakeTrader:
        # factory 按 session_id 造 trader，这里直接吐预置列表的下一个并记录其 session。
        t = next(trader_iter)
        t.session_id = session_id  # 记录该 trader 是用哪个 session 造的，供断言
        return t

    guard = ConnectionGuard(
        trader_factory=trader_factory,
        account=account,
        callback=callback,
        clock=_clock(),
        logger=logger,
        session_id_provider=session_id_provider,
        on_reconnect_backfill=on_reconnect_backfill,
    )
    return guard, logger, account, callback


# ---------------------------------------------------------------------------
# 1) 严格时序：register_callback → start → connect → subscribe
# ---------------------------------------------------------------------------
def test_strict_call_order_on_success():
    trader = RecordingFakeTrader(connect_rc=0, subscribe_rc=0)
    guard, _logger, account, callback = _make_guard([trader], [1001])

    ok = guard.connect_and_subscribe()

    assert ok is True
    assert guard.ready is True
    # 严格顺序断言（不含 run_forever：connect_and_subscribe 不负责常驻）。
    assert trader.calls == ["register_callback", "start", "connect", "subscribe"]
    # 注册的是构造时传入的同一 callback，订阅用的是构造时传入的同一 account。
    assert trader.registered_callback is callback
    assert trader.subscribed_account is account
    # 未触发 run_forever（单测不调 run）。
    assert trader.run_forever_called is False
    # session_id / trader 已保存。
    assert guard.current_session_id == 1001
    assert guard.current_trader is trader


# ---------------------------------------------------------------------------
# 2) connect() 非 0 → ready False、未调 subscribe、未调 run_forever
# ---------------------------------------------------------------------------
def test_connect_failure_blocks_subscribe_and_run():
    trader = RecordingFakeTrader(connect_rc=-1, subscribe_rc=0)
    guard, logger, _account, _callback = _make_guard([trader], [2002])

    ok = guard.connect_and_subscribe()

    assert ok is False
    assert guard.ready is False
    # connect 失败后绝不调 subscribe / run_forever。
    assert trader.calls == ["register_callback", "start", "connect"]
    assert "subscribe" not in trader.calls
    assert trader.run_forever_called is False
    # 产生 connect_failed 告警，带 rc。
    assert "connect_failed" in logger.events()
    rec = [r for r in logger.records if r[1] == "connect_failed"][0]
    assert rec[2]["rc"] == -1
    # 失败也保存了 session_id / trader，供后续重建对比。
    assert guard.current_session_id == 2002
    assert guard.current_trader is trader


# ---------------------------------------------------------------------------
# 3) subscribe() 非 0 → ready False
# ---------------------------------------------------------------------------
def test_subscribe_failure_not_ready():
    trader = RecordingFakeTrader(connect_rc=0, subscribe_rc=5)
    guard, logger, _account, _callback = _make_guard([trader], [3003])

    ok = guard.connect_and_subscribe()

    assert ok is False
    assert guard.ready is False
    # connect 已调用、subscribe 也已尝试，但订阅失败仍判未就绪。
    assert trader.calls == ["register_callback", "start", "connect", "subscribe"]
    assert trader.run_forever_called is False
    assert "subscribe_failed" in logger.events()
    rec = [r for r in logger.records if r[1] == "subscribe_failed"][0]
    assert rec[2]["sub_rc"] == 5


# ---------------------------------------------------------------------------
# 4) 不依赖 on_connected：connect 返 0 即 ready=True，无需任何 connected 回调
# ---------------------------------------------------------------------------
def test_ready_without_on_connected_callback():
    trader = RecordingFakeTrader(connect_rc=0, subscribe_rc=0)
    guard, _logger, _account, _callback = _make_guard([trader], [4004])

    # 直接连接，全程不模拟任何 on_connected 回调。
    ok = guard.connect_and_subscribe()
    assert ok is True
    assert guard.ready is True


def test_source_has_no_on_connected_reference():
    """可执行代码不得引用 on_connected（防误加该不存在的回调，§2.8）。

    口径：就绪判定只读 connect() 返回值。文档/注释可解释「无 on_connected」这一约束，
    但 AST 里不得出现 on_connected 的名字/属性引用。这里用 AST 遍历仅检查可执行代码，
    剔除 docstring 与注释（它们恰恰是在说明这条规则本身）。
    """
    import ast
    import inspect

    import qmt_strategy.connection.connection_guard as mod

    tree = ast.parse(inspect.getsource(mod))
    offending = []
    for node in ast.walk(tree):
        # 属性访问 obj.on_connected
        if isinstance(node, ast.Attribute) and node.attr == "on_connected":
            offending.append(node)
        # 裸名字 on_connected
        if isinstance(node, ast.Name) and node.id == "on_connected":
            offending.append(node)
        # 关键字参数 on_connected=...
        if isinstance(node, ast.keyword) and node.arg == "on_connected":
            offending.append(node)
        # 字符串常量里出现（如误用字符串字段名），但排除模块/类/函数 docstring。
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "on_connected" in node.value:
            offending.append(node)
    # docstring 是 Module/ClassDef/FunctionDef body[0] 的表达式字符串，需从命中里剔除。
    docstring_consts = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstring_consts.add(id(first.value))
    real = [n for n in offending if id(n) not in docstring_consts]
    assert real == [], "on_connected 不应出现在可执行代码中"


# ---------------------------------------------------------------------------
# 5) on_disconnected → 新 session_id(≠旧)、重走全序、补采恰一次
# ---------------------------------------------------------------------------
def test_disconnect_triggers_reconnect_with_new_session_and_backfill_once():
    # 两个 trader：初连用 t0(session=5005)，重连用 t1(session=6006)。
    t0 = RecordingFakeTrader(connect_rc=0, subscribe_rc=0)
    t1 = RecordingFakeTrader(connect_rc=0, subscribe_rc=0)

    # 计数器：断言 on_reconnect_backfill 恰被调用一次。
    backfill_calls = {"n": 0}

    def backfill():
        backfill_calls["n"] += 1

    guard, logger, _account, _callback = _make_guard(
        [t0, t1], [5005, 6006], on_reconnect_backfill=backfill
    )

    # 初连成功。
    assert guard.connect_and_subscribe() is True
    assert guard.ready is True
    assert guard.current_session_id == 5005
    # 初连不触发补采（补采只在重连成功后触发）。
    assert backfill_calls["n"] == 0

    # 触发断线 → 自动重建。
    guard.on_disconnected()

    # 重连后就绪、且换了新 session_id（≠旧）。
    assert guard.ready is True
    assert guard.current_session_id == 6006
    assert guard.current_session_id != 5005
    # 重连用的是新 trader t1，并按新 session 造。
    assert guard.current_trader is t1
    assert t1.session_id == 6006
    # 新 trader 重走了全序。
    assert t1.calls == ["register_callback", "start", "connect", "subscribe"]
    # 补采恰被调用一次。
    assert backfill_calls["n"] == 1
    # 断线与重连事件均有日志。
    assert "disconnected" in logger.events()
    assert "reconnected" in logger.events()


def test_reconnect_failure_does_not_backfill():
    """重连若 connect 失败，则不触发补采（连接未就绪，补采取不到权威数据）。"""
    t0 = RecordingFakeTrader(connect_rc=0, subscribe_rc=0)
    t1 = RecordingFakeTrader(connect_rc=-9, subscribe_rc=0)  # 重连 connect 失败

    backfill_calls = {"n": 0}

    def backfill():
        backfill_calls["n"] += 1

    guard, _logger, _account, _callback = _make_guard(
        [t0, t1], [7007, 8008], on_reconnect_backfill=backfill
    )

    assert guard.connect_and_subscribe() is True
    guard.on_disconnected()

    # 重连未就绪：ready False、补采未触发、新 session 也已换。
    assert guard.ready is False
    assert guard.current_session_id == 8008
    assert backfill_calls["n"] == 0
    # 重连 connect 失败后未调 subscribe。
    assert t1.calls == ["register_callback", "start", "connect"]


def test_default_backfill_is_noop():
    """未注入 on_reconnect_backfill 时，重连成功也不报错（默认 no-op）。"""
    t0 = RecordingFakeTrader(connect_rc=0, subscribe_rc=0)
    t1 = RecordingFakeTrader(connect_rc=0, subscribe_rc=0)
    guard, _logger, _account, _callback = _make_guard([t0, t1], [9009, 9010])

    assert guard.connect_and_subscribe() is True
    # 不传 backfill，断线重建不应抛异常。
    guard.on_disconnected()
    assert guard.ready is True
    assert guard.current_session_id == 9010


# ---------------------------------------------------------------------------
# 6) 评审三轮 EXEC-sched-03：snapshot 一致快照 + 连接序入口先置未就绪
# ---------------------------------------------------------------------------
def test_snapshot_returns_consistent_pair():
    """snapshot() 返回 (ready, current_trader) 一致快照（供主循环跨线程取一致对）。"""
    t0 = RecordingFakeTrader(connect_rc=0, subscribe_rc=0)
    guard, _logger, _account, _callback = _make_guard([t0], [1])
    # 未连接：未就绪、trader 为 None。
    assert guard.snapshot() == (False, None)
    assert guard.connect_and_subscribe() is True
    assert guard.snapshot() == (True, t0)


def test_ready_false_at_entry_of_connect():
    """重连/初连序一进入即 ready=False（不残留旧 True，避免对半就绪句柄 run_forever）。"""
    box = {}

    class _AssertingTrader(RecordingFakeTrader):
        def connect(self):
            # connect 被调用时（已切到本新实例），就绪标记必须已先置 False。
            box["ready_during_connect"] = box["guard"].ready
            return super().connect()

    t = _AssertingTrader(connect_rc=0, subscribe_rc=0)
    guard, _logger, _account, _callback = _make_guard([t], [42])
    box["guard"] = guard
    assert guard.connect_and_subscribe() is True
    assert box["ready_during_connect"] is False
    assert guard.ready is True


# ---------------------------------------------------------------------------
# 7) 评审三轮 EXEC-sched-02：on_connection_state 通知解冻/冻结
# ---------------------------------------------------------------------------
def test_connection_state_notified_on_ready_and_disconnect():
    """连接就绪→on_connection_state(True)、断线→(False)、重连就绪→(True)。"""
    states = []
    t0 = RecordingFakeTrader(connect_rc=0, subscribe_rc=0)
    t1 = RecordingFakeTrader(connect_rc=0, subscribe_rc=0)
    logger = RecordingLogger()
    account = FakeStockAccount("acc1", account_type=2)

    sids = iter([111, 222])
    traders = iter([t0, t1])
    guard = ConnectionGuard(
        trader_factory=lambda s: next(traders),
        account=account, callback=object(), clock=_clock(), logger=logger,
        session_id_provider=lambda: next(sids),
        on_connection_state=states.append,
    )
    assert guard.connect_and_subscribe() is True
    assert states[-1] is True
    guard.on_disconnected()
    # 断线先通知 False，重连就绪再通知 True。
    assert False in states
    assert states[-1] is True


def test_connect_failure_notifies_connection_state_false():
    """connect 失败 → on_connection_state(False)（冻结下单闸）。"""
    states = []
    t = RecordingFakeTrader(connect_rc=-1, subscribe_rc=0)
    logger = RecordingLogger()
    guard = ConnectionGuard(
        trader_factory=lambda s: t, account=FakeStockAccount("acc1"), callback=object(),
        clock=_clock(), logger=logger, session_id_provider=lambda: 9,
        on_connection_state=states.append,
    )
    assert guard.connect_and_subscribe() is False
    assert states == [False]


# ---------------------------------------------------------------------------
# 8) 评审三轮 EXEC-sched-08：session 复用 fail-closed
# ---------------------------------------------------------------------------
def test_reconnect_session_reuse_fails_closed():
    """provider 持续吐相同 session → 退避重取至上限仍复用 → 拒绝重连、未就绪、不补采。"""
    logger = RecordingLogger()
    backfill_calls = {"n": 0}

    # provider 恒返回同一 session 5（模拟误配复用）；trader_factory 每次造新成功 trader。
    def session_provider():
        return 5

    def trader_factory(_sid):
        return RecordingFakeTrader(connect_rc=0, subscribe_rc=0)

    guard = ConnectionGuard(
        trader_factory=trader_factory, account=FakeStockAccount("acc1"), callback=object(),
        clock=_clock(), logger=logger, session_id_provider=session_provider,
        on_reconnect_backfill=lambda: backfill_calls.__setitem__("n", backfill_calls["n"] + 1),
        max_session_retry=3,
    )
    # 初连成功（session=5）。
    assert guard.connect_and_subscribe() is True
    # 断线 → reconnect：新 session 仍是 5（复用）→ 退避重取 3 次仍复用 → 拒绝重连。
    guard.on_disconnected()
    assert guard.ready is False
    assert backfill_calls["n"] == 0                       # 复用 session 绝不补采
    assert "reconnect_aborted_session_reuse" in logger.events()
