"""决策采集发射器 DecisionEmitter —— 复盘用「信号达标→下单/未买→卖出」决策链路的非阻塞采集。

第一硬约束（务必守住，逐条对应实现）：**绝不阻塞、绝不影响真实交易、绝不让异常冒泡，数据可丢失**。
1. 不阻塞：决策点只 `put_nowait`（有界队列，O(1) 内存），热路径无任何 IO；
2. 可丢失：队列满即丢（计数告警）、消费线程异常全吞、回流失败不重试到死；
3. 零异常冒泡：`emit/append` 外层 `try/except Exception: pass`，连构造事件都不许抛进交易线程；
4. 物理隔离：自带有界队列 + 独立 daemon 线程 + 独立 sink，与交易侧 AsyncWriteQueue（无界/不丢/fail-fast）
   完全无共享，决策栈整体崩溃也不触及交易写盘与下单热路径；
5. 不污染对账：本采集不进执行侧 QMT_TABLES、不进 RemoteSyncJob 的 ok 判据；sink 失败只 warn。

回滚口径：构造时 enabled=False（或 sink 为 None）即降级为 no-op（不建队列线程、emit 直接返回，零成本）。

创建日期：2026-06-14
author: claude
"""

from __future__ import annotations

import queue
import threading
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from ..common.time_utils import east8_now_from_utc, now_utc_naive

# 回流目标表名（与信号侧 qmt_decision_log / QMT_INGEST_TABLES 一致）。
DECISION_TABLE = "qmt_decision_log"

# sink 签名：接收一批 JSON 友好行字典，best-effort 持久化（回流）；内部应自吞单行异常。
DecisionSink = Callable[[List[Dict[str, Any]]], None]


def _iso(v: Any) -> Optional[str]:
    """date/datetime → ISO 文本；None 透传；其它原样字符串化。"""
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def _enum_value(v: Any) -> Optional[str]:
    """枚举 → .value；None 透传；已是字符串则原样。"""
    if v is None:
        return None
    return str(getattr(v, "value", v))


class DecisionEmitter:
    """决策事件非阻塞采集器（有界队列 + 独立后台线程 + best-effort sink）。"""

    def __init__(
        self,
        account_id: str,
        clock: Any,
        logger: Any,
        *,
        sink: Optional[DecisionSink] = None,
        enabled: bool = True,
        queue_size: int = 2000,
        batch_size: int = 50,
        poll_interval: float = 2.0,
    ) -> None:
        self._account_id = account_id
        self._clock = clock
        self._logger = logger
        self._sink = sink
        # 缺 sink（未配信号侧回流通道）即无处可送 → 等价 no-op，避免空转线程。
        self._enabled = bool(enabled) and sink is not None
        self._batch_size = max(1, int(batch_size))
        self._poll = float(poll_interval)
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max(1, int(queue_size)))
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = False
        # 仅统计用（非交易关键）：入队成功数 / 满丢数。
        self._emitted = 0
        self._dropped = 0

    # ==================================================================
    # 生命周期
    # ==================================================================
    def start(self) -> None:
        """启动独立 daemon 消费线程（幂等）。enabled=False 时不起线程。"""
        if self._started or not self._enabled:
            return
        self._thread = threading.Thread(target=self._run, name="qmt-decision-writer", daemon=True)
        self._thread.start()
        self._started = True
        try:
            self._logger.info("decision_emitter_started", account_id=self._account_id)
        except Exception:  # noqa: BLE001 连日志失败也不许影响主流程
            pass

    def stop(self, drain_timeout: float = 2.0) -> None:
        """停机：通知线程退出并尽量 drain（不强求清空，超时即放弃，数据可丢失）。"""
        self._stop.set()
        t = self._thread
        if t is not None:
            try:
                t.join(timeout=drain_timeout)
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> Dict[str, int]:
        """采集统计（emitted/dropped/queued），供盘后留痕，非交易关键。"""
        return {"emitted": self._emitted, "dropped": self._dropped, "queued": self._q.qsize()}

    # ==================================================================
    # 采集入口（绝不抛错）
    # ==================================================================
    def append(self, decision: Any) -> None:
        """entry_router.decision_log 的 drop-in：承接一条 EntryDecision，映射为 SIGNAL_QUALIFIED / SKIP。"""
        try:
            kw = self._from_entry_decision(decision)
            if kw is not None:
                self.emit(**kw)
        except Exception:  # noqa: BLE001 采集绝不影响交易
            pass

    def emit(
        self,
        *,
        decision_type: str,
        ts_code: Optional[str] = None,
        signal_trade_date: Any = None,
        trade_date: Any = None,
        decision_stage: Optional[str] = None,
        action: Any = None,
        strategy_family: Optional[str] = None,
        order_phase: Any = None,
        reason: Optional[str] = None,
        reason_code: Optional[str] = None,
        factors: Optional[Dict[str, Any]] = None,
        limit_price: Any = None,
        plan_volume: Optional[int] = None,
        order_id: Optional[int] = None,
        biz_order_no: Optional[str] = None,
        decided_at: Optional[datetime] = None,
    ) -> None:
        """通用决策发射：构造行 → put_nowait（满即丢）。全程 try/except，绝不抛进调用线程。"""
        if not self._enabled:
            return
        try:
            row = self._build_row(
                decision_type=decision_type, ts_code=ts_code, signal_trade_date=signal_trade_date,
                trade_date=trade_date, decision_stage=decision_stage, action=action,
                strategy_family=strategy_family, order_phase=order_phase, reason=reason,
                reason_code=reason_code, factors=factors, limit_price=limit_price,
                plan_volume=plan_volume, order_id=order_id, biz_order_no=biz_order_no,
                decided_at=decided_at,
            )
            self._q.put_nowait(row)
            self._emitted += 1
        except queue.Full:
            # 满即丢：绝不阻塞调用线程（区别于交易侧无界不丢的 AsyncWriteQueue）。
            self._dropped += 1
        except Exception:  # noqa: BLE001 构造/入队任何异常都吞掉，交易优先
            pass

    # ==================================================================
    # 行构造 / EntryDecision 映射
    # ==================================================================
    def _build_row(self, **kw: Any) -> Dict[str, Any]:
        """组装信号侧 qmt_decision_log 的 JSON 友好行（含账户/决策号/双写时间）。"""
        decided_at = kw.get("decided_at") or self._now_utc()
        # 决策交易日缺省取「决策时刻东八区自然日」；显式传入（如买入用 target_trade_date）优先。
        td = kw.get("trade_date")
        trade_date = _iso(td) if td is not None else east8_now_from_utc(decided_at).date().isoformat()
        return {
            "account_id": self._account_id,
            "trade_date": trade_date,
            # 决策号用 uuid 保唯一（幂等 upsert 键之一；可丢失场景重启不复用即可）。
            "decision_id": uuid.uuid4().hex[:20],
            "signal_trade_date": _iso(kw.get("signal_trade_date")),
            "ts_code": kw.get("ts_code"),
            "decision_type": kw["decision_type"],
            "decision_stage": kw.get("decision_stage"),
            "action": _enum_value(kw.get("action")),
            "strategy_family": kw.get("strategy_family"),
            "order_phase": _enum_value(kw.get("order_phase")),
            "reason": (kw.get("reason") or None) and str(kw["reason"])[:255],
            "reason_code": kw.get("reason_code"),
            "factors_snapshot": kw.get("factors") if isinstance(kw.get("factors"), dict) else None,
            "limit_price": str(kw["limit_price"]) if isinstance(kw.get("limit_price"), Decimal) else kw.get("limit_price"),
            "plan_volume": kw.get("plan_volume"),
            "order_id": kw.get("order_id"),
            "biz_order_no": kw.get("biz_order_no"),
            "decided_time": decided_at.isoformat(),
            "decided_time_east8": east8_now_from_utc(decided_at).isoformat(),
            "data_source": "EMITTER",
        }

    def _from_entry_decision(self, decision: Any) -> Optional[Dict[str, Any]]:
        """EntryDecision → emit kwargs：action==SKIP → SKIP_STRATEGY，否则 SIGNAL_QUALIFIED。"""
        action = getattr(decision, "action", None)
        is_skip = _enum_value(action) == "SKIP"
        return {
            "decision_type": "SKIP_STRATEGY" if is_skip else "SIGNAL_QUALIFIED",
            "decision_stage": "STRATEGY",
            "ts_code": getattr(decision, "ts_code", None),
            "signal_trade_date": getattr(decision, "signal_trade_date", None),
            "trade_date": getattr(decision, "target_trade_date", None),
            "action": action,
            "strategy_family": getattr(decision, "strategy_family", None),
            "order_phase": getattr(decision, "order_phase", None),
            "reason": getattr(decision, "reason", None),
            "reason_code": "entry_skip" if is_skip else "entry_qualified",
            "factors": getattr(decision, "factors_snapshot", None),
            "limit_price": getattr(decision, "limit_price", None),
            "plan_volume": getattr(decision, "plan_volume", None),
            "decided_at": getattr(decision, "decided_at", None),
        }

    def _now_utc(self) -> datetime:
        """决策时刻 UTC naive：优先用注入 clock，缺则用模块默认。"""
        try:
            return self._clock.now_utc()
        except Exception:  # noqa: BLE001
            return now_utc_naive()

    # ==================================================================
    # 消费线程（异常全吞，绝不 fail-fast）
    # ==================================================================
    def _run(self) -> None:
        """后台 drain：攒批 → best-effort sink；任何异常只 warn 不抛、不重试到死。"""
        while not self._stop.is_set():
            batch = self._drain_batch()
            if not batch:
                continue
            self._flush(batch)
        # 退出前尽量把残余 drain 一次（best-effort）。
        remaining = self._drain_batch(block=False)
        if remaining:
            self._flush(remaining)

    def _drain_batch(self, block: bool = True) -> List[Dict[str, Any]]:
        """从队列攒一批（首条可阻塞等待 poll 秒，便于及时退出）。"""
        batch: List[Dict[str, Any]] = []
        try:
            if block:
                first = self._q.get(timeout=self._poll)
                batch.append(first)
            while len(batch) < self._batch_size:
                batch.append(self._q.get_nowait())
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            pass
        return batch

    def _flush(self, batch: List[Dict[str, Any]]) -> None:
        """把一批决策行交给 sink；失败只告警不抛、不重试（数据可丢失）。"""
        sink = self._sink
        if sink is None:
            return
        try:
            sink(batch)
        except Exception as exc:  # noqa: BLE001 回流失败绝不影响交易/对账
            try:
                self._logger.warn("decision_emitter_flush_failed", count=len(batch), error=repr(exc))
            except Exception:  # noqa: BLE001
                pass
