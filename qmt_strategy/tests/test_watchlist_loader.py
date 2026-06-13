"""watchlist_loader / sources 单测（§2.3–2.6 / §2.7 单测要点）。

全部用 fake/内存取数源 + RecordingLogger，不连真实 xttrader / MySQL / HTTP。
覆盖：两路取数同契约、A 抛异常切 B / 进降级、universe 兜底、tradable_flag 拆名单、
board 价位预算（SIGNAL/LOCAL_CALC/MISSING）、空仓闸门、取契约失败兜底、非交易日、归一与 today。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import PriceSource
from qmt_strategy.contracts.errors import WatchlistLoadError
from qmt_strategy.contracts.models import SelectedStockRow
from qmt_strategy.watchlist.sources import (
    CallableSelectedStockSource,
    DbSelectedStockSource,
    HttpSelectedStockSource,
)
from qmt_strategy.watchlist.watchlist_loader import WatchlistLoader
from tests.conftest import T_BUY, T_SIGNAL, make_selected_row

# 非交易日（2026-06-13 周六，conftest 的工作日日历不含周末）。
NON_TRADING_DAY = date(2026, 6, 13)


# ---------------------------------------------------------------------------
# 取数源 fake：分别模拟「DB 直读返回固定行集」与「HTTP 端点返回同一行集」
# ---------------------------------------------------------------------------
def _make_db_source(rows):
    """路径 A fake：注入返回固定 SelectedStockRow 行集的查询闭包。"""
    return DbSelectedStockSource(lambda d: list(rows))


def _make_http_source(rows):
    """路径 B fake：注入返回同一行集的「端点」闭包（模拟 JSON 已反序列化为契约）。"""
    return HttpSelectedStockSource(lambda d: list(rows))


def _raising_source(exc=None):
    """注入恒抛异常的取数源，模拟取数失败（DB 断连 / 接口超时等）。"""
    def _boom(d):
        raise (exc or RuntimeError("backend down"))

    return CallableSelectedStockSource(_boom, source_name="boom")


def _make_loader(primary, *, fallback=None, calendar):
    return WatchlistLoader(
        primary=primary,
        calendar=calendar,
        logger=RecordingLogger(),
        settings=Settings(),
        fallback=fallback,
    )


# ---------------------------------------------------------------------------
# 1) 两路取数：A 与 B 同行集 → 同一 WatchlistContext（tradable 键集相同）
# ---------------------------------------------------------------------------
def test_path_a_and_b_produce_same_context(calendar):
    """路径 A（DB）与路径 B（HTTP）返回同一行集 → 产出等价上下文（tradable 键集相同）。"""
    rows = [
        make_selected_row(ts_code="600000.SH"),
        make_selected_row(ts_code="000001.SZ"),
        make_selected_row(ts_code="300750.SZ"),
    ]
    ctx_a = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    ctx_b = _make_loader(_make_http_source(rows), calendar=calendar).load(T_BUY)

    assert set(ctx_a.tradable.keys()) == set(ctx_b.tradable.keys())
    assert set(ctx_a.tradable.keys()) == {"600000.SH", "000001.SZ", "300750.SZ"}
    assert ctx_a.is_open is True and ctx_b.is_open is True
    assert ctx_a.open_new_position_allowed == ctx_b.open_new_position_allowed is True
    assert ctx_a.degraded is False and ctx_b.degraded is False


def test_path_a_fails_switch_to_b(calendar):
    """路径 A 抛异常但配了 B → 切 B 取数成功，正常产出（不降级）。"""
    rows = [make_selected_row(ts_code="600000.SH")]
    loader = _make_loader(
        _raising_source(), fallback=_make_http_source(rows), calendar=calendar
    )
    ctx = loader.load(T_BUY)
    assert ctx.degraded is False
    assert set(ctx.tradable.keys()) == {"600000.SH"}
    assert ctx.open_new_position_allowed is True


def test_path_a_fails_no_fallback_degrades(calendar):
    """路径 A 抛异常且无备路 → 进降级态、不抛异常。"""
    loader = _make_loader(_raising_source(), calendar=calendar)
    ctx = loader.load(T_BUY)
    assert ctx.degraded is True
    assert ctx.tradable == {}
    assert ctx.open_new_position_allowed is False


# ---------------------------------------------------------------------------
# 2) universe 兜底：科创/北交/非白名单前缀 → 剔出 tradable、进 watch_only
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ts_code",
    [
        "688981.SH",   # 科创板
        "830799.BJ",   # 北交 8xx
        "920819.BJ",   # 北交 920
        "200018.SZ",   # B 股 2xx（非白名单前缀）
    ],
)
def test_universe_filter_rejects_non_whitelist(calendar, ts_code):
    """非白名单前缀票被剔出可交易名单、转入观察名单。"""
    rows = [make_selected_row(ts_code=ts_code)]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    assert ctx.tradable == {}
    assert len(ctx.watch_only) == 1


def test_universe_filter_mixed(calendar):
    """白名单 + 非白名单混合 → 仅白名单进 tradable，其余进 watch_only。"""
    rows = [
        make_selected_row(ts_code="600000.SH"),   # 主板，放行
        make_selected_row(ts_code="688981.SH"),   # 科创，剔除
        make_selected_row(ts_code="300750.SZ"),   # 创业板，放行
    ]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    assert set(ctx.tradable.keys()) == {"600000.SH", "300750.SZ"}
    assert [e.norm_code for e in ctx.watch_only] == ["688981.SH"]


# ---------------------------------------------------------------------------
# 3) tradable_flag 拆名单：真/假混合 → 数量与归属正确
# ---------------------------------------------------------------------------
def test_split_by_tradable_flag(calendar):
    """tradable_flag 真进可交易名单、假（含一字/秒封先验买不进）进观察名单。"""
    rows = [
        make_selected_row(ts_code="600000.SH", tradable_flag=True),
        make_selected_row(ts_code="000001.SZ", tradable_flag=False),  # 一字先验买不进
        make_selected_row(ts_code="002594.SZ", tradable_flag=True),
        make_selected_row(ts_code="601318.SH", tradable_flag=False),
    ]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    assert set(ctx.tradable.keys()) == {"600000.SH", "002594.SZ"}
    assert {e.norm_code for e in ctx.watch_only} == {"000001.SZ", "601318.SH"}


def test_tradable_flag_none_goes_watch_only(calendar):
    """tradable_flag 为 None（未给可成交性）→ 保守转观察名单。"""
    rows = [make_selected_row(ts_code="600000.SH", tradable_flag=None)]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    assert ctx.tradable == {}
    assert len(ctx.watch_only) == 1


# ---------------------------------------------------------------------------
# 4) board 价位预算：LOCAL_CALC / SIGNAL / MISSING
# ---------------------------------------------------------------------------
def test_budget_local_calc_main(calendar):
    """主板 signal_close=10.00、信号侧未给价位 → 涨停 11.00、LOCAL_CALC。"""
    rows = [make_selected_row(ts_code="600000.SH", signal_close=Decimal("10.00"))]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    pb = ctx.tradable["600000.SH"].price
    assert pb.limit_up_price == Decimal("11.00")
    assert pb.price_source == PriceSource.LOCAL_CALC


def test_budget_signal_source(calendar):
    """信号侧给 limit_up_price + 合理高开区间 → 直接采用、SIGNAL。"""
    rows = [
        make_selected_row(
            ts_code="600000.SH",
            limit_up_price=Decimal("11.00"),
            reasonable_open_high_low=Decimal("10.20"),
            reasonable_open_high_high=Decimal("10.80"),
        )
    ]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    pb = ctx.tradable["600000.SH"].price
    assert pb.price_source == PriceSource.SIGNAL
    assert pb.limit_up_price == Decimal("11.00")
    assert pb.reasonable_open_low == Decimal("10.20")
    assert pb.reasonable_open_high == Decimal("10.80")


def test_budget_missing_goes_watch_only(calendar):
    """signal_close 与涨停价/区间全缺 → MISSING、该票单票降级转观察名单（不影响整批）。"""
    rows = [
        make_selected_row(ts_code="600000.SH", signal_close=None),
        make_selected_row(ts_code="000001.SZ", signal_close=Decimal("10.00")),
    ]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    # 价位缺失票被剔出 tradable，进 watch_only 且标 MISSING
    assert "600000.SH" not in ctx.tradable
    assert set(ctx.tradable.keys()) == {"000001.SZ"}
    missing_entry = next(e for e in ctx.watch_only if e.norm_code == "600000.SH")
    assert missing_entry.price.price_source == PriceSource.MISSING


# ---------------------------------------------------------------------------
# 5) 空仓闸门：market_state 空仓/启动/None → open_new_position_allowed False/True/False
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "market_state,expected_gate",
    [
        ("空仓", False),
        ("启动", True),
        (None, False),
    ],
)
def test_open_gate_by_market_state(calendar, market_state, expected_gate):
    """空仓/None 禁开新仓、非空仓允许；任一情况仍产出上下文（守仓不受影响）。"""
    rows = [make_selected_row(ts_code="600000.SH", market_state=market_state)]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    assert ctx.open_new_position_allowed is expected_gate
    # 空仓态仍正常装载名单（守仓 / 卖出不受空仓闸门影响）：tradable 不被清空。
    assert set(ctx.tradable.keys()) == {"600000.SH"}
    assert ctx.is_open is True
    assert ctx.degraded is False


def test_empty_position_still_produces_context(calendar):
    """空仓态：开新仓被拒，但可交易名单照常装入内存供守仓 / 复盘对照。"""
    rows = [
        make_selected_row(ts_code="600000.SH", market_state="空仓"),
        make_selected_row(ts_code="000001.SZ", market_state="空仓"),
    ]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    assert ctx.open_new_position_allowed is False
    assert set(ctx.tradable.keys()) == {"600000.SH", "000001.SZ"}


# ---------------------------------------------------------------------------
# 6) 取契约失败兜底：主备双失败 → degraded、禁开新仓、tradable={}、告警、不抛异常
# ---------------------------------------------------------------------------
def test_primary_and_fallback_both_fail_degrades(calendar):
    """主路 + 备路均抛异常 → 降级态，产生告警，且不抛出致命异常。"""
    logger = RecordingLogger()
    loader = WatchlistLoader(
        primary=_raising_source(WatchlistLoadError("db timeout")),
        calendar=calendar,
        logger=logger,
        settings=Settings(),
        fallback=_raising_source(RuntimeError("http 503")),
    )
    ctx = loader.load(T_BUY)  # 不应抛异常
    assert ctx.degraded is True
    assert ctx.degraded_reason is not None
    assert ctx.open_new_position_allowed is False
    assert ctx.tradable == {}
    # 产生降级告警
    assert "watchlist_degraded" in logger.events()


def test_source_returns_none_treated_as_failure(calendar):
    """取数闭包返回 None（非空列表）→ 视为取数失败 → 降级（不当作空名单）。"""
    src = CallableSelectedStockSource(lambda d: None, source_name="db")
    ctx = _make_loader(src, calendar=calendar).load(T_BUY)
    assert ctx.degraded is True
    assert ctx.open_new_position_allowed is False


def test_empty_rows_is_valid_not_degraded(calendar):
    """取数返回空列表是合法结果（当日无候选）→ 不降级、tradable 空、开仓闸门仍按非空仓。"""
    src = CallableSelectedStockSource(lambda d: [], source_name="db")
    ctx = _make_loader(src, calendar=calendar).load(T_BUY)
    assert ctx.degraded is False
    assert ctx.tradable == {}
    assert ctx.watch_only == []
    # 空名单时 market_state 取不到 → 保守按空仓，禁开新仓。
    assert ctx.open_new_position_allowed is False


# ---------------------------------------------------------------------------
# 7) 非交易日：is_open=False、不开新仓、不取数
# ---------------------------------------------------------------------------
def test_non_trading_day_skips(calendar):
    """非交易日 → 空载、is_open=False、不开新仓、不调用取数源。"""
    called = {"n": 0}

    def _counting_fetch(d):
        called["n"] += 1
        return [make_selected_row()]

    src = CallableSelectedStockSource(_counting_fetch)
    ctx = _make_loader(src, calendar=calendar).load(NON_TRADING_DAY)
    assert ctx.is_open is False
    assert ctx.open_new_position_allowed is False
    assert ctx.tradable == {}
    assert ctx.watch_only == []
    # 非交易日不应触发取数（防御性早返回）。
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# 8) 归一与 today：norm_code 归一为 600000.SH 形态、target_trade_date=today
# ---------------------------------------------------------------------------
def test_norm_code_and_target_trade_date(calendar):
    """脏代码归一为标准 ts_code；entry 的 target_trade_date=today、signal_trade_date=T。"""
    rows = [
        make_selected_row(ts_code="SH600000"),     # 前缀形态
        make_selected_row(ts_code="000001"),       # 裸 6 位
    ]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    assert set(ctx.tradable.keys()) == {"600000.SH", "000001.SZ"}
    entry = ctx.tradable["600000.SH"]
    assert entry.norm_code == "600000.SH"
    assert entry.target_trade_date == T_BUY        # = today
    assert entry.signal_trade_date == T_SIGNAL     # = T，透传


def test_unresolved_code_goes_watch_only(calendar):
    """无法归一的脏代码（取不到交易所）→ 转观察名单，不污染 tradable。"""
    rows = [
        make_selected_row(ts_code="garbage"),
        make_selected_row(ts_code="600000.SH"),
    ]
    logger = RecordingLogger()
    loader = WatchlistLoader(
        primary=_make_db_source(rows),
        calendar=calendar,
        logger=logger,
        settings=Settings(),
    )
    ctx = loader.load(T_BUY)
    assert set(ctx.tradable.keys()) == {"600000.SH"}
    assert len(ctx.watch_only) == 1
    assert "watchlist_drop_unresolved_code" in logger.events()


def test_transmitted_fields_passthrough(calendar):
    """透传字段（market_state/role/strategy/先验/路由维度）原样进 entry，供回流期 join。"""
    rows = [
        make_selected_row(
            ts_code="600000.SH",
            role="龙头",
            strategy="打板",
            strategy_family="连板",
            setup="一进二",
            first_board_vol=1000000,
            float_mktcap=Decimal("5000000000"),
            continuation_prob=Decimal("0.62"),
        )
    ]
    ctx = _make_loader(_make_db_source(rows), calendar=calendar).load(T_BUY)
    e = ctx.tradable["600000.SH"]
    assert e.role == "龙头"
    assert e.strategy == "打板"
    assert e.strategy_family == "连板"
    assert e.setup == "一进二"
    assert e.first_board_vol == 1000000
    assert e.float_mktcap == Decimal("5000000000")
    assert e.continuation_prob == Decimal("0.62")
    assert e.market_state == "启动"
