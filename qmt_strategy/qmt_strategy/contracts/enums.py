"""执行侧全链路共享枚举（锁定契约层）。

业务意图：把设计文档（doc/03 第二~六节）中散落的状态/方向/阶段口径集中为单一来源，
避免各模块各写一套字符串导致口径漂移。所有枚举均继承 ``str``，便于直接落库 / 日志 / JSON
透传（值即字符串，无需再做映射）。
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """字符串枚举基类：value 即落库/日志字符串，``str(x)`` 取值不带类名前缀。"""

    def __str__(self) -> str:  # 保证 f-string / 日志里直接拿到纯值
        return str(self.value)


class TradeSide(StrEnum):
    """买卖方向（落 qmt_trade/qmt_order.trade_side，§6.3）。"""

    BUY = "BUY"
    SELL = "SELL"
    # 方向不可判定（评审三轮 EXEC-DW-09）：order_type 既非 BUY/SELL 字符串、又不命中数值表时的显式未知态。
    # 绝不臆造为 BUY（凭空建仓）。落库留痕，_apply_trade_to_position 对 UNKNOWN 拒绝改持仓 + 强告警。
    UNKNOWN = "UNKNOWN"


class OrderStatus(StrEnum):
    """委托终态枚举（落 qmt_order.order_status，§6.3）。

    REPORTED 已报 / PART_TRADED 部成 / TRADED 已成 / CANCELLED 已撤 /
    REJECTED 废单（on_stock_order）/ ERROR 下单失败（on_order_error）。
    """

    REPORTED = "REPORTED"
    PART_TRADED = "PART_TRADED"
    TRADED = "TRADED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class OrderState(StrEnum):
    """order_executor 本地下单状态机（§4.5）。

    PLANNED 已生成 biz_order_no 写台账未发单 → SUBMITTED 已发 order_stock →
    REPORTED 已报 → PART_TRADED 部成 → TRADED 全成；
    CANCELLING 已发撤单 → CANCELLED 已撤；REJECTED 废单；ERROR 下单异常。
    """

    PLANNED = "PLANNED"
    SUBMITTED = "SUBMITTED"
    REPORTED = "REPORTED"
    PART_TRADED = "PART_TRADED"
    TRADED = "TRADED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"

    # 终态集合：到达任一终态后不再推进/重复下单（幂等收口）。
    @classmethod
    def terminal(cls) -> "frozenset[OrderState]":
        return frozenset({cls.TRADED, cls.PART_TRADED, cls.CANCELLED, cls.REJECTED, cls.ERROR})

    # 「活跃单」集合：用于 has_active 幂等判定（已报/部成/已成视为已占用该计划）。
    @classmethod
    def active(cls) -> "frozenset[OrderState]":
        return frozenset({cls.PLANNED, cls.SUBMITTED, cls.REPORTED, cls.PART_TRADED, cls.TRADED, cls.CANCELLING})


class AuctionPhase(StrEnum):
    """集合竞价时段状态机（§3.3 / §3.6，边界精确到秒，按东八区判定）。"""

    PRE_AUCTION = "PRE_AUCTION"            # < 9:15 预热
    AUCTION_CANCELABLE = "AUCTION_CANCELABLE"  # 9:15–9:20 可撤
    AUCTION_LOCKED = "AUCTION_LOCKED"      # 9:20–9:25 不可撤
    SETTLED = "SETTLED"                    # >= 9:25 定盘
    CLOSED_WINDOW = "CLOSED_WINDOW"        # >= 9:30 或窗口结束


class Board(StrEnum):
    """板块（决定涨跌幅规则，§2.3 第 5 步）。"""

    MAIN = "MAIN"        # 主板 ±10%
    CHINEXT = "CHINEXT"  # 创业板 ±20%


class PriceSource(StrEnum):
    """价位来源（§2.5 PriceBudget.price_source）。"""

    SIGNAL = "SIGNAL"          # 采用信号侧给定价位
    LOCAL_CALC = "LOCAL_CALC"  # 信号侧缺失，执行侧按 board 兜底现算
    MISSING = "MISSING"        # 全缺，该票降级转观察名单


class EntryAction(StrEnum):
    """建仓动作（§4.2 五类）。"""

    CHASE_LIMIT_UP = "CHASE_LIMIT_UP"          # 打板跟买
    CHASE_AUCTION_STRONG = "CHASE_AUCTION_STRONG"  # 竞价强开追
    DIP_BUY_MA = "DIP_BUY_MA"                  # 均线低吸
    LEADER_PULLBACK = "LEADER_PULLBACK"        # 龙回头
    SKIP = "SKIP"                              # 放弃（仍留痕）


class OrderPhase(StrEnum):
    """下单阶段（决定可撤性，§4.3 EntryDecision.order_phase）。"""

    AUCTION = "AUCTION"    # 竞价单（9:20 后不可撤）
    OPENING = "OPENING"    # 开盘后单


class PositionState(StrEnum):
    """持仓状态机（§5.2.1）。"""

    LOCKED_T1 = "LOCKED_T1"   # 当日买入锁定（守 T+1）
    HOLDING = "HOLDING"       # 正常持有，等待卖出决策
    SELLING = "SELLING"       # 已发卖出委托，在途
    SOLD = "SOLD"             # 全部卖出，单元关闭
    PART_SOLD = "PART_SOLD"   # 部分卖出（减仓）
    FROZEN = "FROZEN"         # 风控冻结态


class PositionMode(StrEnum):
    """持仓续持模式（§5.2.4 关键分叉）。"""

    SIGNAL_DRIVEN = "SIGNAL_DRIVEN"  # 次日又涨停，吃信号侧先验续持
    TECH_EXIT = "TECH_EXIT"          # 次日没涨停，纯技术退出（仅盘口驱动）


class SellActionType(StrEnum):
    """卖出动作（§5.7 SellAction）。价位不在此处，由 QMT 下单层自定。"""

    HOLD = "HOLD"        # 续持
    REDUCE = "REDUCE"    # 减仓
    CLEAR = "CLEAR"      # 清仓


class RiskVerdict(StrEnum):
    """风控闸门裁决（§5.6 risk.gate）。"""

    ALLOW = "ALLOW"                    # 放行
    FREEZE = "FREEZE"                  # 冻结，暂停一切新决策
    SELL_ONLY_HOLD = "SELL_ONLY_HOLD"  # 空仓闸门：只守仓不新开，存量仍可卖


class DataSource(StrEnum):
    """回流数据来源（§6.2 / DDL data_source 列）。"""

    CALLBACK = "CALLBACK"              # 实时回调
    QUERY_BACKFILL = "QUERY_BACKFILL"  # 收盘兜底 / 断线补采
    QUERY = "QUERY"                    # 定时拉取快照（资产/持仓默认来源）


class SnapshotType(StrEnum):
    """快照类型（§6.2.2 / DDL snapshot_type 列）。历史净值/持仓复盘只认 CLOSE。"""

    OPEN = "OPEN"          # 开盘前昨夜拥股基线
    INTRADAY = "INTRADAY"  # 盘中（不进历史净值）
    CLOSE = "CLOSE"        # 收盘权威


class CentroidTrend(StrEnum):
    """竞价重心趋势（§3.4 因子 3）。"""

    UP = "UP"      # 末帧价 > 重心，越竞越高
    DOWN = "DOWN"  # 末帧价 < 重心，高开回落
    FLAT = "FLAT"  # 持平 / 数据不足
