"""执行侧盘前交易日历校验 + 补取（doc/29 J-3）。

业务意图：本地静态交易日历（QMT_TRADE_CALENDAR_FILE）是有限集，会耗尽——耗尽后 next_open 越界、T+1 映射失准。
原靠人工导出 + scp 同步，易漏。这里在盘前【校验一次】本地日历是否覆盖到今天（含若干前向余量）：
- 覆盖足 → 直接用本地，不拉取（零额外开销）；
- 不足 → 调信号侧内网接口 /api/internal/trade_calendar 取一次开市日，并入内存日历（原地，engine 共享同一日历
  对象即时生效）+ 落回本地文件（下次启动可用）；
- 接口也取不到 / 取到后仍不足 → ensure_coverage 返回 False；engine.prewarm 据（已尽力刷新的）日历覆盖度
  fail-closed 阻断开仓（绝不在日历失准下盲开新仓）。

创建日期：2026-06-21
author: claude
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, List, Optional

from ..common.http_client import SignalHttpError

# 与信号侧 routes_watchlist_export.export_trade_calendar 路径一致（含 /api 前缀）。
TRADE_CALENDAR_PATH = "/api/internal/trade_calendar"


class TradeCalendarRefresher:
    """盘前交易日历覆盖度校验 + 不足补取（J-3）。无 I/O 热路径，仅 PREWARM 调用一次。

    依赖注入：
    - client：SignalHttpClient（带 base_url+token+超时）；为 None（未配信号侧）则只能用本地、不拉取。
    - calendar：执行侧交易日历（与 engine 共享同一对象，补取后 merge_days 原地生效）。
    - min_forward_days：前向余量阈值——trading_days_left(today) ≥ 该值才算「覆盖足」（默认 5，约一周）。
    - persist_fn：把全量开市日落回本地文件的函数 Callable[[list[date]], None]；为 None 则只更新内存不落盘。
    - logger：结构化日志。
    """

    def __init__(
        self,
        client: Any,
        calendar: Any,
        *,
        min_forward_days: int = 5,
        persist_fn: Optional[Callable[[List[date]], None]] = None,
        logger: Any = None,
    ) -> None:
        self._client = client
        self._calendar = calendar
        self._min_forward = max(1, int(min_forward_days))
        self._persist_fn = persist_fn
        self._logger = logger

    def _covered(self, today: date) -> bool:
        """本地日历是否覆盖到今天且有足够前向余量：trading_days_left(today) ≥ min_forward。

        无 trading_days_left（如周末近似日历，next_open 永不越界）→ 视为充足、不拦。
        """
        fn = getattr(self._calendar, "trading_days_left", None)
        if not callable(fn):
            return True
        try:
            return int(fn(today)) >= self._min_forward
        except Exception:  # noqa: BLE001 覆盖度计算异常按「不足」保守处理
            return False

    def ensure_coverage(self, today: date) -> bool:
        """校验并（必要时）补取。返回最终是否覆盖足（False=engine 据此 fail-closed 阻断开仓）。"""
        if self._covered(today):
            return True  # 覆盖足：用本地，不拉取
        if self._client is None:
            self._warn("trade_calendar_refresh_no_client", today)
            return self._covered(today)
        # 不足 → 拉信号侧补取
        try:
            resp = self._client.get_json(TRADE_CALENDAR_PATH)
        except SignalHttpError as exc:
            self._warn(
                "trade_calendar_refresh_http_failed", today, status=exc.status, reason=str(exc)
            )
            return self._covered(today)  # 拉取失败：仍不足 → False（fail-closed）
        days = self._parse(resp)
        if not days:
            self._warn("trade_calendar_refresh_empty", today)
            return self._covered(today)
        merge_fn = getattr(self._calendar, "merge_days", None)
        added = merge_fn(days) if callable(merge_fn) else 0
        # 落回本地文件（持久，下次启动可用）：失败仅告警不阻断（内存已更新、本轮仍可用）。
        # 评审 #5 加固：只用日历的【全量开市日】整体覆盖写；若日历不提供 open_days（无法取全量）则【跳过落盘 + 告警】，
        # 绝不退化为「仅本次拉取的子集」覆盖写（那会把本地文件截断成子集、丢历史/其它交易所日）。
        if added and self._persist_fn is not None:
            open_days_fn = getattr(self._calendar, "open_days", None)
            if callable(open_days_fn):
                try:
                    self._persist_fn(open_days_fn())
                except Exception as exc:  # noqa: BLE001 落盘失败不阻断本轮（内存已更新）
                    self._warn("trade_calendar_persist_failed", today, reason=repr(exc))
            else:
                self._warn("trade_calendar_persist_skipped_no_open_days", today)
        ok = self._covered(today)
        if self._logger is not None:
            # 措辞对齐 engine 实际门控（评审 #4）：ensure_coverage 的 min_forward(默认5) 是【提前补取的余量阈值】，
            # 并非 engine 的 fail-closed 线——engine 仅在 trading_days_left(today)<=0（无 today 之后交易日、T+1 无法映射）
            # 时才真正阻断开仓。故这里 ok=False 表示「覆盖余量不足、请尽快外延日历」，不等同于「engine 必 fail-closed」。
            event_fn = self._logger.info if ok else self._logger.error
            event_fn(
                "trade_calendar_refreshed",
                today=str(today), added=added, covered=ok,
                note=None if ok else "补取后覆盖余量仍不足(< 预取阈值)，请尽快外延日历/排查信号侧连通；"
                "engine 仅在日历耗尽(today 之后无交易日)时 fail-closed 阻断开仓",
            )
        return ok

    def _parse(self, resp: Any) -> List[date]:
        """解析接口响应 {open_days:[ISO...]} → list[date]；非对象/坏日期跳过，不抛。"""
        if not isinstance(resp, dict):
            return []
        out: List[date] = []
        for s in resp.get("open_days") or []:
            try:
                out.append(date.fromisoformat(str(s)[:10]))
            except (ValueError, TypeError):
                continue
        return out

    def _warn(self, event: str, today: date, **kw: Any) -> None:
        if self._logger is not None:
            self._logger.warn(event, today=str(today), **kw)
