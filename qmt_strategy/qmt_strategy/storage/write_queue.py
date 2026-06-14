"""异步写队列 AsyncWriteQueue（doc/05 §三，"不阻塞交易"的核心）。

业务意图：所有本机 SQLite 写入一律走 write-behind——交易热路径（QMT 回报回调、下单决策）只把
「一个写任务」入队即返回，**绝不在调用线程做磁盘 I/O**；由唯一的后台写线程串行执行真正的 SQLite 写。

关键不变量（务必守住）：
1. **不阻塞**：`submit` 仅 `put_nowait`（无界队列，入队不阻塞），交易线程永不等待磁盘；
2. **异常只吞不传播**：写线程内任一任务抛错只记日志 + rollback，绝不冒泡到交易线程（写失败不影响交易）；
3. **单写线程**：SQLite 由本线程独占写（配合 WAL，1 写 N 读），避免「database is locked」与跨线程连接问题。

`flush`（盘后 / 对账前调用，可阻塞至队列清空）与 `stop`（停机 drain）均不在交易热路径上。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Optional

# 写任务签名：接收写线程独占的连接，执行一次写（不在此 commit，由 worker 统一 commit）。
WriteTask = Callable[[Any], None]


class AsyncWriteQueue:
    """单后台写线程 + 无界队列的写后异步落盘器。"""

    def __init__(
        self,
        conn_factory: Callable[[], Any],
        logger,
        name: str = "qmt-writer",
        depth_warn: int = 1000,
        on_failure: Optional[Callable[[str], None]] = None,
    ):
        # conn_factory：返回写线程独占的 DB-API 连接（如 sqlite3.connect(...)）。
        self._conn_factory = conn_factory
        self._logger = logger
        self._name = name
        self._depth_warn = depth_warn
        # on_failure：存储不可用时的告警钩子（写线程已死/建连失败）。供上层触发停盘/运维告警,不抛错(不影响交易)。
        self._on_failure = on_failure
        self._q: "queue.Queue[WriteTask]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._conn: Any = None
        self._stop = threading.Event()
        self._started = False
        # 建连就绪 / 建连失败信号：start() 同步等待写线程成功建连,失败则 fail-fast（评审 medium#1/#2）。
        self._ready = threading.Event()
        self._failed = threading.Event()
        # 边沿触发的积压告警水位（评审 low#1：避免取模相等在并发下漏报）。
        self._warn_level = depth_warn
        # 「本批 commit 全成功」标志（仅写线程读写：任一任务 commit 失败置 False，屏障任务读后复位 True）。
        # 业务意图（评审二轮 P0#1）：flush()/unfinished_tasks 只表示"队列已 drain"，不感知 commit 成败——
        # 写任务 commit 抛错被 rollback 后仍 task_done()，使 flush 对落盘失败误报成功，发单前落盘保证失效。
        # flush_confirm 投递一个"屏障任务"（单写线程 FIFO，屏障必在此前所有任务执行完后才运行），屏障读取
        # 本标志即可判定"自上次确认以来这批写是否全部 commit 成功"，从而区分"已 drain"与"已持久"。
        # 单调复位口径：屏障读取后复位为 True，故 flush_confirm 设计为由单一调用方串行调用（下单/收盘等
        # 非热路径），并发调用至多偏保守（把别的批的失败算到本批，多判一次失败），绝不漏判。
        self._batch_ok = True

    def start(self, ready_timeout: float = 5.0) -> None:
        """启动写线程并【同步等待】其成功打开写连接。重复调用幂等。

        fail-fast（评审 medium#1/#2）：连接打不开 → start() 直接抛错,让进程启动即失败,
        绝不在「持久化全失效」状态下带病运行盘中交易（否则成交回报等会被静默丢弃）。
        """
        if self._started:
            return
        # 连接在写线程外创建会触发 sqlite3 线程检查，故连接在 worker 线程内打开（见 _run）。
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        # 同步等待建连结果（就绪或失败）。
        if not self._ready.wait(ready_timeout):
            raise RuntimeError(f"AsyncWriteQueue[{self._name}] 写线程建连超时,存储不可用")
        if self._failed.is_set():
            raise RuntimeError(f"AsyncWriteQueue[{self._name}] 写连接打开失败,存储不可用,进程不应在持久化失效下交易")
        self._started = True

    def submit(self, task: WriteTask) -> None:
        """入队一个写任务并立即返回（交易热路径调用，绝不阻塞）。

        边界：
        - 队列无界，`put_nowait` 永不阻塞；积压跨档告警（写线程卡住的可观测信号），但不丢任务。
        - 存活性校验（评审 medium#1/#2）：写线程已死 / 建连失败 → 强告警 + 告警钩子,但【不抛错、不入队】
          （不影响交易,且不往无消费者队列无界堆积；数据丢失在此被显式暴露而非静默）。正常运行下 start()
          已 fail-fast,不会走到此分支；此处兜底极端的运行期写线程意外死亡。
        """
        if not self._started:
            raise RuntimeError("AsyncWriteQueue 未 start()")
        if self._failed.is_set() or (self._thread is not None and not self._thread.is_alive()):
            self._logger.error("write_queue_dead_drop", name=self._name)
            if self._on_failure is not None:
                try:
                    self._on_failure("write_queue_dead")
                except Exception:
                    pass
            return
        self._q.put_nowait(task)
        depth = self._q.qsize()
        # 边沿触发：仅在跨过下一档阈值时告警一次（评审 low#1，取模相等在并发下会漏报）。
        if self._depth_warn and depth >= self._warn_level:
            self._logger.warn("write_queue_backlog", depth=depth, name=self._name)
            self._warn_level = ((depth // self._depth_warn) + 1) * self._depth_warn

    def is_healthy(self) -> bool:
        """写线程健康性（供 local_stack / Engine 周期性健康检查）。"""
        return self._started and not self._failed.is_set() and self._thread is not None and self._thread.is_alive()

    def set_on_failure(self, on_failure: Optional[Callable[[str], None]]) -> None:
        """装配后补设存储故障告警钩子（评审二轮 P0#2）。

        业务意图：on_failure 须指向 Engine 的 fail-closed 入口（置"存储不健康"→停开新仓+强告警），
        但 Engine 在 LocalStorage 之后才装配，构造期无法注入，故提供本 setter 在装配末尾接线。
        """
        self._on_failure = on_failure

    def _run(self) -> None:
        """写线程主循环：串行取任务、执行、commit；任一异常只吞不传播（交易不受影响）。"""
        try:
            self._conn = self._conn_factory()
        except Exception as e:  # 建连失败属存储级故障：标 failed + 解除 start() 等待,让 start() fail-fast
            self._logger.error("write_queue_conn_failed", error=repr(e), name=self._name)
            self._failed.set()
            self._ready.set()
            return
        # 建连成功：解除 start() 等待。
        self._ready.set()
        while True:
            # 退出条件：收到 stop 且队列已清空（保证 drain，不丢未写任务）。
            if self._stop.is_set() and self._q.empty():
                break
            try:
                task = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                task(self._conn)
                self._conn.commit()
            except Exception as e:
                # 关键不变量 2：写失败只记日志 + 回滚，绝不冒泡到交易线程。
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                # 标记本批存在 commit 失败（评审二轮 P0#1）：供 flush_confirm 的屏障任务据此判失败。
                self._batch_ok = False
                self._logger.error("async_write_failed", error=repr(e), name=self._name)
            finally:
                self._q.task_done()
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass

    def flush(self, timeout: float = 5.0) -> bool:
        """阻塞直到队列清空（含在途任务执行完）。盘后 / 对账前用，不在交易热路径。

        返回 True=已清空，False=超时（调用方据此告警，不抛错拖垮流程）。
        """
        end = time.monotonic() + timeout
        while self._pending() > 0:
            if time.monotonic() >= end:
                return False
            time.sleep(0.005)
        return True

    def flush_confirm(self, timeout: float = 5.0) -> bool:
        """阻塞等队列清空并【确认这批任务全部 commit 成功】（评审二轮 P0#1 / P0#2）。

        与 flush() 的区别：flush() 只看"队列已 drain"（unfinished_tasks==0），对 commit 失败无感知；
        本方法投递一个"屏障任务"——单写线程 FIFO，屏障必在此前所有任务执行完后才运行——屏障在写线程内
        读取标志 self._batch_ok（任一前序任务 commit 失败时已被置 False），并复位为 True 供下一批：
        - 屏障读到 _batch_ok==False → 这批里有任务落盘失败 → 返回 False（调用方据此 fail-closed / 拒发单）；
        - 屏障读到 _batch_ok==True  → 全部 commit 成功 → 返回 True。

        写线程已死/未建连：直接判失败（不必等超时）并触发 on_failure 告警 → 屏障不入队，返回 False。
        即"写线程死亡 + 关键落盘"场景下本方法必判失败，绝不误报成功。

        线程安全：本方法设计为由单一调用方（下单/收盘等非热路径）串行调用；屏障读取 _batch_ok 后复位 True，
        故并发调用至多偏保守（把别批失败算到本批），绝不漏判。
        """
        if not self._started:
            return False
        # 写线程已死：直接判失败（不必等超时），并触发故障告警钩子（→ 上层 fail-closed）。
        if self._failed.is_set() or (self._thread is not None and not self._thread.is_alive()):
            self._logger.error("write_queue_dead_on_confirm", name=self._name)
            if self._on_failure is not None:
                try:
                    self._on_failure("write_queue_dead")
                except Exception:  # noqa: BLE001 告警钩子异常不得影响调用方
                    pass
            return False
        ev = threading.Event()
        box: dict = {}

        def _barrier(_conn: Any) -> None:
            # 在写线程内、此前所有任务执行后运行：抓拍"本批 commit 是否全成功"，并复位标志供下一批，唤醒等待方。
            box["ok"] = self._batch_ok
            self._batch_ok = True
            ev.set()

        self._q.put_nowait(_barrier)
        if not ev.wait(timeout):
            return False
        # 屏障执行时本批无 commit 失败 → 这批关键写全部已持久。
        return bool(box.get("ok", False))

    def _pending(self) -> int:
        """未完成任务数（含已取出未 task_done 的在途任务）。"""
        # unfinished_tasks 是 queue.join 的依据，比 qsize 更准（qsize 不含在途任务）。
        return getattr(self._q, "unfinished_tasks", self._q.qsize())

    def depth(self) -> int:
        """当前队列深度（监控用）。"""
        return self._q.qsize()

    def stop(self, drain: bool = True, timeout: float = 5.0) -> None:
        """停机：可先 drain 清空队列，再停写线程并关连接（不在交易热路径）。"""
        if drain:
            self.flush(timeout)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._started = False
