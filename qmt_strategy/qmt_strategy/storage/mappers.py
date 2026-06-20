"""记录 ↔ SQLite 行的序列化（doc/05 §三，单一来源）。

业务意图：把契约层 dataclass（TradeRecord/OrderRecord/PositionRecord/AccountRecord/LedgerEntry/
SelectedStockRow）与 SQLite 行互转。存储口径：Decimal→TEXT（保精度）、date/datetime→ISO 文本、
枚举→值、布尔→0/1、列表/任意对象→JSON。repo / ledger / watchlist 源 / 同步任务共用本模块，
保证编解码口径一致、可无损 round-trip。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from ..contracts.enums import (
    DataSource,
    OrderPhase,
    OrderState,
    OrderStatus,
    SnapshotType,
    TradeSide,
)
from ..contracts.models import (
    AccountRecord,
    LedgerEntry,
    OrderRecord,
    PositionRecord,
    SelectedStockRow,
    TradeRecord,
)

# ---------------------------------------------------------------------------
# 基础编解码（None 透传，杜绝精度 / 时区坑）
# ---------------------------------------------------------------------------


def dec_text(v: Any) -> Optional[str]:
    """Decimal/数值 → TEXT（str），None 透传。存 TEXT 避免 SQLite REAL 的 float 误差。"""
    return None if v is None else str(v)


def text_dec(v: Any) -> Optional[Decimal]:
    """TEXT → Decimal，None/空/非法 → None（不臆造）。"""
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def date_iso(d: Optional[date]) -> Optional[str]:
    return None if d is None else d.isoformat()


def iso_date(s: Any) -> Optional[date]:
    if s is None or str(s).strip() == "":
        return None
    return date.fromisoformat(str(s)[:10])


def dt_iso(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else dt.isoformat()


def iso_dt(s: Any) -> Optional[datetime]:
    if s is None or str(s).strip() == "":
        return None
    return datetime.fromisoformat(str(s))


def enum_val(e: Any) -> Optional[str]:
    if e is None:
        return None
    return e.value if hasattr(e, "value") else str(e)


def bool_int(b: Any) -> Optional[int]:
    if b is None:
        return None
    return 1 if b else 0


def int_bool(v: Any) -> bool:
    return bool(v)


def json_dump(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(v), ensure_ascii=False)


def json_load(s: Any) -> Any:
    if s is None or str(s).strip() == "":
        return None
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return None


def _g(row: Any, key: str) -> Any:
    """从 sqlite3.Row / dict 取列值（统一访问）。"""
    if isinstance(row, dict):
        return row.get(key)
    return row[key]


# ---------------------------------------------------------------------------
# 四表回流记录
# ---------------------------------------------------------------------------


def trade_to_row(rec: TradeRecord) -> Dict[str, Any]:
    return {
        "account_id": rec.account_id, "account_type": rec.account_type,
        "trade_date": date_iso(rec.trade_date), "ts_code": rec.ts_code,
        "qmt_stock_code": rec.qmt_stock_code, "traded_id": rec.traded_id,
        "order_id": rec.order_id, "order_sysid": rec.order_sysid,
        "trade_side": enum_val(rec.trade_side), "offset_flag": rec.offset_flag,
        "traded_price": dec_text(rec.traded_price), "traded_volume": rec.traded_volume,
        "traded_amount": dec_text(rec.traded_amount), "traded_time": dt_iso(rec.traded_time),
        "traded_time_east8": dt_iso(rec.traded_time_east8), "strategy_name": rec.strategy_name,
        "order_remark": rec.order_remark, "signal_trade_date": date_iso(rec.signal_trade_date),
        "data_source": enum_val(rec.data_source),
    }


def row_to_trade(row: Any) -> TradeRecord:
    return TradeRecord(
        account_id=_g(row, "account_id"), trade_date=iso_date(_g(row, "trade_date")),
        ts_code=_g(row, "ts_code"), qmt_stock_code=_g(row, "qmt_stock_code"),
        traded_id=_g(row, "traded_id"), trade_side=TradeSide(_g(row, "trade_side")),
        traded_price=text_dec(_g(row, "traded_price")), traded_volume=_g(row, "traded_volume"),
        traded_time=iso_dt(_g(row, "traded_time")), traded_time_east8=iso_dt(_g(row, "traded_time_east8")),
        account_type=_g(row, "account_type"), order_id=_g(row, "order_id"),
        order_sysid=_g(row, "order_sysid"), offset_flag=_g(row, "offset_flag"),
        traded_amount=text_dec(_g(row, "traded_amount")), strategy_name=_g(row, "strategy_name"),
        order_remark=_g(row, "order_remark"), signal_trade_date=iso_date(_g(row, "signal_trade_date")),
        data_source=DataSource(_g(row, "data_source")),
    )


def order_to_row(rec: OrderRecord) -> Dict[str, Any]:
    return {
        "account_id": rec.account_id, "account_type": rec.account_type,
        "trade_date": date_iso(rec.trade_date), "ts_code": rec.ts_code,
        "qmt_stock_code": rec.qmt_stock_code, "order_id": rec.order_id, "order_sysid": rec.order_sysid,
        "trade_side": enum_val(rec.trade_side), "offset_flag": rec.offset_flag,
        "price_type": rec.price_type, "order_price": dec_text(rec.order_price),
        "order_volume": rec.order_volume, "traded_volume": rec.traded_volume,
        "traded_price": dec_text(rec.traded_price), "order_status": enum_val(rec.order_status),
        "status_msg": rec.status_msg, "error_id": rec.error_id, "error_msg": rec.error_msg,
        "cancel_failed": bool_int(rec.cancel_failed), "order_time": dt_iso(rec.order_time),
        "order_time_east8": dt_iso(rec.order_time_east8), "strategy_name": rec.strategy_name,
        "order_remark": rec.order_remark, "signal_trade_date": date_iso(rec.signal_trade_date),
        "data_source": enum_val(rec.data_source),
    }


def row_to_order(row: Any) -> OrderRecord:
    return OrderRecord(
        account_id=_g(row, "account_id"), trade_date=iso_date(_g(row, "trade_date")),
        ts_code=_g(row, "ts_code"), qmt_stock_code=_g(row, "qmt_stock_code"),
        order_id=_g(row, "order_id"), trade_side=TradeSide(_g(row, "trade_side")),
        order_volume=_g(row, "order_volume"), order_status=OrderStatus(_g(row, "order_status")),
        traded_volume=_g(row, "traded_volume") or 0, account_type=_g(row, "account_type"),
        order_sysid=_g(row, "order_sysid"), offset_flag=_g(row, "offset_flag"),
        price_type=_g(row, "price_type"), order_price=text_dec(_g(row, "order_price")),
        traded_price=text_dec(_g(row, "traded_price")), status_msg=_g(row, "status_msg"),
        error_id=_g(row, "error_id"), error_msg=_g(row, "error_msg"),
        cancel_failed=int_bool(_g(row, "cancel_failed")), order_time=iso_dt(_g(row, "order_time")),
        order_time_east8=iso_dt(_g(row, "order_time_east8")), strategy_name=_g(row, "strategy_name"),
        order_remark=_g(row, "order_remark"), signal_trade_date=iso_date(_g(row, "signal_trade_date")),
        data_source=DataSource(_g(row, "data_source")),
    )


def position_to_row(rec: PositionRecord) -> Dict[str, Any]:
    return {
        "account_id": rec.account_id, "account_type": rec.account_type,
        "trade_date": date_iso(rec.trade_date), "snapshot_type": enum_val(rec.snapshot_type),
        "ts_code": rec.ts_code, "qmt_stock_code": rec.qmt_stock_code, "volume": rec.volume,
        "can_use_volume": rec.can_use_volume, "frozen_volume": rec.frozen_volume,
        "on_road_volume": rec.on_road_volume, "yesterday_volume": rec.yesterday_volume,
        "open_price": dec_text(rec.open_price), "avg_price": dec_text(rec.avg_price),
        "market_value": dec_text(rec.market_value), "last_price": dec_text(rec.last_price),
        "float_profit": dec_text(rec.float_profit), "profit_rate": dec_text(rec.profit_rate),
        "data_source": enum_val(rec.data_source),
    }


def row_to_position(row: Any) -> PositionRecord:
    return PositionRecord(
        account_id=_g(row, "account_id"), trade_date=iso_date(_g(row, "trade_date")),
        ts_code=_g(row, "ts_code"), qmt_stock_code=_g(row, "qmt_stock_code"),
        snapshot_type=SnapshotType(_g(row, "snapshot_type")), volume=_g(row, "volume") or 0,
        can_use_volume=_g(row, "can_use_volume") or 0, account_type=_g(row, "account_type"),
        frozen_volume=_g(row, "frozen_volume"), on_road_volume=_g(row, "on_road_volume"),
        yesterday_volume=_g(row, "yesterday_volume"), open_price=text_dec(_g(row, "open_price")),
        avg_price=text_dec(_g(row, "avg_price")), market_value=text_dec(_g(row, "market_value")),
        last_price=text_dec(_g(row, "last_price")), float_profit=text_dec(_g(row, "float_profit")),
        profit_rate=text_dec(_g(row, "profit_rate")), data_source=DataSource(_g(row, "data_source")),
    )


def account_to_row(rec: AccountRecord) -> Dict[str, Any]:
    return {
        "account_id": rec.account_id, "account_type": rec.account_type,
        "trade_date": date_iso(rec.trade_date), "snapshot_type": enum_val(rec.snapshot_type),
        "total_asset": dec_text(rec.total_asset), "cash": dec_text(rec.cash),
        "frozen_cash": dec_text(rec.frozen_cash), "market_value": dec_text(rec.market_value),
        "net_cash_flow": dec_text(rec.net_cash_flow), "prev_total_asset": dec_text(rec.prev_total_asset),
        "daily_pnl": dec_text(rec.daily_pnl), "daily_return": dec_text(rec.daily_return),
        "cash_flow_note": rec.cash_flow_note, "data_source": enum_val(rec.data_source),
    }


def row_to_account(row: Any) -> AccountRecord:
    return AccountRecord(
        account_id=_g(row, "account_id"), trade_date=iso_date(_g(row, "trade_date")),
        total_asset=text_dec(_g(row, "total_asset")), cash=text_dec(_g(row, "cash")),
        snapshot_type=SnapshotType(_g(row, "snapshot_type")),
        frozen_cash=text_dec(_g(row, "frozen_cash")) or Decimal("0"),
        market_value=text_dec(_g(row, "market_value")) or Decimal("0"),
        net_cash_flow=text_dec(_g(row, "net_cash_flow")) or Decimal("0"),
        account_type=_g(row, "account_type"), prev_total_asset=text_dec(_g(row, "prev_total_asset")),
        daily_pnl=text_dec(_g(row, "daily_pnl")), daily_return=text_dec(_g(row, "daily_return")),
        cash_flow_note=_g(row, "cash_flow_note"), data_source=DataSource(_g(row, "data_source")),
    )


# ---------------------------------------------------------------------------
# 本地下单台账
# ---------------------------------------------------------------------------


def ledger_to_row(e: LedgerEntry) -> Dict[str, Any]:
    return {
        "biz_order_no": e.biz_order_no, "account_id": e.account_id,
        "target_trade_date": date_iso(e.target_trade_date), "ts_code": e.ts_code,
        "strategy_family": e.strategy_family, "side": enum_val(e.side), "plan_volume": e.plan_volume,
        "plan_price": dec_text(e.plan_price), "order_remark": e.order_remark,
        "signal_trade_date": date_iso(e.signal_trade_date), "state": enum_val(e.state),
        "order_id": e.order_id, "filled_volume": e.filled_volume,
        "avg_filled_price": dec_text(e.avg_filled_price), "order_phase": enum_val(e.order_phase),
        "cancelable": bool_int(e.cancelable), "miss_reason": e.miss_reason,
        "cancel_failed": bool_int(e.cancel_failed), "error_id": e.error_id, "error_msg": e.error_msg,
        "created_at": dt_iso(e.created_at), "updated_at": dt_iso(e.updated_at),
        # 已计入成交编号集合 → JSON 列表（排序稳定，便于比对）。
        "counted_trade_ids": json_dump(sorted(str(x) for x in e.counted_trade_ids)),
    }


def row_to_ledger(row: Any) -> LedgerEntry:
    ids = json_load(_g(row, "counted_trade_ids")) or []
    return LedgerEntry(
        biz_order_no=_g(row, "biz_order_no"), account_id=_g(row, "account_id"),
        target_trade_date=iso_date(_g(row, "target_trade_date")), ts_code=_g(row, "ts_code"),
        strategy_family=_g(row, "strategy_family"), side=TradeSide(_g(row, "side")),
        plan_volume=_g(row, "plan_volume"), plan_price=text_dec(_g(row, "plan_price")),
        order_remark=_g(row, "order_remark"), signal_trade_date=iso_date(_g(row, "signal_trade_date")),
        state=OrderState(_g(row, "state")), order_id=_g(row, "order_id"),
        filled_volume=_g(row, "filled_volume") or 0,
        avg_filled_price=text_dec(_g(row, "avg_filled_price")),
        order_phase=OrderPhase(_g(row, "order_phase")) if _g(row, "order_phase") else OrderPhase.AUCTION,
        cancelable=int_bool(_g(row, "cancelable")), miss_reason=_g(row, "miss_reason"),
        cancel_failed=int_bool(_g(row, "cancel_failed")), error_id=_g(row, "error_id"),
        error_msg=_g(row, "error_msg"), created_at=iso_dt(_g(row, "created_at")),
        updated_at=iso_dt(_g(row, "updated_at")), counted_trade_ids=set(ids),
    )


# ---------------------------------------------------------------------------
# watchlist 契约（信号侧盘前交付）
# ---------------------------------------------------------------------------


def selected_to_row(r: SelectedStockRow) -> Dict[str, Any]:
    return {
        "ts_code": r.ts_code, "trade_date": date_iso(r.trade_date),
        "target_trade_date": date_iso(r.target_trade_date),
        "leader_strength_score": dec_text(r.leader_strength_score), "role": r.role,
        "strategy": r.strategy, "market_state": r.market_state, "tradable_flag": bool_int(r.tradable_flag),
        "continuation_prob": dec_text(r.continuation_prob),
        "next_day_premium_prob": dec_text(r.next_day_premium_prob), "boost": json_dump(r.boost),
        "fail_conditions": json_dump(r.fail_conditions), "signal_close": dec_text(r.signal_close),
        "limit_up_price": dec_text(r.limit_up_price),
        "reasonable_open_high_low": dec_text(r.reasonable_open_high_low),
        "reasonable_open_high_high": dec_text(r.reasonable_open_high_high),
        "first_board_vol": r.first_board_vol, "float_mktcap": dec_text(r.float_mktcap),
        "strategy_family": r.strategy_family, "setup": r.setup,
        "name": r.name,  # 评审二轮 P1#18/#63：透传证券名称供执行侧 ST 识别/过滤
        # 禁买 ST 硬规则 + F08：显式 ST 标志落 0/1，None(未下发)落 NULL，bool_int 保三态。
        "is_st": bool_int(r.is_st),
        # 连板维度（doc/18 禁买四板及以上）：board_level=连板高度（int 直存，None→NULL），tier=入选分层（TEXT 直存）。
        "board_level": r.board_level,
        "tier": r.tier,
        # 打板因子（契约 1.2.0）：时刻 TEXT 直存（HH:MM:SS，不解析）、open_times int 直存、三比例 dec_text 存 TEXT 保精度。
        "first_limit_time": r.first_limit_time,
        "last_limit_time": r.last_limit_time,
        "open_times": r.open_times,
        "volume_ratio": dec_text(r.volume_ratio),
        "return_5d_pct": dec_text(r.return_5d_pct),
        "return_10d_pct": dec_text(r.return_10d_pct),
        # 数据缺测标记（doc/29 B1；评审 Stage B 修复）：data_missing 落 0/1（bool_int），data_missing_reason TEXT 直存。
        # 必须无损 round-trip，否则盘前 save→盘中 fetch 丢成 False，B2 _rule_data_missing 永不命中、B3 缺测强卖失效。
        "data_missing": bool_int(r.data_missing),
        "data_missing_reason": r.data_missing_reason,
    }


def row_to_selected(row: Any) -> SelectedStockRow:
    tf = _g(row, "tradable_flag")
    return SelectedStockRow(
        ts_code=_g(row, "ts_code"), trade_date=iso_date(_g(row, "trade_date")),
        target_trade_date=iso_date(_g(row, "target_trade_date")),
        leader_strength_score=text_dec(_g(row, "leader_strength_score")), role=_g(row, "role"),
        strategy=_g(row, "strategy"), market_state=_g(row, "market_state"),
        tradable_flag=None if tf is None else int_bool(tf),
        continuation_prob=text_dec(_g(row, "continuation_prob")),
        next_day_premium_prob=text_dec(_g(row, "next_day_premium_prob")),
        boost=json_load(_g(row, "boost")), fail_conditions=json_load(_g(row, "fail_conditions")),
        signal_close=text_dec(_g(row, "signal_close")), limit_up_price=text_dec(_g(row, "limit_up_price")),
        reasonable_open_high_low=text_dec(_g(row, "reasonable_open_high_low")),
        reasonable_open_high_high=text_dec(_g(row, "reasonable_open_high_high")),
        first_board_vol=_g(row, "first_board_vol"), float_mktcap=text_dec(_g(row, "float_mktcap")),
        strategy_family=_g(row, "strategy_family"), setup=_g(row, "setup"),
        name=_g(row, "name"),  # 评审二轮 P1#18/#63
        # 禁买 ST 硬规则 + F08：NULL→None(回退 name 判定)，0/1→False/True，保三态无损 round-trip。
        is_st=(None if (_st := _g(row, "is_st")) is None else int_bool(_st)),
        # 连板维度（doc/18 禁买四板及以上）：board_level NULL→None，tier 直读。
        # 安全前提：board_level/tier 两列由 init_db→_apply_column_migrations 对旧库幂等补齐；本方法消费的行均来自
        # init_db 之后的 `SELECT *`，故 _g 取列必有值。注意 sqlite3.Row 对【不存在的列】是抛 IndexError（非返 None），
        # 故绝不能在未跑迁移的库上 SELECT 老列集再喂本方法——保护来自强制迁移，不是 _g 的容错。
        board_level=_g(row, "board_level"),
        tier=_g(row, "tier"),
        # 打板因子（契约 1.2.0）：与 board_level/tier 同——6 列由 _apply_column_migrations 对旧库幂等补齐，
        # 故 _g 取列必有值。时刻/open_times 直读（NULL→None），三比例 text_dec 还原 Decimal（NULL→None）。
        first_limit_time=_g(row, "first_limit_time"),
        last_limit_time=_g(row, "last_limit_time"),
        open_times=_g(row, "open_times"),
        volume_ratio=text_dec(_g(row, "volume_ratio")),
        return_5d_pct=text_dec(_g(row, "return_5d_pct")),
        return_10d_pct=text_dec(_g(row, "return_10d_pct")),
        # 数据缺测标记（doc/29 B1；评审 Stage B 修复）：data_missing 非可空 bool（默认 False）——NULL/0→False、1→True，
        # 用 bool() 统一兜底（旧行迁移补列后为 NULL）。data_missing_reason TEXT 直读（NULL→None）。
        # 两列同样由 _apply_column_migrations 对旧库幂等补齐，故 SELECT * 列集必含。
        data_missing=bool(_g(row, "data_missing")),
        data_missing_reason=_g(row, "data_missing_reason"),
    )
