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


# ---------------------------------------------------------------------------
# 国金对接核对 F06/F07：account_id / mini_path 启动期 fail-closed（缺配拒启动）
# ---------------------------------------------------------------------------
def test_build_real_engine_fail_closed_without_account_id():
    """缺 QMT_ACCOUNT_ID → build_real_engine 启动期拒启（防假账户号污染台账/回流）。"""
    import pytest
    from qmt_strategy.app.run import build_real_engine
    from qmt_strategy.common.logger import RecordingLogger

    s = Settings.from_env({"QMT_MINI_PATH": "D:/qmt/userdata_mini"})  # 有 mini_path、无 account_id
    with pytest.raises(RuntimeError, match="QMT_ACCOUNT_ID"):
        build_real_engine(s, RecordingLogger())


def test_build_real_engine_fail_closed_without_mini_path():
    """缺 QMT_MINI_PATH → build_real_engine 启动期拒启（防空 userdata 路径建 trader）。"""
    import pytest
    from qmt_strategy.app.run import build_real_engine
    from qmt_strategy.common.logger import RecordingLogger

    s = Settings.from_env({"QMT_ACCOUNT_ID": "acc1"})  # 有 account_id、无 mini_path
    with pytest.raises(RuntimeError, match="QMT_MINI_PATH"):
        build_real_engine(s, RecordingLogger())


# ---------------------------------------------------------------------------
# §7.1.6 fail-closed：竞价择时未实测放行不得开启（assert_safe_to_trade）
# ---------------------------------------------------------------------------
def test_assert_safe_to_trade_blocks_unverified_auction_timing():
    import pytest

    s = Settings.from_env({"QMT_AUCTION_TIMING_ENABLED": "true"})  # 开了但未标记 verified
    assert s.auction_timing_verified is False
    with pytest.raises(RuntimeError, match="QMT_AUCTION_TIMING_VERIFIED"):
        s.assert_safe_to_trade()


# H-3：风控两闸（账户回撤 / 限价偏离）须显式配置，否则 assert_safe_to_trade 拒启动；
# 故下列「放行」用例统一带上这两项的安全配置（_RISK_GATES）。
_RISK_GATES = {"QMT_ACCOUNT_DRAWDOWN_LIMIT": "0.05", "QMT_PRICE_DEVIATION_GUARD_PCT": "0.10"}


def test_assert_safe_to_trade_allows_verified_or_disabled():
    # 已实测放行：开 + verified（+ 两道风控闸已配）→ 放行（不抛）
    s1 = Settings.from_env(
        {"QMT_AUCTION_TIMING_ENABLED": "true", "QMT_AUCTION_TIMING_VERIFIED": "true", **_RISK_GATES}
    )
    s1.assert_safe_to_trade()
    # 竞价择时默认关 + 两道风控闸已配：不抛
    s2 = Settings.from_env(dict(_RISK_GATES))
    s2.assert_safe_to_trade()


# ---------------------------------------------------------------------------
# 评审 doc/19 H-3：账户回撤闸 / 限价偏离闸装配期 fail-closed 强校验
# ---------------------------------------------------------------------------
def test_assert_safe_to_trade_blocks_missing_account_drawdown_limit():
    import pytest

    # 只配偏离闸、漏配回撤闸 → 拒启动（fail-closed）。
    s = Settings.from_env({"QMT_PRICE_DEVIATION_GUARD_PCT": "0.10"})
    assert s.account_drawdown_limit is None
    with pytest.raises(RuntimeError, match="QMT_ACCOUNT_DRAWDOWN_LIMIT"):
        s.assert_safe_to_trade()


def test_assert_safe_to_trade_blocks_missing_price_deviation_guard():
    import pytest

    # 只配回撤闸、漏配偏离闸 → 拒启动（fail-closed）。
    s = Settings.from_env({"QMT_ACCOUNT_DRAWDOWN_LIMIT": "0.05"})
    assert s.price_deviation_guard_pct is None
    with pytest.raises(RuntimeError, match="QMT_PRICE_DEVIATION_GUARD_PCT"):
        s.assert_safe_to_trade()


def test_assert_safe_to_trade_allows_when_both_risk_gates_configured():
    # 两道风控闸均显式配置（含「配大值实际关停」的合法用法）→ 放行（不抛）。
    s = Settings.from_env(_RISK_GATES)
    s.assert_safe_to_trade()
    s_disabled = Settings.from_env(
        {"QMT_ACCOUNT_DRAWDOWN_LIMIT": "0.99", "QMT_PRICE_DEVIATION_GUARD_PCT": "0.99"}
    )
    s_disabled.assert_safe_to_trade()  # 显式配大值=有痕关停，仍属「已显式决策」，不拒启


# ---------------------------------------------------------------------------
# 国金对接核对 B1/H1：盘中卖出链生产门控 + 开启时强制配单票浮亏止损
# ---------------------------------------------------------------------------
def test_sell_pass_live_defaults_off():
    s = Settings.from_env({})
    assert s.sell_pass_live is False                 # 默认关：生产不自动卖出
    s2 = Settings.from_env({"QMT_SELL_PASS_LIVE": "true"})
    assert s2.sell_pass_live is True


def test_assert_safe_to_trade_blocks_sell_pass_live_without_stop_loss():
    import pytest

    s = Settings.from_env({"QMT_SELL_PASS_LIVE": "true"})   # 开了卖出链但没配浮亏止损
    assert s.stock_float_loss_limit is None
    with pytest.raises(RuntimeError, match="QMT_STOCK_FLOAT_LOSS_LIMIT"):
        s.assert_safe_to_trade()


def test_assert_safe_to_trade_allows_sell_pass_live_with_stop_loss():
    # 开卖出链 + 配了单票浮亏止损（+ 两道风控闸已配）→ 放行（不抛）。
    s = Settings.from_env(
        {"QMT_SELL_PASS_LIVE": "true", "QMT_STOCK_FLOAT_LOSS_LIMIT": "0.05", **_RISK_GATES}
    )
    s.assert_safe_to_trade()


def test_per_interface_token_fallback_and_override():
    """评审三轮 XCUT-01：watchlist/ingest 分接口 token 缺省回落统一 signal token，配置后各取各自。"""
    from qmt_strategy.config.settings import Settings

    s = Settings(signal_internal_token="unified")
    assert s.resolve_watchlist_token() == "unified"   # 回落统一
    assert s.resolve_ingest_token() == "unified"
    s2 = Settings(
        signal_internal_token="unified",
        signal_watchlist_token="wl",
        signal_ingest_token="ing",
    )
    assert s2.resolve_watchlist_token() == "wl"        # 各取各自，运维隔离权限不互相 401
    assert s2.resolve_ingest_token() == "ing"
    assert s2.resolve_signal_token() == "unified"
