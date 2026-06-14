"""qmt_* 四表仓储实现（§6.4 写入路径 / §6.5 幂等口径）。

提供两种实现，均满足 contracts.QmtRepository：
- InMemoryQmtRepository：内存幂等仓储，单测 / 离线用，完整实现 ON DUPLICATE KEY UPDATE 语义
  （后到覆盖为终态；signal_trade_date / *_east8 已回填值不被空覆盖，COALESCE 口径）。
- MySqlQmtRepository：直连 MySQL（方案 A），用带 ON DUPLICATE KEY UPDATE 的参数化 SQL；
  接受一个返回 DB-API 连接的 connection_factory，便于注入真实 PyMySQL 连接或测试替身。

唯一键口径（§6.5）：默认按加固方案把 trade_date 纳入明细唯一键（防跨日 ID 复用串号）；
可经 Settings.repository_unique_with_trade_date=False 退回现行 design 键。
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Dict, List, Optional, Tuple

from ..contracts.models import AccountRecord, OrderRecord, PositionRecord, TradeRecord

# 这些列一旦回填为非空，后到的空值不得覆盖（COALESCE(VALUES(col), col) 口径，§6.5）。
_COALESCE_KEEP = ("signal_trade_date", "traded_time_east8", "order_time_east8")


class InMemoryQmtRepository:
    """内存幂等仓储。实现 contracts.QmtRepository 协议。"""

    def __init__(self, unique_with_trade_date: bool = True):
        # 唯一键是否纳入 trade_date（§6.5 加固开关）。
        self._uwd = unique_with_trade_date
        self._trades: Dict[tuple, TradeRecord] = {}
        self._orders: Dict[tuple, OrderRecord] = {}
        self._positions: Dict[tuple, PositionRecord] = {}
        self._accounts: Dict[tuple, AccountRecord] = {}
        # 系统标志位 kv（评审二轮 P1#9）：内存版对账阻断标记等，与 SqliteQmtRepository 同接口。
        self._flags: Dict[str, str] = {}

    def set_flag(self, flag_key: str, flag_value):
        """设置/清除系统标志位（flag_value=None 清除）。"""
        if flag_value is None:
            self._flags.pop(flag_key, None)
        else:
            self._flags[flag_key] = str(flag_value)

    def get_flag(self, flag_key: str):
        """读系统标志位；无则 None。"""
        return self._flags.get(flag_key)

    # —— 唯一键构造 ——
    def _trade_key(self, rec: TradeRecord) -> tuple:
        if self._uwd:
            return (rec.account_id, rec.trade_date, rec.traded_id)
        return (rec.account_id, rec.traded_id)

    def _order_key(self, rec: OrderRecord) -> tuple:
        if self._uwd:
            return (rec.account_id, rec.trade_date, rec.order_id)
        return (rec.account_id, rec.order_id)

    @staticmethod
    def _pos_key(rec: PositionRecord) -> tuple:
        return (rec.account_id, rec.trade_date, rec.ts_code, str(rec.snapshot_type))

    @staticmethod
    def _acc_key(rec: AccountRecord) -> tuple:
        return (rec.account_id, rec.trade_date, str(rec.snapshot_type))

    @staticmethod
    def _merge(existing, incoming):
        """ON DUPLICATE KEY UPDATE：incoming 覆盖 existing，但 COALESCE 列已非空则保留。"""
        merged = copy.deepcopy(incoming)
        for col in _COALESCE_KEEP:
            if hasattr(merged, col):
                inc_val = getattr(merged, col)
                old_val = getattr(existing, col, None)
                if inc_val is None and old_val is not None:
                    setattr(merged, col, old_val)
        return merged

    def upsert_trade(self, rec: TradeRecord) -> None:
        key = self._trade_key(rec)
        if key in self._trades:
            self._trades[key] = self._merge(self._trades[key], rec)
        else:
            self._trades[key] = copy.deepcopy(rec)

    def upsert_order(self, rec: OrderRecord) -> None:
        key = self._order_key(rec)
        if key in self._orders:
            self._orders[key] = self._merge(self._orders[key], rec)
        else:
            self._orders[key] = copy.deepcopy(rec)

    def upsert_position(self, rec: PositionRecord) -> None:
        self._positions[self._pos_key(rec)] = copy.deepcopy(rec)

    def upsert_account_daily(self, rec: AccountRecord) -> None:
        self._accounts[self._acc_key(rec)] = copy.deepcopy(rec)

    def mark_cancel_failed(
        self, account_id: str, order_id: int, error_id: Optional[int], error_msg: Optional[str]
    ) -> None:
        """on_cancel_error：在既有委托行追加 cancel_failed=1 + error_*，不改 order_status 终态（§6.2.1）。

        跨日防误标（评审 medium#5）：order_id 可跨日复用，撤单失败只针对【当日】那笔委托。
        故只对该 (account_id, order_id) 的【最新交易日】行打标，绝不波及历史同号委托行。
        """
        matches = [
            o for o in self._orders.values()
            if o.account_id == account_id and o.order_id == order_id
        ]
        if not matches:
            return
        latest_date = max(o.trade_date for o in matches)
        for o in matches:
            if o.trade_date != latest_date:
                continue
            o.cancel_failed = True
            if error_id is not None:
                o.error_id = error_id
            if error_msg is not None:
                o.error_msg = error_msg

    # —— 对账只读 ——
    def get_orders(self, account_id: str, trade_date: date) -> List[OrderRecord]:
        return [
            copy.deepcopy(o)
            for o in self._orders.values()
            if o.account_id == account_id and o.trade_date == trade_date
        ]

    def get_trades(self, account_id: str, trade_date: date) -> List[TradeRecord]:
        return [
            copy.deepcopy(t)
            for t in self._trades.values()
            if t.account_id == account_id and t.trade_date == trade_date
        ]

    def get_account_daily(self, account_id, trade_date, snapshot_type=None):
        """取账户日快照（供资产对账，§6.7）。snapshot_type 默认 CLOSE（净值/对账权威）。"""
        from ..contracts.enums import SnapshotType

        st = snapshot_type if snapshot_type is not None else SnapshotType.CLOSE
        rec = self._accounts.get((account_id, trade_date, str(st)))
        return copy.deepcopy(rec) if rec is not None else None

    # —— 测试/排查辅助 ——
    def count_trades(self) -> int:
        return len(self._trades)

    def count_orders(self) -> int:
        return len(self._orders)

    def all_positions(self) -> List[PositionRecord]:
        return [copy.deepcopy(p) for p in self._positions.values()]

    def all_accounts(self) -> List[AccountRecord]:
        return [copy.deepcopy(a) for a in self._accounts.values()]


class MySqlQmtRepository:
    """直连 MySQL 仓储（方案 A，§6.4）。实现 contracts.QmtRepository 协议。

    安全口径（§6.10）：调用方注入的连接必须使用「仅 qmt_* 四表 INSERT/UPDATE/SELECT」的独立账号，
    并在网络层做 IP 白名单 / TLS / 隧道；DSN 等敏感信息不硬编码、不入库、不写日志。

    connection_factory(): 返回一个 DB-API 2.0 连接（如 pymysql.connect(...)）；每次 upsert 取连接、
    提交、关闭游标（连接生命周期由 factory/调用方管理，便于连接池接入）。
    """

    def __init__(self, connection_factory, unique_with_trade_date: bool = True):
        self._conn_factory = connection_factory
        self._uwd = unique_with_trade_date

    # SQL 由 build_* 纯函数生成，便于单测断言 SQL 文本与参数顺序而无需真实 DB。
    def upsert_trade(self, rec: TradeRecord) -> None:
        sql, params = build_trade_upsert(rec)
        self._exec(sql, params)

    def upsert_order(self, rec: OrderRecord) -> None:
        sql, params = build_order_upsert(rec)
        self._exec(sql, params)

    def upsert_position(self, rec: PositionRecord) -> None:
        sql, params = build_position_upsert(rec)
        self._exec(sql, params)

    def upsert_account_daily(self, rec: AccountRecord) -> None:
        sql, params = build_account_upsert(rec)
        self._exec(sql, params)

    def mark_cancel_failed(
        self, account_id: str, order_id: int, error_id: Optional[int], error_msg: Optional[str]
    ) -> None:
        sql = (
            "UPDATE qmt_order SET cancel_failed=1, "
            "error_id=COALESCE(%s, error_id), error_msg=COALESCE(%s, error_msg) "
            "WHERE account_id=%s AND order_id=%s"
        )
        self._exec(sql, (error_id, error_msg, account_id, order_id))

    def get_orders(self, account_id: str, trade_date: date) -> List[OrderRecord]:
        raise NotImplementedError("对账只读查询的 ORM 映射由落地阶段按 schema 补全")

    def get_trades(self, account_id: str, trade_date: date) -> List[TradeRecord]:
        raise NotImplementedError("对账只读查询的 ORM 映射由落地阶段按 schema 补全")

    def get_account_daily(self, account_id, trade_date, snapshot_type=None):
        raise NotImplementedError("账户快照只读查询的 ORM 映射由落地阶段按 schema 补全")

    def _exec(self, sql: str, params: tuple) -> None:
        conn = self._conn_factory()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
            cur.close()
        finally:
            # 连接关闭策略交给 factory（连接池场景由池回收）；这里不强制 close。
            pass


def _enum_val(v):
    """枚举取其字符串值，其余原样（落库前规整）。"""
    return v.value if hasattr(v, "value") else v


def _columns_params(rec, cols: List[str]) -> Tuple[List[str], list]:
    """按列名列表从 dataclass 取值，枚举转值。"""
    vals = [_enum_val(getattr(rec, c)) for c in cols]
    return cols, vals


# 各表 UPDATE 段中走 COALESCE（不被空覆盖）的列。
_TRADE_COALESCE = {"traded_time_east8", "signal_trade_date"}
_ORDER_COALESCE = {"order_time_east8", "signal_trade_date"}


def _build_upsert(table: str, cols: List[str], rec, coalesce_cols: set, skip_update: set) -> Tuple[str, tuple]:
    """通用 INSERT ... ON DUPLICATE KEY UPDATE 构造（§6.5 写入语义）。"""
    _, vals = _columns_params(rec, cols)
    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join("`%s`" % c for c in cols)
    updates = []
    for c in cols:
        if c in skip_update:
            continue  # 唯一键列不进 UPDATE
        if c in coalesce_cols:
            updates.append("`%s`=COALESCE(VALUES(`%s`), `%s`)" % (c, c, c))
        else:
            updates.append("`%s`=VALUES(`%s`)" % (c, c))
    sql = "INSERT INTO `%s` (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s" % (
        table, col_sql, placeholders, ", ".join(updates)
    )
    return sql, tuple(vals)


def build_trade_upsert(rec: TradeRecord) -> Tuple[str, tuple]:
    """qmt_trade 成交明细 upsert（对齐 §2.1 DDL 列）。"""
    cols = [
        "account_id", "account_type", "trade_date", "ts_code", "qmt_stock_code", "traded_id",
        "order_id", "order_sysid", "trade_side", "offset_flag", "traded_price", "traded_volume",
        "traded_amount", "traded_time", "traded_time_east8", "strategy_name", "order_remark",
        "signal_trade_date", "data_source",
    ]
    skip = {"account_id", "trade_date", "traded_id"}  # 唯一键列不更新
    return _build_upsert("qmt_trade", cols, rec, _TRADE_COALESCE, skip)


def build_order_upsert(rec: OrderRecord) -> Tuple[str, tuple]:
    """qmt_order 委托 upsert（对齐 §2.2 DDL 列）。"""
    cols = [
        "account_id", "account_type", "trade_date", "ts_code", "qmt_stock_code", "order_id",
        "order_sysid", "trade_side", "offset_flag", "price_type", "order_price", "order_volume",
        "traded_volume", "traded_price", "order_status", "status_msg", "error_id", "error_msg",
        "cancel_failed", "order_time", "order_time_east8", "strategy_name", "order_remark",
        "signal_trade_date", "data_source",
    ]
    skip = {"account_id", "trade_date", "order_id"}
    return _build_upsert("qmt_order", cols, rec, _ORDER_COALESCE, skip)


def build_position_upsert(rec: PositionRecord) -> Tuple[str, tuple]:
    """qmt_position_snapshot 持仓快照 upsert（对齐 §2.3 DDL 列）。"""
    cols = [
        "account_id", "account_type", "trade_date", "snapshot_type", "ts_code", "qmt_stock_code",
        "volume", "can_use_volume", "frozen_volume", "on_road_volume", "yesterday_volume",
        "open_price", "avg_price", "market_value", "last_price", "float_profit", "profit_rate",
        "data_source",
    ]
    skip = {"account_id", "trade_date", "ts_code", "snapshot_type"}
    return _build_upsert("qmt_position_snapshot", cols, rec, set(), skip)


def build_account_upsert(rec: AccountRecord) -> Tuple[str, tuple]:
    """qmt_account_daily 账户日快照 upsert（对齐 §2.4 DDL 列）。"""
    cols = [
        "account_id", "account_type", "trade_date", "snapshot_type", "total_asset", "cash",
        "frozen_cash", "market_value", "net_cash_flow", "prev_total_asset", "daily_pnl",
        "daily_return", "cash_flow_note", "data_source",
    ]
    skip = {"account_id", "trade_date", "snapshot_type"}
    return _build_upsert("qmt_account_daily", cols, rec, set(), skip)
