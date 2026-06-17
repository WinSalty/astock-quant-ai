"""buy_prefilter 买入前置过滤层单测（doc/18）。

覆盖：ST 规则（显式/名称/退市）、四板及以上规则（board_level 阈值边界 + tier 兜底 + 双源缺失放行）、
阈值覆盖（调高/调低/关闭）、规则优先级（ST 先于四板）、放行口径。纯函数、无 I/O。
"""

from __future__ import annotations

from qmt_strategy.common.buy_prefilter import (
    RULE_HIGH_BOARD,
    RULE_ST,
    CandidateView,
    evaluate,
    is_high_board,
)


# ---------------------------------------------------------------------------
# is_high_board：四板及以上双源判定
# ---------------------------------------------------------------------------
def test_high_board_by_board_level_threshold():
    """board_level 精确口径：>=4 判高板，<=3 放行（默认阈值 4）。"""
    assert is_high_board(4, None) is True
    assert is_high_board(5, None) is True
    assert is_high_board(3, None) is False
    assert is_high_board(2, None) is False
    assert is_high_board(1, None) is False


def test_high_board_by_tier_fallback_when_board_level_missing():
    """board_level 缺失/未识别（None 或 0）→ tier==HIGH_BOARD 兜底判高板（信号侧 4+ 板恒落 HIGH_BOARD）。"""
    assert is_high_board(None, "HIGH_BOARD") is True
    assert is_high_board(0, "HIGH_BOARD") is True          # 0=信号侧未识别连板，按缺失走 tier 兜底
    assert is_high_board(None, "high_board") is True        # 大小写/空白不敏感
    assert is_high_board(None, " HIGH_BOARD ") is True
    assert is_high_board(None, "CHAIN") is False            # 两三连不拦
    assert is_high_board(None, "FIRST_BOARD") is False      # 首板不拦
    assert is_high_board(None, None) is False               # 双源无证据 → 放行（不无证据全拦）


def test_high_board_board_level_takes_priority_over_tier():
    """board_level 为正整数时以精确口径为准，不被 tier 干扰（板高已识别最权威）。"""
    # board_level=3（两三连）即便 tier 误标 HIGH_BOARD，精确口径放行（信号侧分桶一致时不会发生，防御口径）。
    assert is_high_board(3, "HIGH_BOARD") is False
    # board_level=4 即便 tier=CHAIN，精确口径仍判高板。
    assert is_high_board(4, "CHAIN") is True


def test_high_board_threshold_override():
    """阈值可配：调低拦更多板、调高放宽到只拦更高板。"""
    # 调低到 3：board_level=3 也判高板。
    assert is_high_board(3, None, min_level=3) is True
    # 调高到 5：board_level=4 放行（精确口径尊重放宽配置，board_level 已知）。
    assert is_high_board(4, None, min_level=5) is False
    assert is_high_board(5, None, min_level=5) is True
    # 调高到 5 且 board_level 缺失但 tier=HIGH_BOARD：板高缺失无法精确判定，按 4+ 保守拦（fail-closed）。
    assert is_high_board(None, "HIGH_BOARD", min_level=5) is True


def test_high_board_tier_fallback_fails_closed_above_threshold():
    """fail-closed 取向（doc/18 自评审 P2 修复）：board_level 缺失 + tier=HIGH_BOARD，即便阈值调高放宽，仍保守拦。

    放宽（min_level>4）只作用于 board_level 已知的精确口径；板高缺失时无法精确判定具体高度，按 4+ 板保守拦，
    绝不因板高缺失而漏放高板。仅显式关闭（min_level<=0）才不拦。
    """
    assert is_high_board(None, "HIGH_BOARD", min_level=5) is True    # 板高缺失 → fail-closed 拦
    assert is_high_board(0, "HIGH_BOARD", min_level=99) is True       # 任意放宽阈值，缺失高板仍拦
    assert is_high_board(4, "HIGH_BOARD", min_level=5) is False       # 板高已知=4，阈值 5 下精确放行
    assert is_high_board(None, "HIGH_BOARD", min_level=0) is False    # 显式关闭 → 不拦


def test_high_board_disabled_when_min_level_non_positive():
    """min_level<=0 = 关闭高板口径（显式放宽逃生口）：任何 board_level/tier 都放行。"""
    assert is_high_board(8, "HIGH_BOARD", min_level=0) is False
    assert is_high_board(8, "HIGH_BOARD", min_level=-1) is False


# ---------------------------------------------------------------------------
# evaluate：有序规则集裁决
# ---------------------------------------------------------------------------
def test_evaluate_allows_clean_low_board_non_st():
    """主板非 ST、首板/两三连 → 放行。"""
    v = CandidateView(ts_code="600000.SH", name="浦发银行", is_st=False, board_level=2, tier="CHAIN")
    verdict = evaluate(v)
    assert verdict.allowed is True
    assert verdict.rule_code == ""


def test_evaluate_blocks_st_by_explicit_flag():
    v = CandidateView(ts_code="600000.SH", name="某某股份", is_st=True, board_level=1, tier="FIRST_BOARD")
    verdict = evaluate(v)
    assert verdict.allowed is False
    assert verdict.rule_code == RULE_ST


def test_evaluate_blocks_st_by_name_when_flag_absent():
    """is_st 缺失但当日证券名含 ST/退 → 仍判 ST（与既有禁买 ST 口径一致）。"""
    assert evaluate(CandidateView(ts_code="600000.SH", name="ST中安", is_st=None)).rule_code == RULE_ST
    assert evaluate(CandidateView(ts_code="600000.SH", name="*ST华业", is_st=None)).rule_code == RULE_ST
    assert evaluate(CandidateView(ts_code="600000.SH", name="退市美都", is_st=None)).rule_code == RULE_ST


def test_evaluate_blocks_high_board_by_level():
    v = CandidateView(ts_code="300750.SZ", name="宁德时代", is_st=False, board_level=4, tier="HIGH_BOARD")
    verdict = evaluate(v)
    assert verdict.allowed is False
    assert verdict.rule_code == RULE_HIGH_BOARD
    assert "四板及以上" in verdict.reason


def test_evaluate_blocks_high_board_by_tier_when_level_missing():
    """board_level 缺失但 tier=HIGH_BOARD → 仍禁买四板及以上。"""
    v = CandidateView(ts_code="600519.SH", name="贵州茅台", is_st=False, board_level=None, tier="HIGH_BOARD")
    assert evaluate(v).rule_code == RULE_HIGH_BOARD


def test_evaluate_st_takes_priority_over_high_board():
    """同时命中 ST 与四板 → 返回 ST（最高优先级，与既有「闸门 0：ST」一致）。"""
    v = CandidateView(ts_code="600000.SH", name="*ST高位", is_st=True, board_level=6, tier="HIGH_BOARD")
    assert evaluate(v).rule_code == RULE_ST


def test_evaluate_respects_custom_threshold():
    """evaluate 透传 high_board_min_level：调低阈值拦更多、调高阈值放宽。"""
    # 调低到 3：三板（board_level=3）也禁买。
    v3 = CandidateView(ts_code="000001.SZ", name="平安银行", is_st=False, board_level=3, tier="CHAIN")
    assert evaluate(v3, high_board_min_level=3).rule_code == RULE_HIGH_BOARD
    # 调高到 5：四板（board_level=4 正整数走精确口径）放行（尊重放宽配置）。
    v4 = CandidateView(ts_code="000001.SZ", name="平安银行", is_st=False, board_level=4, tier="HIGH_BOARD")
    assert evaluate(v4, high_board_min_level=5).allowed is True


def test_evaluate_allows_when_no_board_evidence():
    """board_level 与 tier 均缺失 → 四板规则放行（无证据不拦），非 ST 则整体放行。"""
    v = CandidateView(ts_code="600000.SH", name="某某股份", is_st=False, board_level=None, tier=None)
    assert evaluate(v).allowed is True
