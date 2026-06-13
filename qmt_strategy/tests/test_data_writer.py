"""data_writer 规整 + 落库写端单测（§6.2 / §6.3 / §6.5 / §6.6 / §6.9）。

全部用 fake / 内存实现，不连真实 xttrader / MySQL：
- 回报对象用 contracts.xt_objects 的 FakeXt* 构造（带 / 不带版本可选字段两组）；
- 落库后端用 data_writer.repository.InMemoryQmtRepository（完整实现 ON DUPLICATE KEY UPDATE 语义）；
- 时间断言验证东八区 Unix 时间戳 → UTC naive(−8h) + 东八区 naive 原值双写。

覆盖：
1) 回调落库规整：代码归一 600036.SH、方向 BUY/SELL、状态映射、时间双写正确。
2) 时间转换：east8 时间戳 → traded_time = east8 − 8h、traded_time_east8 = 原值；ts=None → (None, None)。
3) 版本兼容：缺 avg_price/frozen_volume/traded_amount 等字段 → 落 None，不抛 AttributeError。
4) data_writer 委托：upsert 后 repo 可查到记录；position/account 带入 snapshot_type 正确。
5) 幂等：同 traded_id 回调 + QUERY_BACKFILL 各写一次 → 仅 1 行、后到为终态、signal_trade_date/east8 不被空覆盖。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import SHANGHAI
from qmt_strategy.contracts.enums import (
    DataSource,
    OrderStatus,
    SnapshotType,
    TradeSide,
)
from qmt_strategy.contracts.models import AccountRecord, PositionRecord
from qmt_strategy.contracts.xt_objects import (
    FakeXtAsset,
    FakeXtOrder,
    FakeXtPosition,
    FakeXtTrade,
)
from qmt_strategy.data_writer.data_writer import DataWriterImpl
from qmt_strategy.data_writer.normalize import (
    default_side_resolver,
    default_status_resolver,
    normalize_account,
    normalize_order,
    normalize_position,
    normalize_trade,
)
from qmt_strategy.data_writer.repository import InMemoryQmtRepository

ACCOUNT = "1000000365"
T_BUY = date(2026, 6, 12)
T_SIGNAL = date(2026, 6, 11)

# 东八区 2026-06-12 13:31:02（北京时间）的 Unix 时间戳；对应 UTC naive 应为 05:31:02（−8h）。
_EAST8_DT = datetime(2026, 6, 12, 13, 31, 2, tzinfo=SHANGHAI)
TS_EAST8 = int(_EAST8_DT.timestamp())


# ---------------------------------------------------------------------------
# fake 构造助手：分「带全字段」与「缺版本可选字段」两组
# ---------------------------------------------------------------------------


def _full_trade(traded_id="t1", order_type=23, stock_code="600036.SH"):
    """完整成交回报（含 traded_amount / account_type 等版本可选字段）。"""
    return FakeXtTrade(
        account_id=ACCOUNT,
        account_type=2,
        stock_code=stock_code,
        traded_id=traded_id,
        order_id=12345,
        order_sysid="sys-1",
        order_type=order_type,
        offset_flag=48,
        traded_price=35.12,
        traded_volume=200,
        traded_amount=7024.0,
        traded_time=TS_EAST8,
        strategy_name="limit_up",
        order_remark="LUP|2026-06-11|600036.SH",
    )


def _full_order(order_id=12345, order_status=56, order_type=23):
    """完整委托回报（order_status=56 已成，§normalize 默认映射 → TRADED）。"""
    return FakeXtOrder(
        account_id=ACCOUNT,
        stock_code="600036.SH",
        order_id=order_id,
        order_sysid="sys-1",
        order_type=order_type,
        offset_flag=48,
        price_type=5,
        price=35.00,
        order_volume=200,
        traded_volume=200,
        traded_price=35.12,
        order_status=order_status,
        status_msg="全部成交",
        order_time=TS_EAST8,
        strategy_name="limit_up",
        order_remark="LUP|2026-06-11|600036.SH",
    )


# ---------------------------------------------------------------------------
# 1) 回调落库规整：代码归一 / 方向 / 状态 / 时间双写
# ---------------------------------------------------------------------------


def test_normalize_trade_basic_fields():
    """成交规整：代码归一 600036.SH、方向 BUY、原值保留、价量类型、order_remark 透传。"""
    xt = _full_trade(stock_code="SH600036")  # 脏前缀形态，验证归一
    rec = normalize_trade(
        xt,
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
        status_resolver=default_status_resolver,
    )
    assert rec.ts_code == "600036.SH"            # 归一后带交易所后缀
    assert rec.qmt_stock_code == "SH600036"      # 原值保留供排查
    assert rec.traded_id == "t1"                 # 字符串化
    assert rec.trade_side == TradeSide.BUY       # order_type=23 → BUY
    assert rec.traded_price == Decimal("35.12")  # Decimal 承载，无 float 误差
    assert rec.traded_volume == 200
    assert rec.traded_amount == Decimal("7024.0")
    assert rec.offset_flag == 48                 # 原值落库
    assert rec.order_remark == "LUP|2026-06-11|600036.SH"
    assert rec.data_source == DataSource.CALLBACK


def test_normalize_trade_sell_side():
    """方向映射：order_type=24 → SELL（默认解析器数值映射）。"""
    xt = _full_trade(order_type=24)
    rec = normalize_trade(
        xt,
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    assert rec.trade_side == TradeSide.SELL


def test_normalize_trade_side_string_passthrough():
    """方向直传：order_type 已是标准字符串 'SELL' → 直接采用（兼容上游已规整）。"""
    xt = _full_trade(order_type="SELL")
    rec = normalize_trade(
        xt,
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    assert rec.trade_side == TradeSide.SELL


def test_normalize_order_status_mapping():
    """委托状态映射：56 → TRADED、55 → PART_TRADED、54 → CANCELLED、57 → REJECTED。"""
    cases = {
        56: OrderStatus.TRADED,
        55: OrderStatus.PART_TRADED,
        54: OrderStatus.CANCELLED,
        57: OrderStatus.REJECTED,
        50: OrderStatus.REPORTED,
    }
    for raw, expected in cases.items():
        rec = normalize_order(
            _full_order(order_status=raw),
            account_id=ACCOUNT,
            trade_date=T_BUY,
            data_source=DataSource.CALLBACK,
            side_resolver=default_side_resolver,
            status_resolver=default_status_resolver,
        )
        assert rec.order_status == expected, f"order_status={raw}"


def test_normalize_order_status_string_passthrough():
    """委托状态直传：已是标准字符串 'TRADED' → 直接采用。"""
    rec = normalize_order(
        _full_order(order_status="TRADED"),
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
        status_resolver=default_status_resolver,
    )
    assert rec.order_status == OrderStatus.TRADED


def test_normalize_order_unknown_status_falls_back_reported():
    """未知状态码 → 兜底 REPORTED（在途留痕，不臆造终态）。"""
    rec = normalize_order(
        _full_order(order_status=999),
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
        status_resolver=default_status_resolver,
    )
    assert rec.order_status == OrderStatus.REPORTED


# ---------------------------------------------------------------------------
# 2) 时间转换：east8 时间戳 → UTC naive(−8h) + east8 原值；ts=None → (None, None)
# ---------------------------------------------------------------------------


def test_normalize_trade_time_double_write():
    """时间双写：traded_time = east8 − 8h（UTC naive）、traded_time_east8 = 东八区原值。"""
    rec = normalize_trade(
        _full_trade(),
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    # 东八区 13:31:02 → UTC 05:31:02（−8h），且为 naive（无 tzinfo）。
    assert rec.traded_time == datetime(2026, 6, 12, 5, 31, 2)
    assert rec.traded_time.tzinfo is None
    # east8 原值 13:31:02（naive）。
    assert rec.traded_time_east8 == datetime(2026, 6, 12, 13, 31, 2)
    assert rec.traded_time_east8.tzinfo is None
    # 与权威口径互验：UTC = east8 转 UTC。
    assert rec.traded_time == _EAST8_DT.astimezone(timezone.utc).replace(tzinfo=None)


def test_normalize_order_time_double_write():
    """委托时间同口径双写。"""
    rec = normalize_order(
        _full_order(),
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
        status_resolver=default_status_resolver,
    )
    assert rec.order_time == datetime(2026, 6, 12, 5, 31, 2)
    assert rec.order_time_east8 == datetime(2026, 6, 12, 13, 31, 2)


def test_normalize_trade_time_none_no_raise():
    """ts=None（缺 traded_time 字段）→ (None, None)，不抛错。"""
    xt = FakeXtTrade(
        account_id=ACCOUNT, stock_code="600036.SH", traded_id="t9",
        order_type=23, traded_price=10.0, traded_volume=100,
    )  # 无 traded_time
    rec = normalize_trade(
        xt,
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    assert rec.traded_time is None
    assert rec.traded_time_east8 is None


# ---------------------------------------------------------------------------
# 3) 版本兼容：缺版本可选字段 → 落 None，不抛 AttributeError
# ---------------------------------------------------------------------------


def test_normalize_trade_missing_optional_fields_no_raise():
    """成交缺 traded_amount / account_type / order_sysid → 落 None，不抛 AttributeError。"""
    xt = FakeXtTrade(
        account_id=ACCOUNT, stock_code="600036.SH", traded_id="t2",
        order_type=23, traded_price=35.12, traded_volume=200, traded_time=TS_EAST8,
    )  # 故意不带 traded_amount / account_type / offset_flag / order_id 等
    rec = normalize_trade(
        xt,
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    assert rec.traded_amount is None
    assert rec.account_type is None
    assert rec.offset_flag is None
    assert rec.order_id is None
    assert rec.ts_code == "600036.SH"


def test_normalize_position_missing_optional_fields_no_raise():
    """持仓缺 avg_price / frozen_volume / on_road_volume / yesterday_volume → 落 None。"""
    xt = FakeXtPosition(
        account_id=ACCOUNT, stock_code="600036.SH", volume=200, can_use_volume=0,
        open_price=35.0, market_value=7080.0,
    )  # 不带 avg_price / frozen_volume / on_road_volume / yesterday_volume / last_price / float_profit
    rec = normalize_position(
        xt,
        account_id=ACCOUNT,
        trade_date=T_BUY,
        snapshot_type=SnapshotType.CLOSE,
    )
    assert rec.avg_price is None
    assert rec.frozen_volume is None
    assert rec.on_road_volume is None
    assert rec.yesterday_volume is None
    assert rec.last_price is None
    assert rec.float_profit is None
    assert rec.volume == 200
    assert rec.open_price == Decimal("35.0")
    assert rec.snapshot_type == SnapshotType.CLOSE
    assert rec.data_source == DataSource.QUERY


def test_normalize_account_missing_optional_fields_no_raise():
    """资产缺 account_type / frozen_cash → 落 None / 默认 0，不抛错。"""
    xt = FakeXtAsset(
        account_id=ACCOUNT, cash=92000.0, total_asset=100000.0, market_value=8000.0,
    )  # 不带 frozen_cash / account_type
    rec = normalize_account(
        xt,
        account_id=ACCOUNT,
        trade_date=T_BUY,
        snapshot_type=SnapshotType.CLOSE,
    )
    assert rec.total_asset == Decimal("100000.0")
    assert rec.cash == Decimal("92000.0")
    assert rec.frozen_cash == Decimal("0")    # 缺失走默认 0
    assert rec.account_type is None
    assert rec.snapshot_type == SnapshotType.CLOSE


# ---------------------------------------------------------------------------
# 4) data_writer 委托：upsert 后 repo 可查到；position/account snapshot_type 正确
# ---------------------------------------------------------------------------


def _writer():
    repo = InMemoryQmtRepository()
    logger = RecordingLogger()
    return DataWriterImpl(repo, logger), repo, logger


def test_data_writer_upsert_trade_delegates_to_repo():
    """DataWriterImpl.upsert_trade 委托后，repo 中可查到该成交记录。"""
    writer, repo, _ = _writer()
    rec = normalize_trade(
        _full_trade(),
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    writer.upsert_trade(rec)
    got = repo.get_trades(ACCOUNT, T_BUY)
    assert len(got) == 1
    assert got[0].ts_code == "600036.SH"
    assert got[0].trade_side == TradeSide.BUY


def test_data_writer_upsert_order_delegates_to_repo():
    """DataWriterImpl.upsert_order 委托后，repo 中可查到该委托记录与终态。"""
    writer, repo, _ = _writer()
    rec = normalize_order(
        _full_order(order_status=56),
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
        status_resolver=default_status_resolver,
    )
    writer.upsert_order(rec)
    got = repo.get_orders(ACCOUNT, T_BUY)
    assert len(got) == 1
    assert got[0].order_status == OrderStatus.TRADED


def test_data_writer_position_snapshot_type_injected():
    """upsert_position 带入 snapshot_type=INTRADAY 后落库，rec 与库内值均为 INTRADAY。"""
    writer, repo, _ = _writer()
    rec = PositionRecord(
        account_id=ACCOUNT, trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        snapshot_type=SnapshotType.CLOSE, volume=200, can_use_volume=0,
    )
    writer.upsert_position(rec, snapshot_type=SnapshotType.INTRADAY)
    assert rec.snapshot_type == SnapshotType.INTRADAY  # 写端注入生效
    positions = repo.all_positions()
    assert len(positions) == 1
    assert positions[0].snapshot_type == SnapshotType.INTRADAY


def test_data_writer_account_snapshot_type_injected():
    """upsert_account_daily 带入 snapshot_type=CLOSE 后落库正确。"""
    writer, repo, _ = _writer()
    rec = AccountRecord(
        account_id=ACCOUNT, trade_date=T_BUY, total_asset=Decimal("100000"), cash=Decimal("92000"),
        snapshot_type=SnapshotType.INTRADAY,
    )
    writer.upsert_account_daily(rec, snapshot_type=SnapshotType.CLOSE)
    assert rec.snapshot_type == SnapshotType.CLOSE
    accounts = repo.all_accounts()
    assert len(accounts) == 1
    assert accounts[0].snapshot_type == SnapshotType.CLOSE


def test_data_writer_position_close_and_intraday_coexist():
    """同日同票 CLOSE 与 INTRADAY 唯一键含 snapshot_type，互不覆盖（落 2 行）。"""
    writer, repo, _ = _writer()
    base = PositionRecord(
        account_id=ACCOUNT, trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        volume=200, can_use_volume=0,
    )
    writer.upsert_position(base, snapshot_type=SnapshotType.INTRADAY)
    base2 = PositionRecord(
        account_id=ACCOUNT, trade_date=T_BUY, ts_code="600036.SH", qmt_stock_code="600036.SH",
        volume=200, can_use_volume=200,
    )
    writer.upsert_position(base2, snapshot_type=SnapshotType.CLOSE)
    assert len(repo.all_positions()) == 2


def test_data_writer_mark_cancel_failed_delegates():
    """mark_cancel_failed 委托后，既有委托行 cancel_failed=1 + error_*，不改终态。"""
    writer, repo, _ = _writer()
    order = normalize_order(
        _full_order(order_id=777, order_status=50),  # REPORTED
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
        status_resolver=default_status_resolver,
    )
    writer.upsert_order(order)
    writer.mark_cancel_failed(ACCOUNT, 777, error_id=88, error_msg="撤单失败")
    got = repo.get_orders(ACCOUNT, T_BUY)[0]
    assert got.cancel_failed is True
    assert got.error_id == 88
    assert got.order_status == OrderStatus.REPORTED  # 终态不被改


def test_data_writer_upsert_failure_logs_error_and_raises():
    """落库失败不静默吞：记 error 日志并重新抛出（语义错误须暴露给上游重试/告警）。"""
    class _BoomRepo(InMemoryQmtRepository):
        def upsert_trade(self, rec):  # type: ignore[override]
            raise RuntimeError("db down")

    logger = RecordingLogger()
    writer = DataWriterImpl(_BoomRepo(), logger)
    rec = normalize_trade(
        _full_trade(),
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    raised = False
    try:
        writer.upsert_trade(rec)
    except RuntimeError:
        raised = True
    assert raised  # 异常向上抛出
    assert "data_writer_upsert_trade_failed" in logger.events()  # 且已留痕


# ---------------------------------------------------------------------------
# 5) 幂等：回调 + QUERY_BACKFILL → 仅 1 行、后到为终态、COALESCE 列不被空覆盖
# ---------------------------------------------------------------------------


def test_idempotent_callback_then_backfill_single_row_terminal():
    """同 traded_id：回调先到（带 east8 + signal_trade_date）+ QUERY_BACKFILL 后到（这两列空）
    → 表中仅 1 行；data_source 取后到 QUERY_BACKFILL；signal_trade_date / *_east8 不被空覆盖。"""
    writer, repo, _ = _writer()

    # 回调先到：带 signal_trade_date 与 east8 原值（traded_time 双写完整）。
    cb = normalize_trade(
        _full_trade(traded_id="dup1"),
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    cb.signal_trade_date = T_SIGNAL  # 模拟 order_remark 解析回填（§6.8）
    writer.upsert_trade(cb)

    # QUERY_BACKFILL 后到：故意把 signal_trade_date 与 traded_time_east8 置空（缺 traded_time）。
    bf_xt = FakeXtTrade(
        account_id=ACCOUNT, stock_code="600036.SH", traded_id="dup1",
        order_type=23, traded_price=35.20, traded_volume=200,
    )  # 无 traded_time → east8 为 None；无 signal_trade_date
    bf = normalize_trade(
        bf_xt,
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.QUERY_BACKFILL,
        side_resolver=default_side_resolver,
    )
    assert bf.signal_trade_date is None
    assert bf.traded_time_east8 is None
    writer.upsert_trade(bf)

    rows = repo.get_trades(ACCOUNT, T_BUY)
    assert len(rows) == 1                                  # 同唯一键仅 1 行
    row = rows[0]
    assert row.data_source == DataSource.QUERY_BACKFILL    # 后到为终态来源
    assert row.traded_price == Decimal("35.20")            # 价量覆盖为后到值
    # COALESCE 口径：已回填的 signal_trade_date / east8 不被后到空值覆盖（§6.5）。
    assert row.signal_trade_date == T_SIGNAL
    assert row.traded_time_east8 == datetime(2026, 6, 12, 13, 31, 2)


def test_idempotent_order_status_advances_to_terminal():
    """同 order_id：REPORTED 回调 → TRADED 回调，仅 1 行、终态为后到 TRADED。"""
    writer, repo, _ = _writer()
    reported = normalize_order(
        _full_order(order_id=555, order_status=50),  # REPORTED
        account_id=ACCOUNT, trade_date=T_BUY, data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver, status_resolver=default_status_resolver,
    )
    writer.upsert_order(reported)
    traded = normalize_order(
        _full_order(order_id=555, order_status=56),  # TRADED
        account_id=ACCOUNT, trade_date=T_BUY, data_source=DataSource.QUERY_BACKFILL,
        side_resolver=default_side_resolver, status_resolver=default_status_resolver,
    )
    writer.upsert_order(traded)
    rows = repo.get_orders(ACCOUNT, T_BUY)
    assert len(rows) == 1
    assert rows[0].order_status == OrderStatus.TRADED
    assert rows[0].data_source == DataSource.QUERY_BACKFILL
