"""集合竞价轮询 + 四因子单测（§3.9 单测要点全覆盖）。

全部用 fake / 内存实现，不连真实 xttrader / xtdata / MySQL：
  - 时钟用 conftest 的 utc_at_east8 构造固定东八区时刻（再转 UTC naive）。
  - tick 取数用 FakeTickSource（构造时给定固定 / 序列化返回或异常）。
  - 下游 router 用一个收集列表的简单回调，断言每帧 snapshot 被推送。

覆盖：时段映射 / 高开 / 量能比 / 重心趋势 / 虚拟封单 / 整体降级 B / 回调不触发兜底。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List

from qmt_strategy.auction.auction_factors import (
    DQ_NO_BASE_VOL,
    DQ_NO_PRE_CLOSE,
    DQ_NO_SEAL_VOL,
    DQ_NO_TICK,
    auction_centroid,
    auction_volume_ratio,
    compute_auction_factors,
    open_pct,
    virtual_seal,
)
from qmt_strategy.auction.auction_poller import AuctionPoller
from qmt_strategy.auction.tick_source import FakeTickSource, XtdataTickSource
from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.contracts.enums import AuctionPhase, CentroidTrend
from qmt_strategy.contracts.errors import TickSourceError
from qmt_strategy.contracts.models import AuctionSnapshot, PlanRow
from tests.conftest import make_plan_row, utc_at_east8

D = date(2026, 6, 12)


# ---------------------------------------------------------------------------
# 辅助：收集 router_sink 推送的快照
# ---------------------------------------------------------------------------


class CollectingSink:
    """进程内 router_sink fake：把每帧 AuctionSnapshot 收进列表，供断言。"""

    def __init__(self):
        self.snaps: List[AuctionSnapshot] = []

    def __call__(self, snap: AuctionSnapshot) -> None:
        self.snaps.append(snap)


def _make_poller(tick_source, plans: Dict[str, PlanRow], now_utc: datetime):
    """组装一个 AuctionPoller（固定计划 + 固定时钟），返回 (poller, sink, logger)。"""
    from qmt_strategy.config.settings import Settings

    sink = CollectingSink()
    logger = RecordingLogger()
    settings = Settings(auction_poll_interval_sec=1.5)
    poller = AuctionPoller(
        tick_source=tick_source,
        plan_provider=lambda: dict(plans),
        router_sink=sink,
        settings=settings,
        clock=FakeClock(now_utc),
        logger=logger,
    )
    return poller, sink, logger


# ---------------------------------------------------------------------------
# 1. 时段映射（§3.9：边界精确到秒，按东八区）
# ---------------------------------------------------------------------------


def test_resolve_phase_boundaries():
    """9:14:59→PRE_AUCTION、9:15:00→CANCELABLE、9:20:00→LOCKED、9:25:00→SETTLED、9:30:00→CLOSED。"""
    tsrc = FakeTickSource(fixed={})
    poller, _, _ = _make_poller(tsrc, {}, utc_at_east8(D, 9, 16, 0))
    assert poller.resolve_phase(utc_at_east8(D, 9, 14, 59)) == AuctionPhase.PRE_AUCTION
    assert poller.resolve_phase(utc_at_east8(D, 9, 15, 0)) == AuctionPhase.AUCTION_CANCELABLE
    assert poller.resolve_phase(utc_at_east8(D, 9, 20, 0)) == AuctionPhase.AUCTION_LOCKED
    assert poller.resolve_phase(utc_at_east8(D, 9, 25, 0)) == AuctionPhase.SETTLED
    assert poller.resolve_phase(utc_at_east8(D, 9, 30, 0)) == AuctionPhase.CLOSED_WINDOW


# ---------------------------------------------------------------------------
# 2. 高开计算 open_pct（§3.9）
# ---------------------------------------------------------------------------


def test_open_pct_normal():
    """(11.00 − 10.00) / 10.00 = 0.10。"""
    assert open_pct(Decimal("11.00"), Decimal("10.00")) == Decimal("0.10")


def test_open_pct_none_pre_close():
    """pre_close=None → None。"""
    assert open_pct(Decimal("11.00"), None) is None


def test_open_pct_zero_pre_close():
    """pre_close=0 → None（不可定义涨跌幅）。"""
    assert open_pct(Decimal("11.00"), Decimal("0")) is None


def test_compute_open_pct_no_pre_close_flag():
    """tick 无 lastClose → open_pct=None 且 data_quality 含 NO_PRE_CLOSE。"""
    plan = make_plan_row(first_board_vol=100000)
    tick = {"lastPrice": 11.0}  # 无 lastClose
    snap = compute_auction_factors(
        "600000.SH", tick, [], AuctionPhase.AUCTION_CANCELABLE, plan, utc_at_east8(D, 9, 16, 0)
    )
    assert snap.open_pct is None
    assert DQ_NO_PRE_CLOSE in snap.data_quality
    assert snap.last_price == Decimal("11.0")


def test_compute_open_pct_value():
    """tick 给 lastPrice/lastClose → open_pct 正确。"""
    plan = make_plan_row(first_board_vol=100000)
    tick = {"lastPrice": 10.50, "lastClose": 10.00}
    snap = compute_auction_factors(
        "600000.SH", tick, [], AuctionPhase.AUCTION_CANCELABLE, plan, utc_at_east8(D, 9, 16, 0)
    )
    assert snap.open_pct == Decimal("0.05")


# ---------------------------------------------------------------------------
# 3. 量能比例 auction_volume_ratio（§3.9）
# ---------------------------------------------------------------------------


def test_volume_ratio_normal():
    """cum_vol / first_board_vol。"""
    assert auction_volume_ratio(50000, 100000) == Decimal("0.5")


def test_volume_ratio_none_base():
    """first_board_vol=None → None（不报错）。"""
    assert auction_volume_ratio(50000, None) is None


def test_volume_ratio_zero_base():
    """first_board_vol=0 → None。"""
    assert auction_volume_ratio(50000, 0) is None


def test_compute_volume_ratio_no_base_flag():
    """plan.first_board_vol=None → auction_vol_ratio=None 且标 NO_BASE_VOL，不报错。"""
    plan = make_plan_row(first_board_vol=None)
    tick = {"lastPrice": 11.0, "lastClose": 10.0, "volume": 50000}
    snap = compute_auction_factors(
        "600000.SH", tick, [], AuctionPhase.AUCTION_CANCELABLE, plan, utc_at_east8(D, 9, 16, 0)
    )
    assert snap.auction_vol_ratio is None
    assert DQ_NO_BASE_VOL in snap.data_quality
    # 高开仍可得，证明单因子缺失不连累其它因子。
    assert snap.open_pct == Decimal("0.10")


# ---------------------------------------------------------------------------
# 4. 重心趋势 auction_centroid（§3.9）
# ---------------------------------------------------------------------------


def test_centroid_trend_down():
    """先高后低帧序列 → 末帧价 < 重心 → DOWN（诱多型竞价）。

    帧（累计量 volume / lastPrice）：
      f0: vol=0,    price=12.0（开头拉到很高）
      f1: vol=100,  price=12.0（Δ100 @12.0）
      f2: vol=200,  price=10.0（Δ100 @10.0，末帧砸下来）
    重心 = (12.0*100 + 10.0*100) / 200 = 11.0；末帧价 10.0 < 11.0 → DOWN。
    """
    frames = [
        {"volume": 0, "lastPrice": 12.0},
        {"volume": 100, "lastPrice": 12.0},
    ]
    cur = {"volume": 200, "lastPrice": 10.0}
    centroid, trend = auction_centroid(frames, cur)
    assert centroid == Decimal("11.0")
    assert trend == CentroidTrend.DOWN


def test_centroid_trend_up():
    """越竞越高帧序列 → 末帧价 > 重心 → UP。

    帧：
      f0: vol=0,    price=10.0
      f1: vol=100,  price=10.0（Δ100 @10.0）
      f2: vol=200,  price=12.0（Δ100 @12.0，末帧更高）
    重心 = (10*100 + 12*100)/200 = 11.0；末帧 12.0 > 11.0 → UP。
    """
    frames = [
        {"volume": 0, "lastPrice": 10.0},
        {"volume": 100, "lastPrice": 10.0},
    ]
    cur = {"volume": 200, "lastPrice": 12.0}
    centroid, trend = auction_centroid(frames, cur)
    assert centroid == Decimal("11.0")
    assert trend == CentroidTrend.UP


def test_centroid_insufficient_data():
    """帧数不足（仅当前帧、无历史）→ (None, FLAT)。"""
    centroid, trend = auction_centroid([], {"volume": 100, "lastPrice": 10.0})
    assert centroid is None
    assert trend == CentroidTrend.FLAT


def test_centroid_no_volume_growth():
    """累计量无正增量（降级 B 拿不到 volume）→ (None, FLAT)。"""
    frames = [{"lastPrice": 10.0}, {"lastPrice": 11.0}]
    centroid, trend = auction_centroid(frames, {"lastPrice": 12.0})
    assert centroid is None
    assert trend == CentroidTrend.FLAT


# ---------------------------------------------------------------------------
# 5. 虚拟封单 virtual_seal（§3.9）
# ---------------------------------------------------------------------------


def test_virtual_seal_limit_up_with_bidvol():
    """达涨停且 bidVol 有值 → 封单额 = bidVol × bidPrice，封流比 = 封单额 / 流通市值。"""
    tick = {"lastPrice": 11.0, "bidVol": 1000, "bidPrice": 11.0}
    info = virtual_seal(tick, Decimal("11.00"), Decimal("110000"))
    assert info.is_limit_up is True
    assert info.virtual_seal_amount == Decimal("11000")  # 1000 × 11.0
    assert info.seal_to_float_ratio == Decimal("0.1")    # 11000 / 110000
    assert DQ_NO_SEAL_VOL not in info.data_quality


def test_virtual_seal_not_limit_up():
    """未达涨停 → 封单额 0、is_limit_up False、封流比 None（正常态）。"""
    tick = {"lastPrice": 10.50, "bidVol": 1000, "bidPrice": 10.50}
    info = virtual_seal(tick, Decimal("11.00"), Decimal("110000"))
    assert info.is_limit_up is False
    assert info.virtual_seal_amount == Decimal("0")
    assert info.seal_to_float_ratio is None
    assert info.data_quality == []


def test_virtual_seal_limit_up_no_bidvol():
    """达涨停但 bidVol 取不到 → 标 NO_SEAL_VOL，封单额 0，is_limit_up True。"""
    tick = {"lastPrice": 11.0, "bidPrice": 11.0}  # 无 bidVol
    info = virtual_seal(tick, Decimal("11.00"), Decimal("110000"))
    assert info.is_limit_up is True
    assert info.virtual_seal_amount == Decimal("0")
    assert DQ_NO_SEAL_VOL in info.data_quality


def test_virtual_seal_none_float_mktcap():
    """流通市值缺失 → 封单额仍算，封流比 None。"""
    tick = {"lastPrice": 11.0, "bidVol": 1000, "bidPrice": 11.0}
    info = virtual_seal(tick, Decimal("11.00"), None)
    assert info.virtual_seal_amount == Decimal("11000")
    assert info.seal_to_float_ratio is None


def test_compute_virtual_seal_integrated():
    """compute_auction_factors 透传封单字段。"""
    plan = make_plan_row(
        limit_up_price=Decimal("11.00"), first_board_vol=100000, float_mktcap=Decimal("110000")
    )
    tick = {"lastPrice": 11.0, "lastClose": 10.0, "volume": 50000, "bidVol": 1000, "bidPrice": 11.0}
    snap = compute_auction_factors(
        "600000.SH", tick, [], AuctionPhase.AUCTION_LOCKED, plan, utc_at_east8(D, 9, 22, 0)
    )
    assert snap.is_limit_up is True
    assert snap.virtual_seal_amount == Decimal("11000")
    assert snap.seal_to_float_ratio == Decimal("0.1")


# ---------------------------------------------------------------------------
# 6. tick=None 降级（compute 仍产出空壳）
# ---------------------------------------------------------------------------


def test_compute_tick_none():
    """tick=None → 仅产能取到的（基本全 None），标 NO_TICK，tick_seq=已采帧数+1。"""
    plan = make_plan_row(first_board_vol=100000)
    snap = compute_auction_factors(
        "600000.SH", None, [{"lastPrice": 1}, {"lastPrice": 2}],
        AuctionPhase.AUCTION_CANCELABLE, plan, utc_at_east8(D, 9, 16, 0),
    )
    assert snap.open_pct is None
    assert snap.auction_vol_ratio is None
    assert snap.auction_centroid is None
    assert snap.virtual_seal_amount == Decimal("0")
    assert DQ_NO_TICK in snap.data_quality
    assert snap.tick_seq == 3  # len(prev)=2 + 本帧 1


# ---------------------------------------------------------------------------
# 7. poll_once 不依赖回调（用 FakeTickSource 定时器驱动产帧）
# ---------------------------------------------------------------------------


def test_poll_once_produces_snapshots_without_callback():
    """无任何 tick 回调，poll_once 在定时器驱动下仍对每个 code 产出帧并 push 下游。"""
    plans = {
        "600000.SH": make_plan_row("600000.SH", first_board_vol=100000),
        "000001.SZ": make_plan_row("000001.SZ", first_board_vol=200000),
    }
    ticks = {
        "600000.SH": {"lastPrice": 11.0, "lastClose": 10.0, "volume": 50000},
        "000001.SZ": {"lastPrice": 10.5, "lastClose": 10.0, "volume": 80000},
    }
    tsrc = FakeTickSource(fixed=ticks)
    poller, sink, _ = _make_poller(tsrc, plans, utc_at_east8(D, 9, 16, 0))

    snaps = poller.poll_once(utc_at_east8(D, 9, 16, 0))
    assert len(snaps) == 2
    assert len(sink.snaps) == 2  # 每帧都 push 给下游
    by_code = {s.ts_code: s for s in snaps}
    assert by_code["600000.SH"].open_pct == Decimal("0.10")
    assert by_code["600000.SH"].auction_vol_ratio == Decimal("0.5")
    # 验证「批量一次取全部」而非逐股轮询。
    assert tsrc.calls[0] == ["600000.SH", "000001.SZ"]


def test_poll_once_centroid_accumulates_across_frames():
    """连续多轮 poll_once 累积历史帧 → 后续帧能算出重心趋势（证明帧序列由定时器累积）。"""
    plans = {"600000.SH": make_plan_row("600000.SH", first_board_vol=100000)}
    # 三轮：累计量递增、价格先平后跌 → 末帧 DOWN。
    responses = [
        {"600000.SH": {"lastPrice": 12.0, "lastClose": 10.0, "volume": 0}},
        {"600000.SH": {"lastPrice": 12.0, "lastClose": 10.0, "volume": 100}},
        {"600000.SH": {"lastPrice": 10.0, "lastClose": 10.0, "volume": 200}},
    ]
    tsrc = FakeTickSource(responses=responses)
    poller, sink, _ = _make_poller(tsrc, plans, utc_at_east8(D, 9, 16, 0))

    poller.poll_once(utc_at_east8(D, 9, 16, 0))
    poller.poll_once(utc_at_east8(D, 9, 17, 0))
    s3 = poller.poll_once(utc_at_east8(D, 9, 18, 0))[0]
    assert s3.auction_centroid == Decimal("11.0")
    assert s3.centroid_trend == CentroidTrend.DOWN


# ---------------------------------------------------------------------------
# 8. 整体降级 B（get_full_tick 只回 last_price / 或抛错）
# ---------------------------------------------------------------------------


def test_degrade_b_only_last_price():
    """get_full_tick 只回 last_price（无量 / 封单）→ 仅 open_pct 有值、其余 None，snapshot 仍产出。"""
    # limit_up_price=11.00；用 last_price=10.50（未顶涨停）使降级 B 场景纯净：仅 open_pct 可得。
    plans = {"600000.SH": make_plan_row("600000.SH", limit_up_price=Decimal("11.00"), first_board_vol=100000)}
    # 降级 B：tick 只有 lastPrice + lastClose，没有 volume / bidVol / bidPrice。
    ticks = {"600000.SH": {"lastPrice": 10.50, "lastClose": 10.0}}
    tsrc = FakeTickSource(fixed=ticks)
    poller, sink, _ = _make_poller(tsrc, plans, utc_at_east8(D, 9, 16, 0))

    snap = poller.poll_once(utc_at_east8(D, 9, 16, 0))[0]
    assert snap.open_pct == Decimal("0.05")          # 仅高开有值
    assert snap.auction_vol_ratio is None            # 无 volume
    assert snap.auction_centroid is None             # 无逐帧量
    assert snap.virtual_seal_amount == Decimal("0")  # 无封单
    assert snap.is_limit_up is False
    assert len(sink.snaps) == 1                       # 仍正常产出供「开盘后确认」


def test_degrade_b_tick_source_error():
    """get_full_tick 抛 TickSourceError → 本轮无完整 tick，每 code 产出空壳帧（NO_TICK），进程不崩。"""
    plans = {
        "600000.SH": make_plan_row("600000.SH", first_board_vol=100000),
        "000001.SZ": make_plan_row("000001.SZ", first_board_vol=200000),
    }
    tsrc = FakeTickSource(responses=[TickSourceError("xtdata down")])
    poller, sink, logger = _make_poller(tsrc, plans, utc_at_east8(D, 9, 16, 0))

    snaps = poller.poll_once(utc_at_east8(D, 9, 16, 0))
    assert len(snaps) == 2
    for s in snaps:
        assert s.open_pct is None
        assert DQ_NO_TICK in s.data_quality
    assert len(sink.snaps) == 2
    # 留痕降级告警。
    assert "auction_tick_fetch_failed" in logger.events()


# ---------------------------------------------------------------------------
# 9. XtdataTickSource：异常归一为 TickSourceError
# ---------------------------------------------------------------------------


def test_xtdata_tick_source_wraps_exception():
    """底层 xtdata.get_full_tick 抛任意异常 → 归一为 TickSourceError。"""

    class BoomXtData:
        def get_full_tick(self, codes):
            raise RuntimeError("connection reset")

        def subscribe_quote(self, code, period="tick"):
            return 0

    src = XtdataTickSource(BoomXtData())
    try:
        src.get_full_tick(["600000.SH"])
        assert False, "应抛 TickSourceError"
    except TickSourceError as exc:
        assert "connection reset" in str(exc)


def test_xtdata_tick_source_none_result():
    """底层返回 None → TickSourceError（取数失败）。"""

    class NoneXtData:
        def get_full_tick(self, codes):
            return None

        def subscribe_quote(self, code, period="tick"):
            return 0

    src = XtdataTickSource(NoneXtData())
    try:
        src.get_full_tick(["600000.SH"])
        assert False, "应抛 TickSourceError"
    except TickSourceError:
        pass


def test_xtdata_tick_source_delegates():
    """正常路径：委托底层并原样返回。"""

    class OkXtData:
        def get_full_tick(self, codes):
            return {c: {"lastPrice": 10.0} for c in codes}

        def subscribe_quote(self, code, period="tick"):
            return 0

    src = XtdataTickSource(OkXtData())
    out = src.get_full_tick(["600000.SH"])
    assert out["600000.SH"]["lastPrice"] == 10.0


# ---------------------------------------------------------------------------
# 10. run 主循环：可注入 sleep_fn / max_loops，时段跳过，CLOSED 退出
# ---------------------------------------------------------------------------


def test_run_skips_pre_auction_and_stops_at_max_loops():
    """PRE_AUCTION 不取 tick，仅 sleep；max_loops 控制退出（不依赖真实 sleep）。"""
    plans = {"600000.SH": make_plan_row("600000.SH", first_board_vol=100000)}
    tsrc = FakeTickSource(fixed={"600000.SH": {"lastPrice": 11.0, "lastClose": 10.0}})
    # 固定在 9:10（PRE_AUCTION）。
    poller, sink, _ = _make_poller(tsrc, plans, utc_at_east8(D, 9, 10, 0))

    sleeps: List[float] = []
    poller.run(sleep_fn=lambda s: sleeps.append(s), max_loops=3)
    # PRE_AUCTION 段不调用 get_full_tick、不产帧。
    assert tsrc.calls == []
    assert sink.snaps == []
    assert len(sleeps) == 3  # 跑满 max_loops 轮 sleep


def test_run_closed_window_exits_immediately():
    """≥9:30 CLOSED_WINDOW → 立即退出，不产帧。"""
    plans = {"600000.SH": make_plan_row("600000.SH", first_board_vol=100000)}
    tsrc = FakeTickSource(fixed={"600000.SH": {"lastPrice": 11.0, "lastClose": 10.0}})
    poller, sink, logger = _make_poller(tsrc, plans, utc_at_east8(D, 9, 30, 0))

    sleeps: List[float] = []
    poller.run(sleep_fn=lambda s: sleeps.append(s), max_loops=10)
    assert sink.snaps == []
    assert sleeps == []  # 直接 break，不 sleep
    assert "auction_poll_closed" in logger.events()


def test_run_polls_in_cancelable_window():
    """竞价可撤段 run 一轮即产帧（max_loops=1）。"""
    plans = {"600000.SH": make_plan_row("600000.SH", first_board_vol=100000)}
    tsrc = FakeTickSource(fixed={"600000.SH": {"lastPrice": 11.0, "lastClose": 10.0, "volume": 5}})
    poller, sink, _ = _make_poller(tsrc, plans, utc_at_east8(D, 9, 16, 0))

    poller.run(sleep_fn=lambda s: None, max_loops=1)
    assert len(sink.snaps) == 1
    assert sink.snaps[0].open_pct == Decimal("0.10")


def test_run_dense_interval_near_keypoint():
    """临近 9:24:30 加密：间隔压到 ≤1.0s（配置为 1.5s）。"""
    plans = {"600000.SH": make_plan_row("600000.SH", first_board_vol=100000)}
    tsrc = FakeTickSource(fixed={"600000.SH": {"lastPrice": 11.0, "lastClose": 10.0}})
    # 9:24:40 落在 9:24:30–9:25 加密窗口（LOCKED 段）。
    poller, _, _ = _make_poller(tsrc, plans, utc_at_east8(D, 9, 24, 40))

    sleeps: List[float] = []
    poller.run(sleep_fn=lambda s: sleeps.append(s), max_loops=1)
    assert sleeps == [1.0]  # min(1.5, 1.0)


def test_run_normal_interval_not_near_keypoint():
    """非临近时点：间隔取配置值 1.5s。"""
    plans = {"600000.SH": make_plan_row("600000.SH", first_board_vol=100000)}
    tsrc = FakeTickSource(fixed={"600000.SH": {"lastPrice": 11.0, "lastClose": 10.0}})
    poller, _, _ = _make_poller(tsrc, plans, utc_at_east8(D, 9, 16, 0))

    sleeps: List[float] = []
    poller.run(sleep_fn=lambda s: sleeps.append(s), max_loops=1)
    assert sleeps == [1.5]


def test_run_stop_flag_breaks_loop():
    """外部 stop() → 下一轮检查时退出。"""
    plans = {"600000.SH": make_plan_row("600000.SH", first_board_vol=100000)}
    tsrc = FakeTickSource(fixed={"600000.SH": {"lastPrice": 11.0, "lastClose": 10.0}})
    poller, sink, _ = _make_poller(tsrc, plans, utc_at_east8(D, 9, 16, 0))

    poller.stop()
    poller.run(sleep_fn=lambda s: None, max_loops=10)
    assert sink.snaps == []  # 已 stop，不进循环体
