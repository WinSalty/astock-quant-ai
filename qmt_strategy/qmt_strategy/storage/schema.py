"""本机 SQLite 库表结构与表元数据（doc/05 §三）。

业务意图：执行侧本地化数据栈的「库口径」单一来源。本机 SQLite 存：
- `qmt_trade` / `qmt_order` / `qmt_position_snapshot` / `qmt_account_daily`：回流四表的本地副本
  （列与远端 MySQL DDL 一一对应，便于盘后「同 schema 搬行」同步回远端）；多一列 `synced` 标记是否已同步回远端；
- `local_order_ledger`：本地下单台账（持久化，进程重启可重建幂等）；
- `watchlist`：信号侧盘前交付的当日候选清单（执行侧盘中只读它，不再跨网络读远端）。

存储口径（杜绝精度/时区坑）：
- 价位 / 金额一律存 TEXT（`str(Decimal)`，避免 SQLite REAL 的 float 二进制误差）；
- 日期 / 时间一律存 ISO 字符串（date.isoformat / datetime.isoformat），与「UTC naive + east8 原值」口径一致，不二次换算；
- 枚举存其字符串值；布尔存 0/1；列表 / 任意对象存 JSON 文本。

并发口径：WAL 模式（1 写 N 读），库由**单一后台写线程**写入（write_queue），读连接各自短连接，避免锁竞争。
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List

# —— 表元数据：columns（INSERT 列序）、unique（唯一键列）、coalesce（后到不空覆盖列，§6.5）——
# repo / mappers 共用本元数据，保证「列定义单一来源」，避免两处漂移。
TABLE_META: Dict[str, Dict[str, List[str]]] = {
    "qmt_trade": {
        "columns": [
            "account_id", "account_type", "trade_date", "ts_code", "qmt_stock_code", "traded_id",
            "order_id", "order_sysid", "trade_side", "offset_flag", "traded_price", "traded_volume",
            "traded_amount", "traded_time", "traded_time_east8", "strategy_name", "order_remark",
            "signal_trade_date", "data_source",
        ],
        "unique": ["account_id", "trade_date", "traded_id"],
        # 已回填的 signal_trade_date / *_east8 不被后到空值覆盖（COALESCE 口径）。
        "coalesce": ["signal_trade_date", "traded_time_east8"],
    },
    "qmt_order": {
        "columns": [
            "account_id", "account_type", "trade_date", "ts_code", "qmt_stock_code", "order_id",
            "order_sysid", "trade_side", "offset_flag", "price_type", "order_price", "order_volume",
            "traded_volume", "traded_price", "order_status", "status_msg", "error_id", "error_msg",
            "cancel_failed", "order_time", "order_time_east8", "strategy_name", "order_remark",
            "signal_trade_date", "data_source",
        ],
        "unique": ["account_id", "trade_date", "order_id"],
        "coalesce": ["signal_trade_date", "order_time_east8"],
    },
    "qmt_position_snapshot": {
        "columns": [
            "account_id", "account_type", "trade_date", "snapshot_type", "ts_code", "qmt_stock_code",
            "volume", "can_use_volume", "frozen_volume", "on_road_volume", "yesterday_volume",
            "open_price", "avg_price", "market_value", "last_price", "float_profit", "profit_rate",
            "data_source",
        ],
        "unique": ["account_id", "trade_date", "ts_code", "snapshot_type"],
        "coalesce": [],
    },
    "qmt_account_daily": {
        "columns": [
            "account_id", "account_type", "trade_date", "snapshot_type", "total_asset", "cash",
            "frozen_cash", "market_value", "net_cash_flow", "prev_total_asset", "daily_pnl",
            "daily_return", "cash_flow_note", "data_source",
        ],
        "unique": ["account_id", "trade_date", "snapshot_type"],
        "coalesce": [],
    },
    "local_order_ledger": {
        "columns": [
            "biz_order_no", "account_id", "target_trade_date", "ts_code", "strategy_family", "side",
            "plan_volume", "plan_price", "order_remark", "signal_trade_date", "state", "order_id",
            "filled_volume", "avg_filled_price", "order_phase", "cancelable", "miss_reason",
            "cancel_failed", "error_id", "error_msg", "created_at", "updated_at", "counted_trade_ids",
        ],
        "unique": ["biz_order_no"],
        "coalesce": [],
    },
    "watchlist": {
        "columns": [
            "ts_code", "trade_date", "target_trade_date", "leader_strength_score", "role", "strategy",
            "market_state", "tradable_flag", "continuation_prob", "next_day_premium_prob", "boost",
            "fail_conditions", "signal_close", "limit_up_price", "reasonable_open_high_low",
            "reasonable_open_high_high", "first_board_vol", "float_mktcap", "strategy_family", "setup",
            "name", "is_st",
        ],
        "unique": ["ts_code", "target_trade_date"],
        "coalesce": [],
    },
}

# 回流四表（带 synced 标志、参与盘后同步的表）。
QMT_TABLES = ("qmt_trade", "qmt_order", "qmt_position_snapshot", "qmt_account_daily")

# —— 建表 DDL（SQLite 方言）——
# 说明：价位/金额 TEXT、时间 ISO TEXT、布尔 INTEGER；qmt_* 多 synced 列（0 未同步 / 1 已同步回远端）。
_DDL: Dict[str, str] = {
    "qmt_trade": """
        CREATE TABLE IF NOT EXISTS qmt_trade (
            account_id TEXT NOT NULL, account_type INTEGER, trade_date TEXT NOT NULL,
            ts_code TEXT, qmt_stock_code TEXT, traded_id TEXT NOT NULL, order_id INTEGER,
            order_sysid TEXT, trade_side TEXT NOT NULL, offset_flag INTEGER,
            traded_price TEXT, traded_volume INTEGER, traded_amount TEXT,
            traded_time TEXT, traded_time_east8 TEXT, strategy_name TEXT, order_remark TEXT,
            signal_trade_date TEXT, data_source TEXT NOT NULL DEFAULT 'CALLBACK',
            synced INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE (account_id, trade_date, traded_id)
        )""",
    "qmt_order": """
        CREATE TABLE IF NOT EXISTS qmt_order (
            account_id TEXT NOT NULL, account_type INTEGER, trade_date TEXT NOT NULL,
            ts_code TEXT, qmt_stock_code TEXT, order_id INTEGER NOT NULL, order_sysid TEXT,
            trade_side TEXT NOT NULL, offset_flag INTEGER, price_type INTEGER, order_price TEXT,
            order_volume INTEGER, traded_volume INTEGER NOT NULL DEFAULT 0, traded_price TEXT,
            order_status TEXT NOT NULL, status_msg TEXT, error_id INTEGER, error_msg TEXT,
            cancel_failed INTEGER NOT NULL DEFAULT 0, order_time TEXT, order_time_east8 TEXT,
            strategy_name TEXT, order_remark TEXT, signal_trade_date TEXT,
            data_source TEXT NOT NULL DEFAULT 'CALLBACK', synced INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE (account_id, trade_date, order_id)
        )""",
    "qmt_position_snapshot": """
        CREATE TABLE IF NOT EXISTS qmt_position_snapshot (
            account_id TEXT NOT NULL, account_type INTEGER, trade_date TEXT NOT NULL,
            snapshot_type TEXT NOT NULL DEFAULT 'CLOSE', ts_code TEXT, qmt_stock_code TEXT,
            volume INTEGER NOT NULL DEFAULT 0, can_use_volume INTEGER NOT NULL DEFAULT 0,
            frozen_volume INTEGER, on_road_volume INTEGER, yesterday_volume INTEGER,
            open_price TEXT, avg_price TEXT, market_value TEXT, last_price TEXT,
            float_profit TEXT, profit_rate TEXT, data_source TEXT NOT NULL DEFAULT 'QUERY',
            synced INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT (datetime('now')),
            UNIQUE (account_id, trade_date, ts_code, snapshot_type)
        )""",
    "qmt_account_daily": """
        CREATE TABLE IF NOT EXISTS qmt_account_daily (
            account_id TEXT NOT NULL, account_type INTEGER, trade_date TEXT NOT NULL,
            snapshot_type TEXT NOT NULL DEFAULT 'CLOSE', total_asset TEXT, cash TEXT,
            frozen_cash TEXT, market_value TEXT, net_cash_flow TEXT, prev_total_asset TEXT,
            daily_pnl TEXT, daily_return TEXT, cash_flow_note TEXT,
            data_source TEXT NOT NULL DEFAULT 'QUERY', synced INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE (account_id, trade_date, snapshot_type)
        )""",
    "local_order_ledger": """
        CREATE TABLE IF NOT EXISTS local_order_ledger (
            biz_order_no TEXT PRIMARY KEY, account_id TEXT NOT NULL, target_trade_date TEXT NOT NULL,
            ts_code TEXT NOT NULL, strategy_family TEXT NOT NULL, side TEXT NOT NULL,
            plan_volume INTEGER, plan_price TEXT, order_remark TEXT, signal_trade_date TEXT,
            state TEXT NOT NULL, order_id INTEGER, filled_volume INTEGER NOT NULL DEFAULT 0,
            avg_filled_price TEXT, order_phase TEXT, cancelable INTEGER NOT NULL DEFAULT 1,
            miss_reason TEXT, cancel_failed INTEGER NOT NULL DEFAULT 0, error_id INTEGER,
            error_msg TEXT, created_at TEXT, updated_at TEXT, counted_trade_ids TEXT
        )""",
    # 成交明细去重表（评审三轮 EXEC-order-01）：以 (biz_order_no, traded_id) 为主键，INSERT OR IGNORE 天然幂等。
    # 用途：整行台账 local_order_ledger 的 filled_volume 是 write-behind 累计快照——崩溃窗口（内存已累计但镜像未
    # drain）重启后读回旧快照，同一 traded_id 回报重投会二次累计。明细表 append-only + 唯一键去重，重启 load 时
    # 按 biz_order_no 聚合重算 filled_volume / counted_trade_ids（以明细为权威），杜绝二次累计。
    # 主键用 biz_order_no（非 order_id）：QMT order_id 按交易日重置可跨日复用，按 biz 聚合天然不跨日污染
    # （配合 EXEC-order-06 两级反查索引）。traded_id 缺失时存合成键（与 local_ledger 的 _noid_ 去重口径一致）。
    "local_order_fill": """
        CREATE TABLE IF NOT EXISTS local_order_fill (
            biz_order_no TEXT NOT NULL, order_id INTEGER, traded_id TEXT NOT NULL,
            traded_volume INTEGER NOT NULL, traded_price TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (biz_order_no, traded_id)
        )""",
    "watchlist": """
        CREATE TABLE IF NOT EXISTS watchlist (
            ts_code TEXT NOT NULL, trade_date TEXT, target_trade_date TEXT NOT NULL,
            leader_strength_score TEXT, role TEXT, strategy TEXT, market_state TEXT,
            tradable_flag INTEGER, continuation_prob TEXT, next_day_premium_prob TEXT, boost TEXT,
            fail_conditions TEXT, signal_close TEXT, limit_up_price TEXT,
            reasonable_open_high_low TEXT, reasonable_open_high_high TEXT, first_board_vol INTEGER,
            float_mktcap TEXT, strategy_family TEXT, setup TEXT, name TEXT, is_st INTEGER,
            PRIMARY KEY (ts_code, target_trade_date)
        )""",
    # 系统标志位 kv（评审二轮 P1#9）：跨进程/跨日持久的运行态标记，如对账未通过阻断次日开仓。
    "system_flags": """
        CREATE TABLE IF NOT EXISTS system_flags (
            flag_key TEXT PRIMARY KEY, flag_value TEXT, updated_at TEXT
        )""",
}

# 增量列迁移（评审二轮 P1#18/#63 + 禁买 ST 硬规则 is_st）：对已存在的 watchlist 表补列（CREATE TABLE
# IF NOT EXISTS 不会给旧表加列）。{表: [(列, 列定义), ...]}，init_db 幂等执行（已存在的列跳过）。
# is_st（禁买 ST 硬规则 + F08）：旧库无此列时补上，使信号侧显式 ST 标志可经 SQLite 无损 round-trip。
_COLUMN_MIGRATIONS = {
    "watchlist": [("name", "TEXT"), ("is_st", "INTEGER")],
}

# 索引：对账 / 同步按 (trade_date) 查；台账按 (target_trade_date, ts_code, strategy_family) 查活跃单。
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_trade_date ON qmt_trade (trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_trade_sync ON qmt_trade (synced, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_order_date ON qmt_order (trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_order_sync ON qmt_order (synced, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_pos_sync ON qmt_position_snapshot (synced, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_acc_sync ON qmt_account_daily (synced, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_active ON local_order_ledger (target_trade_date, ts_code, strategy_family)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_order ON local_order_ledger (order_id)",
    "CREATE INDEX IF NOT EXISTS idx_watchlist_target ON watchlist (target_trade_date)",
]


def apply_pragmas(conn: sqlite3.Connection) -> None:
    """设置并发 / 耐久 pragma（§六 待确认项 3：WAL + NORMAL，断电极端情形有 QMT query_* 当日兜底）。"""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")  # 极端并发下等锁 5s，避免「database is locked」直接报错


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    """对已存在的表补增量列（评审二轮 P1#18/#63）：读 PRAGMA table_info 取现有列，缺则 ALTER TABLE ADD。

    幂等：已有该列则跳过；SQLite ALTER ADD COLUMN 只能加可空/带默认列，本项均为可空 TEXT，安全。
    """
    for table, cols in _COLUMN_MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, col_def in cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")


def init_db(conn: sqlite3.Connection) -> None:
    """建表 + 增量列迁移 + 建索引 + 设 pragma（幂等，IF NOT EXISTS）。库启动时调用一次。"""
    apply_pragmas(conn)
    for ddl in _DDL.values():
        conn.execute(ddl)
    _apply_column_migrations(conn)
    for idx in _INDEXES:
        conn.execute(idx)
    conn.commit()
