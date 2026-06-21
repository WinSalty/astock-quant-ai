"""卖出/连板决策 sell_decider 单测（§5.3 / §5.9）。

全部用 fake/内存实现，不连真实 xttrader/xtdata/MySQL：
- 时钟用 FakeClock 固定时刻；日志用 RecordingLogger 断言留痕；Settings 直接构造，不读环境变量。
- PositionUnit / OrderBook / SignalPrior 直接用 contracts 数据结构构造（盘口一律走 OrderBook，
  绝不读信号侧 last_price 快照）。

覆盖要点（对齐题目「单测必须覆盖」每一条）：
- T+1 锁定：state=LOCKED_T1 → decide_auction/decide_intraday 短路返回 HOLD（不进实质分支）。
- 连板续持：mode=SIGNAL_DRIVEN、continuation_prob 高、book 秒板稳封、未命中 fail → decide_intraday=HOLD。
- 破位止损：book.below_support+volume_surge → decide_intraday=CLEAR，reason='走弱破位'。
- 纯技术退出：prior=None（mode=TECH_EXIT）、盘口弱势 → 直接出（CLEAR/REDUCE）。
- 竞价背离减仓：book.price_volume_diverge → decide_auction=REDUCE/CLEAR。
- 炸板出：book.broke_board → decide_intraday=CLEAR，reason='炸板出'。
- 空仓闸门：risk_verdict=SELL_ONLY_HOLD → 存量仍按规则可卖（不被强制 HOLD）。
- 冻结态：risk_verdict=FREEZE → HOLD（暂停新卖出）。
- 盘口来源：决策只读 OrderBook 字段（构造 book 与「信号侧 last_price」不一致，断言用 book）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import (
    PositionMode,
    PositionState,
    RiskVerdict,
    SellActionType,
)
from qmt_strategy.contracts.models import OrderBook, PositionUnit, SignalPrior
from qmt_strategy.position.sell_decider import SellDecider

# 领域基准日期（与 conftest / doc 示例一致）：
#   T 信号日 = 2026-06-11；买入日 B = 2026-06-12；可卖日 = next_open(B) = 2026-06-15（跨周末）。
T_SIGNAL = date(2026, 6, 11)
T_BUY = date(2026, 6, 12)
T_SELL = date(2026, 6, 15)
ACCOUNT = "acc1"
TS = "600036.SH"


def _clock() -> FakeClock:
    """固定一个 UTC naive 时钟即可（决策为定性判定，不依赖具体钟点，仅满足构造依赖）。"""
    return FakeClock(datetime(2026, 6, 15, 1, 16, 0))


@pytest.fixture
def decider() -> SellDecider:
    """组装 SellDecider：默认 Settings（不读环境变量）+ 固定时钟 + 记录型日志。

    续板「高」阈值用默认 0.6（continuation_prob >= 0.6 视为强先验）。
    """
    return SellDecider(Settings(), _clock(), RecordingLogger())


def _unit(
    *,
    state: PositionState = PositionState.HOLDING,
    mode: PositionMode = PositionMode.SIGNAL_DRIVEN,
    volume: int = 1000,
    can_use_volume: int = 1000,
) -> PositionUnit:
    """构造一个可卖持仓单元（默认 HOLDING + SIGNAL_DRIVEN，即已跨买入日、次日又涨停吃先验）。"""
    return PositionUnit(
        account_id=ACCOUNT,
        ts_code=TS,
        volume=volume,
        can_use_volume=can_use_volume,
        avg_cost=Decimal("10.00"),
        earliest_sellable_date=T_SELL,
        state=state,
        mode=mode,
        buy_date=T_BUY,
    )


def _prior(
    *,
    continuation_prob: Optional[Decimal] = Decimal("0.7"),
    fail_conditions=None,
    market_state: str = "启动",
) -> SignalPrior:
    """构造信号先验视图（默认强先验：continuation_prob=0.7 >= 0.6 阈值、无失败条件）。"""
    return SignalPrior(
        ts_code=TS,
        trade_date=date(2026, 6, 14),
        target_trade_date=T_SELL,
        continuation_prob=continuation_prob,
        fail_conditions=list(fail_conditions) if fail_conditions else [],
        market_state=market_state,
        role="龙头",
        strategy="打板",
    )


def _book(
    *,
    last_price: Optional[Decimal] = None,
    open_pct: Optional[Decimal] = None,
    seal_to_float_ratio: Optional[Decimal] = None,
    open_times: int = 0,
    is_sealed: bool = False,
    broke_board: bool = False,
    below_support: bool = False,
    volume_surge: bool = False,
    price_volume_diverge: bool = False,
    near_close_weak: bool = False,
) -> OrderBook:
    """构造执行侧 xtdata 盘口快照（仅执行侧可得）。

    默认全 False / None = 中性盘口，便于各用例只翻转关心的字段。
    """
    return OrderBook(
        ts_code=TS,
        last_price=last_price,
        open_pct=open_pct,
        seal_to_float_ratio=seal_to_float_ratio,
        open_times=open_times,
        is_sealed=is_sealed,
        broke_board=broke_board,
        below_support=below_support,
        volume_surge=volume_surge,
        price_volume_diverge=price_volume_diverge,
        near_close_weak=near_close_weak,
    )


# ---------------------------------------------------------------------------
# 用例 1：T+1 锁定——state=LOCKED_T1 → decide_* 短路返回 HOLD（不进实质分支）
# ---------------------------------------------------------------------------
def test_t1_locked_short_circuits_to_hold(decider: SellDecider) -> None:
    unit = _unit(state=PositionState.LOCKED_T1)
    # 即便盘口炸板（强卖信号），守 T+1 也优先短路，绝不进实质卖出分支。
    book = _book(broke_board=True, below_support=True, volume_surge=True)

    a_auction = decider.decide_auction(unit, _prior(), book)
    a_intraday = decider.decide_intraday(unit, _prior(), book)

    assert a_auction.action is SellActionType.HOLD
    assert a_auction.reason == "不可卖(守T+1)"
    assert a_intraday.action is SellActionType.HOLD
    assert a_intraday.reason == "不可卖(守T+1)"


# ---------------------------------------------------------------------------
# 口径变更（2026-06-21）：原 doc/29 B3「缺测持仓强卖」已下线——卖出完全交由 xtdata 实时盘口扳机裁决，
# 不再因信号侧缺测标记强制清仓。原 test_data_missing_* 三例（竞价/分时强清、无盘口强清、不越 T+1）随之移除。
# ---------------------------------------------------------------------------


def test_no_prior_not_force_cleared(decider: SellDecider) -> None:
    """口径③：隔夜不在今日名单的持仓 prior=None → 不因缺测强卖（走现有盘口/技术退出逻辑）。"""
    unit = _unit(state=PositionState.HOLDING)
    # 稳封 + 无先验：正常技术退出逻辑（封板续持），不被强制清仓
    book = _book(is_sealed=True, seal_to_float_ratio=Decimal("0.05"))
    action = decider.decide_intraday(unit, None, book)
    assert action.action is not SellActionType.CLEAR


# ---------------------------------------------------------------------------
# 用例 2：连板续持——SIGNAL_DRIVEN + continuation_prob 高 + 秒板稳封 + 未命中 fail → HOLD
# ---------------------------------------------------------------------------
def test_continuation_hold_on_sealed_strong(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    prior = _prior(continuation_prob=Decimal("0.75"), fail_conditions=[])
    # 秒板稳封：封涨停且封流比高，未炸板 / 未破位 / 未烂板。
    book = _book(is_sealed=True, seal_to_float_ratio=Decimal("0.05"), open_times=0)

    action = decider.decide_intraday(unit, prior, book)
    assert action.action is SellActionType.HOLD
    assert action.reason == "秒板续持"


# ---------------------------------------------------------------------------
# 用例 3：破位止损——below_support + volume_surge → CLEAR，reason='走弱破位'
# ---------------------------------------------------------------------------
def test_break_support_with_volume_surge_clears(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    # 即便先验强，破支撑 + 放量下杀属盘口一票否决，照样清仓。
    book = _book(below_support=True, volume_surge=True)

    action = decider.decide_intraday(unit, _prior(continuation_prob=Decimal("0.8")), book)
    assert action.action is SellActionType.CLEAR
    assert action.reason == "走弱破位"


def test_break_support_without_volume_surge_not_clear(decider: SellDecider) -> None:
    # 仅破支撑但未放量（缩量回踩）→ 不构成破位止损，先验强则维持续持，避免误杀。
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    book = _book(below_support=True, volume_surge=False)
    action = decider.decide_intraday(unit, _prior(continuation_prob=Decimal("0.8")), book)
    assert action.action is SellActionType.HOLD


# ---------------------------------------------------------------------------
# 用例 4：纯技术退出——prior=None（TECH_EXIT）、盘口弱势 → 直接出（CLEAR/REDUCE）
# ---------------------------------------------------------------------------
def test_tech_exit_no_prior_weak_book_exits_intraday(decider: SellDecider) -> None:
    # 次日没涨停：mode=TECH_EXIT，prior 为空，纯盘口驱动。
    unit = _unit(mode=PositionMode.TECH_EXIT)
    # 弱势震荡（未破位但全天偏弱），先验缺失 → 保守了结。
    book = _book(near_close_weak=True)
    action = decider.decide_intraday(unit, None, book)
    # 先验弱 → CLEAR（不恋战）。
    assert action.action in (SellActionType.CLEAR, SellActionType.REDUCE)
    assert action.action is SellActionType.CLEAR


def test_tech_exit_no_prior_neutral_book_still_exits(decider: SellDecider) -> None:
    # 纯技术退出 + 中性盘口（无明确扳机）：先验弱仍倾向了结（不主动续持，安全默认）。
    unit = _unit(mode=PositionMode.TECH_EXIT)
    book = _book()  # 全中性
    action = decider.decide_intraday(unit, None, book)
    assert action.action in (SellActionType.CLEAR, SellActionType.REDUCE)


def test_tech_exit_auction_weak_open_exits(decider: SellDecider) -> None:
    # 纯技术退出 + 竞价低开走弱 → 开盘减/清，reason='弱开'。
    unit = _unit(mode=PositionMode.TECH_EXIT)
    book = _book(open_pct=Decimal("-0.02"))  # 低开
    action = decider.decide_auction(unit, None, book)
    assert action.action in (SellActionType.CLEAR, SellActionType.REDUCE)
    assert action.reason == "弱开"


# ---------------------------------------------------------------------------
# 用例 5：竞价背离减仓——book.price_volume_diverge → decide_auction=REDUCE/CLEAR
# ---------------------------------------------------------------------------
def test_auction_price_volume_diverge_reduces(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    # 高开但量价背离：盘口一票否决续持。先验强 → REDUCE（保留底仓）。
    book = _book(open_pct=Decimal("0.05"), price_volume_diverge=True)
    action = decider.decide_auction(unit, _prior(continuation_prob=Decimal("0.8")), book)
    assert action.action in (SellActionType.REDUCE, SellActionType.CLEAR)
    assert action.action is SellActionType.REDUCE
    assert action.reason == "竞价背离"


def test_auction_diverge_weak_prior_clears(decider: SellDecider) -> None:
    # 量价背离 + 先验弱（continuation_prob 低）→ CLEAR（不保留底仓）。
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    book = _book(open_pct=Decimal("0.05"), price_volume_diverge=True)
    action = decider.decide_auction(unit, _prior(continuation_prob=Decimal("0.3")), book)
    assert action.action is SellActionType.CLEAR
    assert action.reason == "竞价背离"


def test_auction_fail_condition_diverge_keyword_reduces(decider: SellDecider) -> None:
    # fail_conditions 命中背离类关键词（即便盘口 diverge=False）→ 同样作废续持转减/清。
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    prior = _prior(continuation_prob=Decimal("0.8"), fail_conditions=["竞价量价背离"])
    book = _book(open_pct=Decimal("0.05"), price_volume_diverge=False)
    action = decider.decide_auction(unit, prior, book)
    assert action.reason == "竞价背离"
    assert action.action in (SellActionType.REDUCE, SellActionType.CLEAR)


# ---------------------------------------------------------------------------
# 用例 6：炸板出——book.broke_board → decide_intraday=CLEAR，reason='炸板出'
# ---------------------------------------------------------------------------
def test_broke_board_clears(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    # 炸板优先级最高：即便同时封流比高，broke_board 一票出。
    book = _book(broke_board=True, is_sealed=True, seal_to_float_ratio=Decimal("0.05"))
    action = decider.decide_intraday(unit, _prior(continuation_prob=Decimal("0.9")), book)
    assert action.action is SellActionType.CLEAR
    assert action.reason == "炸板出"


# ---------------------------------------------------------------------------
# 用例 7：空仓闸门——risk_verdict=SELL_ONLY_HOLD → 存量仍按规则可卖（不被强制 HOLD）
# ---------------------------------------------------------------------------
def test_sell_only_hold_does_not_block_sell(decider: SellDecider) -> None:
    # 空仓闸门只锁买不锁卖：存量遇炸板仍照常清仓。
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    book = _book(broke_board=True)
    action = decider.decide_intraday(
        unit, _prior(), book, risk_verdict=RiskVerdict.SELL_ONLY_HOLD
    )
    # 未被强制 HOLD，仍按炸板规则清仓。
    assert action.action is SellActionType.CLEAR
    assert action.reason == "炸板出"


def test_sell_only_hold_does_not_block_continuation_hold(decider: SellDecider) -> None:
    # 空仓闸门 + 秒板稳封：卖出闸不被锁，续持判定照常生效（HOLD 是规则结果，非闸门强制）。
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    book = _book(is_sealed=True, seal_to_float_ratio=Decimal("0.05"))
    action = decider.decide_intraday(
        unit, _prior(), book, risk_verdict=RiskVerdict.SELL_ONLY_HOLD
    )
    assert action.action is SellActionType.HOLD
    assert action.reason == "秒板续持"


# ---------------------------------------------------------------------------
# 用例 8：冻结态——risk_verdict=FREEZE → HOLD（暂停新卖出）
# ---------------------------------------------------------------------------
def test_freeze_forces_hold_even_on_break(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    # 即便盘口炸板 + 破位（强卖信号），FREEZE 也优先短路为 HOLD（安全默认宁可不卖）。
    book = _book(broke_board=True, below_support=True, volume_surge=True)
    a_auction = decider.decide_auction(unit, _prior(), book, risk_verdict=RiskVerdict.FREEZE)
    a_intraday = decider.decide_intraday(unit, _prior(), book, risk_verdict=RiskVerdict.FREEZE)
    assert a_auction.action is SellActionType.HOLD
    assert a_auction.reason == "风控冻结,暂停新卖出"
    assert a_intraday.action is SellActionType.HOLD
    assert a_intraday.reason == "风控冻结,暂停新卖出"


# ---------------------------------------------------------------------------
# 用例 9：盘口来源——决策只读 OrderBook 字段，不读信号侧 last_price 快照
# ---------------------------------------------------------------------------
def test_decision_reads_orderbook_not_signal_last_price(decider: SellDecider) -> None:
    # 构造「信号侧 last_price 暗示强势」与「执行侧 OrderBook 炸板」不一致的场景：
    #   - book.last_price=12.00（看似在涨）但 broke_board=True（已炸板）。
    # 断言：决策取 OrderBook 的 broke_board 出清，而非被 last_price 误导成续持。
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    signal_side_last_price = Decimal("12.00")  # 信号侧快照（不应进决策路径）
    book = _book(last_price=signal_side_last_price, broke_board=True)
    action = decider.decide_intraday(unit, _prior(continuation_prob=Decimal("0.9")), book)
    # 取 book.broke_board → 清仓；若误读 last_price「在涨」则会续持，断言确保未误读。
    assert action.action is SellActionType.CLEAR
    assert action.reason == "炸板出"


# ---------------------------------------------------------------------------
# 补充：竞价高开强 + 量价匹配 + 先验强 + 未命中 fail → HOLD（进分时）
# ---------------------------------------------------------------------------
def test_auction_strong_open_matched_holds(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    prior = _prior(continuation_prob=Decimal("0.75"), fail_conditions=[])
    book = _book(open_pct=Decimal("0.06"), price_volume_diverge=False)
    action = decider.decide_auction(unit, prior, book)
    assert action.action is SellActionType.HOLD
    assert action.reason == "竞价高开续持"


# ---------------------------------------------------------------------------
# 补充：竞价 fail_conditions 命中弱开类关键词 → 弱开减/清
# ---------------------------------------------------------------------------
def test_auction_fail_condition_weak_open_keyword(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    # 盘口高开（open_pct>0 不弱），但 fail_conditions 命中「竞价弱于预期」弱开类 → 仍判弱开。
    prior = _prior(continuation_prob=Decimal("0.8"), fail_conditions=["竞价弱于均价"])
    book = _book(open_pct=Decimal("0.03"), price_volume_diverge=False)
    action = decider.decide_auction(unit, prior, book)
    assert action.reason == "弱开"


# ---------------------------------------------------------------------------
# 补充：烂板出——open_times 多（反复开板）→ CLEAR，reason='烂板出'
# ---------------------------------------------------------------------------
def test_messy_board_clears(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    # 反复开板（open_times>=2）但未炸板/未破位 → 烂板出。
    book = _book(open_times=3, is_sealed=False)
    action = decider.decide_intraday(unit, _prior(), book)
    assert action.action is SellActionType.CLEAR
    assert action.reason == "烂板出"


# ---------------------------------------------------------------------------
# 补充：分时冲高止盈——量价背离（量能透支/上方压力）→ 减/清，reason='冲高止盈'
# ---------------------------------------------------------------------------
def test_intraday_surge_overheat_reduces(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    # 未炸板/未破位/未烂板/未稳封，但量价背离（冲高量能透支）→ 先验强 REDUCE。
    book = _book(price_volume_diverge=True)
    action = decider.decide_intraday(unit, _prior(continuation_prob=Decimal("0.8")), book)
    assert action.reason == "冲高止盈"
    assert action.action is SellActionType.REDUCE


# ---------------------------------------------------------------------------
# 补充：分时尾盘了结——全天弱于续板预期（near_close_weak）→ 减/清，reason='尾盘了结'
# ---------------------------------------------------------------------------
def test_intraday_near_close_weak_settles(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    book = _book(near_close_weak=True)
    action = decider.decide_intraday(unit, _prior(continuation_prob=Decimal("0.8")), book)
    assert action.reason == "尾盘了结"
    # 先验强 → REDUCE。
    assert action.action is SellActionType.REDUCE


# ---------------------------------------------------------------------------
# 补充：决策留痕——卖出动作落决策日志（§5.10 第 7 条）
# ---------------------------------------------------------------------------
def test_decision_is_logged() -> None:
    logger = RecordingLogger()
    d = SellDecider(Settings(), _clock(), logger)
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    d.decide_intraday(unit, _prior(), _book(broke_board=True))
    assert "sell_decision" in logger.events()


# ---------------------------------------------------------------------------
# 补充：先验强但 fail_conditions 命中破位关键词亦可在竞价侧不直接清（破位由分时判），
#       此处验证「先验强 + 中性盘口 + 无 fail」分时维持续持（与一票否决互不串）
# ---------------------------------------------------------------------------
def test_strong_prior_neutral_book_holds_intraday(decider: SellDecider) -> None:
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    book = _book()  # 全中性，无任何扳机
    action = decider.decide_intraday(unit, _prior(continuation_prob=Decimal("0.8")), book)
    assert action.action is SellActionType.HOLD
    assert action.reason == "盘口未触发，维持续持"


# ===========================================================================
# 评审三轮 EXEC-position-08：竞价盘口缺失安全默认 HOLD（区别于 open_pct<=0 真实弱开）
# ===========================================================================
def test_auction_missing_book_holds(decider: SellDecider) -> None:
    # 盘口整体缺失（open_pct None + last_price None + 各结构布尔全 False + open_times 0）+ 先验弱：
    # 应安全默认 HOLD，不再以"弱开"误清仓（与 main.py book is None 不卖口径对齐）。
    unit = _unit(mode=PositionMode.TECH_EXIT)
    book = _book()  # 全缺失
    action = decider.decide_auction(unit, None, book)
    assert action.action is SellActionType.HOLD
    assert "盘口缺失" in action.reason


# ===========================================================================
# 评审 doc/19 C-2：竞价段实时封板续持（decide_auction 读 is_sealed，修复误清正封涨停的弱先验隔夜票）
# ===========================================================================
def test_auction_sealed_weak_prior_holds_not_clears(decider: SellDecider) -> None:
    """竞价正封涨停（is_sealed + 封流比强）+ 先验弱（continuation_prob 低）→ HOLD（不再误判弱开清仓）。"""
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    prior = _prior(continuation_prob=Decimal("0.3"))   # 先验弱（< 0.6）
    book = _book(open_pct=Decimal("0.10"), is_sealed=True, seal_to_float_ratio=Decimal("0.05"))
    action = decider.decide_auction(unit, prior, book)
    assert action.action is SellActionType.HOLD
    assert action.reason == "竞价封板续持"


def test_auction_sealed_no_prior_holds(decider: SellDecider) -> None:
    """竞价封板（封流比强）+ 先验缺失（prior=None，原会兜底 CLEAR「弱开」）→ HOLD（实时封板一票续持）。"""
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    book = _book(open_pct=Decimal("0.10"), is_sealed=True, seal_to_float_ratio=Decimal("0.05"))
    action = decider.decide_auction(unit, None, book)
    assert action.action is SellActionType.HOLD
    assert action.reason == "竞价封板续持"


def test_auction_not_sealed_weak_open_still_clears(decider: SellDecider) -> None:
    """反向防过度保护：竞价未封板（is_sealed=False）+ 低开走弱 + 先验弱 → 仍 CLEAR「弱开」，不被新分支挡掉。"""
    unit = _unit(mode=PositionMode.TECH_EXIT)
    book = _book(open_pct=Decimal("-0.02"), is_sealed=False)
    action = decider.decide_auction(unit, None, book)
    assert action.action is SellActionType.CLEAR
    assert action.reason == "弱开"


def test_auction_sealed_ratio_unknown_holds_not_clears(decider: SellDecider) -> None:
    """评审 doc/21 P2：竞价 is_sealed=True 但封流比不可得(seal_to_float_ratio=None,多因缺 float_mktcap)+ 先验弱
    → 保守续持 HOLD，绝不落弱开兜底以涨停价挂【清仓】单砸自家封板（原误判 CLEAR=卖飞正封涨停板）。"""
    unit = _unit(mode=PositionMode.TECH_EXIT)
    # open_pct=-0.01（表观弱开）但 is_sealed=True：验证封板裁决先于弱开分支、不被误判清仓。
    book = _book(open_pct=Decimal("-0.01"), is_sealed=True, seal_to_float_ratio=None)
    action = decider.decide_auction(unit, None, book)
    assert action.action == SellActionType.HOLD
    assert "封流比不可得" in action.reason


def test_intraday_sealed_ratio_unknown_holds_not_clears(decider: SellDecider) -> None:
    """评审 doc/21 P3：分时同源——is_sealed=True 但封流比不可得 + 先验弱 → 保守续持 HOLD，不落尾盘了结清仓砸自家封板。"""
    unit = _unit(mode=PositionMode.TECH_EXIT)
    book = _book(is_sealed=True, seal_to_float_ratio=None)
    action = decider.decide_intraday(unit, None, book)
    assert action.action == SellActionType.HOLD
    assert "封流比不可得" in action.reason


def test_intraday_weak_seal_weak_prior_reduces_not_holds() -> None:
    """评审 doc/21 P4：配了封流比强阈值后，弱封(0<ratio<seal_ratio_min)+ 先验弱 → 减仓 REDUCE（炸板前减仓窗口），
    既不 auto-HOLD（原弱封被误判稳封拒卖）也不全清砸板。"""
    d = SellDecider(Settings(seal_ratio_min=Decimal("0.01")), _clock(), RecordingLogger())
    unit = _unit(mode=PositionMode.TECH_EXIT)
    book = _book(is_sealed=True, seal_to_float_ratio=Decimal("0.005"))  # 弱封 < 0.01 阈值
    action = d.decide_intraday(unit, None, book)
    assert action.action == SellActionType.REDUCE


def test_intraday_weak_seal_strong_prior_holds() -> None:
    """评审 doc/21 P4：弱封但先验强 → 暂持 HOLD（等炸板等硬扳机），不减仓。"""
    d = SellDecider(Settings(seal_ratio_min=Decimal("0.01")), _clock(), RecordingLogger())
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    book = _book(is_sealed=True, seal_to_float_ratio=Decimal("0.005"))
    action = d.decide_intraday(unit, _prior(), book)  # 强先验 0.7
    assert action.action == SellActionType.HOLD


def test_intraday_strong_seal_still_holds(decider: SellDecider) -> None:
    """回归：强封(ratio>0,默认阈值 0)+ 任意先验 → 续持 HOLD（修复不破坏强封续持）。"""
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    book = _book(is_sealed=True, seal_to_float_ratio=Decimal("0.05"))
    action = decider.decide_intraday(unit, _prior(), book)
    assert action.action == SellActionType.HOLD
    assert action.reason == "秒板续持"


def test_auction_flat_open_zero_still_exits(decider: SellDecider) -> None:
    # 平开 open_pct==0（<=0 真实弱开，非缺失：open_pct 非 None）→ 仍走 reduce_or_clear，不被误判为缺失。
    unit = _unit(mode=PositionMode.TECH_EXIT)
    book = _book(open_pct=Decimal("0"))
    action = decider.decide_auction(unit, None, book)
    assert action.action in (SellActionType.CLEAR, SellActionType.REDUCE)


def test_auction_missing_open_pct_but_sealed_not_treated_missing(decider: SellDecider) -> None:
    # open_pct None 但 is_sealed=True（有结构信号）→ 不算缺失，按封板续持路径，不误 HOLD-盘口缺失。
    unit = _unit(mode=PositionMode.SIGNAL_DRIVEN)
    book = _book(is_sealed=True, seal_to_float_ratio=Decimal("0.05"))
    action = decider.decide_auction(unit, _prior(continuation_prob=Decimal("0.8")), book)
    assert action.reason != "盘口缺失,安全默认不卖"
