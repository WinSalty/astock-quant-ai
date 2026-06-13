"""收盘后对账 reconcile（委托 / 成交 / 资产 / 滑点四类勾稽，§6.7 / §6.8 / §6.9）。

业务意图：以本地下单台账（local_order_ledger）为事实源，与 xttrader 回报（qmt_order / qmt_trade）
双向勾稽，定位「漏单 / 下单失败 / 手工单 / 成交未勾稽 / 资产偏差 / 滑点」，把偏差落 logger
并汇总为 ReconcileReport，供复盘与券商对账单兜底。本模块**只读**台账与四表，不回写任何数据。

四类勾稽口径（§6.7）：
- 委托对账：台账每条计划单应在 qmt_order 找到对应回报（先按 order_id，缺则按 order_remark 的 biz 关联）。
  台账有、回报无 → 漏单 / 下单失败（查台账自身是否已是 ERROR 态，或回报里是否有 ERROR 行）；
  回报有、台账无 → 非本系统下单（手工单）单独标记。
- 成交对账：qmt_order.traded_volume 应等于该委托名下 qmt_trade 成交量之和；不一致 → needs_backfill
  （应触发 query_stock_trades 补采 QUERY_BACKFILL）。
- 资产对账：Σ 成交净额（买为现金流出、卖为现金流入，粗略取 traded_amount）与账户资产变动方向粗校验；
  偏差超阈值告警；无账户数据则跳过并在报告说明。
- 滑点对账：台账 plan_price 对比 qmt_trade.traded_price，落滑点指标（差值 / 比例）。

时间口径：本模块不产生入库时刻；所有日期均为东八区自然交易日（trade_date / signal_trade_date），
经 calendar 推算，禁手工 ±1 自然日（§6.6 / §6.8）。价位一律 Decimal（§6.6 禁 float 比较）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..common import identity
from ..common.order_remark import REMARK_PREFIX as _REMARK_PREFIX
from ..common.order_remark import parse_order_remark as _parse_remark_common
from ..contracts.enums import OrderStatus, TradeSide
from ..contracts.models import LedgerEntry, OrderRecord, TradeRecord
from ..contracts.protocols import LocalLedger, QmtRepository, StructLogger, TradeCalendar
# 资产对账方向偏差阈值（粗校验，§6.7）：成交净额与账户资产变动方向若不一致且金额超此绝对阈值才告警，
# 避免费用 / 浮盈浮亏等噪声触发误报。单位为金额（元）。
_ASSET_DEVIATION_THRESHOLD = Decimal("1000")


# ---------------------------------------------------------------------------
# 偏差与报告数据结构（§6.7 汇总）
# ---------------------------------------------------------------------------


@dataclass
class OrderDiscrepancy:
    """单条委托勾稽偏差（台账侧视角 / 回报侧视角各一类）。"""

    kind: str                       # missing_report / order_failed / manual_order
    biz_order_no: Optional[str] = None
    order_id: Optional[int] = None
    ts_code: Optional[str] = None
    detail: str = ""


@dataclass
class TradeDiscrepancy:
    """单条成交量勾稽偏差（qmt_order.traded_volume vs Σ qmt_trade 成交量）。"""

    order_id: int
    ts_code: Optional[str]
    order_traded_volume: int
    trade_volume_sum: int
    needs_backfill: bool = True     # 不一致即应触发 query_stock_trades 补采
    detail: str = ""


@dataclass
class AssetDiscrepancy:
    """资产对账偏差（粗校验，方向 / 阈值）。"""

    expected_net_flow: Decimal      # Σ 成交净额（买流出取负、卖流入取正）
    account_asset_change: Optional[Decimal]  # 账户资产变动（无数据为 None）
    detail: str = ""


@dataclass
class SlippageItem:
    """单条滑点指标（台账计划价 vs 实际成交价）。"""

    biz_order_no: Optional[str]
    order_id: Optional[int]
    ts_code: Optional[str]
    side: Optional[TradeSide]
    plan_price: Optional[Decimal]
    avg_traded_price: Optional[Decimal]
    slippage: Optional[Decimal] = None        # 实际 - 计划（带方向）
    slippage_ratio: Optional[Decimal] = None  # 滑点 / 计划价


@dataclass
class ReconcileReport:
    """四类勾稽结果汇总（§6.7）。供调用方判定是否需补采 / 告警 / 归档。"""

    account_id: str
    trade_date: date
    # 委托对账
    matched_orders: int = 0
    order_discrepancies: List[OrderDiscrepancy] = field(default_factory=list)
    # 成交对账
    trade_discrepancies: List[TradeDiscrepancy] = field(default_factory=list)
    # 资产对账
    asset_discrepancy: Optional[AssetDiscrepancy] = None
    asset_checked: bool = False     # 是否实际执行了资产校验（无账户数据则 False）
    # 滑点对账
    slippage_items: List[SlippageItem] = field(default_factory=list)
    # 其它说明（如资产数据缺失原因）
    notes: List[str] = field(default_factory=list)

    @property
    def needs_backfill(self) -> bool:
        """是否存在需补采的成交不勾稽项（任一 TradeDiscrepancy.needs_backfill）。"""
        return any(d.needs_backfill for d in self.trade_discrepancies)

    @property
    def has_discrepancy(self) -> bool:
        """是否存在任意类别偏差（供调用方决定是否告警 / 人工介入）。"""
        return bool(
            self.order_discrepancies
            or self.trade_discrepancies
            or (self.asset_discrepancy is not None)
        )


# ---------------------------------------------------------------------------
# order_remark 解析与 signal_trade_date 回填（§6.8）
# ---------------------------------------------------------------------------


def parse_order_remark(remark: Optional[str]) -> Optional[date]:
    """从 order_remark 解析信号日 T（§4.4(5) / §6.8）。

    口径统一委托 common.order_remark.parse_order_remark（落库 normalize 与对账 reconcile 共用同一解析器，
    避免两处口径漂移）。保留本函数名供既有调用 / 测试引用。
    """
    return _parse_remark_common(remark)


def backfill_signal_trade_date(
    trade_date: date, remark: Optional[str], calendar: TradeCalendar
) -> date:
    """回填 signal_trade_date（= 信号日 T，§6.8）。

    口径：
    - remark 能解析出 T → 直接用 T（权威，无需反推）；
    - remark 缺失 / 非法 → 用 calendar.prev_open(trade_date) 反推 pretrade_date 作 T 兜底
      （trade_date = 买入日 T+1，其上一交易日即 T；禁自然日 -1，跨周末 / 节假日会错）。
    """
    parsed = parse_order_remark(remark)
    if parsed is not None:
        return parsed
    # 兜底：买入日 T+1 的上一交易日即信号日 T。
    return calendar.prev_open(trade_date)


def _resolve(code: Optional[str]) -> Optional[str]:
    """脏代码归一为标准 ts_code（SH600000 / 600000 / .BJ → 600000.SH 等，§6.8）。

    归一失败（取不到 6 位数字 / 无法判定交易所）返回 None，不臆造，便于排查脏数据。
    """
    return identity.resolve_code(code)


# ---------------------------------------------------------------------------
# Reconcile 主体
# ---------------------------------------------------------------------------


class Reconcile:
    """收盘后四类勾稽对账器（§6.7）。

    依赖（全部经 contracts.Protocol 注入，单测用 fake / 内存实现）：
    - ledger：本地下单台账事实源（只读 all_for_date / get_by_order_id）；
    - repository：qmt_* 四表只读（get_orders / get_trades）；
    - logger：偏差告警留痕（warn / error）；
    - calendar：signal_trade_date 反推兜底（prev_open）。

    account_id 为关键字参数，绑定单一账户的对账范围（多账户应实例化多个 Reconcile）。
    """

    def __init__(
        self,
        ledger: LocalLedger,
        repository: QmtRepository,
        logger: StructLogger,
        calendar: TradeCalendar,
        *,
        account_id: str,
    ):
        self._ledger = ledger
        self._repo = repository
        self._logger = logger
        self._calendar = calendar
        self._account_id = account_id

    # —— 关联键工具 ——
    @staticmethod
    def _ledger_biz_from_remark(remark: Optional[str]) -> Optional[str]:
        """从回报 order_remark 提取本地业务关联标识（这里取归一 ts_code 作为辅助关联依据）。

        说明：order_remark 透传的是 ``LUP|T|ts_code``，并不直接含 biz_order_no；
        台账与回报的强关联键是 order_id，order_remark 仅作 order_id 缺失时的弱关联（按 T + ts_code）。
        本方法返回归一后的 ts_code 供弱关联匹配。
        """
        if not remark:
            return None
        parts = str(remark).split("|")
        if len(parts) >= 3 and parts[0] == _REMARK_PREFIX:
            return _resolve(parts[2])
        return None

    def run(self, trade_date: date) -> ReconcileReport:
        """执行四类勾稽，产出 ReconcileReport。

        步骤：
        1) 取台账 all_for_date(trade_date) 与回报 get_orders / get_trades；
        2) 委托对账（台账 vs 回报双向）；
        3) 成交对账（qmt_order.traded_volume vs Σ qmt_trade）；
        4) 资产对账（Σ 成交净额方向 vs 账户资产变动，粗校验）；
        5) 滑点对账（台账 plan_price vs 实际成交均价）；
        偏差全部经 logger.warn/error 留痕，并汇总进 report。
        重跑口径：本方法纯读、无副作用，可对同一 trade_date 任意重跑得到一致结果。
        """
        report = ReconcileReport(account_id=self._account_id, trade_date=trade_date)

        ledger_entries: List[LedgerEntry] = list(self._ledger.all_for_date(trade_date))
        orders: List[OrderRecord] = list(self._repo.get_orders(self._account_id, trade_date))
        trades: List[TradeRecord] = list(self._repo.get_trades(self._account_id, trade_date))

        # 回报委托按 order_id 建索引，供台账→回报关联与「回报有台账无」反向勾稽。
        orders_by_id: Dict[int, OrderRecord] = {o.order_id: o for o in orders if o.order_id is not None}

        self._reconcile_orders(ledger_entries, orders, orders_by_id, report)
        self._reconcile_trades(orders, trades, report)
        self._reconcile_assets(trades, trade_date, report)
        self._reconcile_slippage(ledger_entries, orders_by_id, trades, report)

        return report

    # ---------------------------------------------------------------
    # 委托对账（§6.7 第一类）
    # ---------------------------------------------------------------
    def _reconcile_orders(
        self,
        ledger_entries: List[LedgerEntry],
        orders: List[OrderRecord],
        orders_by_id: Dict[int, OrderRecord],
        report: ReconcileReport,
    ) -> None:
        """台账每条计划单应在 qmt_order 找到对应回报（先 order_id，缺则 order_remark 弱关联）。

        - 台账有、回报无 → 漏单 / 下单失败：先看台账自身是否已 ERROR 态（下单失败已留痕），
          否则按漏单告警（应触发补采 / 人工核查）。
        - 回报有、台账无 → 手工单（非本系统下单）单独标记。
        """
        matched_order_ids: set = set()

        # 台账→回报：逐条计划单找对应回报。
        for e in ledger_entries:
            matched = self._match_ledger_to_order(e, orders_by_id, orders)
            if matched is not None:
                matched_order_ids.add(matched.order_id)
                report.matched_orders += 1
                continue
            # 未匹配到回报：区分「下单失败（台账已 ERROR）」与「漏单」。
            if self._ledger_state_is_error(e):
                disc = OrderDiscrepancy(
                    kind="order_failed",
                    biz_order_no=e.biz_order_no,
                    order_id=e.order_id,
                    ts_code=e.ts_code,
                    detail="台账状态 ERROR，下单失败已留痕，无 qmt_order 回报属预期",
                )
                report.order_discrepancies.append(disc)
                # 下单失败属已知留痕，按 warn 级别（非 error）记录，便于区分严重程度。
                self._logger.warn(
                    "reconcile_order_failed",
                    biz_order_no=e.biz_order_no,
                    ts_code=e.ts_code,
                    order_id=e.order_id,
                )
            else:
                disc = OrderDiscrepancy(
                    kind="missing_report",
                    biz_order_no=e.biz_order_no,
                    order_id=e.order_id,
                    ts_code=e.ts_code,
                    detail="台账有计划单但 qmt_order 无回报：疑似漏单，需补采 / 人工核查",
                )
                report.order_discrepancies.append(disc)
                # 漏单是计划单凭空消失（§6.10 验收硬指标），按 error 级别告警。
                self._logger.error(
                    "reconcile_missing_report",
                    biz_order_no=e.biz_order_no,
                    ts_code=e.ts_code,
                    order_id=e.order_id,
                )

        # 回报→台账：回报里出现但台账无对应 → 手工单。
        # ERROR 行是 on_order_error 补落的「下单失败骨架」，通常无独立 order_id 或本就由本系统产生，
        # 不应误判为手工单——这里仅对「能在台账按 order_id 命中」之外、且非 ERROR 的回报标手工单。
        for o in orders:
            if o.order_id is not None and o.order_id in matched_order_ids:
                continue
            if o.order_status == OrderStatus.ERROR:
                # 下单失败骨架行：已在台账侧（若有）按 order_failed 处理，这里不重复标手工单。
                continue
            # 二次确认台账确实无该 order_id（防 matched_order_ids 漏统计）。
            if o.order_id is not None and self._ledger.get_by_order_id(o.order_id) is not None:
                continue
            disc = OrderDiscrepancy(
                kind="manual_order",
                biz_order_no=None,
                order_id=o.order_id,
                ts_code=o.ts_code,
                detail="qmt_order 有回报但本地台账无对应：非本系统下单（手工单），单独标记",
            )
            report.order_discrepancies.append(disc)
            self._logger.warn(
                "reconcile_manual_order",
                order_id=o.order_id,
                ts_code=o.ts_code,
                order_status=str(o.order_status),
            )

    def _match_ledger_to_order(
        self,
        e: LedgerEntry,
        orders_by_id: Dict[int, OrderRecord],
        orders: List[OrderRecord],
    ) -> Optional[OrderRecord]:
        """台账行关联回报：优先 order_id 强关联，缺失时按 order_remark(归一 ts_code + signal T) 弱关联。"""
        # 强关联：台账已回填 order_id 且回报存在该 order_id。
        if e.order_id is not None and e.order_id in orders_by_id:
            return orders_by_id[e.order_id]
        # 弱关联：order_id 缺失时按 order_remark 解析的 ts_code + signal T 匹配（仅非 ERROR 回报）。
        e_code = _resolve(e.ts_code)
        for o in orders:
            if o.order_status == OrderStatus.ERROR:
                continue
            remark_code = self._ledger_biz_from_remark(o.order_remark)
            remark_t = parse_order_remark(o.order_remark)
            if (
                remark_code is not None
                and remark_code == e_code
                and remark_t is not None
                and remark_t == e.signal_trade_date
            ):
                return o
        return None

    @staticmethod
    def _ledger_state_is_error(e: LedgerEntry) -> bool:
        """台账行是否处于下单失败态（OrderState.ERROR）。"""
        return str(e.state) == "ERROR"

    # ---------------------------------------------------------------
    # 成交对账（§6.7 第二类）
    # ---------------------------------------------------------------
    def _reconcile_trades(
        self,
        orders: List[OrderRecord],
        trades: List[TradeRecord],
        report: ReconcileReport,
    ) -> None:
        """qmt_order.traded_volume 应等于该委托名下 qmt_trade 成交量之和；不一致 → needs_backfill。

        业务意图：成交回报漏采会导致委托侧成交量大于明细侧之和（明细没采全），
        触发 query_stock_trades 补采（QUERY_BACKFILL）。这里只读检测并标记，不实际补采。
        边界：ERROR 委托行无成交、不参与勾稽。
        """
        # 按 order_id 汇总 qmt_trade 成交量。
        trade_vol_by_order: Dict[int, int] = {}
        for t in trades:
            if t.order_id is None:
                continue
            trade_vol_by_order[t.order_id] = trade_vol_by_order.get(t.order_id, 0) + int(t.traded_volume)

        for o in orders:
            if o.order_id is None or o.order_status == OrderStatus.ERROR:
                continue
            order_vol = int(o.traded_volume or 0)
            sum_vol = trade_vol_by_order.get(o.order_id, 0)
            if order_vol != sum_vol:
                disc = TradeDiscrepancy(
                    order_id=o.order_id,
                    ts_code=o.ts_code,
                    order_traded_volume=order_vol,
                    trade_volume_sum=sum_vol,
                    needs_backfill=True,
                    detail="qmt_order.traded_volume 与 Σqmt_trade 成交量不一致：有成交回报漏采，需 query_stock_trades 补采",
                )
                report.trade_discrepancies.append(disc)
                self._logger.error(
                    "reconcile_trade_volume_mismatch",
                    order_id=o.order_id,
                    ts_code=o.ts_code,
                    order_traded_volume=order_vol,
                    trade_volume_sum=sum_vol,
                )

    # ---------------------------------------------------------------
    # 资产对账（§6.7 第三类，粗校验）
    # ---------------------------------------------------------------
    def _reconcile_assets(
        self,
        trades: List[TradeRecord],
        trade_date: date,
        report: ReconcileReport,
    ) -> None:
        """Σ 成交净额（买流出取负、卖流入取正）与账户资产变动方向粗校验，偏差超阈值告警。

        净额口径（粗略）：买入消耗现金（负流），卖出回笼现金（正流），用 traded_amount 累加；
        traded_amount 缺失时退化为 traded_price * traded_volume。
        账户资产变动：从 qmt_account_daily 取 CLOSE 快照的 daily_pnl / (total_asset - prev_total_asset)；
        仓储未提供账户只读接口（QmtRepository 仅暴露 get_orders/get_trades）→ 跳过并在 notes 说明。
        """
        # 计算成交净现金流（买为负、卖为正）。
        net_flow = Decimal("0")
        for t in trades:
            amt = t.traded_amount
            if amt is None:
                # 缺 traded_amount 时用价 * 量兜底（Decimal 口径）。
                amt = (t.traded_price or Decimal("0")) * Decimal(int(t.traded_volume or 0))
            if t.trade_side == TradeSide.BUY:
                net_flow -= amt        # 买入：现金流出
            elif t.trade_side == TradeSide.SELL:
                net_flow += amt        # 卖出：现金流入

        account_change = self._fetch_account_change(trade_date)
        if account_change is None:
            # 无账户数据：跳过资产对账，仅在报告说明，不视为偏差（§6.7「无账户数据时跳过并说明」）。
            report.asset_checked = False
            report.notes.append("资产对账跳过：无可用现金变动数据（当日或上一交易日缺 CLOSE 账户快照 / cash 字段）")
            self._logger.info(
                "reconcile_asset_skipped",
                trade_date=str(trade_date),
                reason="no_account_data",
                expected_net_flow=str(net_flow),
            )
            return

        report.asset_checked = True
        # 方向一致性粗校验：成交净现金流方向应与账户现金侧变动方向一致（这里用资产变动近似）。
        # 偏差 = |净额 - 账户变动|，超阈值 + 方向相反才告警，避免费用 / 浮盈浮亏噪声误报。
        deviation = abs(net_flow - account_change)
        direction_conflict = (net_flow > 0 and account_change < 0) or (net_flow < 0 and account_change > 0)
        if deviation > _ASSET_DEVIATION_THRESHOLD and direction_conflict:
            disc = AssetDiscrepancy(
                expected_net_flow=net_flow,
                account_asset_change=account_change,
                detail="成交净额与账户资产变动方向不一致且偏差超阈值：疑似漏采成交 / 资金流水异常",
            )
            report.asset_discrepancy = disc
            self._logger.error(
                "reconcile_asset_deviation",
                trade_date=str(trade_date),
                expected_net_flow=str(net_flow),
                account_asset_change=str(account_change),
                deviation=str(deviation),
            )

    def _fetch_account_change(self, trade_date: date) -> Optional[Decimal]:
        """取账户当日现金变动（资产对账粗校验用，§6.7 第三类 / 评审 medium#8）。

        口径：取当日 CLOSE 账户快照与上一交易日 CLOSE 快照的【可用现金 cash】差额，作为「账户现金变动」，
        与成交净现金流（买流出、卖流入）做方向粗校验——买入消耗现金、卖出回笼现金，二者方向应一致。
        用 cash 差而非 total_asset 差：买入是 cash→市值的内部腾挪，total_asset 近似不变，方向校验无意义。
        数据来源：repository.get_account_daily（QmtRepository 协议已补该只读接口）。
        边界：
        - 仓储未实现该接口（鸭子兜底 get_account_change）则退化用之；
        - 当日 / 上一交易日 CLOSE 快照缺失，或 cash 字段缺失 → 返回 None（跳过资产对账，report 注明）。
        """
        # 优先用协议化的账户只读接口；失败/缺失则尝试鸭子兜底 get_account_change。
        getter = getattr(self._repo, "get_account_daily", None)
        if callable(getter):
            try:
                from ..contracts.enums import SnapshotType

                today_rec = getter(self._account_id, trade_date, SnapshotType.CLOSE)
                if today_rec is None or today_rec.cash is None:
                    return self._duck_account_change(trade_date)
                prev_date = self._calendar.prev_open(trade_date)
                prev_rec = getter(self._account_id, prev_date, SnapshotType.CLOSE)
                if prev_rec is None or prev_rec.cash is None:
                    # 无上一交易日基线（如首日）：无法算现金变动，跳过资产对账（非偏差）。
                    return self._duck_account_change(trade_date)
                return today_rec.cash - prev_rec.cash
            except NotImplementedError:
                # MySQL 仓储只读查询尚未落地：退化用鸭子兜底，仍无则跳过。
                return self._duck_account_change(trade_date)
            except Exception:  # noqa: BLE001 - 资产对账失败不应拖垮整体对账，降级跳过
                return None
        return self._duck_account_change(trade_date)

    def _duck_account_change(self, trade_date: date) -> Optional[Decimal]:
        """鸭子兜底：若注入的 repository 额外提供 get_account_change(account_id, trade_date) 则用之。"""
        getter = getattr(self._repo, "get_account_change", None)
        if callable(getter):
            try:
                return getter(self._account_id, trade_date)
            except Exception:  # noqa: BLE001
                return None
        return None

    # ---------------------------------------------------------------
    # 滑点对账（§6.7 第四类）
    # ---------------------------------------------------------------
    def _reconcile_slippage(
        self,
        ledger_entries: List[LedgerEntry],
        orders_by_id: Dict[int, OrderRecord],
        trades: List[TradeRecord],
        report: ReconcileReport,
    ) -> None:
        """台账 plan_price 对比实际成交均价（Σ traded_amount / Σ traded_volume），落滑点指标。

        滑点（带方向）= 实际成交均价 - 计划价；滑点比例 = 滑点 / 计划价。
        边界：无成交（filled_volume 为 0 / 无对应 trade）或无计划价 → 滑点指标为 None，仍落一条留痕。
        """
        # 按 order_id 汇总成交额与成交量，算成交均价。
        amt_by_order: Dict[int, Decimal] = {}
        vol_by_order: Dict[int, int] = {}
        for t in trades:
            if t.order_id is None:
                continue
            vol = int(t.traded_volume or 0)
            amt = t.traded_amount
            if amt is None:
                amt = (t.traded_price or Decimal("0")) * Decimal(vol)
            amt_by_order[t.order_id] = amt_by_order.get(t.order_id, Decimal("0")) + amt
            vol_by_order[t.order_id] = vol_by_order.get(t.order_id, 0) + vol

        for e in ledger_entries:
            avg_price: Optional[Decimal] = None
            if e.order_id is not None:
                v = vol_by_order.get(e.order_id, 0)
                if v > 0:
                    avg_price = amt_by_order[e.order_id] / Decimal(v)

            slippage: Optional[Decimal] = None
            slippage_ratio: Optional[Decimal] = None
            if e.plan_price is not None and avg_price is not None and e.plan_price != 0:
                slippage = avg_price - e.plan_price
                slippage_ratio = slippage / e.plan_price

            item = SlippageItem(
                biz_order_no=e.biz_order_no,
                order_id=e.order_id,
                ts_code=_resolve(e.ts_code),
                side=e.side,
                plan_price=e.plan_price,
                avg_traded_price=avg_price,
                slippage=slippage,
                slippage_ratio=slippage_ratio,
            )
            report.slippage_items.append(item)
