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

        key = (account_id, ts_code)
        unit = self._units.get(key)

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

        # —— 最早可卖日：经交易日历取买入日 B 的下一交易日（禁自然日 +1）——
        earliest_sellable_date = self._calendar.next_open(today)

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
        for unit in self._units.values():
            # 终态/冻结态不推进：SOLD 已关闭、FROZEN 由风控恢复链路处理。
            if unit.state in (PositionState.SOLD, PositionState.FROZEN):
                continue

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
        result: List[PositionUnit] = []
        for unit in self._units.values():
            if today >= unit.earliest_sellable_date and unit.state in sellable_states:
                result.append(unit)
        return result

    # ------------------------------------------------------------------
    # 卖出委托发出 / 卖出成交回写（§5.2.1 SELLING / SOLD / PART_SOLD）
    # ------------------------------------------------------------------
    def mark_selling(self, unit: PositionUnit) -> None:
        """标记单元进入 SELLING（已发卖出委托、等待回报）。

        业务意图：卖单幂等的状态闸——同一单元在 SELLING 态不应重复发同向卖单（§5.8）。
        边界：只允许从可卖态（HOLDING / PART_SOLD）进入 SELLING；其余态（LOCKED_T1 / SOLD /
        FROZEN）拒绝，避免越权改写（守 T+1 / 终态 / 冻结）。
        """
        if unit.state not in (PositionState.HOLDING, PositionState.PART_SOLD):
            raise ValueError(
                f"mark_selling: 单元 {unit.account_id}/{unit.ts_code} 当前状态 {unit.state} 不可发卖单"
            )
        unit.state = PositionState.SELLING
        self._logger.info(
            "position_marked_selling",
            account_id=unit.account_id,
            ts_code=unit.ts_code,
            volume=unit.volume,
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
        # 钳到剩余持仓量，绝不超卖（防负仓）。
        applied = min(sell_vol, unit.volume)
        unit.volume -= applied
        # 可用量同步扣减（不低于 0）。
        unit.can_use_volume = max(0, unit.can_use_volume - applied)

        if unit.volume <= 0:
            # 全部卖出：仓位归零，单元关闭（SOLD 为终态）。
            unit.volume = 0
            unit.can_use_volume = 0
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
        """按 (account_id, ts_code) 取持仓单元，无则返回 None。"""
        return self._units.get((account_id, ts_code))

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
        unit = self._units.get((account_id, ts_code))
        if unit is None:
            return None
        unit.volume = int(volume)
        # can_use_volume 直接采用 QMT 权威值（T+1 不含当日买入由 QMT 计算）。
        unit.can_use_volume = int(can_use_volume)
        self._logger.info(
            "position_snapshot_applied",
            account_id=account_id,
            ts_code=ts_code,
            volume=unit.volume,
            can_use_volume=unit.can_use_volume,
        )
        return unit
