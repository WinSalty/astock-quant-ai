"""本机 SQLite watchlist 取数源（doc/05 §三 storage/watchlist_source，T2.3）。

业务意图：执行侧本地化改造后，盘中下单决策不再跨服务器直连信号侧 MySQL 读
``limit_up_selected_stock``，而是改为读**本机 SQLite 的 `watchlist` 表**——名单由盘前
一次性同步入库（``save_watchlist``），盘中 ``fetch`` 只读本机库，彻底规避盘中网络抖动波及交易。

本类实现 ``contracts.SelectedStockSource`` 协议，与 sources.py 的 A/B 两路（DB 直读 / HTTP）
返回同一契约（``list[SelectedStockRow]``），对上层 ``WatchlistLoader`` 等价、可互为来源；
区别仅在「数据从哪来」——本类的来源是已落地的本机 SQLite，延迟最低、不依赖跨网络。

读写分工与不变量：
- ``fetch``：盘中只读路径。用 ``sqlite_sql.read_conn`` 开**短连接**（WAL 下不阻塞写线程），
  读完即关；任何 SQLite 读异常一律包成 ``WatchlistLoadError`` 上抛，由 loader 据此降级
  「只守仓、不开新仓」，**绝不让异常逸出拖垮常驻进程**（§2.6）。
- ``save_watchlist``：盘前同步入路径（**非交易热路径**）。此刻交易/写线程尚未活跃，故用本方法
  自己的 ``sqlite3.connect`` **直连同步写**最简，不走 AsyncWriteQueue（写队列服务于盘中热路径，
  盘前直写无并发压力、且需要拿到「写入行数」做同步对账）。表已由 ``init_db`` 建好，本方法不建表。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import List

from . import mappers
from . import sqlite_sql
from ..contracts.errors import WatchlistLoadError
from ..contracts.models import SelectedStockRow


class SqliteSelectedStockSource:
    """从本机 SQLite ``watchlist`` 表取当日名单的取数源（实现 contracts.SelectedStockSource）。

    依赖注入：
    - db_path：本机 SQLite 文件路径（与写队列 / repository 同一库文件）；
    - logger：结构化日志（读失败 / 盘前写入留痕，不含敏感信息）。
    """

    def __init__(self, db_path: str, logger):
        # db_path 全程只读不变；读连接每次 fetch 现开现关（短连接），写连接在 save_watchlist 内开。
        self._db_path = db_path
        self._logger = logger

    # ------------------------------------------------------------------
    # 盘中只读：fetch
    # ------------------------------------------------------------------
    def fetch(self, target_trade_date: date) -> List[SelectedStockRow]:
        """整批取 ``target_trade_date=today`` 的当日候选行（§2.4 _fetch_selected_stocks）。

        口径与边界：
        - 入参 date 以 ``isoformat()`` 作为查询参数，与建表 / mappers 的 ISO 文本口径一致；
        - 读连接用 ``sqlite_sql.read_conn``（短连接，WAL 下不阻塞写线程），读完在 finally 关闭；
        - 无匹配行 → 返回 ``[]``（**合法**：当日确实无候选，由 loader 当作空名单，不是失败）；
        - 任何 SQLite 读异常 → 包成 ``WatchlistLoadError`` 上抛（保留原异常链），由 loader 走降级，
          绝不静默吞、也绝不让异常逸出拖垮常驻进程。
        """
        conn = None
        try:
            conn = sqlite_sql.read_conn(self._db_path)
            # 按 target_trade_date 精确查当日名单；SELECT * 列由 row_to_selected 按列名解码（顺序无关）。
            cursor = conn.execute(
                "SELECT * FROM watchlist WHERE target_trade_date = ?",
                (target_trade_date.isoformat(),),
            )
            rows = cursor.fetchall()
            # 行 → 契约 dataclass 的解码也放进 try（评审 medium#4）：脏数据解码异常同样收敛为
            # WatchlistLoadError，绝不让它逸出 fetch 绕过 loader 降级 / 拖垮常驻进程。
            # mappers 单一来源：TEXT→Decimal、ISO→date、JSON→list 等无损解码。
            return [mappers.row_to_selected(row) for row in rows]
        except Exception as exc:  # noqa: BLE001 SQLite 读/解码异常种类繁多，统一收敛为约定异常交 loader 降级
            # 读/解码失败留痕（不含敏感信息：只记日期与异常摘要）。
            self._logger.warn(
                "watchlist_sqlite_fetch_failed",
                target_trade_date=target_trade_date.isoformat(),
                reason=repr(exc),
            )
            raise WatchlistLoadError(
                f"sqlite watchlist fetch failed for {target_trade_date.isoformat()}: {exc}"
            ) from exc
        finally:
            # 短连接用完即关；关闭失败不影响已取数据，吞掉即可（避免 finally 覆盖业务异常）。
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 关闭失败无害，不向上传播
                    pass

    # ------------------------------------------------------------------
    # 盘前同步入：save_watchlist
    # ------------------------------------------------------------------
    def save_watchlist(self, rows: List[SelectedStockRow]) -> int:
        """盘前把当日名单整批写入本机 ``watchlist`` 表，返回写入行数（§4.1 盘前同步入）。

        latest-wins 语义（同一 ``target_trade_date`` 后到全量覆盖先到）：
        1) 先按 rows 中出现的所有 ``target_trade_date`` 做 ``DELETE FROM watchlist WHERE
           target_trade_date IN (...)``——清掉这些日期下的旧名单，保证「重跑盘前同步 = 当日名单
           整体替换」，旧集合里被新集合剔除的票（如不再入选的 A）不会残留；
        2) 再用 ``sqlite_sql.build_replace("watchlist")`` + ``mappers.selected_to_row`` 批量
           ``INSERT OR REPLACE`` 写入新集合（REPLACE 兜底单批内同 (ts_code, target_trade_date)
           重复行，取最后一条，幂等）；
        3) 一次性 ``commit``（盘前无并发，单事务最简且要么全成要么全不成）。

        边界 / 口径：
        - 表已由 ``init_db`` 建好，本方法**不建表**；
        - rows 为空 → 直接返回 0（不开事务、不误删任何日期的名单）；
        - **不走 AsyncWriteQueue**：盘前交易/写线程尚未活跃，直连同步写最简，且需返回真实写入行数
          供盘前同步对账（write-behind 入队即返回拿不到行数）；
        - 写失败由调用方（盘前同步流程）处理为「当日无名单 → 降级只守仓」，本方法不静默吞异常。
        """
        # 空入参早返回：无任何 target_trade_date，既不删旧名单也不写新行，返回 0。
        if not rows:
            return 0

        # 收集本批涉及的 target_trade_date（去重）：latest-wins 只清这些日期，不动其它日期的名单。
        target_dates = sorted(
            {r.target_trade_date.isoformat() for r in rows if r.target_trade_date is not None}
        )

        # 预先把每行序列化为按 TABLE_META 列序排好的参数（mappers 单一来源，保证列序一致）。
        replace_sql, _cols = sqlite_sql.build_replace("watchlist")
        params_list = [sqlite_sql.params_for("watchlist", mappers.selected_to_row(r)) for r in rows]

        conn = sqlite3.connect(self._db_path)
        try:
            # latest-wins 第 1 步：删掉本批涉及日期下的全部旧名单（按 IN 列表，占位防注入）。
            if target_dates:
                placeholders = ", ".join(["?"] * len(target_dates))
                conn.execute(
                    f"DELETE FROM watchlist WHERE target_trade_date IN ({placeholders})",
                    target_dates,
                )
            # 第 2 步：批量写入新集合（executemany 单语句多行，盘前一次性入库）。
            conn.executemany(replace_sql, params_list)
            # 第 3 步：单事务提交（要么全成要么全不成，避免半截名单流入盘中）。
            conn.commit()
        except Exception:
            # 写失败回滚，原样上抛交盘前同步流程处理为降级（不静默吞，便于告警与重试）。
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 回滚失败无害，不掩盖原始写异常
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 关闭失败无害
                pass

        written = len(params_list)
        # 盘前同步入留痕：记写入行数与涉及日期，供盘前同步对账（本地条数 vs 期望条数）。
        self._logger.info(
            "watchlist_saved",
            written=written,
            target_trade_dates=target_dates,
        )
        return written
