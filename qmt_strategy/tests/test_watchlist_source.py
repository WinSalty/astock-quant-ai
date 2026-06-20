"""SqliteSelectedStockSource 单测（doc/05 §三 T2.3 验收：watchlist 读回一致 + latest-wins）。

全部用 tmp_path 建临时 SQLite 文件，不连真实 MySQL / xtquant。覆盖：
- save→fetch round-trip：写两只票再取回，关键字段（market_state / 限价 / 先验 / fail_conditions）一致；
- 空：fetch 一个无数据的日期 → []（当日无候选，合法，非失败）；
- latest-wins：同 target_trade_date 重跑 save，新集合整体替换旧集合（被剔除的票不残留）；
- 与 WatchlistLoader 集成：可交易票从本机 SQLite 读回后进 tradable，证明 loader 能消费本源。

夹具范式（照 doc/05 §三 SQLite + 写队列）：先 init_db 建表（独立连接），起 AsyncWriteQueue
（写线程内开自己的连接），盘前用被测对象直写名单，读前 flush 写队列（保证盘后/读前一致），读断言后 stop。
注：save_watchlist 是盘前直连同步写、不走写队列，但仍按范式起停写队列并在读前 flush，
以贴合「盘中异步落盘、读前 flush」的统一纪律（此处写队列为空，flush 立即返回 True）。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.trade_calendar import StaticTradeCalendar
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.errors import WatchlistLoadError
from qmt_strategy.contracts.models import SelectedStockRow
from qmt_strategy.storage.schema import init_db
from qmt_strategy.storage.watchlist_source import SqliteSelectedStockSource
from qmt_strategy.storage.write_queue import AsyncWriteQueue
from qmt_strategy.watchlist.watchlist_loader import WatchlistLoader

# 领域基准日（与 conftest 一致）：T 信号日 / T+1 买入日。
T_SIGNAL = date(2026, 6, 11)
T_BUY = date(2026, 6, 12)


def _make_row(
    ts_code: str,
    *,
    target_trade_date: date = T_BUY,
    market_state: str = "启动",
    tradable_flag: bool = True,
    limit_up_price: Decimal = Decimal("11.00"),
    reasonable_open_high_low: Decimal = Decimal("10.20"),
    reasonable_open_high_high: Decimal = Decimal("10.80"),
    continuation_prob: Decimal = Decimal("0.62"),
    next_day_premium_prob: Decimal = Decimal("0.55"),
    fail_conditions=None,
    first_limit_time=None,
    last_limit_time=None,
    open_times=None,
    volume_ratio=None,
    return_5d_pct=None,
    return_10d_pct=None,
    data_missing: bool = False,
    data_missing_reason=None,
) -> SelectedStockRow:
    """构造一行信号契约（默认主板可交易强势票，带完整价位 / 先验 / 失败条件，便于断言字段一致）。"""
    return SelectedStockRow(
        ts_code=ts_code,
        trade_date=T_SIGNAL,
        target_trade_date=target_trade_date,
        market_state=market_state,
        tradable_flag=tradable_flag,
        signal_close=Decimal("10.00"),
        limit_up_price=limit_up_price,
        reasonable_open_high_low=reasonable_open_high_low,
        reasonable_open_high_high=reasonable_open_high_high,
        continuation_prob=continuation_prob,
        next_day_premium_prob=next_day_premium_prob,
        leader_strength_score=Decimal("0.8"),
        role="龙头",
        strategy="打板",
        strategy_family="连板",
        setup="一进二",
        first_board_vol=1_000_000,
        float_mktcap=Decimal("5000000000"),
        fail_conditions=fail_conditions if fail_conditions is not None else ["炸板", "尾盘跳水"],
        first_limit_time=first_limit_time,
        last_limit_time=last_limit_time,
        open_times=open_times,
        volume_ratio=volume_ratio,
        return_5d_pct=return_5d_pct,
        return_10d_pct=return_10d_pct,
        data_missing=data_missing,
        data_missing_reason=data_missing_reason,
    )


def _setup_db(tmp_path):
    """按 doc/05 §三范式建库 + 起写队列：返回 (db_path, write_queue)。

    先用独立连接 init_db 建表（建表为一次性盘前动作），再起 AsyncWriteQueue（写线程内开自己的连接）。
    """
    db = str(tmp_path / "q.db")
    init_db(sqlite3.connect(db))  # 先建表（独立连接，与读 / 写队列连接相互独立）
    wq = AsyncWriteQueue(lambda: sqlite3.connect(db), RecordingLogger())  # 写线程内开自己的连接
    wq.start()
    return db, wq


def _calendar() -> StaticTradeCalendar:
    """只含 T+1 买入日 is_open=True 的简单日历（loader 集成用，证明交易日校验放行）。"""
    return StaticTradeCalendar([T_BUY])


# ---------------------------------------------------------------------------
# 1) save → fetch round-trip：写两只票，取回 2 行，关键字段一致
# ---------------------------------------------------------------------------
def test_save_then_fetch_round_trip(tmp_path):
    """save 两只票 → fetch(target_trade_date) 取回 2 行，且关键字段无损 round-trip。"""
    db, wq = _setup_db(tmp_path)
    try:
        src = SqliteSelectedStockSource(db, RecordingLogger())
        rows = [
            _make_row("600000.SH", market_state="启动", limit_up_price=Decimal("11.00")),
            _make_row(
                "000001.SZ",
                market_state="高潮",
                tradable_flag=False,
                limit_up_price=Decimal("13.20"),
                continuation_prob=Decimal("0.71"),
                fail_conditions=["开盘秒水"],
            ),
        ]
        written = src.save_watchlist(rows)
        assert written == 2

        assert wq.flush(timeout=2.0)  # 读前 flush 写队列（统一纪律；本例写队列为空，立即 True）

        got = src.fetch(T_BUY)
        assert len(got) == 2
        by_code = {r.ts_code: r for r in got}
        assert set(by_code) == {"600000.SH", "000001.SZ"}

        # market_state 一致
        assert by_code["600000.SH"].market_state == "启动"
        assert by_code["000001.SZ"].market_state == "高潮"
        # 限价（Decimal，TEXT 存取无精度损失）一致
        assert by_code["600000.SH"].limit_up_price == Decimal("11.00")
        assert by_code["000001.SZ"].limit_up_price == Decimal("13.20")
        # 合理高开区间一致
        assert by_code["600000.SH"].reasonable_open_high_low == Decimal("10.20")
        assert by_code["600000.SH"].reasonable_open_high_high == Decimal("10.80")
        # 先验概率一致
        assert by_code["600000.SH"].continuation_prob == Decimal("0.62")
        assert by_code["000001.SZ"].continuation_prob == Decimal("0.71")
        assert by_code["600000.SH"].next_day_premium_prob == Decimal("0.55")
        # tradable_flag 布尔（0/1 ↔ bool）一致
        assert by_code["600000.SH"].tradable_flag is True
        assert by_code["000001.SZ"].tradable_flag is False
        # fail_conditions（JSON 列表）一致
        assert by_code["600000.SH"].fail_conditions == ["炸板", "尾盘跳水"]
        assert by_code["000001.SZ"].fail_conditions == ["开盘秒水"]
        # 日期口径（ISO 存取）一致
        assert by_code["600000.SH"].target_trade_date == T_BUY
        assert by_code["600000.SH"].trade_date == T_SIGNAL
    finally:
        wq.stop()


def test_round_trip_preserves_daban_factors(tmp_path):
    """打板因子 E1：6 个新字段(封板时序 + 位置/强度) save→fetch 无损 round-trip；缺值保 None。"""
    db, wq = _setup_db(tmp_path)
    try:
        src = SqliteSelectedStockSource(db, RecordingLogger())
        src.save_watchlist(
            [
                _make_row(
                    "600000.SH",
                    first_limit_time="09:32:05", last_limit_time="13:10:00", open_times=2,
                    volume_ratio=Decimal("2.3000"), return_5d_pct=Decimal("31.20"),
                    return_10d_pct=Decimal("-5.40"),
                ),
                _make_row("000001.SZ"),  # 6 字段全缺 → 应保 None
            ]
        )
        assert wq.flush(timeout=2.0)
        got = {r.ts_code: r for r in src.fetch(T_BUY)}
        a = got["600000.SH"]
        assert a.first_limit_time == "09:32:05"      # 时刻字符串原样（不解析、不做时区）
        assert a.last_limit_time == "13:10:00"
        assert a.open_times == 2                       # int 原样
        assert a.volume_ratio == Decimal("2.3000")     # Decimal 经 TEXT 存取无精度损失
        assert a.return_5d_pct == Decimal("31.20")
        assert a.return_10d_pct == Decimal("-5.40")    # 可为负
        b = got["000001.SZ"]
        assert b.first_limit_time is None and b.last_limit_time is None and b.open_times is None
        assert b.volume_ratio is None and b.return_5d_pct is None and b.return_10d_pct is None
    finally:
        wq.stop()


def test_round_trip_preserves_data_missing_sentinel(tmp_path):
    """评审 Stage B 修复：data_missing/data_missing_reason 经【真实 SQLite save→fetch】无损 round-trip。
    回归此前断裂——schema/mapper 未含该两列时盘前 save 会把 data_missing 丢成 False，致 B2/B3 在实盘失效。"""
    db, wq = _setup_db(tmp_path)
    try:
        src = SqliteSelectedStockSource(db, RecordingLogger())
        src.save_watchlist(
            [
                _make_row("600000.SH", tradable_flag=False, data_missing=True,
                          data_missing_reason="missing:close,open_times"),
                _make_row("000001.SZ"),  # 无缺测 → data_missing=False、reason=None
            ]
        )
        assert wq.flush(timeout=2.0)
        got = {r.ts_code: r for r in src.fetch(T_BUY)}
        assert got["600000.SH"].data_missing is True
        assert got["600000.SH"].data_missing_reason == "missing:close,open_times"
        assert got["000001.SZ"].data_missing is False
        assert got["000001.SZ"].data_missing_reason is None
    finally:
        wq.stop()


def test_old_db_missing_daban_columns_migrated_by_init(tmp_path):
    """打板因子 E1（旧库兼容·最高优先）：旧 watchlist 表无 6 新列 → init_db 幂等 ALTER ADD 补齐，
    随后 save+fetch（SELECT *）不抛 IndexError、新字段可无损 round-trip。"""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    # 旧版建表：只到 board_level/tier，无 6 个打板因子列（模拟 1.2.0 之前的库）。
    conn.execute(
        "CREATE TABLE watchlist (ts_code TEXT NOT NULL, trade_date TEXT, "
        "target_trade_date TEXT NOT NULL, leader_strength_score TEXT, role TEXT, strategy TEXT, "
        "market_state TEXT, tradable_flag INTEGER, continuation_prob TEXT, next_day_premium_prob TEXT, "
        "boost TEXT, fail_conditions TEXT, signal_close TEXT, limit_up_price TEXT, "
        "reasonable_open_high_low TEXT, reasonable_open_high_high TEXT, first_board_vol INTEGER, "
        "float_mktcap TEXT, strategy_family TEXT, setup TEXT, name TEXT, is_st INTEGER, "
        "board_level INTEGER, tier TEXT, PRIMARY KEY (ts_code, target_trade_date))"
    )
    conn.commit()
    cols0 = {r[1] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()}
    assert "open_times" not in cols0  # 迁移前确认旧库无新列
    conn.close()

    init_db(sqlite3.connect(db))  # 幂等列迁移：对旧表 ALTER TABLE ADD COLUMN × 6

    conn2 = sqlite3.connect(db)
    cols1 = {r[1] for r in conn2.execute("PRAGMA table_info(watchlist)").fetchall()}
    for c in (
        "first_limit_time", "last_limit_time", "open_times",
        "volume_ratio", "return_5d_pct", "return_10d_pct",
    ):
        assert c in cols1  # 6 列已补齐
    conn2.close()

    # 迁移后 save+fetch（row_to_selected 走 SELECT *）正常、新字段无损 → 证明不抛 IndexError。
    wq = AsyncWriteQueue(lambda: sqlite3.connect(db), RecordingLogger())
    wq.start()
    try:
        src = SqliteSelectedStockSource(db, RecordingLogger())
        src.save_watchlist([_make_row("600000.SH", open_times=1, return_5d_pct=Decimal("12.00"))])
        assert wq.flush(timeout=2.0)
        got = src.fetch(T_BUY)
        assert got[0].open_times == 1 and got[0].return_5d_pct == Decimal("12.00")
    finally:
        wq.stop()


# ---------------------------------------------------------------------------
# 2) 空：fetch 一个无数据的日期 → []
# ---------------------------------------------------------------------------
def test_fetch_empty_date_returns_empty_list(tmp_path):
    """fetch 一个从未写入名单的日期 → 返回 []（当日无候选，合法，非失败）。"""
    db, wq = _setup_db(tmp_path)
    try:
        src = SqliteSelectedStockSource(db, RecordingLogger())
        # 表已建好但无任何行；查一个无数据日期。
        assert wq.flush(timeout=2.0)
        got = src.fetch(date(2026, 6, 10))
        assert got == []

        # 即便先写了别的日期的名单，查无数据日期仍空（按 target_trade_date 精确过滤）。
        src.save_watchlist([_make_row("600000.SH", target_trade_date=T_BUY)])
        assert wq.flush(timeout=2.0)
        assert src.fetch(date(2026, 6, 10)) == []
        assert len(src.fetch(T_BUY)) == 1
    finally:
        wq.stop()


# ---------------------------------------------------------------------------
# 3) latest-wins：同 target_trade_date 重跑 save，新集合整体替换旧集合
# ---------------------------------------------------------------------------
def test_latest_wins_replaces_prior_set(tmp_path):
    """同 target_trade_date 先 save 旧集合(含 A) 再 save 新集合(不含 A、含 B) → 只剩新集合，A 不残留。"""
    db, wq = _setup_db(tmp_path)
    try:
        src = SqliteSelectedStockSource(db, RecordingLogger())
        # 第一次盘前同步：旧集合 = {A=600000.SH, 002594.SZ}
        src.save_watchlist(
            [
                _make_row("600000.SH", market_state="启动"),
                _make_row("002594.SZ", market_state="启动"),
            ]
        )
        assert wq.flush(timeout=2.0)
        assert {r.ts_code for r in src.fetch(T_BUY)} == {"600000.SH", "002594.SZ"}

        # 重跑盘前同步：新集合 = {002594.SZ(改 market_state), B=000001.SZ}，剔除了 A=600000.SH
        written = src.save_watchlist(
            [
                _make_row("002594.SZ", market_state="退潮"),
                _make_row("000001.SZ", market_state="退潮"),
            ]
        )
        assert written == 2
        assert wq.flush(timeout=2.0)

        got = {r.ts_code: r for r in src.fetch(T_BUY)}
        # A 被整体替换剔除（不残留）；只剩新集合。
        assert set(got) == {"002594.SZ", "000001.SZ"}
        assert "600000.SH" not in got
        # 保留下来的同票字段取到的是新集合的值（latest-wins，非旧值）。
        assert got["002594.SZ"].market_state == "退潮"
    finally:
        wq.stop()


def test_latest_wins_other_date_untouched(tmp_path):
    """latest-wins 只影响本批涉及的 target_trade_date，其它日期名单不被误删。"""
    db, wq = _setup_db(tmp_path)
    other_date = date(2026, 6, 15)
    try:
        src = SqliteSelectedStockSource(db, RecordingLogger())
        src.save_watchlist([_make_row("600519.SH", target_trade_date=other_date)])
        # 重跑 T_BUY 的盘前同步，不应动 other_date 的名单。
        src.save_watchlist([_make_row("600000.SH", target_trade_date=T_BUY)])
        assert wq.flush(timeout=2.0)
        assert {r.ts_code for r in src.fetch(other_date)} == {"600519.SH"}
        assert {r.ts_code for r in src.fetch(T_BUY)} == {"600000.SH"}
    finally:
        wq.stop()


def test_save_empty_returns_zero_no_delete(tmp_path):
    """save 空列表 → 返回 0、不开事务、不误删任何已有名单。"""
    db, wq = _setup_db(tmp_path)
    try:
        src = SqliteSelectedStockSource(db, RecordingLogger())
        src.save_watchlist([_make_row("600000.SH", target_trade_date=T_BUY)])
        assert wq.flush(timeout=2.0)
        assert src.save_watchlist([]) == 0
        # 空写不应删掉已有名单。
        assert {r.ts_code for r in src.fetch(T_BUY)} == {"600000.SH"}
    finally:
        wq.stop()


# ---------------------------------------------------------------------------
# 4) 读异常兜底：损坏的库路径 → 抛 WatchlistLoadError（loader 据此降级）
# ---------------------------------------------------------------------------
def test_fetch_corrupt_db_raises_watchlist_error(tmp_path):
    """SQLite 读异常（表不存在 / 库损坏）→ 包成 WatchlistLoadError 上抛，供 loader 降级。"""
    # 故意建一个「未 init_db、内容非法」的库文件，使 SELECT watchlist 抛错。
    db = str(tmp_path / "broken.db")
    with open(db, "w", encoding="utf-8") as f:
        f.write("this is not a sqlite database")
    logger = RecordingLogger()
    src = SqliteSelectedStockSource(db, logger)
    try:
        src.fetch(T_BUY)
        raise AssertionError("应抛 WatchlistLoadError")
    except WatchlistLoadError:
        pass
    # 读失败留痕。
    assert "watchlist_sqlite_fetch_failed" in logger.events()


# ---------------------------------------------------------------------------
# 5) 与 WatchlistLoader 集成：可交易票从本机 SQLite 读回后进 tradable
# ---------------------------------------------------------------------------
def test_loader_integration_reads_from_local_sqlite(tmp_path):
    """WatchlistLoader(SqliteSelectedStockSource, is_open=True 日历, RecordingLogger, Settings.from_env({}))
    .load(T_BUY) → 可交易票进 tradable，证明 loader 能从本机 SQLite 读名单。"""
    db, wq = _setup_db(tmp_path)
    try:
        src = SqliteSelectedStockSource(db, RecordingLogger())
        # 盘前同步：一只可交易主板票 + 一只 tradable_flag=False 的票（应进观察名单）。
        src.save_watchlist(
            [
                _make_row(
                    "600000.SH", market_state="启动", tradable_flag=True,
                    first_limit_time="09:40:00", open_times=1, return_5d_pct=Decimal("18.00"),
                ),
                _make_row("000001.SZ", market_state="启动", tradable_flag=False),
            ]
        )
        assert wq.flush(timeout=2.0)

        loader = WatchlistLoader(
            primary=src,
            calendar=_calendar(),
            logger=RecordingLogger(),
            settings=Settings.from_env({}),
        )
        ctx = loader.load(T_BUY)

        assert ctx.is_open is True
        assert ctx.degraded is False
        # 可交易票进 tradable（证明 loader 从本机 SQLite 读到了名单并放行）。
        assert set(ctx.tradable.keys()) == {"600000.SH"}
        # tradable_flag=False 的票进观察名单。
        assert [e.norm_code for e in ctx.watch_only] == ["000001.SZ"]
        # market_state=启动 → 允许开新仓。
        assert ctx.open_new_position_allowed is True
        # 价位 / 先验透传正确（来源于本机库读回）。
        entry = ctx.tradable["600000.SH"]
        assert entry.price.limit_up_price == Decimal("11.00")
        assert entry.continuation_prob == Decimal("0.62")
        assert entry.signal_trade_date == T_SIGNAL
        # 打板因子 E1：6 新字段经本机 SQLite 读回 → TradableEntry，并 to_plan_row 透传到 PlanRow（端到端）。
        assert entry.first_limit_time == "09:40:00"
        assert entry.open_times == 1
        assert entry.return_5d_pct == Decimal("18.00")
        plan = entry.to_plan_row()
        assert plan.first_limit_time == "09:40:00"
        assert plan.open_times == 1
        assert plan.return_5d_pct == Decimal("18.00")
        assert plan.last_limit_time is None and plan.volume_ratio is None  # 未设字段透传 None
    finally:
        wq.stop()


def test_loader_integration_degrades_on_read_error(tmp_path):
    """loader 集成下，本机库读失败 → loader 落降级态（只守仓不开新仓），不抛异常、不崩进程。"""
    db = str(tmp_path / "broken.db")
    with open(db, "w", encoding="utf-8") as f:
        f.write("not a db")
    src = SqliteSelectedStockSource(db, RecordingLogger())
    loader = WatchlistLoader(
        primary=src,
        calendar=_calendar(),
        logger=RecordingLogger(),
        settings=Settings.from_env({}),
    )
    ctx = loader.load(T_BUY)  # 不应抛异常
    assert ctx.degraded is True
    assert ctx.open_new_position_allowed is False
    assert ctx.tradable == {}
