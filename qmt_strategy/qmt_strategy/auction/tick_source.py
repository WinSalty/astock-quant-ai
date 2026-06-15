"""竞价 / 盘中 tick 取数源（封装 xtdata.get_full_tick，§3.2 / §3.8）。

业务意图：把 xtdata 行情接口（Windows-only）封装为 contracts.TickSource 协议，
使 auction_poller 只依赖协议而非真实 xtdata，既能在 macOS/Linux 用 fake 跑全部单测，
又便于真实落地时注入 xtquant 实现。绝不在此 import xtquant（用 contracts.XtDataLike Protocol）。

降级口径（§3.7）：底层取数任何异常一律转 TickSourceError 抛出，由 poller 主循环统一捕获走降级，
不让原始 xtdata 异常穿透污染上层。
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Union

from ..contracts.errors import TickSourceError
from ..contracts.protocols import XtDataLike


class XtdataTickSource:
    """真实 xtdata 取数源：委托注入的 XtDataLike.get_full_tick（实现 contracts.TickSource）。"""

    def __init__(self, xtdata: XtDataLike):
        # 注入 xtdata 接口（真实 xtquant 模块对象或其包装），本类不直接 import xtquant。
        self._xtdata = xtdata
        # 已订阅 code 集合（评审二轮 P2#44）：get_full_tick 前须先 subscribe_quote，否则可能取不到 / 取到陈旧
        # tick；按 code 首次取数前订阅一次（幂等），避免重复订阅。
        self._subscribed: set = set()

    def _ensure_subscribed(self, codes: List[str]) -> None:
        """对未订阅的 code 调用 xtdata.subscribe_quote 订阅行情（评审二轮 P2#44）。

        订阅失败不阻断取数（仅跳过该 code 的订阅记录，下次再试）：真实取数失败会由 get_full_tick 归一为
        TickSourceError 走降级。xtdata 无 subscribe_quote（旧版本 / fake）→ 整体跳过，不影响既有行为。
        """
        sub = getattr(self._xtdata, "subscribe_quote", None)
        if not callable(sub):
            return
        for code in codes:
            if code in self._subscribed:
                continue
            try:
                sub(code, "tick")
                self._subscribed.add(code)
            except Exception:  # noqa: BLE001 订阅失败不阻断，取数失败再走降级
                pass

    def get_full_tick(self, codes: List[str]) -> Dict[str, dict]:
        """批量取全推 tick；任何底层异常转 TickSourceError 抛出（§3.8）。

        业务意图：竞价段需对失败统一降级，故在此把 xtdata 可能抛的各类异常
        （网络 / 行情未就绪 / KeyError 等）归一为 TickSourceError，由 poller 主循环捕获走降级 B。
        """
        # 取数前确保已订阅（评审二轮 P2#44）：xtdata.get_full_tick 依赖先 subscribe_quote。
        self._ensure_subscribed(codes)
        try:
            result = self._xtdata.get_full_tick(codes)
        except Exception as exc:  # noqa: BLE001 - 故意宽捕获后归一为领域异常
            # 边界：保留原始异常信息（链式），但对上层只暴露 TickSourceError 单一类型。
            raise TickSourceError(f"get_full_tick failed: {exc}") from exc
        # 防御：底层返回 None 视为取数失败，同样走降级。
        if result is None:
            raise TickSourceError("get_full_tick returned None")
        return result


# Fake 每次调用的返回单元：要么是一份 {code: tick} 映射，要么是一个异常实例（模拟抛错）。
FakeTickResponse = Union[Dict[str, dict], BaseException]


class FakeTickSource:
    """单测用 tick 取数源（实现 contracts.TickSource）。

    业务意图：让 auction_poller 在不连真实 xtdata 的前提下被穷举测试，且可证明
    poll_once 完全由定时器驱动、不依赖任何 tick 回调（构造时给定固定 / 序列化的返回即可）。

    构造支持三种喂数方式（按 responses 元素逐次消费）：
      - dict（{code: tick}）：本次调用返回该映射。
      - 异常实例：本次调用 raise 之（模拟 get_full_tick 失败 → 走降级）。
    若提供 fixed（固定映射），则每次调用都返回它（不消费 responses）。
    responses 耗尽后默认返回最后一次的值（或空 dict），便于 run 多轮循环不越界。
    """

    def __init__(
        self,
        responses: Optional[Sequence[FakeTickResponse]] = None,
        *,
        fixed: Optional[Dict[str, dict]] = None,
    ):
        # 固定返回模式：每次调用都回同一份映射。
        self._fixed = fixed
        # 序列返回模式：按调用次序逐个消费。
        self._responses: List[FakeTickResponse] = list(responses) if responses else []
        self._idx = 0
        # 记录每次被调用时传入的 codes，供测试断言批量传参口径。
        self.calls: List[List[str]] = []

    def get_full_tick(self, codes: List[str]) -> Dict[str, dict]:
        # 记录调用入参（验证「批量一次取全部」而非逐股轮询）。
        self.calls.append(list(codes))

        if self._fixed is not None:
            return dict(self._fixed)

        if not self._responses:
            # 无任何预置返回：视为本轮无行情（空映射），poll_once 会对每个 code 走降级。
            return {}

        # 取本次应返回项；序列耗尽后复用最后一项（持续轮询不越界）。
        if self._idx < len(self._responses):
            item = self._responses[self._idx]
            self._idx += 1
        else:
            item = self._responses[-1]

        # 异常项：模拟底层取数失败，由 poller 捕获走降级 B。
        if isinstance(item, BaseException):
            raise item
        return dict(item)


# 便捷类型别名：plan_provider 返回当日计划行映射（key=ts_code）。
PlanProvider = Callable[[], Dict[str, object]]
