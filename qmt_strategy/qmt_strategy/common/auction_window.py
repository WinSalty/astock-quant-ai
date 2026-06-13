"""集合竞价时段映射（§3.3 强约束，按东八区钟点精确到秒）。

业务意图：竞价时段决定轮询行为与撤单约束。本纯函数被 auction_poller（决定采集/降级）与
order_executor（决定 9:20–9:25 是否禁撤）共用，故放在 common 层，避免两模块互相依赖。

时段边界（§3.3 / §3.9 单测口径，闭左开右）：
  t < 09:15:00            → PRE_AUCTION
  09:15:00 ≤ t < 09:20:00 → AUCTION_CANCELABLE（可撤）
  09:20:00 ≤ t < 09:25:00 → AUCTION_LOCKED（不可撤：此段挂的竞价单无法撤回）
  09:25:00 ≤ t < 09:30:00 → SETTLED（定盘）
  t ≥ 09:30:00            → CLOSED_WINDOW（转连续竞价口径）
"""

from __future__ import annotations

from datetime import datetime, time

from ..contracts.enums import AuctionPhase
from .time_utils import east8_time_of

# 关键时点（东八区钟点）。
_T_0915 = time(9, 15, 0)
_T_0920 = time(9, 20, 0)
_T_0925 = time(9, 25, 0)
_T_0930 = time(9, 30, 0)


def resolve_phase(now_utc: datetime) -> AuctionPhase:
    """把 UTC naive 时刻映射为竞价时段（先转东八区时分秒再比较，§3.3）。"""
    t = east8_time_of(now_utc)
    if t < _T_0915:
        return AuctionPhase.PRE_AUCTION
    if t < _T_0920:
        return AuctionPhase.AUCTION_CANCELABLE
    if t < _T_0925:
        return AuctionPhase.AUCTION_LOCKED
    if t < _T_0930:
        return AuctionPhase.SETTLED
    return AuctionPhase.CLOSED_WINDOW


def is_cancel_forbidden(now_utc: datetime) -> bool:
    """是否处于禁撤段（9:20–9:25，AUCTION_LOCKED）。

    order_executor 在该段禁发 cancel_order（撤单必废，§3.3 / §4.5）；其余时段可撤。
    """
    return resolve_phase(now_utc) == AuctionPhase.AUCTION_LOCKED
