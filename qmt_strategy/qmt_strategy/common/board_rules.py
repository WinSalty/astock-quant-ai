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
from .universe_filter import is_st_stock

_CENT = Decimal("0.01")

# 各板涨跌幅比例（创业板 / 科创为 20%，主板 10%；本项目只主板 + 创业板）。
_BOARD_LIMIT_RATIO = {
    Board.MAIN: Decimal("0.10"),
    Board.CHINEXT: Decimal("0.20"),
}
# 主板 ST / 退市整理股涨跌幅 5%（评审二轮 P1#18）。注意：创业板/科创的 ST 仍按 20%（其无 5% 制度），
# 故 ST 5% 只对主板生效；非主板 ST 沿用板块比例。
_ST_MAIN_RATIO = Decimal("0.05")


def round_to_cent(v: Decimal) -> Decimal:
    """四舍五入到 0.01（A 股价位精度，§2.3 第 5 步）。"""
    return v.quantize(_CENT, rounding=ROUND_HALF_UP)


def _limit_ratio(board: Board, is_st: bool) -> Decimal:
    """涨跌幅比例：主板 ST/退市整理 → 5%（评审二轮 P1#18），否则按板块（主板 10% / 创业板 20%）。"""
    if is_st and board == Board.MAIN:
        return _ST_MAIN_RATIO
    return _BOARD_LIMIT_RATIO[board]


def limit_up_price(signal_close: Decimal, board: Board, is_st: bool = False) -> Decimal:
    """按 board(+ST) 现算理论涨停价 = 收盘价 ×(1+涨幅比例)，四舍五入到 0.01。

    评审二轮 P1#18：主板 ST/退市整理股涨停 5%，不再恒按 10% 现算（否则挂出超 ST 法定涨停价的废单 /
    对真实 5% 顶板静默漏买）。is_st 由调用方据证券名称判定（universe_filter.is_st_name）。
    """
    return round_to_cent(signal_close * (Decimal("1") + _limit_ratio(board, is_st)))


def limit_down_price(signal_close: Decimal, board: Board, is_st: bool = False) -> Decimal:
    """按 board(+ST) 现算理论跌停价（备用，对称口径）。"""
    return round_to_cent(signal_close * (Decimal("1") - _limit_ratio(board, is_st)))


def budget_prices(row: SelectedStockRow) -> PriceBudget:
    """预算单票今日价位（§2.4 _budget_prices）。

    优先采用信号侧 limit_up_price / reasonable_open_high_low/high；缺失则以 signal_close + board
    规则兜底现算，标 price_source=LOCAL_CALC。
    边界：signal_close 也缺失且涨停价/高开区间均缺 → price_source=MISSING（上游据此把该票转观察名单，§2.6）。
    """
    board = board_of(row.ts_code)
    # board 判不出（科创 688 / 北交 8xx / 未知段）：本项目只做主板 + 创业板，这类标的本应已被
    # universe_filter 剔除。eff_board 仅用于「信号侧已给权威价位」时的 board 记录，绝不再用它对
    # 这类标的兜底现算涨停价（评审 P0-F1：默认主板 10% 会对 20%/30% 板算错、对 ST 算错）。
    eff_board = board if board is not None else Board.MAIN
    # ST/退市整理判定（评审二轮 P1#18 + 三轮 F3 + 禁买 ST 硬规则）：统一走 is_st_stock——显式 is_st=True 或
    # 证券名命中 ST/退即判 ST。主板 ST 涨停 5%，避免按 10% 算出超法定涨停价的废单 / 对真实 5% 顶板漏买。
    # 注：name 缺失且 is_st 非 True 时 is_st_stock 返 False（非 ST/10%）——主板非 ST 票若误按 5% 算限价会挂在
    # +5% 远低于真实 +10% 涨停板而漏买，故不把缺失当 ST；真 ST 由显式 is_st 或当日 name 收口（二者均接线后可靠）。
    is_st = is_st_stock(getattr(row, "is_st", None), row.name)

    has_signal_limit = row.limit_up_price is not None
    has_signal_range = (
        row.reasonable_open_high_low is not None and row.reasonable_open_high_high is not None
    )

    if has_signal_limit and has_signal_range:
        # 信号侧价位齐全，直接采用（最高优先级）。信号侧按其板块/ST 元数据算过，含科创/北交/ST 均正确。
        return PriceBudget(
            limit_up_price=row.limit_up_price,
            reasonable_open_low=row.reasonable_open_high_low,
            reasonable_open_high=row.reasonable_open_high_high,
            board=eff_board,
            price_source=PriceSource.SIGNAL,
        )

    # 评审 P0-F1：board 判不出且信号侧未给涨停价 → 无法可信现算（科创/北交 20%/30%、ST 5% 各不同），
    # 不再按主板 10% 兜底（会算出错误涨停价进而误挂单 / 误判顶板），降级 MISSING 转观察名单。
    # 注：ST 票若落在 600/000 前缀会被 board_of 判为主板（执行侧 row 无 is_st 标志，无法识别）——
    #     ST 的正确 5% 口径依赖信号侧 universe_filter 排除 + 信号侧已给 limit_up_price；
    #     供数契约补 is_st 后可在此对主板 ST 改用 5%（见评审 F1 / 批次 3.4）。
    if board is None and not has_signal_limit:
        return PriceBudget(
            limit_up_price=Decimal("0"),
            reasonable_open_low=Decimal("0"),
            reasonable_open_high=Decimal("0"),
            board=Board.MAIN,
            price_source=PriceSource.MISSING,
        )

    # signal_close>0 才可现算（评审二轮 P2#45）：原实现 `is not None` 会把 signal_close<=0 现算成涨停价 0
    # 并标 LOCAL_CALC 进可交易名单（错价/可能 0 价下单）。<=0 属脏数据，应降级 MISSING 转观察名单。
    if row.signal_close is not None and row.signal_close > 0:
        # 用收盘价 + board(+ST) 规则兜底现算涨停价（仅主板/创业板，board 已非 None 或已有信号涨停价）；
        # 合理高开区间优先用信号侧给的，缺则用一段保守默认。
        lup = row.limit_up_price if has_signal_limit else limit_up_price(row.signal_close, eff_board, is_st)
        low, high = _fallback_open_range(row, eff_board, is_st)
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


def _fallback_open_range(
    row: SelectedStockRow, board: Board, is_st: bool = False
) -> Tuple[Decimal, Decimal]:
    """合理高开区间兜底：信号侧给则用其值；否则以 signal_close 的 [+2%, +涨停] 作保守默认区间。

    说明：执行侧不臆造精细高开阈值，这里只给「不为空」的保守占位，真实阈值由配置/信号侧主导；
    标 LOCAL_CALC 后复盘可识别该区间为兜底现算。high 用 board(+ST) 涨停价（评审二轮 P1#18，ST 高沿不超 5%）。
    """
    if row.reasonable_open_high_low is not None and row.reasonable_open_high_high is not None:
        return row.reasonable_open_high_low, row.reasonable_open_high_high
    close = row.signal_close
    low = round_to_cent(close * Decimal("1.02"))
    high = limit_up_price(close, board, is_st)
    return low, high
