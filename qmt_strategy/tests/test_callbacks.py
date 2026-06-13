"""实时回调采集 ExecCallback 单测（§6.2.1 / §6.9）。

全部用 fake / 内存实现，不连真实 xttrader / MySQL：
- 回报对象用 contracts.xt_objects 的 FakeXt* / FakeXtOrderError / FakeXtCancelError 构造
  （带 / 不带版本可选字段两组）；
- 落库写端用 DataWriterImpl(InMemoryQmtRepository())（真实幂等语义）；
- 台账用 InMemoryLocalLedger；日志用 RecordingLogger 断言断线告警。

覆盖（对齐题面「单测必须覆盖」）：
1) 回调落库：on_stock_trade / on_stock_order / on_stock_asset / on_stock_position →
   repo 中落到规整后记录（代码归一 / 方向状态映射 / 时间双写 / INTRADAY 快照）。
2) 废单 / 拒单：on_order_error → qmt_order 行 order_status=ERROR + error_*。
3) 撤单失败：on_cancel_error → 既有委托行 cancel_failed=1，不误改终态、不新增重复行。
4) 幂等：同一 traded_id 经 on_stock_trade 与（模拟）QUERY_BACKFILL 各写一次 → repo 仅 1 行、终态为后到值。
5) 版本兼容：缺可选字段的 fake 对象 → 落 None 不抛 AttributeError。
6) on_stock_trade 同步台账：台账已有该 order_id 计划单 → add_fill 后 filled_volume 增加。
7) on_disconnected：告警 + 触发钩子。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import SHANGHAI
from qmt_strategy.contracts.enums import (
    DataSource,
    OrderPhase,
    OrderState,
    OrderStatus,
    SnapshotType,
    TradeSide,
)
from qmt_strategy.contracts.models import LedgerEntry, TradeRecord
from qmt_strategy.contracts.xt_objects import (
    FakeXtAsset,
    FakeXtCancelError,
    FakeXtOrder,
    FakeXtOrderError,
    FakeXtPosition,
    FakeXtTrade,
)
from qmt_strategy.data_writer.callbacks import ExecCallback
from qmt_strategy.data_writer.data_writer import DataWriterImpl
from qmt_strategy.data_writer.normalize import normalize_trade
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.order.local_ledger import InMemoryLocalLedger

ACCOUNT = "1000000365"
T_BUY = date(2026, 6, 12)
T_SIGNAL = date(2026, 6, 11)

# 东八区 2026-06-12 13:31:02 → Unix 时间戳；对应 UTC naive 应为 05:31:02（−8h）。
_EAST8_DT = datetime(2026, 6, 12, 13, 31, 2, tzinfo=SHANGHAI)
TS_EAST8 = int(_EAST8_DT.timestamp())


# ---------------------------------------------------------------------------
# 公共装配：DataWriterImpl(InMemoryQmtRepository) + InMemoryLocalLedger + ExecCallback
# ---------------------------------------------------------------------------


def _build(on_disconnected_hook=None):
    """构造一套 (repo, writer, ledger, logger, callback)，trade_date 固定为 T_BUY。"""
    repo = InMemoryQmtRepository()
    logger = RecordingLogger()
    writer = DataWriterImpl(repo, logger)
    ledger = InMemoryLocalLedger()
    hook_calls = {"n": 0}

    def _default_hook() -> None:
        hook_calls["n"] += 1

    cb = ExecCallback(
        writer,
        ledger,
        logger,
        account_id=ACCOUNT,
        trade_date_provider=lambda: T_BUY,
        on_disconnected_hook=on_disconnected_hook or _default_hook,
    )
    return repo, writer, ledger, logger, cb, hook_calls


def _full_trade(traded_id="t1", order_id=12345, order_type=23, stock_code="600036.SH",
                traded_volume=200, traded_price=35.12):
    """完整成交回报（含 traded_amount / account_type 等版本可选字段）。"""
    return FakeXtTrade(
        account_id=ACCOUNT,
        account_type=2,
        stock_code=stock_code,
        traded_id=traded_id,
        order_id=order_id,
        order_sysid="sys-1",
        order_type=order_type,
        offset_flag=48,
        traded_price=traded_price,
        traded_volume=traded_volume,
        traded_amount=7024.0,
        traded_time=TS_EAST8,
        strategy_name="limit_up",
        order_remark="LUP|2026-06-11|600036.SH",
    )


def _full_order(order_id=12345, order_status=56, order_type=23):
    """完整委托回报（order_status=56 已成 → TRADED）。"""
    return FakeXtOrder(
        account_id=ACCOUNT,
        stock_code="600036.SH",
        order_id=order_id,
        order_sysid="sys-1",
        order_type=order_type,
        offset_flag=48,
        price_type=5,
        price=35.12,
        order_volume=200,
        traded_volume=200,
        traded_price=35.12,
        order_status=order_status,
        status_msg="全部成交",
        order_time=TS_EAST8,
        strategy_name="limit_up",
        order_remark="LUP|2026-06-11|600036.SH",
    )


def _make_ledger_entry(order_id=12345, plan_volume=200, ts_code="600036.SH"):
    """构造一笔已发出、台账已回填 order_id 的计划单（SUBMITTED）。"""
    return LedgerEntry(
        biz_order_no="BIZ-1",
        account_id=ACCOUNT,
        target_trade_date=T_BUY,
        ts_code=ts_code,
        strategy_family="打板",
        side=TradeSide.BUY,
        plan_volume=plan_volume,
        plan_price=Decimal("35.12"),
        order_remark="LUP|2026-06-11|600036.SH",
        signal_trade_date=T_SIGNAL,
        state=OrderState.SUBMITTED,
        order_id=order_id,
        order_phase=OrderPhase.OPENING,
    )


# ---------------------------------------------------------------------------
# 1) 回调落库：成交规整正确（代码归一 / 方向 / 时间双写）
# ---------------------------------------------------------------------------


def test_on_stock_trade_lands_normalized_record():
    repo, _w, _l, _lg, cb, _h = _build()
    cb.on_stock_trade(_full_trade())

    trades = repo.get_trades(ACCOUNT, T_BUY)
    assert len(trades) == 1
    rec = trades[0]
    # 代码归一：600036.SH 原样归一，qmt_stock_code 保原值。
    assert rec.ts_code == "600036.SH"
    assert rec.qmt_stock_code == "600036.SH"
    # 方向：order_type=23 → BUY。
    assert rec.trade_side == TradeSide.BUY
    # 价位 Decimal 承载、数量 int。
    assert rec.traded_price == Decimal("35.12")
    assert rec.traded_volume == 200
    # 时间双写：UTC naive = east8 − 8h；east8 原值保留。
    assert rec.traded_time == datetime(2026, 6, 12, 5, 31, 2)
    assert rec.traded_time_east8 == datetime(2026, 6, 12, 13, 31, 2)
    # data_source 固定 CALLBACK。
    assert rec.data_source == DataSource.CALLBACK
    # trade_date 来自 provider。
    assert rec.trade_date == T_BUY


def test_on_stock_trade_code_normalization_dirty_code():
    """脏代码 SH600036 也应归一为 600036.SH，原值落 qmt_stock_code。"""
    repo, _w, _l, _lg, cb, _h = _build()
    cb.on_stock_trade(_full_trade(stock_code="SH600036"))
    rec = repo.get_trades(ACCOUNT, T_BUY)[0]
    assert rec.ts_code == "600036.SH"
    assert rec.qmt_stock_code == "SH600036"


# ---------------------------------------------------------------------------
# 1) 回调落库：委托规整 + 状态映射 + 台账状态同步
# ---------------------------------------------------------------------------


def test_on_stock_order_lands_and_maps_status():
    repo, _w, ledger, _lg, cb, _h = _build()
    # 台账先有该 order_id 的计划单，验证 sync_status 推进。
    ledger.insert(_make_ledger_entry(order_id=12345))

    cb.on_stock_order(_full_order(order_id=12345, order_status=56))

    orders = repo.get_orders(ACCOUNT, T_BUY)
    assert len(orders) == 1
    rec = orders[0]
    assert rec.order_id == 12345
    assert rec.order_status == OrderStatus.TRADED       # 56 → 已成
    assert rec.trade_side == TradeSide.BUY
    assert rec.order_time == datetime(2026, 6, 12, 5, 31, 2)
    assert rec.data_source == DataSource.CALLBACK
    # 台账同步：OrderStatus.TRADED → OrderState.TRADED。
    assert ledger.get_by_order_id(12345).state == OrderState.TRADED


def test_on_stock_order_sells_side_and_part_traded():
    """卖出 + 部成：order_type=24 → SELL，order_status=55 → PART_TRADED，台账同步。"""
    repo, _w, ledger, _lg, cb, _h = _build()
    ledger.insert(_make_ledger_entry(order_id=999, plan_volume=400))

    o = _full_order(order_id=999, order_status=55, order_type=24)
    o.order_volume = 400
    o.traded_volume = 200
    cb.on_stock_order(o)

    rec = repo.get_orders(ACCOUNT, T_BUY)[0]
    assert rec.trade_side == TradeSide.SELL
    assert rec.order_status == OrderStatus.PART_TRADED
    assert ledger.get_by_order_id(999).state == OrderState.PART_TRADED


def test_on_stock_order_unknown_order_id_does_not_touch_ledger():
    """台账无该 order_id（手工单）：落库照常，sync_status 自身忽略，不抛错。"""
    repo, _w, ledger, _lg, cb, _h = _build()
    cb.on_stock_order(_full_order(order_id=55555, order_status=50))
    assert len(repo.get_orders(ACCOUNT, T_BUY)) == 1
    assert ledger.get_by_order_id(55555) is None


# ---------------------------------------------------------------------------
# 2) 废单 / 拒单：on_order_error 独有落库（order_status=ERROR + error_*）
# ---------------------------------------------------------------------------


def test_on_order_error_lands_error_record():
    repo, _w, ledger, _lg, cb, _h = _build()
    ledger.insert(_make_ledger_entry(order_id=777))

    e = FakeXtOrderError(order_id=777, error_id=-15, error_msg="资金不足")
    cb.on_order_error(e)

    orders = repo.get_orders(ACCOUNT, T_BUY)
    assert len(orders) == 1
    rec = orders[0]
    assert rec.order_id == 777
    assert rec.order_status == OrderStatus.ERROR        # 回调独有失败终态
    assert rec.error_id == -15
    assert rec.error_msg == "资金不足"
    assert rec.data_source == DataSource.CALLBACK
    # 台账同步推进 ERROR 终态，避免计划单凭空消失。
    assert ledger.get_by_order_id(777).state == OrderState.ERROR


def test_on_order_error_missing_optional_fields_no_attribute_error():
    """版本兼容：XtOrderError 仅 order_id/error_id/error_msg，缺 stock_code 等 → 落 None 不抛错。"""
    repo, _w, _l, _lg, cb, _h = _build()
    e = FakeXtOrderError(order_id=888, error_id=-1, error_msg="拒单")
    cb.on_order_error(e)  # 不应抛 AttributeError

    rec = repo.get_orders(ACCOUNT, T_BUY)[0]
    assert rec.order_id == 888
    assert rec.order_status == OrderStatus.ERROR
    assert rec.ts_code is None            # 无 stock_code → 归一为 None
    assert rec.qmt_stock_code is None
    assert rec.order_volume == 0          # 缺 order_volume → 默认 0
    assert rec.order_remark is None


# ---------------------------------------------------------------------------
# 3) 撤单失败：on_cancel_error 在既有委托行打 cancel_failed=1，不改终态、不新增行
# ---------------------------------------------------------------------------


def test_on_cancel_error_marks_existing_row_without_new_row_or_status_change():
    repo, _w, _l, _lg, cb, _h = _build()
    # 先有一条已成委托行。
    cb.on_stock_order(_full_order(order_id=321, order_status=56))
    assert repo.count_orders() == 1
    before = repo.get_orders(ACCOUNT, T_BUY)[0]
    assert before.order_status == OrderStatus.TRADED
    assert before.cancel_failed is False

    # 撤单失败回调（典型场景：已成单又发撤单被拒）。
    e = FakeXtCancelError(order_id=321, error_id=-20, error_msg="不可撤")
    cb.on_cancel_error(e)

    # 不新增重复行。
    assert repo.count_orders() == 1
    after = repo.get_orders(ACCOUNT, T_BUY)[0]
    # 仅追加 cancel_failed=1 + error_*；不改 order_status 终态。
    assert after.cancel_failed is True
    assert after.error_id == -20
    assert after.error_msg == "不可撤"
    assert after.order_status == OrderStatus.TRADED


def test_on_cancel_error_missing_order_id_warns_and_skips():
    """缺 order_id：无法定位既有行 → 记 warn 后返回，不臆造新行、不抛错。"""
    repo, _w, _l, logger, cb, _h = _build()
    e = FakeXtCancelError(error_id=-1, error_msg="未知")  # 无 order_id
    cb.on_cancel_error(e)
    assert repo.count_orders() == 0
    assert "on_cancel_error_missing_order_id" in logger.events()


# ---------------------------------------------------------------------------
# 4) 幂等：同一 traded_id 经 CALLBACK 与 QUERY_BACKFILL 各写一次 → 仅 1 行、终态为后到值
# ---------------------------------------------------------------------------


def test_trade_idempotent_callback_then_backfill_single_row_latest_wins():
    repo, writer, _l, _lg, cb, _h = _build()
    # 第一次：回调路径，成交量 200。
    cb.on_stock_trade(_full_trade(traded_id="dup-1", traded_volume=200, traded_price=35.12))
    # 第二次：模拟收盘兜底 / 断线补采，同 traded_id、QUERY_BACKFILL、后到值（成交量 250 视为权威修正）。
    backfill = _full_trade(traded_id="dup-1", traded_volume=250, traded_price=35.20)
    rec_bf: TradeRecord = normalize_trade(
        backfill,
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.QUERY_BACKFILL,
        side_resolver=cb._side_resolver,
    )
    writer.upsert_trade(rec_bf)

    trades = repo.get_trades(ACCOUNT, T_BUY)
    assert len(trades) == 1                      # 同唯一键仅 1 行
    assert trades[0].traded_volume == 250        # 终态为后到值
    assert trades[0].data_source == DataSource.QUERY_BACKFILL


def test_trade_idempotent_does_not_null_overwrite_signal_trade_date():
    """COALESCE 口径：先写带 signal_trade_date，后到空值不得覆盖（§6.5）。"""
    repo, writer, _l, _lg, cb, _h = _build()
    # 先经 backfill 写入带 signal_trade_date 的行。
    first = _full_trade(traded_id="coal-1")
    rec1 = normalize_trade(
        first, account_id=ACCOUNT, trade_date=T_BUY,
        data_source=DataSource.QUERY_BACKFILL, side_resolver=cb._side_resolver,
    )
    rec1.signal_trade_date = T_SIGNAL
    writer.upsert_trade(rec1)
    # 后经回调写入同键、signal_trade_date 为 None（回调侧不带）。
    cb.on_stock_trade(_full_trade(traded_id="coal-1"))

    rec = repo.get_trades(ACCOUNT, T_BUY)[0]
    assert rec.signal_trade_date == T_SIGNAL     # 不被空覆盖
    assert rec.data_source == DataSource.CALLBACK  # 普通列后到覆盖


# ---------------------------------------------------------------------------
# 5) 版本兼容：缺可选字段的 fake 对象 → 落 None 不抛 AttributeError
# ---------------------------------------------------------------------------


def test_trade_minimal_object_no_attribute_error():
    """最小成交对象（缺 traded_amount / account_type / order_remark 等）→ 落 None 不抛错。"""
    repo, _w, _l, _lg, cb, _h = _build()
    minimal = FakeXtTrade(
        stock_code="000001.SZ",
        traded_id="m1",
        order_type=23,
        traded_price=12.34,
        traded_volume=100,
        # 故意不带 traded_time / traded_amount / order_id / account_type / strategy_name
    )
    cb.on_stock_trade(minimal)  # 不应抛 AttributeError
    rec = repo.get_trades(ACCOUNT, T_BUY)[0]
    assert rec.ts_code == "000001.SZ"
    assert rec.traded_amount is None
    assert rec.account_type is None
    assert rec.order_id is None
    assert rec.traded_time is None and rec.traded_time_east8 is None


def test_position_minimal_object_intraday_no_attribute_error():
    """最小持仓对象（缺 avg_price / frozen_volume 等）→ 落 None / 默认 0，snapshot_type=INTRADAY。"""
    repo, _w, _l, _lg, cb, _h = _build()
    p = FakeXtPosition(
        stock_code="600036.SH",
        volume=300,
        can_use_volume=0,
        # 缺 avg_price / frozen_volume / on_road_volume / yesterday_volume / market_value
    )
    cb.on_stock_position(p)  # 不应抛 AttributeError
    poss = repo.all_positions()
    assert len(poss) == 1
    rec = poss[0]
    assert rec.ts_code == "600036.SH"
    assert rec.snapshot_type == SnapshotType.INTRADAY
    assert rec.data_source == DataSource.CALLBACK
    assert rec.volume == 300
    assert rec.avg_price is None
    assert rec.frozen_volume is None


def test_asset_object_intraday_lands():
    """盘中资产 → INTRADAY 行，CALLBACK 来源；缺字段落默认。"""
    repo, _w, _l, _lg, cb, _h = _build()
    a = FakeXtAsset(
        account_id=ACCOUNT,
        cash=50000.0,
        frozen_cash=0.0,
        market_value=150000.0,
        total_asset=200000.0,
    )
    cb.on_stock_asset(a)
    accts = repo.all_accounts()
    assert len(accts) == 1
    rec = accts[0]
    assert rec.snapshot_type == SnapshotType.INTRADAY
    assert rec.data_source == DataSource.CALLBACK
    assert rec.total_asset == Decimal("200000.0")
    assert rec.cash == Decimal("50000.0")


# ---------------------------------------------------------------------------
# 6) on_stock_trade 同步台账：filled_volume 增加，推进状态
# ---------------------------------------------------------------------------


def test_on_stock_trade_syncs_ledger_fill():
    repo, _w, ledger, _lg, cb, _h = _build()
    ledger.insert(_make_ledger_entry(order_id=12345, plan_volume=200))
    assert ledger.get_by_order_id(12345).filled_volume == 0

    cb.on_stock_trade(_full_trade(order_id=12345, traded_volume=200, traded_price=35.12))

    e = ledger.get_by_order_id(12345)
    assert e.filled_volume == 200                 # 累计成交增加
    assert e.avg_filled_price == Decimal("35.12")
    assert e.state == OrderState.TRADED           # 达计划量 → TRADED
    # 同时成交也落库（成交是唯一事实源）。
    assert len(repo.get_trades(ACCOUNT, T_BUY)) == 1


def test_on_stock_trade_partial_then_full_accumulates():
    """两笔成交累计：100 + 100 → filled 200，先 PART_TRADED 后 TRADED。"""
    _repo, _w, ledger, _lg, cb, _h = _build()
    ledger.insert(_make_ledger_entry(order_id=12345, plan_volume=200))

    cb.on_stock_trade(_full_trade(traded_id="p1", order_id=12345, traded_volume=100))
    assert ledger.get_by_order_id(12345).state == OrderState.PART_TRADED
    assert ledger.get_by_order_id(12345).filled_volume == 100

    cb.on_stock_trade(_full_trade(traded_id="p2", order_id=12345, traded_volume=100))
    assert ledger.get_by_order_id(12345).state == OrderState.TRADED
    assert ledger.get_by_order_id(12345).filled_volume == 200


def test_on_stock_trade_unknown_order_id_ignored_by_ledger_but_lands():
    """成交 order_id 台账无对应单：add_fill 自身忽略，但成交仍落库（绝不可丢）。"""
    repo, _w, ledger, _lg, cb, _h = _build()
    cb.on_stock_trade(_full_trade(traded_id="x1", order_id=42))
    assert ledger.get_by_order_id(42) is None
    assert len(repo.get_trades(ACCOUNT, T_BUY)) == 1


# ---------------------------------------------------------------------------
# 7) on_disconnected：告警 + 触发钩子
# ---------------------------------------------------------------------------


def test_on_disconnected_warns_and_triggers_hook():
    repo, _w, _l, logger, cb, hook_calls = _build()
    cb.on_disconnected()
    assert "disconnected" in logger.events()
    assert hook_calls["n"] == 1


def test_on_disconnected_custom_hook_invoked():
    calls = {"n": 0}

    def hook() -> None:
        calls["n"] += 1

    _repo, _w, _l, logger, cb, _h = _build(on_disconnected_hook=hook)
    cb.on_disconnected()
    cb.on_disconnected()
    assert calls["n"] == 2
    assert logger.events().count("disconnected") == 2
