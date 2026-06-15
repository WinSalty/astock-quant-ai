"""SQLite 持久化本地下单台账 PersistentLocalLedger（doc/05 §三 T2.2）。

业务意图：实现 contracts.LocalLedger 协议，在 InMemoryLocalLedger 之上叠加「重启幂等」与
「异步镜像落盘」两层能力，作为执行侧本地化存储栈里下单台账的生产实现：

- **内存为热路径权威**：所有读写先落内存（InMemoryLocalLedger，含 order_id 反查索引），
  交易热路径（下单决策、xttrader 回报回调）只碰内存 + 一次 put_nowait 入队，绝不在调用线程做磁盘 I/O。
- **SQLite 仅作异步镜像**：每次写内存后，把受影响的最新 entry 通过 AsyncWriteQueue 异步整行 replace 落盘
  （write-behind）。写失败由写队列「只吞不传播」，不波及交易线程。
- **重启幂等**：进程启动调用 load_from_db() 从 local_order_ledger 重建内存台账（含反查索引），
  重启后 has_active 仍有效 → 不会对同一计划重复下单（§关键不变量「重启幂等」）。

线程安全：内存权威被一把 RLock 保护（QMT 回调线程、决策线程、调度线程并发访问）。
**锁只护内存——锁内绝不做任何 SQLite I/O**：落盘动作只是 self._wq.submit(...) 入队即返回，
真正的磁盘写在独立的单写线程内串行执行，从根本上杜绝「持锁等盘」拖垮交易热路径。
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..contracts.enums import OrderState
from ..contracts.models import LedgerEntry
from ..order.local_ledger import InMemoryLocalLedger
from . import mappers, sqlite_sql
from .write_queue import AsyncWriteQueue

# 启动重建 / 盘后清理的默认保留天数（评审三轮 EXEC-storage-05）：load_from_db 只装载 target_trade_date 在
# [today-N, today] 的台账行；N 须覆盖最长在途周期——T+1 买入 + 跨周末/长假（A 股最长 9 天假），故取 14 天留余量。
DEFAULT_LEDGER_KEEP_DAYS = 14


class PersistentLocalLedger:
    """内存权威 + 异步镜像 + 启动重建的本地下单台账。实现 contracts.LocalLedger 协议。

    - db_path：本机 SQLite 文件路径（读用短连接 read_conn；写一律经 write_queue 的写线程连接）。
    - write_queue：已 start() 的异步写队列；本类只负责 submit，不负责其生命周期（由 local_stack 装配/停机）。
    - logger：结构化日志（重建/异常留痕）。
    """

    def __init__(self, db_path: str, write_queue: AsyncWriteQueue, logger):
        self._db_path = db_path
        self._wq = write_queue
        self._logger = logger
        # 内存权威：复用 InMemoryLocalLedger 的全部内存语义（幂等判定、成交去重、状态收口、反查索引）。
        # 注入 logger 供 order_id 跨日冲突告警（评审三轮 EXEC-order-06）。
        self._mem = InMemoryLocalLedger(logger=logger)
        # 可重入锁：保护内存权威的并发访问。RLock 允许同线程内嵌套调用（写方法内复用读方法时不自锁）。
        self._lock = threading.RLock()
        # 预生成台账整行 replace 的 SQL 与列序（单一来源，避免列序漂移）。biz_order_no 为 PK，整行覆盖最新内存态。
        self._replace_sql, self._replace_cols = sqlite_sql.build_replace("local_order_ledger")

    # ------------------------------------------------------------------
    # 启动重建（重启幂等的落地手段）
    # ------------------------------------------------------------------
    def load_from_db(
        self, today: Optional[date] = None, keep_days: int = DEFAULT_LEDGER_KEEP_DAYS
    ) -> None:
        """进程启动时从 SQLite 重建内存台账（含两套反查索引），并按成交明细重算 filled_volume。

        业务意图：上一进程异步镜像落盘的台账行，在本进程启动时读回内存，使重启后 has_active /
        get_by_order_id 立即有效 → 同一计划不会被重复下单（重启幂等）。
        窗口装载（评审三轮 EXEC-storage-05）：传入 today 时只装载 target_trade_date >= today-keep_days 的行
        （配合 purge_before 防 local_order_ledger 跨日只增不减、内存与 find_active 不随天数线性膨胀）；
        today=None 时退化为【全量装载】（向后兼容旧调用/单测）。窗口配合 EXEC-order-06 两级 order_id 索引，
        即便装载多日也不会跨日串单。
        明细重算（评审三轮 EXEC-order-01）：整行台账的 filled_volume 是 write-behind 累计快照，崩溃窗口重启会
        读回旧值；这里读 local_order_fill（append-only + 唯一键去重）按 biz 聚合重算 filled/均价/counted_trade_ids
        （以明细为权威），纠正快照偏差，使同一 traded_id 二次回报绝不二次累计。
        重跑口径：本方法应在对外提供读写前调用一次；用短连接读、读完即关，不占写线程。
        """
        cutoff_iso: Optional[str] = None
        if today is not None and keep_days is not None:
            cutoff_iso = (today - timedelta(days=keep_days)).isoformat()
        conn = sqlite_sql.read_conn(self._db_path)
        try:
            if cutoff_iso is not None:
                ledger_rows = conn.execute(
                    "SELECT * FROM local_order_ledger WHERE target_trade_date >= ?", (cutoff_iso,)
                ).fetchall()
            else:
                ledger_rows = conn.execute("SELECT * FROM local_order_ledger").fetchall()
            # 成交明细：一次性读回（明细表受 purge_before 约束、规模有界），在内存里按 biz 聚合。
            fill_rows = conn.execute(
                "SELECT biz_order_no, traded_id, traded_volume, traded_price FROM local_order_fill"
            ).fetchall()
        finally:
            conn.close()
        # 明细按 biz 聚合为 {biz: [(dedup_key, vol, price), ...]}。
        fills_by_biz: Dict[str, List[Tuple[str, int, Any]]] = {}
        for fr in fill_rows:
            fills_by_biz.setdefault(fr["biz_order_no"], []).append(
                (fr["traded_id"], fr["traded_volume"], fr["traded_price"])
            )
        count = 0
        with self._lock:
            for row in ledger_rows:
                entry = mappers.row_to_ledger(row)
                # 复用内存 insert：同时维护 _by_biz 与两套反查索引（与运行期写入同一口径）。
                self._mem.insert(entry)
                # 有明细的计划单：以明细重算 filled/counted（权威），纠正整行快照在崩溃窗口的偏差。
                detail = fills_by_biz.get(entry.biz_order_no)
                if detail:
                    self._mem.reconcile_fills_from_detail(entry.biz_order_no, detail)
                count += 1
        self._logger.info(
            "ledger_reload_from_db", count=count, db=self._db_path, window_cutoff=cutoff_iso
        )

    def purge_before(self, cutoff: date) -> None:
        """盘后清理：删除 target_trade_date < cutoff 的过期台账行及其成交明细（评审三轮 EXEC-storage-05）。

        归档=丢弃不再管：只清【明显过期】的终态行，cutoff 必须严格小于任何可能仍活跃的 target_trade_date
        （调用方传足够早的日期，如 today-DEFAULT_LEDGER_KEEP_DAYS），绝不误删当日/在途活跃单。
        先删明细（按将被清理的 biz）再删台账行；都经写队列在写线程串行执行，不阻塞热路径。本方法只清 SQLite，
        内存由下次重启 load_from_db 的窗口装载自然收敛。
        """
        cutoff_iso = cutoff.isoformat()
        # 第 1 步：删将被清理台账行对应的成交明细（子查询定位 biz）。
        self._wq.submit(
            lambda conn: conn.execute(
                "DELETE FROM local_order_fill WHERE biz_order_no IN "
                "(SELECT biz_order_no FROM local_order_ledger WHERE target_trade_date < ?)",
                (cutoff_iso,),
            )
        )
        # 第 2 步：删过期台账行本身。
        self._wq.submit(
            lambda conn: conn.execute(
                "DELETE FROM local_order_ledger WHERE target_trade_date < ?", (cutoff_iso,)
            )
        )
        self._logger.info("ledger_purge_before", cutoff=cutoff_iso, db=self._db_path)

    # ------------------------------------------------------------------
    # 内部：把单条最新内存 entry 异步镜像落盘（write-behind，绝不在锁内做 I/O）
    # ------------------------------------------------------------------
    def _mirror(self, entry: Optional[LedgerEntry]) -> None:
        """把一条 entry 整行 replace 异步落盘。entry 为 None（未知单）则跳过，与内存语义一致。

        线程安全：本方法只构造行参数 + submit 入队，submit 内部 put_nowait 立即返回；
        真正写盘在写线程内执行。即便本方法在持锁期间被调用，也不会发生磁盘 I/O，故不阻塞交易热路径。
        """
        if entry is None:
            return
        row = mappers.ledger_to_row(entry)
        # 按预生成列序取参数，闭包捕获本次的 sql/params；写线程内 conn.execute 一次整行覆盖。
        params = [row[c] for c in self._replace_cols]
        sql = self._replace_sql
        self._wq.submit(lambda conn: conn.execute(sql, params))

    # ------------------------------------------------------------------
    # 读方法：with 锁委托内存（只读内存，快、线程安全；不碰 SQLite）
    # ------------------------------------------------------------------
    def has_active(self, target_trade_date: date, ts_code: str, strategy_family: str) -> bool:
        """幂等判定：同 (target_trade_date, ts_code, strategy_family) 有活跃单则 True（只读内存）。"""
        with self._lock:
            return self._mem.has_active(target_trade_date, ts_code, strategy_family)

    def find_active(
        self, target_trade_date: date, ts_code: str, strategy_family: str
    ) -> Optional[LedgerEntry]:
        with self._lock:
            return self._mem.find_active(target_trade_date, ts_code, strategy_family)

    def get(self, biz_order_no: str) -> Optional[LedgerEntry]:
        with self._lock:
            return self._mem.get(biz_order_no)

    def get_by_order_id(self, order_id: int) -> Optional[LedgerEntry]:
        with self._lock:
            return self._mem.get_by_order_id(order_id)

    def all_for_date(self, target_trade_date: date) -> List[LedgerEntry]:
        with self._lock:
            return self._mem.all_for_date(target_trade_date)

    def all(self) -> List[LedgerEntry]:
        with self._lock:
            return self._mem.all()

    # ------------------------------------------------------------------
    # 写方法：with 锁先改内存（权威），再把受影响的最新 entry 异步镜像落盘
    # ------------------------------------------------------------------
    def insert(self, entry: LedgerEntry) -> None:
        """写入新计划单：内存权威落定后镜像该 entry。重复 biz_order_no 覆盖（同号同计划，幂等）。"""
        with self._lock:
            self._mem.insert(entry)
            # 镜像内存里的权威副本（self._mem.get 取深拷贝，避免外部对入参的后续改动污染落盘行）。
            self._mirror(self._mem.get(entry.biz_order_no))

    def flush_pending(self, timeout: float = 5.0) -> bool:
        """同步等待已入队的镜像写全部落盘（评审 P0-C3）。

        业务意图：write-behind 下 insert 仅把 PLANNED 行投递给异步写队列即返回，若在"已发券商委托、
        但该行尚未 drain 落盘"的窗口崩溃，重启 load_from_db 读不到该行 → 同一计划被重复下单。
        故唯一下单点在 order_stock 之前调用本方法，阻塞等待写队列清空，保证"磁盘有计划单"先于
        "券商收到委托"。仅用于发单前的关键落盘（非回报热路径），返回是否在 timeout 内清空。

        评审二轮 P0#1/#2：必须用 flush_confirm（区分"已 drain"与"已持久 commit 成功"），不能用 flush——
        flush 对 commit 失败/写线程死亡会误报成功，使发单前落盘保证形同虚设、崩溃后重复下单。
        """
        return self._wq.flush_confirm(timeout)

    def is_healthy(self) -> bool:
        """透传写队列健康（评审三轮 EXEC-storage-01）：写线程存活 + 未失败 + 最近写成功。

        供 OrderExecutor 在 flush_pending 返回 False 时区分两类：写线程死亡/commit 失败（确定性写丢失，
        应 fail-closed 拒发单）vs 纯超时（队列拥堵但线程健康，可继续下单 + 强告警）。
        """
        return self._wq.is_healthy()

    def update(self, biz_order_no: str, **fields: Any) -> None:
        """按字段更新台账行：内存改完后镜像该 biz 的最新 entry。biz 不存在时内存层抛 KeyError（沿用语义）。"""
        with self._lock:
            self._mem.update(biz_order_no, **fields)
            self._mirror(self._mem.get(biz_order_no))

    def sync_status(self, order_id: int, state: OrderState, msg: Optional[str] = None) -> None:
        """按 order_id 同步委托状态：内存收口后镜像。

        边界：台账无该 order_id（手工单/非本系统单）时内存层直接忽略——此处先取 get_by_order_id，
        存在才镜像，不存在则不落盘，与内存「未知单忽略」语义完全一致（不臆造行）。
        """
        with self._lock:
            self._mem.sync_status(order_id, state, msg)
            # 取 order_id 反查到的最新 entry；为 None 即未知单，_mirror 内部跳过。
            self._mirror(self._mem.get_by_order_id(order_id))

    def add_fill(
        self, order_id: int, traded_id: Any, traded_volume: int, traded_price: Any
    ) -> None:
        """累计成交：内存按 traded_id 去重 + 推进状态后镜像该 entry。

        签名与 InMemoryLocalLedger 对齐：(order_id, traded_id, traded_volume, traded_price)。
        幂等：同 traded_id 重投在内存层已被去重（filled_volume 不翻倍），镜像的是去重后的权威态。
        边界：未知 order_id / 无效成交量在内存层忽略；此处仅在 entry 存在时镜像（_mirror 对 None 跳过）。

        成交明细落盘（评审三轮 EXEC-order-01）：内存 add_fill 真正【新计入】一笔时返回 (biz, dedup_key, vol, price)，
        据此把该笔写入 local_order_fill（INSERT OR IGNORE，append-only 幂等）；崩溃重启 load_from_db 据明细重算
        filled_volume，杜绝整行快照在崩溃窗口被同一 traded_id 二次回报二次累计。被去重/忽略时返回 None，不落明细。
        """
        with self._lock:
            recorded = self._mem.add_fill(order_id, traded_id, traded_volume, traded_price)
            self._mirror(self._mem.get_by_order_id(order_id))
            if recorded is not None:
                biz, dedup_key, vol, price = recorded
                price_text = str(price) if price is not None else None
                # INSERT OR IGNORE：同 (biz, dedup_key) 重投天然幂等；闭包捕获本次参数，写线程内执行。
                self._wq.submit(
                    lambda conn: conn.execute(
                        "INSERT OR IGNORE INTO local_order_fill"
                        "(biz_order_no, order_id, traded_id, traded_volume, traded_price) "
                        "VALUES (?,?,?,?,?)",
                        (biz, order_id, dedup_key, vol, price_text),
                    )
                )
