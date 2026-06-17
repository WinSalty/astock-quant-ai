"""持仓状态机 position_manager（§5.2）。

业务意图：把买入成交回报落地为「按 account_id + ts_code 聚合」的持仓单元（PositionUnit），
负责守 T+1 的硬口径（买入当日不可卖）、跨买入日推进状态、挂接信号侧先验/纯技术退出分叉，
并对外提供「可卖单元集合」与卖出成交累计回写。它只做卖出方向的状态维护，不做下单（下单在
sell_decider + order 层）。

不可推翻的口径（与 doc/03 §5.1~§5.2 一致）：
- A 股 T+1：买入日 B 当日不可卖；最早可卖日 earliest_sellable_date = trade_cal_next(B)，
  经交易日历映射，**禁止自然日 +1**（跨周末/节假日会错，§5.2.2）。
- 「次日」一律指买入日 B 的次一交易日（首个可卖日），即 calendar.next_open(B)。
- 多笔买入按 FIFO 合并成本（avg_cost 按量加权，§5.2.1）。
- 幂等：同一笔成交回报（traded_id）只计一次，重复回报不重复加仓（§5.6）。

时间口径：本模块只处理「交易日（date）」级别的状态推进，所有日推算经 calendar；
落库时刻另由回流侧统一 UTC naive，本模块不直接写库。
"""

from __future__ import annotations

import copy
import threading
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..contracts.enums import PositionMode, PositionState
from ..contracts.models import PositionUnit, SignalPrior
from ..contracts.protocols import Clock, StructLogger, TradeCalendar


class PositionManager:
    """持仓状态机（§5.2）。

    内部按 (account_id, ts_code) 维护唯一 PositionUnit（一股一单元、FIFO 合并成本）。

    依赖：
    - calendar：交易日历，earliest_sellable_date / 跨买入日推进全部经此（禁自然日 ±1）。
    - clock：时钟抽象（contracts.Clock），用于决策/日志时刻取数，不做手工 ±8h。
    - logger：结构化日志（contracts.StructLogger），状态流转留痕。
    - prior_provider：先验取数回调 (ts_code, today) -> Optional[SignalPrior]。
        当日刷新的 limit_up_selected_stock 中有该股（次日又涨停）→ 返回 SignalPrior；
        无该股（次日没涨停 / 退出信号池）→ 返回 None。默认实现恒返回 None
        （等价「无信号侧先验」，自动落入纯技术退出分支，符合安全默认）。
    """

    def __init__(
        self,
        calendar: TradeCalendar,
        clock: Clock,
        logger: StructLogger,
        prior_provider: Callable[[str, date], Optional[SignalPrior]] = lambda ts_code, today: None,
    ) -> None:
        self._calendar = calendar
        self._clock = clock
        self._logger = logger
        # 先验取数回调：默认返回 None（无先验 → 纯技术退出，安全默认）。
        self._prior_provider = prior_provider
        # 持仓单元表：key = (account_id, ts_code)，保证一股一行聚合。
        self._units: Dict[Tuple[str, str], PositionUnit] = {}
        # 并发护栏（评审三轮 EXEC-position-01+EXEC-DW-04 收敛）：QMT 回调线程（成交回写 / 断线重连 rebuild）
        # 与调度线程（refresh_state / sellable_units / sweep / 卖出巡检）会并发读写 self._units 及单元字段。
        # 本锁不仅保护单元表的「结构性增删 / 迭代取快照」，还把所有「单元字段级读-改-写」(mark_position_on_fill /
        # apply_sell_fill / mark_selling / revert_selling / apply_position_snapshot / refresh_state / rebuild /
        # counted_trade_ids 维护）整体纳入临界区，杜绝脏读/丢更新（volume/can_use_volume 双扣或漏扣、去重集竞态）。
        # 写入一律走原子方法（apply_sell_fill_by_trade / revert_selling_by_code），get_unit 对外只返回只读深拷贝
        # 快照，绝不把 live 引用交给调用方在锁外改。可重入(RLock)以便同线程内嵌套调用（如原子方法内调 apply_sell_fill）不自锁。
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 买入成交回报落地（§5.6 mark_position_on_fill）
    # ------------------------------------------------------------------
    def mark_position_on_fill(
        self, fill: Any, today: date, *, account_id: str, ts_code: Optional[str] = None
    ) -> PositionUnit:
        """买入成交回报落地：建立/更新持仓单元，打最早可卖日标记。

        业务意图：守 T+1——买入当日 B（= today）不可卖，最早 trade_cal_next(B) 才可卖。
        入参：
        - fill：成交回报对象（XtTrade 或规整后的 TradeRecord），读取 traded_id / traded_price /
          traded_volume。用 getattr 容忍版本字段差异（§6.3）。
        - today：当前交易日，即买入日 B（= target_trade_date = T+1）。
        - account_id：账户号（关键字参数，强制显式传入，避免与 fill 内字段口径混淆）。
        - ts_code：可选的归一证券代码（评审 P0-A2）。回调侧应传入 normalize 后的 ts_code，
          使持仓单元键与盘口 books / 计划行 plan_map / QMT 快照口径一致（均为归一码，如 600000.SH）；
          缺省 None 时回退读 fill.stock_code（兼容直接喂 XtTrade 的单测口径）。

        边界：
        - earliest_sellable_date = calendar.next_open(B)，经交易日历映射（禁自然日 +1）。
        - 当日买入（today == B）→ 状态置 LOCKED_T1，can_use_volume 不含当日买入（置 0）。

        幂等：按 (account_id, ts_code) 合并；按 traded_id 去重（counted_trade_ids），
        重复回报不重复加仓（§5.6）。同一 traded_id 第二次进入直接返回当前单元，不改量。

        FIFO 合并成本：avg_cost = (原成本×原量 + 本次价×本次量) / 总量。
        """
        # —— 读取成交回报字段（容忍版本差异，缺失即异常数据，按 0/空处理）——
        # ts_code 优先用显式传入的归一码（回调侧口径），缺省回退 fill.stock_code（XtTrade 原始码）。
        ts_code = ts_code if ts_code is not None else getattr(fill, "stock_code", None)
        # traded_id 归一为 str（评审 P1#7）：与台账去重 / 持久化口径一致，避免 int/str 混用导致去重失效。
        traded_id_raw = getattr(fill, "traded_id", None)
        traded_id = str(traded_id_raw) if traded_id_raw is not None else None
        traded_price_raw = getattr(fill, "traded_price", None)
        traded_volume_raw = getattr(fill, "traded_volume", None)
        if ts_code is None:
            raise ValueError("mark_position_on_fill: 成交回报缺少证券代码，无法定位持仓单元")

        # 价位一律走 Decimal（禁 float 累加比较）；成交量取 int。
        traded_price = Decimal(str(traded_price_raw)) if traded_price_raw is not None else Decimal("0")
        traded_volume = int(traded_volume_raw) if traded_volume_raw is not None else 0

        # —— 最早可卖日：经交易日历取买入日 B 的下一交易日（禁自然日 +1）——
        # 日历计算无共享状态，放在锁外；后续对单元字典/字段的读-改-写整体纳入 self._lock（评审三轮
        # EXEC-position-01：杜绝回调线程与调度线程对同一 unit 的脏读/丢更新）。
        earliest_sellable_date = self._calendar.next_open(today)

        key = (account_id, ts_code)
        # 全段读-改-写纳入 RLock 临界区（可重入，内部互调不自锁）。
        with self._lock:
            unit = self._units.get(key)

            # —— 再买入命中已关闭(SOLD)单元（评审二轮 P1#13）——
            # 旧单元已全部卖出、终态关闭；本次买入是【全新建仓】，不能并入旧 SOLD 单元（否则状态停 SOLD、
            # 新仓永不进卖出决策 = 漏卖）。这里把 unit 视为 None 走"首次建仓"分支重建全新单元；旧单元被覆盖。
            # 注意须在 traded_id 去重之前重置：新单元应独立去重，不受旧单元 counted_trade_ids 影响。
            if unit is not None and unit.state == PositionState.SOLD:
                self._logger.info(
                    "position_rebuy_after_sold_new_unit",
                    account_id=account_id,
                    ts_code=ts_code,
                )
                unit = None

            # —— 幂等去重：同一 traded_id 已计入则直接返回，不重复加仓（§5.6）——
            # 边界：traded_id 为 None（异常回报）时不进入去重集合，但仍按一次成交合并（保守计入）。
            if unit is not None and traded_id is not None and traded_id in unit.counted_trade_ids:
                self._logger.info(
                    "position_fill_duplicate",
                    account_id=account_id,
                    ts_code=ts_code,
                    traded_id=traded_id,
                )
                return unit

            if unit is None:
                # 首次建仓：当日买入 → LOCKED_T1；can_use_volume 不含当日买入，置 0（守 T+1 量闸）。
                unit = PositionUnit(
                    account_id=account_id,
                    ts_code=ts_code,
                    volume=traded_volume,
                    can_use_volume=0,
                    avg_cost=traded_price,
                    earliest_sellable_date=earliest_sellable_date,
                    state=PositionState.LOCKED_T1,
                    mode=PositionMode.TECH_EXIT,  # 默认纯技术退出，refresh_state 据先验再校正
                    buy_date=today,
                )
                self._units[key] = unit
            elif unit.volume_authoritative and traded_id is not None and traded_id not in unit.counted_trade_ids:
                # 量权威单元（评审三轮 EXEC-position-07）：volume 来自券商快照/重建权威量，本笔很可能是「已被
                # 权威量计入的迟到/重复买入回调」——只登记 traded_id 去重，**不再二次累加 volume**（否则多报持仓）。
                # 成本/可卖日不动，待下次 apply_position_snapshot/rebuild 以券商权威重新校准。
                self._logger.info(
                    "position_authoritative_late_fill_dedup_only",
                    account_id=account_id, ts_code=ts_code, traded_id=traded_id,
                )
            else:
                # 加仓合并：FIFO 按量加权成本（原成本×原量 + 本次价×本次量）/ 总量。
                prev_vol = unit.volume
                prev_cost = unit.avg_cost
                new_vol = prev_vol + traded_volume
                if new_vol > 0:
                    unit.avg_cost = (prev_cost * Decimal(prev_vol) + traded_price * Decimal(traded_volume)) / Decimal(
                        new_vol
                    )
                unit.volume = new_vol
                unit.buy_date = today
                # 最早可卖日取「最早一笔」为准（取 min，绝不被新一笔抬高）：单元的 earliest_sellable_date
                # 表示「该单元最早从哪天起有可卖量」。若被新一笔抬高，会把昨日已过 T+1 的可卖存量也一起
                # 重新锁住、当日卖不掉，违反 §5.2.2「卖出只对昨日及更早持仓生效」（评审 medium#1）。
                unit.earliest_sellable_date = min(unit.earliest_sellable_date, earliest_sellable_date)
                # 守 T+1 的「当日补买不可卖」由 can_use_volume 量闸承载（refresh_state 开盘前已据昨仓置量，
                # 当日补买不计入 can_use_volume），故此处【不】把已可卖单元整体重锁回 LOCKED_T1。
                # 仅当单元尚未跨过最早可卖日（首笔买入当日）时维持 LOCKED_T1。
                if today < unit.earliest_sellable_date and unit.state not in (
                    PositionState.HOLDING,
                    PositionState.PART_SOLD,
                    PositionState.SELLING,
                    PositionState.SOLD,
                ):
                    unit.state = PositionState.LOCKED_T1

            # —— 记入去重集合（防重复回报重复加仓）——
            if traded_id is not None:
                unit.counted_trade_ids.add(traded_id)

        self._logger.info(
            "position_marked_on_fill",
            account_id=account_id,
            ts_code=ts_code,
            traded_id=traded_id,
            buy_date=str(today),
            earliest_sellable_date=str(unit.earliest_sellable_date),
            volume=unit.volume,
            state=str(unit.state),
        )
        return unit

    # ------------------------------------------------------------------
    # 每日开盘前推进状态 + 先验挂接（§5.6 refresh_state）
    # ------------------------------------------------------------------
    def refresh_state(self, today: date) -> None:
        """每交易日开盘前推进状态：跨过买入日的 LOCKED_T1 → HOLDING，并刷新先验挂接。

        业务意图：守 T+1 的状态侧落地——只要 today >= earliest_sellable_date 就跨过了买入日，
        当日买入锁定（LOCKED_T1）转为可决策的 HOLDING；同时把昨日买入部分计入可用量。

        先验挂接分叉（§5.2.4 关键分叉）：
        - prior 存在（次日又涨停、命中当日 limit_up_selected_stock）→ mode = SIGNAL_DRIVEN，
          续持决策继续吃信号侧先验。
        - prior 为空（次日没涨停、退出信号池）→ mode = TECH_EXIT，转纯技术退出，不再等先验。

        边界：
        - SOLD（单元已关闭）/ FROZEN（风控冻结）不在此推进，避免覆盖终态/冻结态。
        - PART_SOLD 的剩余部分由本方法刷回 HOLDING（部成后剩余继续按规则可卖，§5.2.1）。
        """
        # 整段循环纳入锁（评审三轮 EXEC-position-01）：盘前推进对单元字段(can_use_volume/state/mode)的写须与
        # 回调线程的 apply_sell_fill/mark_position_on_fill 字段级读改互斥，杜绝脏读/丢更新。先验回调 _prior_provider
        # 为纯读外部信号，置于锁内可接受（盘前一次性、非热路径）。
        with self._lock:
          for unit in list(self._units.values()):
            # 终态/冻结态不推进：SOLD 已关闭、FROZEN 由风控恢复链路处理。
            if unit.state in (PositionState.SOLD, PositionState.FROZEN):
                continue

            # —— 跨日清算残留在途卖量（评审 F02/F16）——
            # A 股「日内单」隔夜全部失效/被撤，故任何【非 SELLING】单元在新交易日开盘前的 on_road_sell_volume
            # 必是昨日已死委托的残留（典型：昨日 REDUCE 部成后剩余委托被撤、但 filled>0 不触发零成交复位钩子，
            # 且撤单回执断线丢失，on_road 永挂 → sellable_remaining=can_use-on_road 把可卖量无依据下调=永久漏卖）。
            # 这里在跨日推进前统一清零，杜绝漏卖；SELLING 单元交由 reconcile_stuck_selling 按券商终态裁决，不在此动。
            if unit.state != PositionState.SELLING:
                if getattr(unit, "on_road_sell_volume", 0):
                    stale = unit.on_road_sell_volume
                    unit.on_road_sell_volume = 0
                    self._logger.info(
                        "position_stale_on_road_cleared",
                        account_id=unit.account_id, ts_code=unit.ts_code,
                        cleared=stale, today=str(today),
                    )
                # 重置卖单回扣去重集（review 幂等）：隔夜旧卖单 order_id 失效，清空防 order_id 跨日复用被误判「已回扣」。
                if getattr(unit, "released_sell_order_ids", None):
                    unit.released_sell_order_ids = set()

            crossed_buy_date = today >= unit.earliest_sellable_date

            if crossed_buy_date:
                # 跨过买入日：昨日及更早持仓全部可用（守 T+1 量闸放开），can_use_volume = volume。
                unit.can_use_volume = unit.volume
                # LOCKED_T1 / PART_SOLD（剩余）跨买入日后回到可决策的 HOLDING；
                # SELLING（委托在途）不在此覆盖，等成交回报推进。
                if unit.state in (PositionState.LOCKED_T1, PositionState.PART_SOLD):
                    prev_state = unit.state
                    unit.state = PositionState.HOLDING
                    self._logger.info(
                        "position_state_advanced",
                        account_id=unit.account_id,
                        ts_code=unit.ts_code,
                        from_state=str(prev_state),
                        to_state=str(unit.state),
                        today=str(today),
                    )
            else:
                # 仍在买入当日：守 T+1，当日买入部分不可卖，can_use_volume 保持 0。
                unit.can_use_volume = 0
                if unit.state == PositionState.LOCKED_T1:
                    # 维持锁定，无需流转。
                    pass

            # —— 先验挂接：经回调取当日先验，决定续持模式（§5.2.4）——
            # 仅对已可卖（跨买入日）的单元刷新先验，买入当日尚无「次日表现」可言。
            if crossed_buy_date:
                prior = self._prior_provider(unit.ts_code, today)
                if prior is not None:
                    # 次日又涨停：吃信号侧先验续持。
                    unit.mode = PositionMode.SIGNAL_DRIVEN
                else:
                    # 次日没涨停 / 退出信号池：转纯技术退出（无先验，仅盘口驱动）。
                    unit.mode = PositionMode.TECH_EXIT
                self._logger.info(
                    "position_prior_attached",
                    account_id=unit.account_id,
                    ts_code=unit.ts_code,
                    today=str(today),
                    mode=str(unit.mode),
                )

    # ------------------------------------------------------------------
    # 可卖单元集合（§5.2.2 卖出只对昨日及更早持仓生效）
    # ------------------------------------------------------------------
    def sellable_units(self, today: date) -> List[PositionUnit]:
        """返回当前可卖的持仓单元：today >= earliest_sellable_date 且状态可卖。

        业务意图：守 T+1 的标的集合闸——卖出决策与下单的标的恒为「昨日及更早持仓」，
        买入当日（LOCKED_T1）即使风控想清仓也卖不掉（T+1 物理约束，§5.2.2）。

        可卖状态集合：HOLDING / PART_SOLD（减仓后剩余继续可卖）/ SELLING（委托在途，仍属可卖标的，
        供上层判定重复下单幂等）。SOLD（已关闭）/ LOCKED_T1（守 T+1）/ FROZEN（风控冻结）不返回。
        """
        sellable_states = (PositionState.HOLDING, PositionState.PART_SOLD, PositionState.SELLING)
        # 锁内取快照后再筛（评审复审 P1）：迭代期间回调线程并发改字典不致崩溃。
        with self._lock:
            units = list(self._units.values())
        result: List[PositionUnit] = []
        for unit in units:
            if today >= unit.earliest_sellable_date and unit.state in sellable_states:
                result.append(unit)
        return result

    # ------------------------------------------------------------------
    # 卖出委托发出 / 卖出成交回写（§5.2.1 SELLING / SOLD / PART_SOLD）
    # ------------------------------------------------------------------
    def mark_selling(self, unit: PositionUnit, *, sell_volume: int = 0) -> None:
        """标记单元进入 SELLING（已发卖出委托、等待回报），并冻结本次在途委托量（评审三轮 EXEC-position-03）。

        业务意图：卖单幂等的状态闸——同一单元在 SELLING 态不应重复发同向卖单（§5.8）；并把本次已挂出的卖量
        计入 on_road_sell_volume 冻结，使可卖上限 = can_use_volume - on_road_sell_volume，防 REDUCE 部成后
        PART_SOLD 单元相邻 tick 就「在途未成量」重复挂减仓单（超挂/废单/对账阻断）。
        边界：只允许从可卖态（HOLDING / PART_SOLD）进入 SELLING；其余态拒绝，避免越权改写。
        """
        with self._lock:
            if unit.state not in (PositionState.HOLDING, PositionState.PART_SOLD):
                raise ValueError(
                    f"mark_selling: 单元 {unit.account_id}/{unit.ts_code} 当前状态 {unit.state} 不可发卖单"
                )
            unit.state = PositionState.SELLING
            if sell_volume > 0:
                unit.on_road_sell_volume += int(sell_volume)
            self._logger.info(
                "position_marked_selling",
                account_id=unit.account_id,
                ts_code=unit.ts_code,
                volume=unit.volume,
                on_road_sell_volume=unit.on_road_sell_volume,
            )

    @staticmethod
    def sellable_remaining(unit: PositionUnit) -> int:
        """可卖上限 = can_use_volume - 在途未成卖出量（评审三轮 EXEC-position-03），不低于 0。"""
        return max(0, unit.can_use_volume - getattr(unit, "on_road_sell_volume", 0))

    def revert_selling(self, unit: PositionUnit, *, reason: str = "") -> None:
        """卖单终态失败时把单元从 SELLING 复位回可卖态（评审二轮 P1#11/#12/#31）。

        业务意图：原实现卖单一旦置 SELLING，**唯一**退出路径是"真实卖出成交回报"——卖单同步下单失败 /
        被券商拒单 / 全撤零成交 / 挂不到价超时这四类"零成交终态失败"都没有复位钩子，单元永久卡死在
        SELLING，止损/破位清仓永久失效（漏卖裸奔）。本方法是这四类场景的统一复位入口。

        复位口径：
        - 只对 SELLING 态生效（幂等防御）：若期间已收到部分/全部成交回报，状态已是 PART_SOLD/SOLD，
          不在此覆盖（避免把已成交事实回退）。
        - 仍有持仓量(volume>0) → 回 HOLDING，下一轮卖出巡检可重新挂单；volume<=0（理论上不应出现）→ SOLD。
        - can_use_volume 在 mark_selling 时未扣减（只有 apply_sell_fill 扣减），复位后仍可用，无需恢复。
        - on_road_sell_volume（在途未成卖量）在终态失败时清零（评审三轮 EXEC-position-03）：零成交终态失败
          意味着这批在途委托全部撤销，不再占可卖上限。
        """
        with self._lock:
            if unit.state != PositionState.SELLING:
                return
            unit.state = PositionState.HOLDING if unit.volume > 0 else PositionState.SOLD
            unit.on_road_sell_volume = 0  # 终态失败：在途未成卖量全部撤销，解冻可卖上限
        self._logger.warn(
            "position_revert_selling",
            account_id=unit.account_id,
            ts_code=unit.ts_code,
            to_state=str(unit.state),
            reason=reason,
        )

    def apply_sell_fill(self, unit: PositionUnit, traded_volume: int) -> PositionUnit:
        """卖出成交回报累计回写：扣减持仓量，按累计成交推进状态。

        业务意图：卖出成交累计 == 原持仓量 → SOLD、单元归零关闭；
        累计 < 原持仓量 → PART_SOLD（剩余部分次日由 refresh_state 刷回 HOLDING，§5.2.1）。

        边界：
        - traded_volume 钳到剩余量上限，绝不卖成负仓（防废单/对账失真，§5.8）。
        - 全成后 volume / can_use_volume 归零，单元置 SOLD（关闭，refresh_state 不再推进）。
        - 部成后可用量同步扣减（剩余可继续卖）。
        """
        sell_vol = int(traded_volume)
        if sell_vol <= 0:
            return unit
        # 字段级读-改-写纳入锁（评审三轮 EXEC-position-01）：与调度线程/盘前推进对同一 unit 字段互斥。
        with self._lock:
            # 钳到剩余持仓量，绝不超卖（防负仓）。
            applied = min(sell_vol, unit.volume)
            unit.volume -= applied
            # 可用量同步扣减（不低于 0）。
            unit.can_use_volume = max(0, unit.can_use_volume - applied)
            # 在途未成卖量回扣（评审三轮 EXEC-position-03）：本批成交即从在途冻结量扣回，使后续可卖上限正确。
            unit.on_road_sell_volume = max(0, unit.on_road_sell_volume - applied)

            if unit.volume <= 0:
                # 全部卖出：仓位归零，单元关闭（SOLD 为终态）。
                unit.volume = 0
                unit.can_use_volume = 0
                unit.on_road_sell_volume = 0
                unit.state = PositionState.SOLD
                self._logger.info(
                    "position_sold",
                    account_id=unit.account_id,
                    ts_code=unit.ts_code,
                )
            else:
                # 部分卖出（减仓）：置 PART_SOLD，剩余部分次日 refresh_state 刷回 HOLDING。
                unit.state = PositionState.PART_SOLD
                self._logger.info(
                    "position_part_sold",
                    account_id=unit.account_id,
                    ts_code=unit.ts_code,
                    remaining=unit.volume,
                )
        return unit

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_unit(self, account_id: str, ts_code: str) -> Optional[PositionUnit]:
        """按 (account_id, ts_code) 取持仓单元【只读深拷贝快照】（评审三轮 EXEC-position-01）。

        对外只返回深拷贝：调用方对快照的任何改动不影响内部 live 单元，杜绝「拿 live 引用在锁外改字段」的
        脏读/丢更新。需要写入持仓的调用方一律走原子方法（apply_sell_fill_by_trade / revert_selling_by_code /
        mark_position_on_fill），而非改 get_unit 的返回值。
        """
        with self._lock:
            unit = self._units.get((account_id, ts_code))
            return copy.deepcopy(unit) if unit is not None else None

    # ------------------------------------------------------------------
    # 原子写入方法（评审三轮 EXEC-position-01+EXEC-DW-04）：去重+扣减+状态推进一体，调用方不持 live 引用
    # ------------------------------------------------------------------
    def apply_sell_fill_by_trade(
        self, account_id: str, ts_code: str, traded_id: Optional[str], traded_volume: int
    ) -> Optional[PositionUnit]:
        """卖出成交回报原子落地：单一临界区内一体完成「取 live 单元 → 按 traded_id 去重 → 扣减 + 状态推进」。

        替代原 main 层「get_unit + 手动 counted_trade_ids 判重/add + apply_sell_fill」的锁外三步（去重检查与
        扣减之间的窗口可被另一回调插入导致重复扣减/丢更新）。返回处理后的 live 单元（None=无该单元/被去重跳过）。
        """
        with self._lock:
            unit = self._units.get((account_id, ts_code))
            if unit is None:
                return None
            if traded_id is not None:
                if traded_id in unit.counted_trade_ids:
                    return None  # 重复回报：已计入，直接跳过（不重复扣减）
                unit.counted_trade_ids.add(traded_id)
            vol = int(traded_volume) if traded_volume else 0
            if vol > 0:
                self.apply_sell_fill(unit, vol)  # RLock 可重入，内部不二次取锁问题
            return unit

    def revert_selling_by_code(self, account_id: str, ts_code: str, *, reason: str = "") -> None:
        """按 (account_id, ts_code) 原子复位 SELLING 单元（替代 main 层 get_unit+revert_selling 两步）。"""
        with self._lock:
            unit = self._units.get((account_id, ts_code))
            if unit is None:
                return
            self.revert_selling(unit, reason=reason)

    def release_on_road_by_code(
        self, account_id: str, ts_code: str, released_qty: int, *, order_id: Optional[int] = None, reason: str = ""
    ) -> None:
        """卖单终态失败时，精确按本单未成量回扣在途冻结量（评审 F02/F16 + review 幂等）。

        业务意图：原实现仅在「整单零成交且单元 SELLING」时经 revert_selling 把 on_road 清零；部成单
        （filled>0，单元已被 apply_sell_fill 推进为 PART_SOLD）剩余委托被撤/废时，其未成在途量永不回扣 →
        sellable_remaining = can_use_volume - on_road_sell_volume 把可卖上限永久下调 = 漏卖、止损卖不出。
        本方法按【本单未成量 released_qty】精确回扣（不动其它在途单的冻结量，多单在途也不误清），
        是卖单终态失败的唯一在途量释放入口。
        幂等（review）：回扣是非幂等减法，同一卖单终态回调若重复触发（拒单经 on_order_error+on_stock_order 双面
        回报 / 同帧重投）会重复扣减、误清兄弟在途单冻结量。按 order_id 去重——同单第二次进入直接 return，保证每单只
        回扣一次。order_id 为 None（旧装配/无单号兜底）时不去重、保守回扣一次（退化为旧行为，仍优于不回扣）。
        边界：
        - released_qty<=0 / 无该单元 / order_id 已回扣 → no-op；on_road 钳到 0 不为负；
        - 回扣后若 on_road 归零且单元仍卡 SELLING → 复位可卖态供重挂（PART_SOLD 单元保持 PART_SOLD 不误复位）。
        """
        if released_qty is None or released_qty <= 0:
            return
        with self._lock:
            unit = self._units.get((account_id, ts_code))
            if unit is None:
                return
            # 幂等去重：同一卖单只回扣一次，防双面回报/重投重复扣减误清兄弟在途单（review）。
            if order_id is not None:
                if order_id in unit.released_sell_order_ids:
                    return
                unit.released_sell_order_ids.add(order_id)
            before = getattr(unit, "on_road_sell_volume", 0)
            unit.on_road_sell_volume = max(0, before - int(released_qty))
            # 兜底：回扣后无在途且仍卡 SELLING → 复位（部成单一般为 PART_SOLD，不会进此分支；防御冗余）。
            if unit.on_road_sell_volume == 0 and unit.state == PositionState.SELLING:
                unit.state = PositionState.HOLDING if unit.volume > 0 else PositionState.SOLD
            self._logger.info(
                "position_on_road_released",
                account_id=account_id, ts_code=ts_code, released=int(released_qty),
                before=before, after=unit.on_road_sell_volume, state=str(unit.state), reason=reason,
            )

    def reconcile_stuck_selling(
        self, today: date, broker_order_state_query: Callable[[str, str], Optional[str]]
    ) -> int:
        """盘前对账跨日卡死的 SELLING 单元（评审三轮 EXEC-position-04）。

        业务意图：隔夜/断线重启后，昨日置 SELLING 的单元若其卖单实际已被券商撤单/废单/全撤零成交，而复位
        回调（sell_revert_sink）在断线期间丢失，则该单元跨日仍卡 SELLING——refresh_state 不推进 SELLING、
        _evaluate_and_sell_unit 又对 SELLING 一律 return False → 永远不再挂新卖单 = 跨日漏卖卡死（止损/破位失效）。
        这里盘前主动 query 券商委托终态：
        - 'CANCELLED'/'REJECTED'/'EXPIRED'/None-zero-fill 等【确认零成交终态】→ revert_selling 复位 HOLDING 重挂；
        - 'FILLED'/'PARTIAL' → 不在此复位（交由卖出回报补采推进，绝不回退已成交事实）；
        - 'UNKNOWN'/查不到 → 保守不动 + 强告警（无可信终态不擅自复位）。
        broker_order_state_query(account_id, ts_code) 返回该票当前卖单终态字符串或 None。返回处理单元数。
        """
        reverted = 0
        with self._lock:
            for unit in list(self._units.values()):
                if unit.state != PositionState.SELLING:
                    continue
                try:
                    terminal = broker_order_state_query(unit.account_id, unit.ts_code)
                except Exception as exc:  # noqa: BLE001 查询失败保守不动 + 告警
                    self._logger.warn(
                        "position_stuck_selling_query_failed",
                        account_id=unit.account_id, ts_code=unit.ts_code, error=str(exc),
                    )
                    continue
                t = (terminal or "").upper()
                if t in ("CANCELLED", "REJECTED", "EXPIRED", "CANCELED", "ERROR"):
                    self.revert_selling(unit, reason="stuck_selling_terminal_cancelled")
                    reverted += 1
                elif t in ("FILLED", "TRADED", "PARTIAL", "PART_FILLED", "PART_TRADED",
                           "ACTIVE", "REPORTED"):
                    # 已成交/部成 → 交回报补采推进；仍在途(ACTIVE/REPORTED) → 卖单确为活跃，单元应保持 SELLING。
                    # 二者均不复位、不告警（非「卡死」）。
                    continue
                else:
                    self._logger.warn(
                        "position_stuck_selling_unknown",
                        account_id=unit.account_id, ts_code=unit.ts_code, terminal=str(terminal),
                    )
        if reverted:
            self._logger.info("position_stuck_selling_reverted", count=reverted, today=str(today))
        return reverted

    # ------------------------------------------------------------------
    # 以 QMT 持仓快照校准可卖量（§5.2.2 can_use_volume 量闸为权威）
    # ------------------------------------------------------------------
    def apply_position_snapshot(
        self, account_id: str, ts_code: str, volume: int, can_use_volume: int
    ) -> Optional[PositionUnit]:
        """用 QMT query_stock_positions 的权威值校准本地单元的 volume / can_use_volume。

        业务意图：守 T+1 的量闸（can_use_volume）应以 QMT 为权威，避免本地按买入批次推算误差导致
        误卖 / 漏卖（评审 medium#1 建议）。本地 FIFO 单元负责状态机与成本，可卖量以快照为准。
        边界：本地无该单元时不凭空建仓（快照里的非本系统持仓由对账标记，不在此建单元），返回 None。
        """
        with self._lock:
            unit = self._units.get((account_id, ts_code))
            if unit is None:
                return None
            unit.volume = int(volume)
            # can_use_volume 直接采用 QMT 权威值（T+1 不含当日买入由 QMT 计算）。
            unit.can_use_volume = int(can_use_volume)
            # 标记量权威（评审三轮 EXEC-position-07）：校准后 volume 为券商权威，迟到/重复买入回调只登记去重
            # 不再二次累加，避免在权威量上多报持仓。
            unit.volume_authoritative = True
            self._logger.info(
                "position_snapshot_applied",
                account_id=account_id,
                ts_code=ts_code,
                volume=unit.volume,
                can_use_volume=unit.can_use_volume,
            )
            return unit

    # ------------------------------------------------------------------
    # 以 QMT 权威持仓快照重建 / 校准全部单元（评审二轮 P0#6/#29/#30/#38）
    # ------------------------------------------------------------------
    def rebuild_from_broker_positions(
        self,
        records: Any,
        today: date,
        *,
        create_missing: bool = True,
    ) -> int:
        """用券商(QMT) query_stock_positions 的权威持仓重建 / 校准本地持仓单元。

        业务意图（堵三个相互关联的致命断裂）：
        - P0#6：持仓状态机纯内存，进程重启后清空 → 隔夜 T+1 持仓永不进卖出决策（裸奔扛单）。
          单进程系统设计为"每日盘前主动重连 + 异常自动重启"，重启是常态，故启动必须能从券商权威重建。
        - P1#30：断线期间发生的买入成交只经 query_* 补采落库、不回写持仓状态机 → 同样漏卖。
          重连补采后调用本方法，券商已反映该持仓即被重建为可卖单元。
        - P1#29/P2#38：apply_position_snapshot 是死代码、从未被调用 → T+1 可卖量纯本地推算。
          本方法对已有单元用 QMT 权威值校准 volume/can_use_volume。

        入参 records：可迭代，每项需可取 ts_code / volume / can_use_volume（PositionRecord 或等价对象），
        avg_price/avg_cost 可选（缺则按 0 记成本，仅影响展示，不影响 T+1 量闸/可卖判定）。

        重建口径（守 T+1，以 QMT 可卖量为权威）：
        - 已有单元：校准 volume/can_use_volume；若本地为终态 SOLD 而券商仍有量(volume>0) → 视为遗漏，
          重建为可卖单元（断线期间建仓 / 重启后券商有量但本地已关闭）。
        - 本地无单元且 create_missing：
            can_use_volume>0      → HOLDING，earliest_sellable_date=today（隔夜已过 T+1，当日即可卖）；
            can_use_volume==0     → LOCKED_T1，earliest_sellable_date=next_open(today)（当日新建仓/全冻结，守 T+1）。
          buy_date：隔夜可卖 → prev_open(today)（已无真实买入日，取上一交易日作保守标签）；当日锁定 → today。
        - 券商无量(volume<=0)：跳过（不建空单元）。
        返回重建 / 校准的单元数（供日志 / 断言）。
        """
        touched = 0
        # 整段重建/校准纳入锁（评审三轮 EXEC-position-01）：断线重连在回调线程批量重建/校准单元字段，须与
        # 调度线程的 refresh_state/卖出巡检对同一 unit 字段读改互斥。
        with self._lock:
          for rec in records or []:
            ts_code = getattr(rec, "ts_code", None)
            if ts_code is None:
                continue
            vol_raw = getattr(rec, "volume", None)
            volume = int(vol_raw) if vol_raw is not None else 0
            cu_raw = getattr(rec, "can_use_volume", None)
            can_use = int(cu_raw) if cu_raw is not None else 0
            if volume <= 0:
                continue  # 券商无持仓量：不建空单元
            avg_raw = getattr(rec, "avg_price", None)
            if avg_raw is None:
                avg_raw = getattr(rec, "avg_cost", None)
            avg_cost = Decimal(str(avg_raw)) if avg_raw is not None else Decimal("0")

            # account_id 取记录自带（多账户隔离）；normalize_position 已填，缺失则该记录无法定位单元键。
            rec_account_id = getattr(rec, "account_id", None)
            if rec_account_id is None:
                continue
            key = (rec_account_id, ts_code)
            unit = self._units.get(key)

            if unit is not None and unit.state != PositionState.SOLD:
                # 已有活跃单元：以 QMT 权威校准 volume/can_use_volume（保留状态机/成本/buy_date）。
                unit.volume = volume
                unit.can_use_volume = can_use
                unit.volume_authoritative = True  # 评审三轮 EXEC-position-07：校准后量权威，迟到回调只去重不累加
                touched += 1
                continue

            if not create_missing and (unit is None or unit.state == PositionState.SOLD):
                continue

            # 口径边界（评审复审 P2）：以券商快照"量权威"重建的单元 counted_trade_ids 为空——若重建后又收到一笔
            # 已被该快照 volume 计入的【迟到买入成交回调】，mark_position_on_fill 去重集为空会在权威量上再加一次
            # （多报持仓）。属"快照量权威 vs 事件量权威"混用的固有边界，概率低（重建多在盘前/重连点、晚于当日成交
            # 回放窗口），且收盘对账的资产/持仓偏差会兜底告警。如需根治可在拿到券商成交序号时预填去重集。
            # 新建 / 重建（本地无单元，或本地终态 SOLD 但券商仍有量=遗漏）。
            if can_use > 0:
                state = PositionState.HOLDING
                earliest = today
                buy_date = self._calendar.prev_open(today)
            else:
                state = PositionState.LOCKED_T1
                earliest = self._calendar.next_open(today)
                buy_date = today
            # 已在外层 self._lock 临界区内（评审三轮 EXEC-position-01），结构性写入直接落定。
            self._units[key] = PositionUnit(
                account_id=key[0],
                ts_code=ts_code,
                volume=volume,
                can_use_volume=can_use,
                avg_cost=avg_cost,
                earliest_sellable_date=earliest,
                state=state,
                mode=PositionMode.TECH_EXIT,  # 重建单元默认纯技术退出，refresh_state 据先验再校正
                buy_date=buy_date,
                volume_authoritative=True,  # 评审三轮 EXEC-position-07：以券商快照量重建，迟到回调只去重不累加
            )
            touched += 1
            self._logger.info(
                "position_rebuilt_from_broker",
                account_id=key[0],
                ts_code=ts_code,
                volume=volume,
                can_use_volume=can_use,
                state=str(state),
                earliest_sellable_date=str(earliest),
            )
        if touched:
            self._logger.info("position_rebuild_done", count=touched, today=str(today))
        return touched
