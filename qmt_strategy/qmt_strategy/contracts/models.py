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
    # 显式 ST 标志（评审三轮 F3）：主板 ST 涨停 5% 原完全依赖 name(name 缺失即按 10% 算出超法定涨停价废单/
    # 漏判 5% 顶板)。信号侧契约补此显式布尔后，board_rules/loader 优先采用 is_st，不再单点押在 name 上。
    # None=信号侧未下发(回退 name 判定)；True/False=显式 ST 与否。
    is_st: Optional[bool] = None
    # 连板维度（doc/18 禁买四板及以上硬规则）：信号侧 watchlist 契约已下发，执行侧据此做「禁买四板及以上」前置过滤。
    # board_level=连板高度（KPL「N 天 M 板/X 连/首板」解析，可空）；tier=入选分层 FIRST_BOARD/CHAIN/HIGH_BOARD
    # （信号侧恒非空，HIGH_BOARD ⟺ board_level>=4）。二者经 buy_prefilter 双源判高板：board_level 优先、tier 兜底。
    board_level: Optional[int] = None
    tier: Optional[str] = None
    # 打板因子（watchlist 契约 1.2.0，信号侧已下发，执行侧透传供策略消费；默认不改行为，配阈值才生效）：
    # 封板时序——first_limit_time/last_limit_time=首/末封时刻(HH:MM:SS 文本，东八区，不参与 UTC 下注体系、不做±8h)；
    # open_times=当日开板(炸板)次数(0=未开板/一字)。位置/强度——volume_ratio=T 日量比(≠盘中 auction_vol_ratio)；
    # return_5d_pct/return_10d_pct=近 5/10 日涨幅%(可为负)。均可空，缺失即 None、策略侧降级不误杀。
    first_limit_time: Optional[str] = None
    last_limit_time: Optional[str] = None
    open_times: Optional[int] = None
    volume_ratio: Optional[Decimal] = None
    return_5d_pct: Optional[Decimal] = None
    return_10d_pct: Optional[Decimal] = None
    # 数据缺测标记（doc/29 B1，对接信号侧 watchlist 契约 tradable_flag="DATA_MISSING"）：
    # data_missing=True 表示信号侧判定该票【约定核心交易指标(真正重要的行情数据)缺测】（close/board_level/
    # 封流比及分母/量能比及分母/位置收益/封板时序等任一缺；续板档 continuation_prob 已于 2026-06-22 移出核心
    # 缺测集，属 LLM 软先验、缺它不判缺测）。执行侧据此：买入侧放弃买入(B2)。
    # 口径变更（2026-06-21）：B3「缺测持仓强卖」已下线——缺测【不再强卖】已有持仓，卖出完全交由执行侧实时盘口
    # 扳机裁决；缺测只在买入侧拦截。与普通 tradable_flag=False（空仓/LLM 降级 BLOCKED）同为不可买，方向一致。
    # data_missing 仍保留为显式独立布尔：买入侧 fail-closed 收手须与「普通先验字段缺→fail-open 按盘口把关」区分。
    # data_missing_reason 记「缺哪些字段」(missing:col1,col2)，仅留痕/复盘用。
    data_missing: bool = False
    data_missing_reason: Optional[str] = None


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
    # ST 识别透传（禁买 ST 硬规则 + F08）：证券名称与显式 ST 标志一路透传到 PlanRow/EntryDecision，
    # 使「绝不买入 ST」的三层闸（loader universe / entry_router / order_executor）都拿得到可靠 ST 信号，
    # 不再只在 loader 单点判定后丢失。name=证券名（live ST 兜底），is_st=信号侧显式布尔（None=未下发回退 name）。
    name: Optional[str] = None
    is_st: Optional[bool] = None
    # 连板维度透传（doc/18 禁买四板及以上）：board_level/tier 一路带到 PlanRow/EntryDecision，
    # 使「禁买四板及以上」三层闸（loader 前置过滤 / entry_router / order_executor）都拿得到连板高度信号。
    board_level: Optional[int] = None
    tier: Optional[str] = None
    # 打板因子透传（1.2.0）：封板时序 + 位置/强度，一路带到 PlanRow 供策略消费（口径见 SelectedStockRow 同名字段）。
    first_limit_time: Optional[str] = None
    last_limit_time: Optional[str] = None
    open_times: Optional[int] = None
    volume_ratio: Optional[Decimal] = None
    return_5d_pct: Optional[Decimal] = None
    return_10d_pct: Optional[Decimal] = None
    # 数据缺测标记透传（doc/29 B1）：一路带到 PlanRow 供买入拦截(B2)；口径见 SelectedStockRow 同名字段。
    data_missing: bool = False
    data_missing_reason: Optional[str] = None

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
            # ST 信号透传给路由/下单层做禁买 ST 闸（禁买 ST 硬规则）。
            name=self.name,
            is_st=self.is_st,
            # 连板维度透传给路由/下单层做禁买四板及以上闸（doc/18）。
            board_level=self.board_level,
            tier=self.tier,
            # 打板因子透传（1.2.0）：封板时序 + 位置/强度，供策略消费（默认不改行为，配阈值才生效）。
            first_limit_time=self.first_limit_time,
            last_limit_time=self.last_limit_time,
            open_times=self.open_times,
            volume_ratio=self.volume_ratio,
            return_5d_pct=self.return_5d_pct,
            return_10d_pct=self.return_10d_pct,
            # 缺测标记透传给路由层做买入拦截（doc/29 B2）。
            data_missing=self.data_missing,
            data_missing_reason=self.data_missing_reason,
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
    # ST 识别透传（禁买 ST 硬规则）：name=证券名（live ST 兜底），is_st=信号侧显式 ST 布尔；
    # entry_router._should_skip 据此对 ST 标的一律 SKIP，绝不产 BUY 决策。
    name: Optional[str] = None
    is_st: Optional[bool] = None
    # 连板维度（doc/18 禁买四板及以上）：board_level=连板高度、tier=入选分层（HIGH_BOARD ⟺ 4 板+）；
    # entry_router._should_skip 经 buy_prefilter 据此对四板及以上标的一律 SKIP，绝不产 BUY 决策。
    board_level: Optional[int] = None
    tier: Optional[str] = None
    # 打板因子（watchlist 契约 1.2.0）：封板时序 + 位置/强度，供 entry 策略消费（口径见 SelectedStockRow 同名字段）。
    # 消费点（默认全关、配 settings 阈值才生效）：open_times→烂板/反复炸板弃；return_5d_pct→高位弃；
    # first_limit_time→龙回头首封太晚弃。volume_ratio/last_limit_time/return_10d_pct 本期仅透传留痕、不接判定。
    first_limit_time: Optional[str] = None
    last_limit_time: Optional[str] = None
    open_times: Optional[int] = None
    volume_ratio: Optional[Decimal] = None
    return_5d_pct: Optional[Decimal] = None
    return_10d_pct: Optional[Decimal] = None
    # 数据缺测标记（doc/29 B2）：data_missing=True → buy_prefilter/entry_router/base.prior_gate_reason 一律 SKIP，
    # 绝不产 BUY 决策（信号侧已判核心指标缺测）。口径见 SelectedStockRow 同名字段。
    data_missing: bool = False
    data_missing_reason: Optional[str] = None


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
    # 禁买 ST 最终闸标志（禁买 ST 硬规则第 3 层）：_build_decision 据统一口径 is_st_stock 算定；
    # order_executor.place 见此为真即拒单——所有买入（含转次优）必过唯一下单点，是绝不买入 ST 的最终硬保证。
    is_st: bool = False
    # 连板维度锚定（doc/18 禁买四板及以上第 3 层）：_build_decision 从 plan 透传，order_executor.place 据此
    # 经 buy_prefilter 复核四板及以上——所有买入必过唯一下单点，是绝不买入四板及以上的最终硬保证。
    board_level: Optional[int] = None
    tier: Optional[str] = None
    # 数据缺测最终闸标志（doc/29 B2 第 3 层冗余）：_build_decision 从 plan 透传；order_executor.place 见此为真即
    # 拒单——所有买入必过唯一下单点，是「缺测即放弃买入」的最终硬保证（与禁买 ST/四板同层）。
    data_missing: bool = False


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
    # 说明（口径变更 2026-06-21）：原 doc/29 B3「缺测持仓强卖」字段（data_missing/data_missing_reason）已下线——
    # 卖出决策完全交由执行侧 xtdata 实时盘口（炸板/破位/烂板/止损等盘口扳机），不再因信号侧缺测标记强制清仓。
    # 缺测仅在【买入侧】生效（SelectedStockRow/PlanRow/EntryDecision.data_missing → 放弃买入，doc/29 B2 保留），
    # 故 SignalPrior（卖出先验视图）不再携带缺测字段。


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
    # 在途未成卖出量（评审三轮 EXEC-position-03）：已挂出但尚未成交的卖单量。可卖上限 =
    # can_use_volume - on_road_sell_volume，防 REDUCE 部成后 PART_SOLD 单元相邻 tick 就在途未成量重复挂减仓单。
    # mark_selling 时 += 本次卖量冻结，apply_sell_fill 成交回扣，revert_selling 终态失败清零。
    on_road_sell_volume: int = 0
    # 量权威标记（评审三轮 EXEC-position-07）：True 表示 volume 来自券商快照/重建权威量，迟到/重复买入
    # 回调只登记 traded_id 去重、不再二次累加 volume（避免在权威量上重复加仓多报持仓）；下次快照重新校准。
    volume_authoritative: bool = False
    # 已回扣在途量的卖单 order_id 去重集（review 幂等）：on_road 的终态回扣是非幂等减法，同一卖单终态回调若
    # 重复触发（miniQMT 拒单常经 on_order_error + on_stock_order 双面回报，或同帧重投）会重复回扣、误清兄弟在途单
    # 冻结量并损坏状态。按 order_id 去重保证每单只回扣一次；refresh_state 跨日重置（隔夜旧单失效、防 order_id 跨日复用误判）。
    released_sell_order_ids: set = field(default_factory=set)


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
