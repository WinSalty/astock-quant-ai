"""identity / board_rules / universe_filter 单测（§2.3 / §6.3）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from qmt_strategy.common.board_rules import budget_prices, limit_up_price, round_to_cent
from qmt_strategy.common.identity import board_of, resolve_code
from qmt_strategy.common.universe_filter import is_allowed_prefix, is_st_name, is_tradable_universe
from qmt_strategy.contracts.enums import Board, PriceSource
from tests.conftest import make_selected_row


# —— identity 归一 ——
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("600036.SH", "600036.SH"),
        ("600036.sh", "600036.SH"),
        ("SH600036", "600036.SH"),
        ("sh.600036", "600036.SH"),
        ("600036", "600036.SH"),
        ("000001", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("688981", "688981.SH"),   # 科创：归一保留，由 universe 剔除
        ("830799", "830799.BJ"),   # 北交
        ("garbage", None),
        (None, None),
    ],
)
def test_resolve_code(raw, expected):
    assert resolve_code(raw) == expected


def test_board_of():
    assert board_of("600036.SH") == Board.MAIN
    assert board_of("000001.SZ") == Board.MAIN
    assert board_of("300750.SZ") == Board.CHINEXT
    assert board_of("301318") == Board.CHINEXT
    assert board_of("688981.SH") is None   # 科创非目标段
    assert board_of("830799.BJ") is None   # 北交非目标段


# —— board 价位预算 ——
def test_limit_up_price_main_and_chinext():
    assert limit_up_price(Decimal("10.00"), Board.MAIN) == Decimal("11.00")
    assert limit_up_price(Decimal("10.00"), Board.CHINEXT) == Decimal("12.00")


def test_round_to_cent():
    assert round_to_cent(Decimal("11.005")) == Decimal("11.01")
    assert round_to_cent(Decimal("11.004")) == Decimal("11.00")


def test_budget_prices_signal_first():
    row = make_selected_row(
        limit_up_price=Decimal("11.00"),
        reasonable_open_high_low=Decimal("10.20"),
        reasonable_open_high_high=Decimal("10.80"),
    )
    pb = budget_prices(row)
    assert pb.price_source == PriceSource.SIGNAL
    assert pb.limit_up_price == Decimal("11.00")


def test_budget_prices_local_calc_main():
    row = make_selected_row(signal_close=Decimal("10.00"))  # 无信号涨停价/区间
    pb = budget_prices(row)
    assert pb.price_source == PriceSource.LOCAL_CALC
    assert pb.limit_up_price == Decimal("11.00")
    assert pb.board == Board.MAIN


def test_budget_prices_local_calc_chinext():
    row = make_selected_row(ts_code="300750.SZ", signal_close=Decimal("10.00"))
    pb = budget_prices(row)
    assert pb.price_source == PriceSource.LOCAL_CALC
    assert pb.limit_up_price == Decimal("12.00")
    assert pb.board == Board.CHINEXT


def test_budget_prices_missing():
    row = make_selected_row(signal_close=None)
    pb = budget_prices(row)
    assert pb.price_source == PriceSource.MISSING


def test_budget_prices_non_target_board_degrades_missing():
    """评审 P0-F1：科创 688/北交等非主板创业板段、且信号侧未给涨停价
    → 降级 MISSING（不再按主板 10% 兜底现算出错误涨停价）。"""
    row = make_selected_row(ts_code="688981.SH", signal_close=Decimal("10.00"))
    pb = budget_prices(row)
    assert pb.price_source == PriceSource.MISSING
    assert pb.limit_up_price == Decimal("0")     # 此前会错误算成 11.00(=10×1.1)


def test_budget_prices_non_target_board_uses_signal_limit_when_given():
    """非主板创业板段但信号侧已给齐价位 → 仍采用信号侧权威价位（科创/北交/ST 由信号侧算对）。"""
    row = make_selected_row(
        ts_code="688981.SH", signal_close=Decimal("10.00"),
        limit_up_price=Decimal("12.00"),                 # 信号侧按科创 20% 给
        reasonable_open_high_low=Decimal("10.40"),
        reasonable_open_high_high=Decimal("11.60"),
    )
    pb = budget_prices(row)
    assert pb.price_source == PriceSource.SIGNAL
    assert pb.limit_up_price == Decimal("12.00")


# —— universe 过滤 ——
@pytest.mark.parametrize(
    "code,allowed",
    [
        ("600036.SH", True),
        ("000001.SZ", True),
        ("300750.SZ", True),
        ("301318.SZ", True),
        ("688981.SH", False),   # 科创
        ("830799.BJ", False),   # 北交
        ("920819.BJ", False),   # 北交 920
    ],
)
def test_is_allowed_prefix(code, allowed):
    assert is_allowed_prefix(code) is allowed


def test_is_st_name():
    assert is_st_name("ST康美") is True
    assert is_st_name("*ST海航") is True
    assert is_st_name("退市辅仁") is True
    assert is_st_name("贵州茅台") is False
    assert is_st_name(None) is False


def test_is_tradable_universe():
    assert is_tradable_universe("600036.SH", "招商银行") is True
    assert is_tradable_universe("600036.SH", "ST招行") is False
    assert is_tradable_universe("688981.SH", "中芯国际") is False
    assert is_tradable_universe("600036.SH", "招商银行", is_halted=True) is False
    assert is_tradable_universe("600036.SH", "招商银行", is_delisted=True) is False


# ===========================================================================
# 评审三轮 F1：锚点化后缀，矛盾脏串返 None（不判到错误交易所）
# ===========================================================================
@pytest.mark.parametrize("dirty", ["SH000001", "000001.SH", "SZ600036", "600036.BJ"])
def test_resolve_code_contradictory_dirty_returns_none(dirty):
    # 显式后缀与数字前缀矛盾 → 脏数据返 None（绝不下错单/join 错配）。
    assert resolve_code(dirty) is None


def test_resolve_code_embedded_literal_not_hijacked():
    # 名称/板块标记里嵌入 SH/SZ 字面量但与前缀一致 → 仍按前缀正确归一（不被任意位置字面量劫持）。
    assert resolve_code("AI算力SZ300750") == "300750.SZ"
    assert resolve_code("人工智能300750") == "300750.SZ"


# ===========================================================================
# 评审三轮 F3 + 禁买 ST 硬规则：board_rules 统一走 is_st_stock（显式 is_st=True 或 name 含 ST 即 ST）
# ===========================================================================
def test_budget_prices_prefers_explicit_is_st():
    from qmt_strategy.common.board_rules import budget_prices
    # 主板票、name 不含 ST，但显式 is_st=True → 按 5% 现算（10.00→10.50），不被 name 误判为非 ST。
    row = make_selected_row(ts_code="600036.SH", signal_close=Decimal("10.00"))
    row.name = "招商银行"
    row.is_st = True
    assert budget_prices(row).limit_up_price == Decimal("10.50")  # ST 5%
    # 禁买 ST 硬规则口径更新：name 含 ST/退即视为 ST，即便显式 is_st=False（视为信号侧漂移/滞后）也按 5%——
    # name 是 point-in-time 实时事实，宁可对疑似 ST 偏保守（5%），绝不放真 ST 进买入路径。
    row2 = make_selected_row(ts_code="600036.SH", signal_close=Decimal("10.00"))
    row2.name = "ST华微"
    row2.is_st = False
    assert budget_prices(row2).limit_up_price == Decimal("10.50")  # name 含 ST → ST 5%
    # 非 ST 名称 + is_st 缺失/False → 主板 10%（不把缺失/非 ST 当 ST，避免对非 ST 票按 5% 漏买）。
    row3 = make_selected_row(ts_code="600036.SH", signal_close=Decimal("10.00"))
    row3.name = "招商银行"
    row3.is_st = False
    assert budget_prices(row3).limit_up_price == Decimal("11.00")  # 非 ST 10%
