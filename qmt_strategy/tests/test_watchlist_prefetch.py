"""盘前 watchlist 拉取单测：item→SelectedStockRow 映射、prefetch 拉取/落库、失败降级。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from qmt_strategy.common.http_client import SignalHttpError
from qmt_strategy.watchlist.remote_watchlist import (
    WATCHLIST_PATH,
    WatchlistPrefetcher,
    watchlist_item_to_selected,
)


class _FakeLogger:
    def info(self, e, **f):
        pass

    def warn(self, e, **f):
        pass

    def error(self, e, **f):
        pass


class _FakeCalendar:
    """固定 prev_open 的假日历（prefetch 只用 prev_open 反推信号日 T）。"""

    def __init__(self, prev: date):
        self._prev = prev

    def is_open(self, d):
        return True

    def next_open(self, d):
        return d

    def prev_open(self, d):
        return self._prev


class _FakeClient:
    def __init__(self, items=None, fail=False):
        self._items = items or []
        self._fail = fail
        self.gets = []

    def get_json(self, path, params=None):
        self.gets.append((path, dict(params or {})))
        if self._fail:
            raise SignalHttpError("boom", status=503)
        return {"items": self._items}

    def post_json(self, path, payload):  # prefetch 不应 POST
        raise AssertionError("prefetch 不应调用 post_json")


def test_item_mapping_full():
    item = {
        "ts_code": "300750.SZ", "trade_date": "2026-06-12", "target_trade_date": "2026-06-13",
        "tradable_flag": "TRADABLE", "role_tags": ["龙头", "中军"], "leader_strength_score": "88.5",
        "close": 45.2, "continuation_prob": "0.6", "next_day_premium_prob": 0.55,
        "strategy_family": "打板", "setup": "首板", "boost_conditions": ["竞价高开3-5%"],
        "fail_conditions": ["炸板"], "market_state": "高潮",
    }
    row = watchlist_item_to_selected(item, date(2026, 6, 13))
    assert row.ts_code == "300750.SZ"
    assert row.trade_date == date(2026, 6, 12)
    assert row.target_trade_date == date(2026, 6, 13)  # 对齐 today
    assert row.tradable_flag is True
    assert row.role == "龙头"  # role_tags 首个
    assert row.leader_strength_score == Decimal("88.5")
    assert row.signal_close == Decimal("45.2")  # close→signal_close，float 也无损
    assert row.continuation_prob == Decimal("0.6")
    assert row.next_day_premium_prob == Decimal("0.55")
    assert row.strategy_family == "打板"
    assert row.setup == "首板"
    assert row.boost == ["竞价高开3-5%"]
    assert row.fail_conditions == ["炸板"]
    assert row.market_state == "高潮"
    # 信号侧 watchlist 契约不给价位/因子，留空由 board_rules 兜底
    assert row.limit_up_price is None
    assert row.first_board_vol is None


def test_item_mapping_non_tradable_and_missing_fields():
    row = watchlist_item_to_selected(
        {"ts_code": "600000.SH", "trade_date": "2026-06-12", "tradable_flag": "CAUTION"},
        date(2026, 6, 13),
    )
    assert row.tradable_flag is False  # 非 TRADABLE 一律不可交易
    assert row.role is None
    assert row.leader_strength_score is None
    assert row.signal_close is None


def test_prefetch_pulls_by_signal_date_and_saves():
    saved = {}

    def save_fn(rows):
        saved["rows"] = rows
        return len(rows)

    client = _FakeClient(items=[
        {"ts_code": "300750.SZ", "trade_date": "2026-06-12", "target_trade_date": "2026-06-13",
         "tradable_flag": "TRADABLE", "close": "45.2"},
    ])
    pf = WatchlistPrefetcher(client, _FakeCalendar(prev=date(2026, 6, 12)), save_fn, _FakeLogger())
    n = pf.prefetch(date(2026, 6, 13))

    assert n == 1
    # GET 用 signal_T = prev_open(today) = 2026-06-12
    assert client.gets[0][0] == WATCHLIST_PATH
    assert client.gets[0][1] == {"date": "2026-06-12"}
    # 落库行 target 对齐 today
    assert saved["rows"][0].target_trade_date == date(2026, 6, 13)


def test_prefetch_http_failure_degrades_no_save():
    calls = {"n": 0}

    def save_fn(rows):
        calls["n"] += 1
        return len(rows)

    pf = WatchlistPrefetcher(
        _FakeClient(fail=True), _FakeCalendar(prev=date(2026, 6, 12)), save_fn, _FakeLogger()
    )
    assert pf.prefetch(date(2026, 6, 13)) == 0
    assert calls["n"] == 0  # 失败不落库 → loader 降级无名单


def test_prefetch_empty_items_no_save():
    calls = {"n": 0}

    def save_fn(rows):
        calls["n"] += 1
        return len(rows)

    pf = WatchlistPrefetcher(
        _FakeClient(items=[]), _FakeCalendar(prev=date(2026, 6, 12)), save_fn, _FakeLogger()
    )
    assert pf.prefetch(date(2026, 6, 13)) == 0
    assert calls["n"] == 0  # 空清单不删本机旧名单
