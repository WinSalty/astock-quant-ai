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
        fail_after: int = 5,
        max_queue: int = 0,
        stuck_seconds: float = 0.0,
    ):
        # conn_factory：返回写线程独占的 DB-API 连接（如 sqlite3.connect(...)）。
        self._conn_factory = conn_factory
        self._logger = logger
        self._name = name
        self._depth_warn = depth_warn
        # 持续 I/O 故障熔断（评审三轮 EXEC-storage-03）：磁盘满时 commit 持续抛 OSError 被吞、写线程不死、
        # _failed 不 set、is_healthy 误报健康、on_failure 不触发 → 持续静默丢数据。这里对「连续 commit 失败」
        # 计数超阈值即 _failed.set()+on_failure(区分瞬时锁等待与持续 I/O 故障)。
        self._fail_after = fail_after
        self._consecutive_fail = 0       # 仅写线程读写
        # 队列长度硬上限（>0 启用）：写线程阻塞挂起(如 fsync 永久卡死)时防无界堆积内存熔断，超限触发 on_failure。
        self._max_queue = max_queue
        # 卡死看门狗阈值秒（评审 F09，>0 启用）：写线程卡在永不返回的 I/O(fsync/NFS hang)时，commit 既不成功也不
        # 抛错，_last_write_ok 永停在卡死前的 True、_failed 不 set、线程 is_alive 仍真 → is_healthy 恒误报健康、
        # fail-closed 永不触发。这里记「最近一次任务推进时刻」，若有积压(pending>0)却连续该秒数无推进即判 hang。
        self._stuck_seconds = stuck_seconds
        # 最近一次任务处理完成(成功/失败均算推进)的单调时刻；仅写线程写、健康检查线程读(float 读写原子，不引锁)。
        self._last_progress_monotonic = time.monotonic()
        # 最近一次写是否成功（仅写线程写、健康检查线程读；CPython bool 读写原子，不引锁以免污染热路径）。
        # 让 is_healthy 纳入「最近写成功」而非只看线程存活，否则磁盘满下写线程存活但持续丢数据仍误报健康。
        self._last_write_ok = True
        # E-4（评审 doc/24）：健康位 sticky 门闩——任一写失败即置 degraded，**单次成功不清零**，需连续
        # _fail_after 次成功（持续恢复）才解除。否则「失败、成功、失败、成功…」间歇丢数据时 _consecutive_fail
        # 永攒不到阈值、is_healthy 在 True/False 间抖动、storage_health_tick 恰落「刚成功」即漏判，而期间失败的
        # 成交/状态写已被 rollback 丢弃（write-behind 不重投）→ 台账与券商不一致却无人察觉。
        self._write_degraded = False     # sticky：任一写失败置 True
        self._healthy_streak = 0         # 连续成功计数，达 _fail_after 才解除 degraded
        # 累计写失败数（执行-R10 修正 2026-06-22）：永不因成功清零，用于区分「单次瞬时抖动」与「持续/间歇丢数据」。
        # 周期体检的 latching 永久 fail-closed 在「当前仍 degraded 且累计失败已达 _fail_after」时触发——既不被一次
        # 无害瞬时 blip 冻结(执行-4 诉求)，又能堵住「失败/成功交替」式间歇丢数据(E-4 诉求，此前被本轮折中漏掉)。
        self._total_fail = 0
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
            self._invoke_on_failure("write_queue_dead")
            return
        # 队列上限熔断（评审三轮 EXEC-storage-03）：写线程阻塞挂起(如 fsync 永久卡死)时无界堆积会内存爆掉；
        # max_queue>0 且积压超限时显式丢弃本任务 + 强告警 + on_failure，绝不静默无界堆积。
        if self._max_queue > 0 and self._q.qsize() >= self._max_queue:
            # 溢出即丢弃本任务——但丢弃的可能是「发单前 PLANNED 关键落盘」。若此时 is_healthy 仍报健康，
            # _persist_critical_before_order 会在「flush_confirm 超时 + 健康」下放行下单（评审 doc/24 E-1），
            # 导致券商已收委托而本机 SQLite 无台账行、崩溃重启读不到 → 同一计划重复下单（真金重复建仓）。
            # 故溢出视为存储级 fail-closed 故障：置 _last_write_ok=False + set _failed，使 is_healthy 立即转 False、
            # flush_confirm 立即判失败（line 255 已先检 _failed），当前这笔发单前关键落盘即被拒发；并触发 on_failure
            # 告警停开新仓。_failed 一旦置位即持久 fail-closed（队列打到上限=持久化已严重崩坏，本日不再带病交易）。
            self._logger.error("write_queue_overflow", depth=self._q.qsize(), name=self._name)
            self._last_write_ok = False
            self._failed.set()
            self._invoke_on_failure("write_queue_overflow")
            return
        self._q.put_nowait(task)
        depth = self._q.qsize()
        # 边沿触发：仅在跨过下一档阈值时告警一次（评审 low#1，取模相等在并发下会漏报）。
        if self._depth_warn and depth >= self._warn_level:
            self._logger.warn("write_queue_backlog", depth=depth, name=self._name)
            self._warn_level = ((depth // self._depth_warn) + 1) * self._depth_warn

    def _invoke_on_failure(self, reason: str) -> None:
        """触发存储故障告警钩子（评审三轮 EXEC-storage-03）；全程吞异常，绝不影响交易线程/写线程。"""
        if self._on_failure is None:
            return
        try:
            self._on_failure(reason)
        except Exception:  # noqa: BLE001 告警钩子异常不传播
            pass

    def is_healthy(self) -> bool:
        """写线程健康性（供 local_stack / Engine 周期性健康检查）。

        纳入「最近一次写是否成功」（评审三轮 EXEC-storage-03）：否则磁盘满下写线程存活但持续 commit 失败丢数据，
        旧实现只看线程存活仍误报健康、fail-closed 永不触发。
        """
        return (
            self._started
            and not self._failed.is_set()
            and self._thread is not None
            and self._thread.is_alive()
            and self._last_write_ok
            and not self._write_degraded
            and not self._is_stuck()
        )

    def is_persistently_failed(self) -> bool:
        """持续性/致命存储故障（供 Engine 周期体检的【latching 永久 fail-closed】用，执行-4 修正 2026-06-22）。

        以下任一为真即【确定性/持续性故障】：未启动 / 写线程死亡 / 建连或连续 commit 失败已熔断(_failed) /
        看门狗判卡死 / **当前仍 degraded 且累计失败已达 _fail_after**（持续或间歇丢数据，执行-R10 补）。
        【刻意不含】单次写瞬时失败造成、且累计失败未达阈值的 sticky degraded——那是瞬时抖动（如一笔无关紧要的
        镜像写撞 5 秒锁超时），不应让 15 秒一次的体检把它 latch 成永久不可用、冻结当天剩余开仓(执行-4 诉求)。
        但「失败、成功、失败、成功…」式间歇丢数据会让 _write_degraded 持续为真、_total_fail 不断累加，达 _fail_after
        即在此 latch fail-closed（堵住 E-4 漏洞：本轮把全部 degraded 一并移出周期 latch 过粗，这里按累计失败补回）。
        瞬时 degraded 仍由 is_healthy()=False 反映，供【发单前关键落盘】等可恢复的逐单检查使用——逐单只挡当前这笔、
        随写恢复自动放行，不 latch。
        """
        return (
            not self._started
            or self._failed.is_set()
            or self._thread is None
            or not self._thread.is_alive()
            or self._is_stuck()
            or (self._write_degraded and self._total_fail >= self._fail_after)
        )

    def _is_stuck(self) -> bool:
        """写线程卡死判定（评审 F09）：启用看门狗(stuck_seconds>0) 且有积压(pending>0) 且连续 stuck_seconds
        无任务推进 → 视为卡死。idle(无积压)时不判卡死(空闲无推进属正常)。"""
        if self._stuck_seconds <= 0:
            return False
        if self._pending() <= 0:
            return False
        return (time.monotonic() - self._last_progress_monotonic) > self._stuck_seconds

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
        # 建连成功：解除 start() 等待；看门狗基线置为「刚就绪」时刻（避免建连耗时被算成无进展）。
        self._ready.set()
        self._last_progress_monotonic = time.monotonic()
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
                # 写成功：清零连续失败计数 + 置最近写成功（评审三轮 EXEC-storage-03）。
                self._consecutive_fail = 0
                self._last_write_ok = True
                # E-4 sticky 恢复：需连续 _fail_after 次成功（持续恢复）才解除 degraded，单次成功不清零。
                self._healthy_streak += 1
                if self._write_degraded and self._healthy_streak >= self._fail_after:
                    self._write_degraded = False
            except Exception as e:
                # 关键不变量 2：写失败只记日志 + 回滚，绝不冒泡到交易线程。
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                # 标记本批存在 commit 失败（评审二轮 P0#1）：供 flush_confirm 的屏障任务据此判失败。
                self._batch_ok = False
                self._logger.error("async_write_failed", error=repr(e), name=self._name)
                # 持续 I/O 故障熔断（评审三轮 EXEC-storage-03）：连续 commit 失败计数；置最近写失败；达阈值
                # 即 _failed.set()+on_failure，让 is_healthy 转 False、上层 fail-closed（区分瞬时锁等待与持续故障）。
                self._consecutive_fail += 1
                self._total_fail += 1  # 累计失败（执行-R10）：永不清零，供周期体检判「持续/间歇丢数据」
                self._last_write_ok = False
                # E-4：任一写失败即 sticky 置 degraded + 清零成功连击；is_healthy 据此持续 False 直到持续恢复。
                self._write_degraded = True
                self._healthy_streak = 0
                if self._consecutive_fail >= self._fail_after and not self._failed.is_set():
                    self._failed.set()
                    self._logger.error(
                        "write_queue_persist_failed",
                        consecutive_fail=self._consecutive_fail, name=self._name,
                    )
                    self._invoke_on_failure("write_persist_failed")
            finally:
                self._q.task_done()
                # 看门狗推进时刻（评审 F09）：每处理完一个任务(无论成功/失败)即刷新，使 is_healthy 能据
                # 「有积压却长时间无推进」识别写线程卡死；卡在 task()/commit() 内则到不了这里、时刻停滞→判 hang。
                self._last_progress_monotonic = time.monotonic()
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
