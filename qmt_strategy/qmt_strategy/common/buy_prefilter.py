"""watchlist 买入前置过滤层（doc/18）。

业务意图：把执行侧所有「禁买硬规则」收敛为「有序规则集 + 结构化裁决」的过滤层抽象，作为买入禁止
规则的**单一来源**。三层——盘前装载 ``watchlist_loader``、建仓路由 ``entry_router``、唯一下单点
``order_executor``——都委托本层判定，既消除散落各处的内联 ST 判断，又叠加「四板及以上」禁买，
形成「绝不买入」的三层冗余防线（与既有禁买 ST 硬规则同构）。

内置两条硬规则（优先级从高到低）：
  1) RULE_ST：绝不买入 ST/退市整理——复用 ``universe_filter.is_st_stock``，口径不变（最严格：显式
     ``is_st=True`` 或当日证券名含 ``ST/退`` 即判 ST）。
  2) RULE_HIGH_BOARD：禁买四板及以上——``board_level >= min_level``（默认 4），或 ``tier==HIGH_BOARD``
     兜底（信号侧分桶 HIGH_BOARD ⟺ ``board_level>=4``；``tier`` 恒非空、``board_level`` 可空，双源更稳）。

边界口径：
  - 只在「有正向证据」时拦四板——``board_level`` 与 ``tier`` 都无法判高板时**放行**（无法证明它是 4+ 板，
    绝不无证据全拦）。生产中信号侧恒下发 ``tier``，故 4+ 板恒被 ``tier==HIGH_BOARD`` 命中。
  - 纯函数、无 I/O、确定性，便于单测；三层各自把自身行类型适配成 ``CandidateView`` 后调用。
  - 新增禁买规则 = 往 ``_RULES`` 追加一个纯函数，落点唯一。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .universe_filter import is_st_stock

# 四板及以上禁买默认阈值：board_level >= 该值即禁买（doc/18；可由 settings.forbid_board_level_min 覆盖）。
DEFAULT_HIGH_BOARD_MIN_LEVEL = 4

# 信号侧高位连板分层标识（与 limit_up_push_service 分桶口径一致：HIGH_BOARD ⟺ board_level>=4）。
_TIER_HIGH_BOARD = "HIGH_BOARD"

# 规则码（留痕 / 决策采集用，便于按口径检索「为何禁买」）。
RULE_ST = "st"
RULE_HIGH_BOARD = "high_board"
# 数据缺测禁买（doc/29 B2）：信号侧判该票【约定核心交易指标缺测】(tradable_flag=DATA_MISSING)，执行侧放弃买入。
RULE_DATA_MISSING = "data_missing"


@dataclass(frozen=True)
class CandidateView:
    """过滤层只依赖的最小候选视图。

    三层各自从 ``SelectedStockRow`` / ``PlanRow`` / ``EntryDecision`` 取这几个字段构造，
    使过滤层不耦合具体行类型（本模块仅依赖 ``universe_filter``，不 import ``contracts``）。
    """

    ts_code: Optional[str] = None
    name: Optional[str] = None
    is_st: Optional[bool] = None
    board_level: Optional[int] = None
    tier: Optional[str] = None
    # 数据缺测标记（doc/29 B2）：信号侧 watchlist tradable_flag=DATA_MISSING 解析而来；命中即禁买。
    data_missing: bool = False


@dataclass(frozen=True)
class PrefilterVerdict:
    """过滤层裁决：``allowed=False`` 时携命中规则码与中文理由（供日志 / 决策采集留痕）。"""

    allowed: bool
    rule_code: str = ""   # 放行时空串；否则 RULE_ST / RULE_HIGH_BOARD
    reason: str = ""


def is_high_board(
    board_level: Optional[int],
    tier: Optional[str],
    *,
    min_level: int = DEFAULT_HIGH_BOARD_MIN_LEVEL,
) -> bool:
    """是否四板及以上（doc/18 双源判定）。

    口径：
    - ``min_level <= 0``：视为「关闭高板口径」，一律返回 False（显式放宽逃生口）。
    - ``board_level`` 为正整数（>0 = 已识别连板高度）：**精确口径优先**，完全以 ``board_level >= min_level``
      判定（尊重配置，含调高 ``min_level`` 放宽到只拦更高板）。
    - ``board_level`` 缺失 / 未识别（None 或 <=0；信号侧无法解析连板时 board_level 落 0）：以 ``tier`` 兜底——
      ``tier==HIGH_BOARD`` 是信号侧权威「4+ 板」分桶。**只要高板规则启用（min_level>0）即保守拦截（fail-closed）**，
      绝不因板高缺失而漏放高板。``min_level > 4``（放宽到只拦更高板）的放宽**只作用于 board_level 已知的精确口径**；
      板高缺失时无法精确判定具体高度，按 4+ 板保守拦（与「绝不买入四板及以上」硬规则取向一致，宁可错杀不可漏放）。
    - 二者均无证据（board_level 缺失且 tier 非 HIGH_BOARD）→ False（绝不无证据全拦）。
    """
    if min_level <= 0:
        return False
    # 精确口径优先：board_level 为正整数即完全以阈值判定（板高已识别，最权威）。
    if isinstance(board_level, int) and board_level > 0:
        return board_level >= min_level
    # board_level 缺失 / 未识别：tier 兜底——HIGH_BOARD 是信号侧权威 4+ 板分桶，规则启用时一律 fail-closed 拦截。
    return tier is not None and str(tier).strip().upper() == _TIER_HIGH_BOARD


# 规则函数签名：命中禁买 → 返回 PrefilterVerdict(allowed=False, ...)；放行 → None。
# 入参 (view, min_level)：min_level 仅四板规则消费，ST 规则忽略（统一签名便于注册进有序规则集）。
_Rule = Callable[["CandidateView", int], Optional["PrefilterVerdict"]]


def _rule_st(view: "CandidateView", _min_level: int) -> Optional["PrefilterVerdict"]:
    """规则 1：绝不买入 ST/退市整理（最高优先级，与既有禁买 ST 硬规则口径完全一致）。"""
    if is_st_stock(view.is_st, view.name):
        return PrefilterVerdict(
            allowed=False,
            rule_code=RULE_ST,
            reason=f"禁买:ST/退市整理标的(name={view.name} is_st={view.is_st})",
        )
    return None


def _rule_high_board(view: "CandidateView", min_level: int) -> Optional["PrefilterVerdict"]:
    """规则 2：禁买四板及以上（board_level>=min_level 或 tier==HIGH_BOARD 兜底，见 is_high_board）。"""
    if is_high_board(view.board_level, view.tier, min_level=min_level):
        return PrefilterVerdict(
            allowed=False,
            rule_code=RULE_HIGH_BOARD,
            reason=(
                f"禁买:四板及以上(board_level={view.board_level} "
                f"tier={view.tier} min_level={min_level})"
            ),
        )
    return None


def _rule_data_missing(view: "CandidateView", _min_level: int) -> Optional["PrefilterVerdict"]:
    """规则 3：数据缺测即放弃买入（doc/29 B2）。信号侧 watchlist 判该票【约定核心交易指标缺测】
    （tradable_flag=DATA_MISSING）→ 执行侧绝不买入（最保守口径，宁可错过不买盲）。与 ST/四板同为硬禁买，
    在 loader/entry_router/order_executor 三层各自构造 CandidateView 时都带上 data_missing，形成三层冗余。"""
    if view.data_missing:
        return PrefilterVerdict(
            allowed=False,
            rule_code=RULE_DATA_MISSING,
            reason=f"禁买:核心交易指标缺测(ts_code={view.ts_code} data_missing=True)",
        )
    return None


# 有序禁买规则集（优先级从高到低）：新增禁买规则在此追加一个纯函数即可，落点唯一。
_RULES: Tuple[_Rule, ...] = (_rule_st, _rule_high_board, _rule_data_missing)

# 放行裁决单例（不可变、可复用，避免每次放行新建对象）。
_ALLOWED = PrefilterVerdict(allowed=True)


def evaluate(
    view: "CandidateView",
    *,
    high_board_min_level: int = DEFAULT_HIGH_BOARD_MIN_LEVEL,
) -> "PrefilterVerdict":
    """跑完有序禁买规则集：命中第一条禁买规则即返回该裁决；全部通过返回 allowed=True。

    high_board_min_level：四板规则阈值（默认 4），由调用方从 settings.forbid_board_level_min 透传。
    """
    for rule in _RULES:
        verdict = rule(view, high_board_min_level)
        if verdict is not None:
            return verdict
    return _ALLOWED
