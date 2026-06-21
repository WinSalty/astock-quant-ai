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
    sec = int(ts)
    # 单位归一（执行-19 修正 2026-06-22）：QMT 约定秒级，但不同 xtquant 版本可能返回毫秒(13位)/微秒(16位)。
    # 旧实现一律按秒解释 → fromtimestamp 对毫秒值算出公元五万多年而抛 OverflowError，被下方 except 静默吞成
    # (None,None)，使 traded_time/order_time 及 *_east8 整列无声变 NULL、按时间排序/对账失真且无任何提示。
    # 这里按数量级把明显非秒的时间戳降为秒——各单位量级互不重叠（当代秒级≈1.7e9，毫秒≈1.7e12，微秒≈1.7e15），
    # 属确定性的单位换算、非臆造默认值；换算后仍越界才落 (None,None)。真机实际单位仍须按待办清单核实固化。
    _abs = abs(sec)
    if _abs >= 1_000_000_000_000_000:      # >=1e15：微秒级 → /1e6
        sec = sec // 1_000_000
    elif _abs >= 100_000_000_000:          # >=1e11：毫秒级 → /1e3（秒级当代约 1.7e9，落不到此区）
        sec = sec // 1_000
    try:
        east8 = datetime.fromtimestamp(sec, tz=SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None, None
    # 合理区间兜底（执行-R11 修正 2026-06-22）：归一只覆盖秒/毫秒/微秒三种规整位数；若是 14 位或 1e13~1e15 中段等
    # 非常规位数脏值，会被当毫秒只 /1e3 而落到公元 2500+ 远期、变成「看似合法的错误时间」比静默 NULL 更难察觉。
    # 这里对换算结果年份做兜底：落在 [2000,2100] 外即判脏值返回 (None,None)，不写远期时间污染对账/排序。
    if not (2000 <= east8.year <= 2100):
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
