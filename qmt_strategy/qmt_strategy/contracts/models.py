"""执行侧全链路共享数据结构（锁定契约层）。

所有模块只依赖本文件与 ``enums`` / ``protocols``，不互相依赖对方实现，
以便按依赖分层并行开发并保持口径一致。字段口径严格对齐 doc/03 各节命名。

价位一律用 ``Decimal`` 承载（A 股价位 0.01 精度，禁止用 float 做比较 / 累加），
时间一律 UTC naive（东八区原值另存 ``*_east8``，§6.6 时间口径）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .enums import (
    AuctionPhase,
    Board,
    CentroidTrend,
    DataSource,
    EntryAction,
    OrderPhase,
    OrderState,
    OrderStatus,
    PositionMode,
    PositionState,
    PriceSource,
    SellActionType,
    SnapshotType,
    TradeSide,
)

# ---------------------------------------------------------------------------
# 一、信号侧契约（watchlist 只读消费）
# ---------------------------------------------------------------------------


@dataclass
class SelectedStockRow:
    """信号侧 limit_up_selected_stock 的一行原始契约（§1.2.2 / 闭环归因 §1.1）。

    执行侧只读消费、绝不回写。可选字段缺失时为 None，由下游按降级口径处理
    （§2.6 单票价位缺失只降级单票、不影响整批）。
    """

    ts_code: str                                   # 标准代码 600000.SH / 000001.SZ（未必已归一）
    trade_date: date                               # = T 信号日
    target_trade_date: date                        # = T+1 计划买入日
    # 强度与画像
    leader_strength_score: Optional[Decimal] = None
    role: Optional[str] = None                     # 龙头/中军/补涨/分歧转一致...
    strategy: Optional[str] = None                 # 战法
    market_state: Optional[str] = None             # 情绪周期：启动/高潮/震荡/退潮/冰点/空仓
    tradable_flag: Optional[bool] = None           # 可成交性 / 是否重点可参与
    # 先验概率
    continuation_prob: Optional[Decimal] = None    # 次日续板概率先验
    next_day_premium_prob: Optional[Decimal] = None  # 隔日溢价为正概率先验
    # 卖出侧先验（§5.3.1）
    boost: Optional[Any] = None                    # 加分项（龙头/题材卡位/封板质量等正向因子）
    fail_conditions: Optional[List[str]] = None    # 结构化失败条件
    # 参考价位
    signal_close: Optional[Decimal] = None         # T 日收盘价
    limit_up_price: Optional[Decimal] = None        # T 日理论涨停价
    reasonable_open_high_low: Optional[Decimal] = None   # 合理高开区间下沿
    reasonable_open_high_high: Optional[Decimal] = None  # 合理高开区间上沿
    # 因子基准（§3.4 因子 2/4）
    first_board_vol: Optional[int] = None          # 首板放量基准（缺失则因子降级）
    float_mktcap: Optional[Decimal] = None         # 流通市值（算封流比用）
    # 路由维度（§4.2，可由 strategy/role 推导，信号侧给则优先）
    strategy_family: Optional[str] = None          # 战法大类
    setup: Optional[str] = None                    # 技术形态/位置
    # 证券名称（评审二轮 P1#18/#63）：信号侧契约已含，执行侧据此识别 ST/退市（主板 ST 涨停 5%）并做 live 过滤。
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# 二、watchlist 内存契约（盘中只读，§2.5）
# ---------------------------------------------------------------------------


@dataclass
class PriceBudget:
    """今日价位预算（§2.5）。"""

    limit_up_price: Decimal             # 今日理论涨停价（信号侧给或 board 现算）
    reasonable_open_low: Decimal        # 合理高开区间下沿
    reasonable_open_high: Decimal       # 合理高开区间上沿
    board: Board                        # MAIN(±10%) / CHINEXT(±20%)
    price_source: PriceSource           # SIGNAL / LOCAL_CALC / MISSING


@dataclass
class TradableEntry:
    """可交易 / 观察名单中的一只票（§2.5）。透传字段供回流期 join。"""

    norm_code: str                      # 归一 ts_code（600000.SH），与 qmt_trade 关联键一致
    target_trade_date: date             # = 今日（= 信号 T 的 T+1 买入日）
    signal_trade_date: date             # = T（透传，便于回流期 join limit_up_selected_stock）
    market_state: Optional[str]         # 情绪周期（透传）
    tradable_flag: Optional[bool]       # 可成交性
    role: Optional[str]
    strategy: Optional[str]
    leader_strength_score: Optional[Decimal]
    continuation_prob: Optional[Decimal]
    next_day_premium_prob: Optional[Decimal]
    price: PriceBudget
    # 路由维度透传（供 entry_router）
    strategy_family: Optional[str] = None
    setup: Optional[str] = None
    first_board_vol: Optional[int] = None
    float_mktcap: Optional[Decimal] = None

    def to_plan_row(self) -> "PlanRow":
        """转为 auction/entry 消费的计划行（窄视图）。"""
        return PlanRow(
            ts_code=self.norm_code,
            signal_trade_date=self.signal_trade_date,
            target_trade_date=self.target_trade_date,
            limit_up_price=self.price.limit_up_price,
            reasonable_open_low=self.price.reasonable_open_low,
            reasonable_open_high=self.price.reasonable_open_high,
            board=self.price.board,
            first_board_vol=self.first_board_vol,
            float_mktcap=self.float_mktcap,
            role=self.role,
            strategy=self.strategy,
            strategy_family=self.strategy_family,
            setup=self.setup,
            market_state=self.market_state,
            tradable_flag=self.tradable_flag,
            continuation_prob=self.continuation_prob,
            next_day_premium_prob=self.next_day_premium_prob,
            leader_strength_score=self.leader_strength_score,
        )


@dataclass
class WatchlistContext:
    """盘前一次性装载的当日契约（§2.5），盘中只读。"""

    trade_date: date                            # target_trade_date = 今日
    is_open: bool                               # a_trade_calendar 校验
    open_new_position_allowed: bool             # 空仓闸门：False = 只守仓不开新仓
    tradable: Dict[str, TradableEntry]          # 可交易名单，key=norm_code，盘中 O(1) 查
    watch_only: List[TradableEntry]             # 观察名单（不下单，留作复盘/买不进对照）
    degraded: bool = False                      # 是否进入降级态（取契约失败兜底）
    degraded_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# 三、竞价决策因子（§3.4 / §3.6）
# ---------------------------------------------------------------------------


@dataclass
class PlanRow:
    """auction_poller / entry_router 消费的当日计划行（TradableEntry 的窄视图）。"""

    ts_code: str
    signal_trade_date: date
    target_trade_date: date
    limit_up_price: Optional[Decimal] = None
    reasonable_open_low: Optional[Decimal] = None
    reasonable_open_high: Optional[Decimal] = None
    board: Optional[Board] = None
    first_board_vol: Optional[int] = None
    float_mktcap: Optional[Decimal] = None
    role: Optional[str] = None
    strategy: Optional[str] = None
    strategy_family: Optional[str] = None
    setup: Optional[str] = None
    market_state: Optional[str] = None
    tradable_flag: Optional[bool] = None
    continuation_prob: Optional[Decimal] = None
    next_day_premium_prob: Optional[Decimal] = None
    leader_strength_score: Optional[Decimal] = None


@dataclass
class SealInfo:
    """虚拟封单计算结果（§3.4 因子 4）。"""

    virtual_seal_amount: Decimal = Decimal("0")     # 虚拟封单额 = 买一量 × 买一价
    seal_to_float_ratio: Optional[Decimal] = None   # 封流比 = 封单额 / 流通市值
    is_limit_up: bool = False                        # 虚拟成交价是否已达涨停价
    data_quality: List[str] = field(default_factory=list)


@dataclass
class AuctionSnapshot:
    """单只股票每帧因子聚合对象（§3.6）。下游 entry_router 据此做买/弃。"""

    ts_code: str
    phase: AuctionPhase
    ts: datetime                                # 采集时刻 UTC naive
    open_pct: Optional[Decimal] = None          # 竞价高开幅度
    auction_vol_ratio: Optional[Decimal] = None  # 竞价量能 / 首板爆量比例
    auction_centroid: Optional[Decimal] = None   # 分时重心（量加权均价）
    centroid_trend: CentroidTrend = CentroidTrend.FLAT
    virtual_seal_amount: Decimal = Decimal("0")
    seal_to_float_ratio: Optional[Decimal] = None
    is_limit_up: bool = False                    # 是否顶涨停
    last_price: Optional[Decimal] = None         # 当前帧虚拟成交价（留痕，降级 B 用）
    pre_close: Optional[Decimal] = None
    data_quality: List[str] = field(default_factory=list)
    tick_seq: int = 0                            # 已采帧数


# ---------------------------------------------------------------------------
# 四、建仓决策与下单台账（§4.3 / §4.5）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryDecision:
    """建仓决策（§4.3）。frozen：决策一旦产出不可变，便于复盘/回放。"""

    ts_code: str
    signal_trade_date: date                 # T（透传给 order_remark / qmt_*.signal_trade_date）
    target_trade_date: date                 # T+1 买入日 = 今日
    strategy_family: str
    setup: str
    action: EntryAction
    decided_at: datetime                    # UTC naive
    reason: str                             # 买/弃理由（含命中的因子值）
    side: TradeSide = TradeSide.BUY         # 建仓只有 BUY
    limit_price: Optional[Decimal] = None   # 计划限价；SKIP 为 None
    plan_volume: Optional[int] = None       # 计划买入股数；SKIP 为 None
    order_phase: OrderPhase = OrderPhase.AUCTION
    factors_snapshot: Dict[str, Any] = field(default_factory=dict)
    # 次优候选序列（§4.4(7) 转次优）：主标的买不进时按序尝试。
    next_best: tuple = ()                   # tuple[EntryDecision, ...]（用 tuple 保 frozen 可哈希）


@dataclass
class LedgerEntry:
    """本地下单台账一行（§4.4 / §6.7 对账事实源之一）。"""

    biz_order_no: str                       # 业务唯一单号（§4.4(1)）
    account_id: str
    target_trade_date: date
    ts_code: str
    strategy_family: str
    side: TradeSide
    plan_volume: int
    plan_price: Optional[Decimal]
    order_remark: str
    signal_trade_date: Optional[date]
    state: OrderState = OrderState.PLANNED
    order_id: Optional[int] = None
    filled_volume: int = 0                  # 累计成交量
    avg_filled_price: Optional[Decimal] = None
    order_phase: OrderPhase = OrderPhase.AUCTION
    cancelable: bool = True                 # 9:20–9:25 段下单标记不可撤
    miss_reason: Optional[str] = None       # 一字未成 / 秒封未成 / 排队未成
    cancel_failed: bool = False
    error_id: Optional[int] = None
    error_msg: Optional[str] = None
    created_at: Optional[datetime] = None   # UTC naive
    updated_at: Optional[datetime] = None
    # 已计入的成交编号集合：add_fill 按 traded_id 去重，防回报重投/断线重连重复累计（§6.5/§4.4(4)）。
    counted_trade_ids: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# 五、持仓管理 / 卖出 / 风控（§5.7）
# ---------------------------------------------------------------------------


@dataclass
class SignalPrior:
    """消费 limit_up_selected_stock 的只读先验视图（§5.7）。执行侧不回写。"""

    ts_code: str
    trade_date: date                        # T
    target_trade_date: date                 # T+1
    continuation_prob: Optional[Decimal] = None
    boost: Optional[Any] = None
    fail_conditions: List[str] = field(default_factory=list)
    market_state: Optional[str] = None
    role: Optional[str] = None
    strategy: Optional[str] = None


@dataclass
class PositionUnit:
    """持仓单元（按 account_id + ts_code 聚合，FIFO 合并成本，§5.7）。"""

    account_id: str
    ts_code: str
    volume: int                             # 总持仓（含当日买入）
    can_use_volume: int                     # 可用（T+1 不含当日买入）
    avg_cost: Decimal
    earliest_sellable_date: date            # 最早可卖日 = trade_cal_next(买入日 B)
    state: PositionState = PositionState.LOCKED_T1
    mode: PositionMode = PositionMode.TECH_EXIT
    buy_date: Optional[date] = None         # 买入日 B（= target_trade_date）
    # 去重：已计入的 traded_id，防重复回报重复加仓（§5.6 幂等）
    counted_trade_ids: set = field(default_factory=set)


@dataclass
class OrderBook:
    """执行侧 xtdata 实时盘口快照（§5.7，仅执行侧可得，非信号侧 last_price 快照）。

    卖出决策为定性判断，这里给出已加工的盘口信号；具体阈值在 sell_decider 按战法配置。
    """

    ts_code: str
    last_price: Optional[Decimal] = None
    open_pct: Optional[Decimal] = None          # 竞价/开盘高开幅度
    seal_amount: Optional[Decimal] = None        # 封单额
    seal_to_float_ratio: Optional[Decimal] = None  # 封流比
    open_times: int = 0                          # 开板次数
    is_sealed: bool = False                      # 当前是否封涨停
    broke_board: bool = False                    # 是否炸板（已封后开板）
    below_support: bool = False                  # 是否跌破分时关键支撑/均价线
    volume_surge: bool = False                   # 是否放量（下杀放量/换手剧增）
    price_volume_diverge: bool = False           # 量价背离（高开但量能虚高或骤缩）
    near_close_weak: bool = False                # 全天弱于先验续板预期（尾盘了结判据）
    data_quality: List[str] = field(default_factory=list)


@dataclass
class SellAction:
    """卖出决策动作（§5.7）。价位由 QMT 下单层自定，不在此处。"""

    ts_code: str
    action: SellActionType
    reason: str
    reduce_ratio: Optional[Decimal] = None  # REDUCE 时的减仓比例（None=策略默认）


@dataclass
class RiskDecision:
    """风控闸门裁决（§5.6）。"""

    verdict: "Any"                          # RiskVerdict（避免循环 import，运行期为枚举）
    reason: str = ""


# ---------------------------------------------------------------------------
# 六、回流落库记录（对齐四表 DDL，§2.1–2.4 / §6.3）
# ---------------------------------------------------------------------------


@dataclass
class TradeRecord:
    """qmt_trade 成交明细落库记录（§2.1 DDL）。"""

    account_id: str
    trade_date: date
    ts_code: str                            # 已归一
    qmt_stock_code: str                     # 原值
    traded_id: str
    trade_side: TradeSide
    traded_price: Decimal
    traded_volume: int
    traded_time: Optional[datetime] = None       # UTC naive
    traded_time_east8: Optional[datetime] = None  # 东八区 naive 原值
    account_type: Optional[int] = None
    order_id: Optional[int] = None
    order_sysid: Optional[str] = None
    offset_flag: Optional[int] = None
    traded_amount: Optional[Decimal] = None
    strategy_name: Optional[str] = None
    order_remark: Optional[str] = None
    signal_trade_date: Optional[date] = None
    data_source: DataSource = DataSource.CALLBACK


@dataclass
class OrderRecord:
    """qmt_order 委托落库记录（§2.2 DDL）。"""

    account_id: str
    trade_date: date
    ts_code: str
    qmt_stock_code: str
    order_id: int
    trade_side: TradeSide
    order_volume: int
    order_status: OrderStatus
    traded_volume: int = 0
    account_type: Optional[int] = None
    order_sysid: Optional[str] = None
    offset_flag: Optional[int] = None
    price_type: Optional[int] = None
    order_price: Optional[Decimal] = None
    traded_price: Optional[Decimal] = None
    status_msg: Optional[str] = None
    error_id: Optional[int] = None
    error_msg: Optional[str] = None
    cancel_failed: bool = False
    order_time: Optional[datetime] = None
    order_time_east8: Optional[datetime] = None
    strategy_name: Optional[str] = None
    order_remark: Optional[str] = None
    signal_trade_date: Optional[date] = None
    data_source: DataSource = DataSource.CALLBACK


@dataclass
class PositionRecord:
    """qmt_position_snapshot 持仓快照落库记录（§2.3 DDL）。"""

    account_id: str
    trade_date: date
    ts_code: str
    qmt_stock_code: str
    snapshot_type: SnapshotType = SnapshotType.CLOSE
    volume: int = 0
    can_use_volume: int = 0
    account_type: Optional[int] = None
    frozen_volume: Optional[int] = None
    on_road_volume: Optional[int] = None
    yesterday_volume: Optional[int] = None
    open_price: Optional[Decimal] = None
    avg_price: Optional[Decimal] = None
    market_value: Optional[Decimal] = None
    last_price: Optional[Decimal] = None
    float_profit: Optional[Decimal] = None
    profit_rate: Optional[Decimal] = None
    data_source: DataSource = DataSource.QUERY


@dataclass
class AccountRecord:
    """qmt_account_daily 账户资产日快照落库记录（§2.4 DDL）。"""

    account_id: str
    trade_date: date
    total_asset: Decimal
    cash: Decimal
    snapshot_type: SnapshotType = SnapshotType.CLOSE
    frozen_cash: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    net_cash_flow: Decimal = Decimal("0")
    account_type: Optional[int] = None
    prev_total_asset: Optional[Decimal] = None
    daily_pnl: Optional[Decimal] = None
    daily_return: Optional[Decimal] = None
    cash_flow_note: Optional[str] = None
    data_source: DataSource = DataSource.QUERY
