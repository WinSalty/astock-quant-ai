"""仓储幂等单测（§6.5）：InMemory upsert 语义 + MySQL SQL 构造。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from qmt_strategy.contracts.enums import DataSource, OrderStatus, SnapshotType, TradeSide
from qmt_strategy.contracts.models import AccountRecord, OrderRecord, PositionRecord, TradeRecord
from qmt_strategy.data_writer.repository import (
    InMemoryQmtRepository,
    build_trade_upsert,
    build_order_upsert,
)

T_BUY = date(2026, 6, 12)


def _trade(traded_id="t1", signal_trade_date=None, east8=None, source=DataSource.CALLBACK):
    return TradeRecord(
        account_id="acc1",
        trade_date=T_BUY,
        ts_code="600036.SH",
        qmt_stock_code="600036.SH",
        traded_id=traded_id,
        trade_side=TradeSide.BUY,
        traded_price=Decimal("35.12"),
        traded_volume=200,
        traded_time=datetime(2026, 6, 12, 5, 31, 2),
        traded_time_east8=east8,
        signal_trade_date=signal_trade_date,
        data_source=source,
    )


def test_upsert_trade_idempotent_same_key():
    repo = InMemoryQmtRepository(unique_with_trade_date=True)
    repo.upsert_trade(_trade("t1"))
    repo.upsert_trade(_trade("t1"))  # 同唯一键再写
    assert repo.count_trades() == 1


def test_upsert_trade_coalesce_keeps_filled_value():
    repo = InMemoryQmtRepository()
    # 回调先到（带 east8 与 signal_trade_date），补采后到（这两列为空）应保留旧非空值
    repo.upsert_trade(_trade("t1", signal_trade_date=date(2026, 6, 11), east8=datetime(2026, 6, 12, 13, 31, 2)))
    repo.upsert_trade(_trade("t1", signal_trade_date=None, east8=None, source=DataSource.QUERY_BACKFILL))
    got = repo.get_trades("acc1", T_BUY)[0]
    assert got.signal_trade_date == date(2026, 6, 11)        # COALESCE 保留
    assert got.traded_time_east8 == datetime(2026, 6, 12, 13, 31, 2)
    assert got.data_source == DataSource.QUERY_BACKFILL       # data_source 取后到值


def test_unique_key_with_trade_date_separates_cross_day():
    repo = InMemoryQmtRepository(unique_with_trade_date=True)
    a = _trade("dup")
    b = _trade("dup")
    b.trade_date = date(2026, 6, 15)  # 跨日相同 traded_id
    repo.upsert_trade(a)
    repo.upsert_trade(b)
    assert repo.count_trades() == 2  # 纳入 trade_date 后落两行


def test_unique_key_without_trade_date_merges_cross_day():
    repo = InMemoryQmtRepository(unique_with_trade_date=False)
    a = _trade("dup")
    b = _trade("dup")
    b.trade_date = date(2026, 6, 15)
    repo.upsert_trade(a)
    repo.upsert_trade(b)
    assert repo.count_trades() == 1  # 现行键（不含 trade_date）跨日合并为一行


def test_position_and_account_snapshot_upsert():
    repo = InMemoryQmtRepository()
    pos = PositionRecord(
        account_id="acc1", trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        snapshot_type=SnapshotType.CLOSE, volume=200, can_use_volume=0,
    )
    repo.upsert_position(pos)
    repo.upsert_position(pos)
    assert len(repo.all_positions()) == 1
    acc = AccountRecord(
        account_id="acc1", trade_date=T_BUY, total_asset=Decimal("100000"), cash=Decimal("92000"),
        snapshot_type=SnapshotType.CLOSE,
    )
    repo.upsert_account_daily(acc)
    assert len(repo.all_accounts()) == 1


def test_mark_cancel_failed_keeps_status():
    repo = InMemoryQmtRepository()
    o = OrderRecord(
        account_id="acc1", trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        order_id=123, trade_side=TradeSide.BUY, order_volume=200, order_status=OrderStatus.REPORTED,
    )
    repo.upsert_order(o)
    repo.mark_cancel_failed("acc1", 123, error_id=99, error_msg="撤单失败")
    got = repo.get_orders("acc1", T_BUY)[0]
    assert got.cancel_failed is True
    assert got.error_id == 99
    assert got.order_status == OrderStatus.REPORTED  # 终态不被改


def test_build_trade_upsert_sql_has_coalesce():
    sql, params = build_trade_upsert(_trade("t1", signal_trade_date=date(2026, 6, 11)))
    assert "INSERT INTO `qmt_trade`" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "COALESCE(VALUES(`signal_trade_date`)" in sql
    assert "COALESCE(VALUES(`traded_time_east8`)" in sql
    # 唯一键列不进 UPDATE
    assert "`traded_id`=VALUES(`traded_id`)" not in sql
    assert len(params) == sql.count("%s")


def test_build_order_upsert_columns():
    o = OrderRecord(
        account_id="acc1", trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        order_id=1, trade_side=TradeSide.BUY, order_volume=100, order_status=OrderStatus.TRADED,
    )
    sql, params = build_order_upsert(o)
    assert "INSERT INTO `qmt_order`" in sql
    assert len(params) == sql.count("%s")
