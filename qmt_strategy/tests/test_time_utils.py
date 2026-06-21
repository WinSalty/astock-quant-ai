"""time_utils 单测（§6.6 时间口径，杜绝 ±8h）。"""

from __future__ import annotations

from datetime import datetime, timezone

from qmt_strategy.common.time_utils import (
    FakeClock,
    SHANGHAI,
    east8_time_of,
    east8_trade_date,
    qmt_ts_to_db,
)


def test_qmt_ts_to_db_east8_minus_8h():
    # 构造东八区 2026-06-12 13:31:02 的时间戳
    east8 = datetime(2026, 6, 12, 13, 31, 2, tzinfo=SHANGHAI)
    ts = int(east8.timestamp())
    utc_naive, east8_naive = qmt_ts_to_db(ts)
    assert east8_naive == datetime(2026, 6, 12, 13, 31, 2)
    # UTC = 东八区 − 8h
    assert utc_naive == datetime(2026, 6, 12, 5, 31, 2)


def test_qmt_ts_to_db_none_zero_returns_pair_none():
    assert qmt_ts_to_db(None) == (None, None)
    assert qmt_ts_to_db(0) == (None, None)


def test_qmt_ts_to_db_normalizes_ms_and_us_units():
    """执行-19：毫秒(13位)/微秒(16位)时间戳按量级归一为秒，得到与秒级同一时刻，
    不再因被当秒解释而 OverflowError 静默落 (None, None)（整列时间无声变 NULL）。"""
    east8 = datetime(2026, 6, 12, 13, 31, 2, tzinfo=SHANGHAI)
    sec = int(east8.timestamp())
    expect = datetime(2026, 6, 12, 13, 31, 2)
    assert qmt_ts_to_db(sec)[1] == expect          # 秒
    assert qmt_ts_to_db(sec * 1_000)[1] == expect  # 毫秒
    assert qmt_ts_to_db(sec * 1_000_000)[1] == expect  # 微秒


def test_east8_time_and_date_from_utc():
    # UTC 01:16 == 东八区 09:16
    utc = datetime(2026, 6, 12, 1, 16, 0)
    assert east8_time_of(utc).hour == 9
    assert east8_time_of(utc).minute == 16
    assert east8_trade_date(utc) == datetime(2026, 6, 12).date()


def test_east8_trade_date_no_utc_drift():
    # 东八区 2026-06-12 00:30 → UTC 2026-06-11 16:30，但 trade_date 仍取东八区当日
    east8 = datetime(2026, 6, 12, 0, 30, tzinfo=SHANGHAI)
    utc_naive = east8.astimezone(timezone.utc).replace(tzinfo=None)
    assert east8_trade_date(utc_naive) == datetime(2026, 6, 12).date()


def test_fake_clock_advance():
    c = FakeClock(datetime(2026, 6, 12, 1, 0, 0))
    c.advance(90)
    assert c.now_utc() == datetime(2026, 6, 12, 1, 1, 30)
