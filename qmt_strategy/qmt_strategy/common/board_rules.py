"""按 board 预算今日价位（§2.3 第 5 步 / §2.4 _budget_prices）。

业务意图：执行侧在信号侧未给 limit_up_price / 合理高开区间时，用 signal_close + board 涨跌幅
规则兜底现算今日理论涨停价（主板 ±10%、创业板 ±20%，不复权），标 price_source=LOCAL_CALC，
便于复盘区分。A 股涨跌停价按「四舍五入到 0.01」规则取整。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Tuple

from ..contracts.enums import Board, PriceSource
from ..contracts.models import PriceBudget, SelectedStockRow
from .identity import board_of

_CENT = Decimal("0.01")

# 各板涨跌幅比例（创业板 / 科创为 20%，主板 10%；本项目只主板 + 创业板）。
_BOARD_LIMIT_RATIO = {
    Board.MAIN: Decimal("0.10"),
    Board.CHINEXT: Decimal("0.20"),
}


def round_to_cent(v: Decimal) -> Decimal:
    """四舍五入到 0.01（A 股价位精度，§2.3 第 5 步）。"""
    return v.quantize(_CENT, rounding=ROUND_HALF_UP)


def limit_up_price(signal_close: Decimal, board: Board) -> Decimal:
    """按 board 现算理论涨停价 = 收盘价 ×(1+涨幅比例)，四舍五入到 0.01。"""
    ratio = _BOARD_LIMIT_RATIO[board]
    return round_to_cent(signal_close * (Decimal("1") + ratio))


def limit_down_price(signal_close: Decimal, board: Board) -> Decimal:
    """按 board 现算理论跌停价（备用，对称口径）。"""
    ratio = _BOARD_LIMIT_RATIO[board]
    return round_to_cent(signal_close * (Decimal("1") - ratio))


def budget_prices(row: SelectedStockRow) -> PriceBudget:
    """预算单票今日价位（§2.4 _budget_prices）。

    优先采用信号侧 limit_up_price / reasonable_open_high_low/high；缺失则以 signal_close + board
    规则兜底现算，标 price_source=LOCAL_CALC。
    边界：signal_close 也缺失且涨停价/高开区间均缺 → price_source=MISSING（上游据此把该票转观察名单，§2.6）。
    """
    board = board_of(row.ts_code)
    # board 判不出（非目标段）时，沿用主板比例兜底以免崩溃；该票本应已被 universe_filter 剔除。
    eff_board = board if board is not None else Board.MAIN

    has_signal_limit = row.limit_up_price is not None
    has_signal_range = (
        row.reasonable_open_high_low is not None and row.reasonable_open_high_high is not None
    )

    if has_signal_limit and has_signal_range:
        # 信号侧价位齐全，直接采用（最高优先级）。
        return PriceBudget(
            limit_up_price=row.limit_up_price,
            reasonable_open_low=row.reasonable_open_high_low,
            reasonable_open_high=row.reasonable_open_high_high,
            board=eff_board,
            price_source=PriceSource.SIGNAL,
        )

    if row.signal_close is not None:
        # 用收盘价 + board 规则兜底现算涨停价；合理高开区间优先用信号侧给的，缺则用一段保守默认。
        lup = row.limit_up_price if has_signal_limit else limit_up_price(row.signal_close, eff_board)
        low, high = _fallback_open_range(row, eff_board)
        return PriceBudget(
            limit_up_price=lup,
            reasonable_open_low=low,
            reasonable_open_high=high,
            board=eff_board,
            price_source=PriceSource.LOCAL_CALC,
        )

    # 全缺：无法预算价位（§2.6 单票降级转观察名单，MISSING）。
    return PriceBudget(
        limit_up_price=Decimal("0"),
        reasonable_open_low=Decimal("0"),
        reasonable_open_high=Decimal("0"),
        board=eff_board,
        price_source=PriceSource.MISSING,
    )


def _fallback_open_range(row: SelectedStockRow, board: Board) -> Tuple[Decimal, Decimal]:
    """合理高开区间兜底：信号侧给则用其值；否则以 signal_close 的 [+2%, +涨停] 作保守默认区间。

    说明：执行侧不臆造精细高开阈值，这里只给「不为空」的保守占位，真实阈值由配置/信号侧主导；
    标 LOCAL_CALC 后复盘可识别该区间为兜底现算。
    """
    if row.reasonable_open_high_low is not None and row.reasonable_open_high_high is not None:
        return row.reasonable_open_high_low, row.reasonable_open_high_high
    close = row.signal_close
    low = round_to_cent(close * Decimal("1.02"))
    high = limit_up_price(close, board)
    return low, high
