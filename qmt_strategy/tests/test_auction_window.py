"""竞价时段映射单测（§3.9 边界精确到秒，按东八区）。"""

from __future__ import annotations

from datetime import date

from qmt_strategy.common.auction_window import (
    is_cancel_forbidden,
    is_lunch_break,
    resolve_phase,
)
from qmt_strategy.contracts.enums import AuctionPhase
from tests.conftest import utc_at_east8

D = date(2026, 6, 12)


def test_is_lunch_break_boundaries():
    """评审 P1#11：午休停牌段 11:30:00 ≤ t < 13:00:00（闭左开右）。"""
    assert is_lunch_break(utc_at_east8(D, 11, 29, 59)) is False
    assert is_lunch_break(utc_at_east8(D, 11, 30, 0)) is True
    assert is_lunch_break(utc_at_east8(D, 12, 0, 0)) is True
    assert is_lunch_break(utc_at_east8(D, 12, 59, 59)) is True
    assert is_lunch_break(utc_at_east8(D, 13, 0, 0)) is False     # 复牌即非午休
    assert is_lunch_break(utc_at_east8(D, 10, 0, 0)) is False
    assert is_lunch_break(utc_at_east8(D, 14, 0, 0)) is False


def test_phase_boundaries():
    assert resolve_phase(utc_at_east8(D, 9, 14, 59)) == AuctionPhase.PRE_AUCTION
    assert resolve_phase(utc_at_east8(D, 9, 15, 0)) == AuctionPhase.AUCTION_CANCELABLE
    assert resolve_phase(utc_at_east8(D, 9, 19, 59)) == AuctionPhase.AUCTION_CANCELABLE
    assert resolve_phase(utc_at_east8(D, 9, 20, 0)) == AuctionPhase.AUCTION_LOCKED
    assert resolve_phase(utc_at_east8(D, 9, 24, 59)) == AuctionPhase.AUCTION_LOCKED
    assert resolve_phase(utc_at_east8(D, 9, 25, 0)) == AuctionPhase.SETTLED
    assert resolve_phase(utc_at_east8(D, 9, 29, 59)) == AuctionPhase.SETTLED
    assert resolve_phase(utc_at_east8(D, 9, 30, 0)) == AuctionPhase.CLOSED_WINDOW


def test_cancel_forbidden_only_in_locked():
    assert is_cancel_forbidden(utc_at_east8(D, 9, 16, 0)) is False
    assert is_cancel_forbidden(utc_at_east8(D, 9, 22, 0)) is True   # 9:20–9:25 禁撤
    assert is_cancel_forbidden(utc_at_east8(D, 9, 31, 0)) is False
