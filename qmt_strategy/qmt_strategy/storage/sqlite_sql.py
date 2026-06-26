"""SQLite SQL 构造与读连接助手（doc/05 §三，repo/ledger/sync 共用，保证 SQL 单一来源）。

业务意图：把「按 TABLE_META 生成幂等 upsert / 全行 replace / 读连接」的逻辑收敛到一处，
避免 sqlite_repository / sync_job / sqlite_ledger 各写一套 SQL 导致列序或 COALESCE 口径漂移。
"""

from __future__ import annotations

import sqlite3
from typing import List, Tuple

from .schema import TABLE_META, apply_pragmas


def read_conn(db_path: str) -> sqlite3.Connection:
    """打开一个只读用途连接（row_factory=Row，便于按列名取值）。

    WAL 模式下读连接不阻塞写线程；busy_timeout 兜底极端并发。读完由调用方关闭。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def params_for(table: str, row: dict) -> list:
    """按 TABLE_META 列序从 to_row 字典取参数（与 build_upsert/build_replace 的占位顺序一致）。"""
    return [row[c] for c in TABLE_META[table]["columns"]]


def build_upsert(table: str) -> Tuple[str, List[str]]:
    """生成幂等 upsert（§6.5 写入语义）：INSERT ... ON CONFLICT(unique) DO UPDATE。

    口径：
    - 写 qmt_* 时连带把 synced 置 0（任何写入都标记「待同步回远端」，由 sync_job 置 1）；非 qmt_* 表无 synced 列；
    - UPDATE 段：唯一键列不更新；coalesce 列用 COALESCE(excluded.col, table.col)（已回填值不被空覆盖）；其余 col=excluded.col。
    返回 (sql, columns)；调用方用 params_for(table, row) 取参数（顺序与 columns 一致）。
    """
    meta = TABLE_META[table]
    cols: List[str] = list(meta["columns"])
    unique = meta["unique"]
    coalesce = set(meta["coalesce"])
    has_synced = table in ("qmt_trade", "qmt_order", "qmt_position_snapshot", "qmt_account_daily")

    insert_cols = list(cols) + (["synced"] if has_synced else [])
    placeholders = ", ".join(["?"] * len(cols) + (["0"] if has_synced else []))
    col_sql = ", ".join(insert_cols)

    sets = []
    for c in cols:
        if c in unique:
            continue  # 唯一键列不进 UPDATE
        if c in coalesce:
            sets.append(f"{c}=COALESCE(excluded.{c}, {table}.{c})")
        else:
            sets.append(f"{c}=excluded.{c}")
    if has_synced:
        sets.append("synced=0")  # 数据变更 → 重新标记待同步（再同步对远端幂等，安全）
        # 乐观锁版本号自增（评审修复 SYNC-1）：每次 upsert（含盘后同步窗内迟到回报覆盖本行）都把 row_version+1。
        # 盘后 mark_synced 以「SELECT 时读到的 row_version」做 CAS 守卫——若读后该行被改写过（版本已变），
        # mark_synced 命中 0 行、不会把「未推到远端的最新数据」误标为已同步（原仅 synced=0 守卫挡不住 0→0 的重写）。
        sets.append(f"row_version = {table}.row_version + 1")

    sql = (
        f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(unique)}) DO UPDATE SET {', '.join(sets)}"
    )
    return sql, cols


def build_replace(table: str) -> Tuple[str, List[str]]:
    """生成全行 replace（INSERT OR REPLACE，按 PK/唯一键整行覆盖）。

    用于本地下单台账（local_order_ledger，按 biz_order_no 整行写最新内存态）。
    返回 (sql, columns)，参数顺序与 columns 一致。
    """
    cols: List[str] = list(TABLE_META[table]["columns"])
    placeholders = ", ".join(["?"] * len(cols))
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    return sql, cols
