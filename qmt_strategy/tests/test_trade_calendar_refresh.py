"""执行侧盘前交易日历校验 + 补取单测（doc/29 J-3）。

验收（doc/29 D）：覆盖足→用本地不拉取；不足→拉信号侧补取并入本地+落盘；拉取失败/补取后仍不足→fail-closed。
全部用 fake client + StaticTradeCalendar，不连真实 HTTP/文件（落盘走注入的 persist_fn 探针）。
"""

from __future__ import annotations

from datetime import date

from qmt_strategy.common.http_client import SignalHttpError
from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.trade_calendar import StaticTradeCalendar
from qmt_strategy.watchlist.trade_calendar_refresh import (
    TRADE_CALENDAR_PATH,
    TradeCalendarRefresher,
)

TODAY = date(2026, 6, 12)


class _FakeClient:
    """假信号侧客户端：按需返回 open_days 或抛 SignalHttpError，记录调用次数。"""

    def __init__(self, *, open_days=None, fail=False):
        self._open_days = open_days
        self._fail = fail
        self.gets = []

    def get_json(self, path, params=None):
        self.gets.append((path, dict(params or {})))
        if self._fail:
            raise SignalHttpError("boom", status=503)
        return {"exchange": "SSE", "open_days": list(self._open_days or [])}


def _cal(days):
    return StaticTradeCalendar(days)


def test_coverage_sufficient_does_not_fetch():
    """覆盖足（前向≥min_forward）→ 直接用本地，不调接口。"""
    # 6/12 之后还有 6/15..6/19 共 4 个交易日；min_forward=3 → 足。
    cal = _cal([date(2026, 6, d) for d in (12, 15, 16, 17, 18)])
    client = _FakeClient(open_days=["2026-07-01"])
    refresher = TradeCalendarRefresher(client, cal, min_forward_days=3, logger=RecordingLogger())
    assert refresher.ensure_coverage(TODAY) is True
    assert client.gets == []  # 覆盖足，零拉取


def test_insufficient_fetches_merges_and_persists():
    """不足 → 拉信号侧补取，并入共享日历（原地生效）+ 落盘，复检覆盖足 → True。"""
    # 本地仅到 6/15（6/12 之后只剩 1 个交易日），min_forward=3 → 不足。
    cal = _cal([date(2026, 6, 12), date(2026, 6, 15)])
    client = _FakeClient(open_days=["2026-06-12", "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18"])
    persisted = {}
    refresher = TradeCalendarRefresher(
        client, cal, min_forward_days=3,
        persist_fn=lambda days: persisted.update(days=list(days)), logger=RecordingLogger(),
    )
    assert refresher.ensure_coverage(TODAY) is True
    assert client.gets and client.gets[0][0] == TRADE_CALENDAR_PATH
    # 共享日历原地补全（engine 同步生效）：6/12 之后≥3 个交易日
    assert cal.trading_days_left(TODAY) >= 3
    assert cal.is_open(date(2026, 6, 18)) is True
    # 落盘：全量开市日（含新增）写出
    assert date(2026, 6, 18) in persisted["days"]


def test_fetch_failure_fail_closed():
    """拉取失败(HTTP 非2xx/网络) 且本地仍不足 → 返回 False（engine 据此 fail-closed 阻断开仓）。"""
    cal = _cal([date(2026, 6, 12), date(2026, 6, 15)])  # 不足
    client = _FakeClient(fail=True)
    logger = RecordingLogger()
    refresher = TradeCalendarRefresher(client, cal, min_forward_days=3, logger=logger)
    assert refresher.ensure_coverage(TODAY) is False
    assert client.gets  # 试图拉取
    assert "trade_calendar_refresh_http_failed" in logger.events()


def test_fetch_returns_insufficient_still_fail_closed():
    """补取成功但返回的日历仍不覆盖到足够前向 → 复检仍不足 → False。"""
    cal = _cal([date(2026, 6, 12), date(2026, 6, 15)])
    # 接口只回到 6/16（6/12 之后仅 2 个交易日），min_forward=3 → 补取后仍不足。
    client = _FakeClient(open_days=["2026-06-12", "2026-06-15", "2026-06-16"])
    refresher = TradeCalendarRefresher(client, cal, min_forward_days=3, logger=RecordingLogger())
    assert refresher.ensure_coverage(TODAY) is False


def test_no_client_uses_local_only():
    """未配信号侧 client（None）→ 不拉取，仅按本地覆盖度判定（不足即 False）。"""
    cal = _cal([date(2026, 6, 12), date(2026, 6, 15)])
    refresher = TradeCalendarRefresher(None, cal, min_forward_days=3, logger=RecordingLogger())
    assert refresher.ensure_coverage(TODAY) is False
