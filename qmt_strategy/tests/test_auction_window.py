"""竞价时段映射单测（§3.9 边界精确到秒，按东八区）。"""

from __future__ import annotations

from datetime import date

from qmt_strategy.common.auction_window import is_cancel_forbidden, resolve_phase
from qmt_strategy.contracts.enums import AuctionPhase
from tests.conftest import utc_at_east8

D = date(2026, 6, 12)


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
