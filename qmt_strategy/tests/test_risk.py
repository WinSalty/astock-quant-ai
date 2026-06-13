"""风控护栏单测（§5.4 / §5.9 风控相关用例）。

全部用 fake/内存实现，不连真实 xttrader/xtdata/MySQL：
- 时钟用 FakeClock 固定时刻；日志用 RecordingLogger 断言留痕。
- Settings 直接构造（带入风控阈值与 market_state_block），不读环境变量。

覆盖要点（对齐题目「单测必须覆盖」每一条）：
- 空仓闸门：market_state='空仓' → SELL_ONLY_HOLD（不新开、存量仍可卖）。
- 冻结态：market_feed_ok=False 或 trade_conn_ok=False → FREEZE。
- 账户级/单票级击穿：超阈值 → FREEZE。
- 卖量钳制：clamp_sell_volume(1000, 600)==600；can_use_volume=0 → 0。
- 安全默认：行情断流（market_feed_ok=False）即 FREEZE，不论 market_state。
- is_open_blocked：退潮/冰点/空仓 → True；启动 → False。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.common.time_utils import FakeClock
from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import RiskVerdict
from qmt_strategy.risk.risk import Risk


def _clock() -> FakeClock:
    # 固定 UTC naive 时刻；风控裁决与时刻无关，仅满足构造依赖。
    return FakeClock(datetime(2026, 6, 12, 1, 30, 0))


def _settings(**overrides) -> Settings:
    """构造带风控阈值的配置；overrides 覆盖默认阈值。

    默认给定账户级/单票级阈值便于击穿用例；market_state_block 用 Settings 默认（退潮/冰点/空仓）。
    """
    base = dict(
        account_drawdown_limit=Decimal("0.05"),   # 账户当日回撤 5% 红线
        account_loss_limit=Decimal("10000"),       # 账户当日已实现亏损 1 万元红线
        stock_float_loss_limit=Decimal("2000"),    # 单票浮亏 2000 元红线
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def risk() -> Risk:
    return Risk(_settings(), _clock(), RecordingLogger())


# ---------------------------------------------------------------------------
# 空仓闸门：market_state='空仓' → SELL_ONLY_HOLD（不新开、存量仍可卖）
# ---------------------------------------------------------------------------
def test_gate_empty_position_returns_sell_only_hold(risk: Risk) -> None:
    decision = risk.gate(market_state="空仓", market_feed_ok=True, trade_conn_ok=True)
    # 空仓只关买入闸、不锁卖出闸，故为 SELL_ONLY_HOLD 而非 FREEZE。
    assert decision.verdict is RiskVerdict.SELL_ONLY_HOLD
    assert "空仓" in decision.reason


def test_gate_empty_position_allows_existing_sell_via_clamp(risk: Risk) -> None:
    # 存量仍可卖：空仓态下卖量钳制照常工作，不被锁死为 0。
    assert risk.clamp_sell_volume(500, 500) == 500


# ---------------------------------------------------------------------------
# 冻结态：market_feed_ok=False 或 trade_conn_ok=False → FREEZE
# ---------------------------------------------------------------------------
def test_gate_market_feed_down_freezes(risk: Risk) -> None:
    decision = risk.gate(market_state="启动", market_feed_ok=False, trade_conn_ok=True)
    assert decision.verdict is RiskVerdict.FREEZE


def test_gate_trade_conn_down_freezes(risk: Risk) -> None:
    decision = risk.gate(market_state="启动", market_feed_ok=True, trade_conn_ok=False)
    assert decision.verdict is RiskVerdict.FREEZE


def test_gate_freeze_is_logged(risk: Risk) -> None:
    logger = RecordingLogger()
    r = Risk(_settings(), _clock(), logger)
    r.gate(market_state="启动", market_feed_ok=False)
    # 冻结须留痕（§5.10 第 7 条）。
    assert "risk_gate_freeze" in logger.events()


# ---------------------------------------------------------------------------
# 账户级击穿：回撤 / 已实现亏损超阈值 → FREEZE
# ---------------------------------------------------------------------------
def test_gate_account_drawdown_breach_freezes(risk: Risk) -> None:
    # 回撤 6% > 5% 红线。
    decision = risk.gate(market_state="启动", account_drawdown=Decimal("0.06"))
    assert decision.verdict is RiskVerdict.FREEZE


def test_gate_account_realized_loss_breach_freezes(risk: Risk) -> None:
    # 已实现亏损 1.2 万 > 1 万红线。
    decision = risk.gate(market_state="启动", account_realized_loss=Decimal("12000"))
    assert decision.verdict is RiskVerdict.FREEZE


def test_gate_account_within_limit_allows(risk: Risk) -> None:
    # 回撤 4% < 5%、亏损 8000 < 1 万：均未击穿，正常情绪周期 → ALLOW。
    decision = risk.gate(
        market_state="启动",
        account_drawdown=Decimal("0.04"),
        account_realized_loss=Decimal("8000"),
    )
    assert decision.verdict is RiskVerdict.ALLOW


def test_gate_threshold_none_not_constrained() -> None:
    # 阈值未配置（None）时该层不约束，即便给了很大的回撤也不冻结。
    r = Risk(_settings(account_drawdown_limit=None), _clock(), RecordingLogger())
    decision = r.gate(market_state="启动", account_drawdown=Decimal("0.99"))
    assert decision.verdict is RiskVerdict.ALLOW


# ---------------------------------------------------------------------------
# 单票级击穿：单票浮亏超阈值 → FREEZE
# ---------------------------------------------------------------------------
def test_gate_unit_float_loss_breach_freezes(risk: Risk) -> None:
    # 单票浮亏 2500 > 2000 红线。
    decision = risk.gate(market_state="启动", unit_float_loss=Decimal("2500"))
    assert decision.verdict is RiskVerdict.FREEZE


def test_gate_unit_float_loss_within_limit_allows(risk: Risk) -> None:
    decision = risk.gate(market_state="启动", unit_float_loss=Decimal("1000"))
    assert decision.verdict is RiskVerdict.ALLOW


# ---------------------------------------------------------------------------
# 卖量钳制：min(决策量, 可用量)，绝不超量卖
# ---------------------------------------------------------------------------
def test_clamp_sell_volume_caps_at_can_use(risk: Risk) -> None:
    assert risk.clamp_sell_volume(1000, 600) == 600


def test_clamp_sell_volume_zero_can_use(risk: Risk) -> None:
    # can_use_volume=0（当日全为新买入、整仓 T+1 锁定）→ 钳为 0，不发卖单。
    assert risk.clamp_sell_volume(1000, 0) == 0


def test_clamp_sell_volume_decision_below_can_use(risk: Risk) -> None:
    # 决策量小于可用量时按决策量出，不放大到可用量。
    assert risk.clamp_sell_volume(300, 600) == 300


def test_clamp_sell_volume_negative_inputs_floor_zero(risk: Risk) -> None:
    # 防御性：负值兜底为 0，绝不产出负卖量。
    assert risk.clamp_sell_volume(-100, 600) == 0
    assert risk.clamp_sell_volume(1000, -1) == 0


# ---------------------------------------------------------------------------
# 安全默认：行情断流即 FREEZE，不论 market_state（优先级最高）
# ---------------------------------------------------------------------------
def test_safety_default_feed_down_freezes_regardless_of_market_state(risk: Risk) -> None:
    # 即便 market_state='空仓'，行情断流也优先 FREEZE，而非降级为 SELL_ONLY_HOLD。
    decision = risk.gate(market_state="空仓", market_feed_ok=False, trade_conn_ok=True)
    assert decision.verdict is RiskVerdict.FREEZE


def test_safety_default_feed_down_freezes_even_when_none_state(risk: Risk) -> None:
    # market_state 未知（None）+ 行情断流 → 仍 FREEZE，宁可不交易（§5.4.3）。
    decision = risk.gate(market_state=None, market_feed_ok=False)
    assert decision.verdict is RiskVerdict.FREEZE


def test_freeze_priority_over_account_and_empty(risk: Risk) -> None:
    # 同时命中行情断流 + 空仓 + 账户击穿：关键输入不可信优先级最高，统一 FREEZE。
    decision = risk.gate(
        market_state="空仓",
        market_feed_ok=False,
        account_drawdown=Decimal("0.99"),
    )
    assert decision.verdict is RiskVerdict.FREEZE


# ---------------------------------------------------------------------------
# is_open_blocked：退潮/冰点/空仓 → True；启动 → False
# ---------------------------------------------------------------------------
def test_is_open_blocked_blocks_retreat_freeze_empty(risk: Risk) -> None:
    assert risk.is_open_blocked("退潮") is True
    assert risk.is_open_blocked("冰点") is True
    assert risk.is_open_blocked("空仓") is True


def test_is_open_blocked_allows_active_state(risk: Risk) -> None:
    assert risk.is_open_blocked("启动") is False


def test_is_open_blocked_none_state_not_blocked(risk: Risk) -> None:
    # 情绪周期未知不在禁开集合内（冻结由 gate 负责，本方法只做开仓闸判定）。
    assert risk.is_open_blocked(None) is False


# ---------------------------------------------------------------------------
# 正常放行路径留痕
# ---------------------------------------------------------------------------
def test_gate_allow_normal_state() -> None:
    logger = RecordingLogger()
    r = Risk(_settings(), _clock(), logger)
    decision = r.gate(market_state="启动", market_feed_ok=True, trade_conn_ok=True)
    assert decision.verdict is RiskVerdict.ALLOW
    assert "risk_gate_allow" in logger.events()
