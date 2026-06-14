"""配置层单测（§7.1）：env 解析、默认值、敏感脱敏、竞价择时默认关。"""

from __future__ import annotations

from decimal import Decimal

from qmt_strategy.config.settings import Settings


def test_defaults_auction_timing_off_and_kill_off():
    s = Settings.from_env({})
    assert s.auction_timing_enabled is False  # §7.1.6 实测前必须关
    assert s.kill_switch is False
    assert s.watchlist_source == "DB"
    assert s.auction_poll_interval_sec == 3.0
    # 评审 2.5：禁开仓集合改为三档真实 market_state（加入「谨慎参与」）+ 退潮/冰点 防御性冗余。
    assert s.market_state_block == ["空仓", "谨慎参与", "退潮", "冰点"]
    assert "谨慎参与" in s.market_state_block      # 核心修复：谨慎参与=禁开仓
    assert s.repository_unique_with_trade_date is True


def test_env_parsing():
    env = {
        "QMT_ACCOUNT_ID": "1000000365",
        "QMT_AUCTION_TIMING_ENABLED": "true",
        "QMT_KILL_SWITCH": "1",
        "QMT_AUCTION_POLL_INTERVAL_SEC": "1.5",
        "QMT_MAX_POSITION_PER_STOCK": "20000",
        "QMT_MARKET_STATE_BLOCK": "退潮,空仓",
        "QMT_STRATEGY_FIRST_BOARD_ENABLED": "true",
        "QMT_STRATEGY_HIGH_LEADER_ENABLED": "false",
    }
    s = Settings.from_env(env)
    assert s.account_id == "1000000365"
    assert s.auction_timing_enabled is True
    assert s.kill_switch is True
    assert s.auction_poll_interval_sec == 1.5
    assert s.max_position_per_stock == Decimal("20000")
    assert s.market_state_block == ["退潮", "空仓"]
    assert s.strategy_enabled == {"first_board": True, "high_leader": False}


def test_redacted_hides_sensitive():
    s = Settings.from_env({"QMT_ACCOUNT_ID": "1000000365", "QMT_MYSQL_DSN": "mysql://u:p@h/db"})
    red = s.redacted()
    assert red["account_id"] == "***REDACTED***"
    assert red["mysql_dsn"] == "***REDACTED***"


# ---------------------------------------------------------------------------
# 评审 P0-E1/3.1：交易日历 fail-closed（生产禁止静默用周末近似日历）
# ---------------------------------------------------------------------------
def test_build_calendar_fail_closed_without_file():
    import pytest
    from qmt_strategy.app.run import _build_calendar
    from qmt_strategy.common.logger import RecordingLogger

    s = Settings.from_env({})  # 无日历文件、allow_weekday=False
    with pytest.raises(RuntimeError):
        _build_calendar(s, RecordingLogger())


def test_build_calendar_allows_weekday_when_opted_in():
    from qmt_strategy.app.run import _build_calendar
    from qmt_strategy.common.logger import RecordingLogger
    from qmt_strategy.common.trade_calendar import WeekdayTradeCalendar

    s = Settings.from_env({"QMT_ALLOW_WEEKDAY_CALENDAR": "true"})
    assert isinstance(_build_calendar(s, RecordingLogger()), WeekdayTradeCalendar)


def test_build_calendar_static_from_file(tmp_path):
    from datetime import date
    from qmt_strategy.app.run import _build_calendar
    from qmt_strategy.common.logger import RecordingLogger
    from qmt_strategy.common.trade_calendar import StaticTradeCalendar

    f = tmp_path / "cal.txt"
    f.write_text("# trading days\n2026-06-12\n2026-06-15\n", encoding="utf-8")
    s = Settings.from_env({"QMT_TRADE_CALENDAR_FILE": str(f)})
    cal = _build_calendar(s, RecordingLogger())
    assert isinstance(cal, StaticTradeCalendar)
    assert cal.is_open(date(2026, 6, 12)) and not cal.is_open(date(2026, 6, 13))
