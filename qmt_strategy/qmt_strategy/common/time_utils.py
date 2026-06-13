"""时间口径统一入口（§6.6，杜绝 ±8h）。

业务意图：QMT traded_time/order_time 为东八区运行的 Unix 时间戳（秒）。统一在此
先按 Asia/Shanghai 解释，再转 UTC naive 入库（traded_time/order_time），同时把东八区
naive 原值落 *_east8 供对账。任何写端/前端不得在 SQL 手工 ±8h。

时段判定（§3.3）按东八区钟点比较；落库时刻统一 UTC naive。
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def qmt_ts_to_db(ts: Optional[int]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """QMT 时间戳 → (UTC naive, 东八区 naive) 双写（§6.6）。

    业务意图：第一个返回值入 traded_time/order_time（与后端 UTC naive 字段同口径），
    第二个入 *_east8（东八区原值，仅供人工核对对账）。
    边界：ts 为 None/0/异常时返回 (None, None)，不写错误时间。
    """
    if not ts:
        return None, None
    try:
        east8 = datetime.fromtimestamp(int(ts), tz=SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None, None
    utc_naive = east8.astimezone(UTC).replace(tzinfo=None)
    east8_naive = east8.replace(tzinfo=None)
    return utc_naive, east8_naive


def now_utc_naive() -> datetime:
    """当前 UTC naive 时间（tzinfo=None），落库时刻统一口径。"""
    return datetime.now(UTC).replace(tzinfo=None)


def utc_naive_to_east8(dt: Optional[datetime]) -> Optional[datetime]:
    """UTC naive → 东八区 naive（展示/对账辅助）。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(SHANGHAI).replace(tzinfo=None)


def east8_now_from_utc(now_utc: datetime) -> datetime:
    """把 UTC naive 当前时刻转成东八区 naive，用于按东八区钟点做时段判定（§3.3）。"""
    return now_utc.replace(tzinfo=UTC).astimezone(SHANGHAI).replace(tzinfo=None)


def east8_time_of(now_utc: datetime) -> time:
    """取 UTC naive 时刻对应的东八区「时:分:秒」，供集合竞价时段映射。"""
    return east8_now_from_utc(now_utc).time()


def east8_trade_date(now_utc: datetime) -> date:
    """取 UTC naive 时刻对应的东八区自然日（= trade_date 口径，不随入库机 UTC 漂移，§6.6）。"""
    return east8_now_from_utc(now_utc).date()


class SystemClock:
    """真实时钟：now_utc 取系统 UTC naive。实现 contracts.Clock 协议。"""

    def now_utc(self) -> datetime:
        return now_utc_naive()


class FakeClock:
    """可控时钟（单测注入固定/递进时刻）。实现 contracts.Clock 协议。"""

    def __init__(self, now: datetime):
        # 约定传入 UTC naive；若带 tzinfo 则规整为 UTC naive。
        self._now = now.replace(tzinfo=None) if now.tzinfo is None else now.astimezone(UTC).replace(tzinfo=None)

    def now_utc(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now.replace(tzinfo=None) if now.tzinfo is None else now.astimezone(UTC).replace(tzinfo=None)

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)
