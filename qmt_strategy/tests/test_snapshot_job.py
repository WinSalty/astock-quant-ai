"""定时全量快照 / 补采作业 SnapshotJob 单测（§6.2.2 / §6.2.3 / §6.9）。

全部用 fake / 内存实现，不连真实 xttrader / MySQL：
- 回报对象用 contracts.xt_objects 的 FakeXt*（带 / 不带版本可选字段两组）；
- 拉取端用 QueryFakeTrader（实现 XtTraderLike 的 query_* 子集，可注入资产查询失败序列）；
- 落库写端用 DataWriterImpl(InMemoryQmtRepository())（真实幂等语义）；
- 日志用 RecordingLogger 断言 backfill_done / 失败强告警等事件。

覆盖（对齐题面「单测必须覆盖」）：
1) 收盘兜底：query_stock_trades / orders 返回当日全集 → 漏采明细被补齐为当日权威全集（repo 条数正确）；
   CLOSE 资产 / 持仓快照各落 1 行。
2) 断线补采：run_backfill → 断线期间缺失明细被补回、data_source=QUERY_BACKFILL；
   asset / positions 刷 INTRADAY；日志含 backfill_done(missing_recovered=N)。
3) CLOSE 失败重试：query_stock_asset 前两次抛异常、第三次成功 → 断言退避重试且最终落库；
   全失败 → 强告警 snapshot_close_asset_failed 且向上抛出。
4) 版本兼容：fake 对象缺可选字段 → 规整落 None 不抛错。
5) run_open：query_stock_positions → OPEN 快照（昨夜拥股基线）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, List, Optional

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import SHANGHAI, FakeClock
from qmt_strategy.contracts.enums import DataSource, SnapshotType
from qmt_strategy.contracts.xt_objects import (
    FakeStockAccount,
    FakeXtAsset,
    FakeXtOrder,
    FakeXtPosition,
    FakeXtTrade,
)
from qmt_strategy.data_writer.data_writer import DataWriterImpl
from qmt_strategy.data_writer.repository import InMemoryQmtRepository
from qmt_strategy.data_writer.snapshot_job import SnapshotJob

ACCOUNT = "1000000365"
T_BUY = date(2026, 6, 12)
T_SIGNAL = date(2026, 6, 11)

# 东八区 2026-06-12 13:31:02 → Unix 时间戳；对应 UTC naive 应为 05:31:02（−8h）。
_EAST8_DT = datetime(2026, 6, 12, 13, 31, 2, tzinfo=SHANGHAI)
TS_EAST8 = int(_EAST8_DT.timestamp())


# ---------------------------------------------------------------------------
# fake 拉取端：实现 XtTraderLike 的 query_* 子集，可注入资产查询失败序列
# ---------------------------------------------------------------------------


class QueryFakeTrader:
    """记录 query_* 调用并返回预置数据的 fake XtQuantTrader（实现 XtTraderLike query_* 子集）。

    - trades / orders / positions：预置当日全集，query_* 直接返回；
    - asset：预置资产对象；
    - asset_fail_times：query_stock_asset 前 N 次抛异常模拟瞬时故障（用于 CLOSE 退避重试用例）；
    - asset_calls / 各计数：记录调用次数，供断言重试次数 / 是否被调。
    """

    def __init__(
        self,
        *,
        trades: Optional[List[Any]] = None,
        orders: Optional[List[Any]] = None,
        positions: Optional[List[Any]] = None,
        asset: Any = None,
        asset_fail_times: int = 0,
    ) -> None:
        self._trades = trades or []
        self._orders = orders or []
        self._positions = positions or []
        self._asset = asset
        self._asset_fail_times = asset_fail_times
        self.asset_calls = 0
        self.trades_calls = 0
        self.orders_calls = 0
        self.positions_calls = 0

    def query_stock_trades(self, account: Any) -> List[Any]:
        self.trades_calls += 1
        return list(self._trades)

    def query_stock_orders(self, account: Any) -> List[Any]:
        self.orders_calls += 1
        return list(self._orders)

    def query_stock_positions(self, account: Any) -> List[Any]:
        self.positions_calls += 1
        return list(self._positions)

    def query_stock_asset(self, account: Any) -> Any:
        self.asset_calls += 1
        # 前 asset_fail_times 次抛异常，模拟收盘资产查询瞬时故障（DB / 网络抖动）。
        if self.asset_calls <= self._asset_fail_times:
            raise RuntimeError("query_stock_asset transient failure #%d" % self.asset_calls)
        return self._asset


# ---------------------------------------------------------------------------
# 构造助手
# ---------------------------------------------------------------------------


def _full_trade(traded_id="t1", order_id=12345, stock_code="600036.SH",
                traded_volume=200, traded_price=35.12):
    """完整成交回报（含 traded_amount / account_type 等版本可选字段）。"""
    return FakeXtTrade(
        account_id=ACCOUNT,
        account_type=2,
        stock_code=stock_code,
        traded_id=traded_id,
        order_id=order_id,
        order_sysid="sys-1",
        order_type=23,
        offset_flag=48,
        traded_price=traded_price,
        traded_volume=traded_volume,
        traded_amount=7024.0,
        traded_time=TS_EAST8,
        strategy_name="limit_up",
        order_remark="LUP|2026-06-11|600036.SH",
    )


def _bare_trade(traded_id="t-bare", order_id=99999, stock_code="000001.SZ"):
    """精简成交回报：故意缺 traded_amount / account_type / order_sysid / strategy_name 等可选字段。

    用于版本兼容用例：normalize 用 getattr(..., None) 取值，缺失字段必须落 None 不抛 AttributeError。
    """
    return FakeXtTrade(
        stock_code=stock_code,
        traded_id=traded_id,
        order_id=order_id,
        order_type=23,
        traded_price=12.34,
        traded_volume=100,
        # 无 traded_time / traded_amount / account_type / offset_flag / strategy_name / order_remark
    )


def _full_order(order_id=12345, order_status=56, order_type=23, stock_code="600036.SH"):
    """完整委托回报（order_status=56 已成 → TRADED）。"""
    return FakeXtOrder(
        account_id=ACCOUNT,
        account_type=2,
        stock_code=stock_code,
        order_id=order_id,
        order_sysid="sys-1",
        order_type=order_type,
        offset_flag=48,
        price_type=5,
        price=35.10,
        order_volume=200,
        traded_volume=200,
        traded_price=35.12,
        order_status=order_status,
        status_msg="全部成交",
        order_time=TS_EAST8,
        strategy_name="limit_up",
        order_remark="LUP|2026-06-11|600036.SH",
    )


def _full_asset(total_asset=100000.0, cash=40000.0, market_value=60000.0, frozen_cash=0.0):
    """完整资产对象（XtAsset 基础字段）。"""
    return FakeXtAsset(
        account_id=ACCOUNT,
        account_type=2,
        cash=cash,
        frozen_cash=frozen_cash,
        market_value=market_value,
        total_asset=total_asset,
    )


def _full_position(stock_code="600036.SH", volume=200, can_use_volume=0):
    """完整持仓对象（含版本可选 frozen_volume / avg_price）。"""
    return FakeXtPosition(
        account_id=ACCOUNT,
        account_type=2,
        stock_code=stock_code,
        volume=volume,
        can_use_volume=can_use_volume,
        frozen_volume=0,
        on_road_volume=0,
        yesterday_volume=0,
        open_price=34.00,
        avg_price=35.00,
        market_value=7024.0,
    )


def _bare_position(stock_code="000001.SZ", volume=100):
    """精简持仓对象：缺 avg_price / frozen_volume / on_road_volume / yesterday_volume / open_price。"""
    return FakeXtPosition(
        stock_code=stock_code,
        volume=volume,
        can_use_volume=volume,
        market_value=1234.0,
        # 无 avg_price / frozen_volume / on_road_volume / yesterday_volume / open_price / account_type
    )


def _bare_asset(total_asset=50000.0, cash=50000.0):
    """精简资产对象：缺 frozen_cash / market_value / account_type。"""
    return FakeXtAsset(total_asset=total_asset, cash=cash)


def _clock() -> FakeClock:
    # 固定 UTC naive 时刻；本作业不直接读 clock 判时段，仅留作扩展占位。
    return FakeClock(datetime(2026, 6, 12, 7, 5, 0))


def _build(trader: QueryFakeTrader, *, max_retries: int = 3, backoff_sleeper=None):
    """装配一套 (repo, writer, logger, job)，trade_date 固定 provider 回落为 T_BUY。"""
    repo = InMemoryQmtRepository()
    logger = RecordingLogger()
    writer = DataWriterImpl(repo, logger)
    account = FakeStockAccount(ACCOUNT, account_type=2)
    kwargs = {}
    if backoff_sleeper is not None:
        kwargs["backoff_sleeper"] = backoff_sleeper
    job = SnapshotJob(
        trader,
        account,
        writer,
        logger,
        _clock(),
        account_id=ACCOUNT,
        trade_date_provider=lambda: T_BUY,
        max_retries=max_retries,
        **kwargs,
    )
    return repo, writer, logger, job


# ===========================================================================
# 1) 收盘兜底：明细全集补齐 + CLOSE 资产 / 持仓快照各 1 行
# ===========================================================================


def test_run_close_backfills_full_detail_set_and_close_snapshots():
    """收盘批次：query_* 返回当日全集 → 漏采明细补齐为权威全集；CLOSE 资产 / 持仓各落 1 行。"""
    trades = [_full_trade(traded_id="t1", order_id=1),
              _full_trade(traded_id="t2", order_id=2, stock_code="000001.SZ")]
    orders = [_full_order(order_id=1), _full_order(order_id=2, stock_code="000001.SZ")]
    positions = [_full_position(stock_code="600036.SH"),
                 _full_position(stock_code="000001.SZ")]
    trader = QueryFakeTrader(
        trades=trades, orders=orders, positions=positions, asset=_full_asset()
    )
    repo, writer, logger, job = _build(trader)

    recovered = job.run_close(T_BUY)

    # 明细兜底：成交 2 条 + 委托 2 条 = 4 条补回。
    assert recovered == 4
    assert repo.count_trades() == 2
    assert repo.count_orders() == 2
    # 明细 data_source 标记为 QUERY_BACKFILL（收盘兜底来源）。
    for t in repo.get_trades(ACCOUNT, T_BUY):
        assert t.data_source == DataSource.QUERY_BACKFILL
    for o in repo.get_orders(ACCOUNT, T_BUY):
        assert o.data_source == DataSource.QUERY_BACKFILL

    # CLOSE 资产快照恰 1 行、类型 CLOSE、净值字段规整正确。
    accounts = repo.all_accounts()
    assert len(accounts) == 1
    assert accounts[0].snapshot_type == SnapshotType.CLOSE
    assert accounts[0].total_asset == Decimal("100000.0")

    # CLOSE 持仓快照 2 行、均为 CLOSE。
    positions_rows = repo.all_positions()
    assert len(positions_rows) == 2
    assert all(p.snapshot_type == SnapshotType.CLOSE for p in positions_rows)

    # 收盘完成留痕。
    assert "snapshot_close_done" in logger.events()


def test_run_close_recovers_missed_callback_detail():
    """盘中回调只落了部分明细，收盘兜底拉全集 → 漏采的那条被补齐（最终权威全集条数正确）。"""
    # 模拟回调先落 t1（CALLBACK），收盘 query_* 返回 t1 + t2 全集。
    repo = InMemoryQmtRepository()
    logger = RecordingLogger()
    writer = DataWriterImpl(repo, logger)
    # 先用回调路径口径写入 t1（这里直接 normalize 成 CALLBACK 落库模拟「盘中已采」）。
    from qmt_strategy.data_writer.normalize import default_side_resolver, normalize_trade

    pre = normalize_trade(
        _full_trade(traded_id="t1", order_id=1),
        account_id=ACCOUNT,
        trade_date=T_BUY,
        data_source=DataSource.CALLBACK,
        side_resolver=default_side_resolver,
    )
    writer.upsert_trade(pre)
    assert repo.count_trades() == 1

    # 收盘兜底：query_* 返回 t1 + t2（t2 是盘中漏采的）。
    trader = QueryFakeTrader(
        trades=[_full_trade(traded_id="t1", order_id=1),
                _full_trade(traded_id="t2", order_id=2, stock_code="000001.SZ")],
        orders=[],
        positions=[_full_position()],
        asset=_full_asset(),
    )
    account = FakeStockAccount(ACCOUNT, account_type=2)
    job = SnapshotJob(
        trader, account, writer, logger, _clock(),
        account_id=ACCOUNT, trade_date_provider=lambda: T_BUY,
    )
    job.run_close(T_BUY)

    # 当日权威全集应为 2 条（t1 幂等不重复，t2 被补回）。
    assert repo.count_trades() == 2
    ids = {t.traded_id for t in repo.get_trades(ACCOUNT, T_BUY)}
    assert ids == {"t1", "t2"}
    # t1 被收盘兜底覆盖为 QUERY_BACKFILL 终态（后到覆盖语义）。
    t1 = [t for t in repo.get_trades(ACCOUNT, T_BUY) if t.traded_id == "t1"][0]
    assert t1.data_source == DataSource.QUERY_BACKFILL


# ===========================================================================
# 2) 断线补采：缺失明细补回 + INTRADAY 刷新 + backfill_done 日志
# ===========================================================================


def test_run_backfill_recovers_details_as_query_backfill_and_refreshes_intraday():
    """断线补采：明细补回标 QUERY_BACKFILL；资产 / 持仓刷 INTRADAY；日志含 backfill_done。"""
    trades = [_full_trade(traded_id="t1", order_id=1),
              _full_trade(traded_id="t2", order_id=2, stock_code="000001.SZ")]
    orders = [_full_order(order_id=1)]
    positions = [_full_position()]
    trader = QueryFakeTrader(
        trades=trades, orders=orders, positions=positions, asset=_full_asset()
    )
    repo, writer, logger, job = _build(trader)

    n = job.run_backfill(T_BUY)

    # 补回明细 = 成交 2 + 委托 1 = 3。
    assert n == 3
    assert repo.count_trades() == 2
    assert repo.count_orders() == 1
    for t in repo.get_trades(ACCOUNT, T_BUY):
        assert t.data_source == DataSource.QUERY_BACKFILL
    for o in repo.get_orders(ACCOUNT, T_BUY):
        assert o.data_source == DataSource.QUERY_BACKFILL

    # 资产 / 持仓刷 INTRADAY（断线补采属盘中态，不进历史净值）。
    accounts = repo.all_accounts()
    assert len(accounts) == 1
    assert accounts[0].snapshot_type == SnapshotType.INTRADAY
    positions_rows = repo.all_positions()
    assert len(positions_rows) == 1
    assert positions_rows[0].snapshot_type == SnapshotType.INTRADAY

    # 日志含 backfill_done(missing_recovered=3)。
    backfill_logs = [r for r in logger.records if r[1] == "backfill_done"]
    assert len(backfill_logs) == 1
    assert backfill_logs[0][2]["missing_recovered"] == 3


def test_backfill_does_not_pollute_close_netvalue():
    """断线补采落 INTRADAY，绝不落 CLOSE → 不污染净值曲线（净值只认 CLOSE 资产）。"""
    trader = QueryFakeTrader(trades=[], orders=[], positions=[_full_position()],
                             asset=_full_asset())
    repo, writer, logger, job = _build(trader)
    job.run_backfill(T_BUY)
    accounts = repo.all_accounts()
    assert all(a.snapshot_type != SnapshotType.CLOSE for a in accounts)
    positions_rows = repo.all_positions()
    assert all(p.snapshot_type != SnapshotType.CLOSE for p in positions_rows)


# ===========================================================================
# 3) CLOSE 失败重试：前两次抛异常、第三次成功 → 退避重试且最终落库 + 告警
# ===========================================================================


def test_run_close_asset_retry_succeeds_on_third_attempt():
    """query_stock_asset 前两次抛异常、第三次成功 → 退避重试且最终 CLOSE 资产落 1 行。"""
    trader = QueryFakeTrader(
        trades=[_full_trade()], orders=[_full_order()], positions=[_full_position()],
        asset=_full_asset(), asset_fail_times=2,
    )
    backoff_calls: List[int] = []
    repo, writer, logger, job = _build(
        trader, max_retries=3, backoff_sleeper=lambda n: backoff_calls.append(n)
    )

    recovered = job.run_close(T_BUY)

    # 明细兜底正常完成。
    assert recovered == 2
    # 资产查询被调 3 次（前 2 次失败、第 3 次成功）。
    assert trader.asset_calls == 3
    # 退避被调 2 次（前两次失败后各退避一次；第三次成功不退避）。
    assert backoff_calls == [1, 2]
    # 最终 CLOSE 资产快照落 1 行（重试成功）。
    accounts = repo.all_accounts()
    assert len(accounts) == 1
    assert accounts[0].snapshot_type == SnapshotType.CLOSE
    # 重试过程告警 2 条 + 恢复留痕 1 条；无最终强告警 snapshot_close_asset_failed。
    retry_logs = [r for r in logger.records if r[1] == "snapshot_close_asset_retry"]
    assert len(retry_logs) == 2
    assert "snapshot_close_asset_recovered" in logger.events()
    assert "snapshot_close_asset_failed" not in logger.events()
    # 持仓 CLOSE 仍正常落库（重试成功后继续）。
    assert len(repo.all_positions()) == 1


def test_run_close_asset_all_attempts_fail_alerts_and_raises():
    """CLOSE 资产全失败（超过 max_retries）→ 强告警 snapshot_close_asset_failed 且向上抛出。"""
    # max_retries=2 → 总尝试 3 次；asset_fail_times=5 保证全失败。
    trader = QueryFakeTrader(
        trades=[_full_trade()], orders=[], positions=[_full_position()],
        asset=_full_asset(), asset_fail_times=5,
    )
    backoff_calls: List[int] = []
    repo, writer, logger, job = _build(
        trader, max_retries=2, backoff_sleeper=lambda n: backoff_calls.append(n)
    )

    # CLOSE 净值快照隔日不可补，全失败须向上抛出交由调度告警（不静默吞）。
    with pytest.raises(RuntimeError):
        job.run_close(T_BUY)

    # 总尝试 3 次（首次 + 2 次重试）。
    assert trader.asset_calls == 3
    # 退避在前两次失败后各一次（最后一次失败不再退避）。
    assert backoff_calls == [1, 2]
    # 强告警恰 1 条，且带累计失败次数。
    failed_logs = [r for r in logger.records if r[1] == "snapshot_close_asset_failed"]
    assert len(failed_logs) == 1
    assert failed_logs[0][2]["failures"] == 3
    # 明细兜底先于资产快照完成，故成交已落库（资产失败不回滚明细）。
    assert repo.count_trades() == 1
    # 资产快照未成功落库（全失败）。
    assert len(repo.all_accounts()) == 0


def test_run_close_asset_zero_retries_attempts_once():
    """max_retries=0 → 仍至少尝试 1 次；首次失败即强告警并抛出，不退避。"""
    trader = QueryFakeTrader(
        trades=[], orders=[], positions=[], asset=_full_asset(), asset_fail_times=1,
    )
    backoff_calls: List[int] = []
    repo, writer, logger, job = _build(
        trader, max_retries=0, backoff_sleeper=lambda n: backoff_calls.append(n)
    )
    with pytest.raises(RuntimeError):
        job.run_close(T_BUY)
    assert trader.asset_calls == 1
    assert backoff_calls == []  # 无重试机会，不退避
    assert "snapshot_close_asset_failed" in logger.events()


# ===========================================================================
# 4) 版本兼容：缺可选字段的 fake 对象 → 规整落 None 不抛错
# ===========================================================================


def test_version_compat_missing_optional_fields_land_none_without_error():
    """精简 fake 对象（缺多数可选字段）经收盘兜底 → 落库不抛 AttributeError，缺字段落 None。"""
    trader = QueryFakeTrader(
        trades=[_bare_trade()],
        orders=[],
        positions=[_bare_position()],
        asset=_bare_asset(),
    )
    repo, writer, logger, job = _build(trader)

    # 不抛错即通过版本兼容。
    job.run_close(T_BUY)

    # 成交：缺 traded_amount / traded_time → None；价位仍规整为 Decimal。
    trades = repo.get_trades(ACCOUNT, T_BUY)
    assert len(trades) == 1
    t = trades[0]
    assert t.traded_amount is None
    assert t.traded_time is None
    assert t.traded_time_east8 is None
    assert t.account_type is None
    assert t.traded_price == Decimal("12.34")

    # 持仓：缺 avg_price / frozen_volume 等 → None；volume 仍正确。
    pos = repo.all_positions()
    assert len(pos) == 1
    p = pos[0]
    assert p.avg_price is None
    assert p.frozen_volume is None
    assert p.on_road_volume is None
    assert p.yesterday_volume is None
    assert p.open_price is None
    assert p.volume == 100

    # 资产：缺 frozen_cash / market_value → 默认 Decimal("0")；account_type → None。
    accounts = repo.all_accounts()
    assert len(accounts) == 1
    a = accounts[0]
    assert a.total_asset == Decimal("50000.0")
    assert a.frozen_cash == Decimal("0")
    assert a.market_value == Decimal("0")
    assert a.account_type is None


# ===========================================================================
# 5) run_open：昨夜拥股基线 OPEN 快照
# ===========================================================================


def test_run_open_writes_open_position_snapshot_only():
    """run_open：query_stock_positions → OPEN 持仓快照；不落资产、不查成交 / 委托。"""
    trader = QueryFakeTrader(
        trades=[_full_trade()], orders=[_full_order()],
        positions=[_full_position(), _full_position(stock_code="000001.SZ")],
        asset=_full_asset(),
    )
    repo, writer, logger, job = _build(trader)

    count = job.run_open(T_BUY)

    assert count == 2
    positions_rows = repo.all_positions()
    assert len(positions_rows) == 2
    assert all(p.snapshot_type == SnapshotType.OPEN for p in positions_rows)
    # 开盘前只落持仓基线，不落资产（净值只认收盘），也不查成交 / 委托。
    assert len(repo.all_accounts()) == 0
    assert repo.count_trades() == 0
    assert repo.count_orders() == 0
    assert trader.asset_calls == 0
    assert trader.trades_calls == 0
    assert trader.orders_calls == 0
    assert "snapshot_open_done" in logger.events()


def test_open_intraday_close_position_snapshots_coexist():
    """同日 OPEN / INTRADAY / CLOSE 三类持仓快照互不覆盖（唯一键含 snapshot_type，§6.5）。"""
    trader = QueryFakeTrader(
        trades=[], orders=[], positions=[_full_position()], asset=_full_asset()
    )
    repo, writer, logger, job = _build(trader)
    job.run_open(T_BUY)        # OPEN
    job.run_backfill(T_BUY)    # INTRADAY（含资产 INTRADAY）
    job.run_close(T_BUY)       # CLOSE（含资产 CLOSE）

    types = {p.snapshot_type for p in repo.all_positions()}
    assert types == {SnapshotType.OPEN, SnapshotType.INTRADAY, SnapshotType.CLOSE}
    # 资产：INTRADAY（补采）与 CLOSE（收盘）两行并存。
    acc_types = {a.snapshot_type for a in repo.all_accounts()}
    assert acc_types == {SnapshotType.INTRADAY, SnapshotType.CLOSE}


# ===========================================================================
# 6) trade_date 口径：入参优先，缺省回落 provider
# ===========================================================================


def test_trade_date_provider_fallback_when_arg_omitted():
    """run_close 不带 trade_date 入参 → 回落 trade_date_provider()（= T_BUY）。"""
    trader = QueryFakeTrader(
        trades=[_full_trade()], orders=[], positions=[_full_position()], asset=_full_asset()
    )
    repo, writer, logger, job = _build(trader)
    job.run_close()  # 不传 trade_date
    # 落库 trade_date 应为 provider 返回的 T_BUY。
    assert repo.get_trades(ACCOUNT, T_BUY)
    assert repo.all_accounts()[0].trade_date == T_BUY
