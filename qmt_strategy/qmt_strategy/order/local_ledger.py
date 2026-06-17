"""本地下单台账（§4.4 / §4.8 / §6.7 对账事实源之一）。

业务意图：每次 order_stock 计划单落盘，承载业务级幂等（biz_order_no 去重）与对账事实源
（台账 vs xttrader 回报）。与 qmt_order 的 DB 级唯一键互补：DB 级防「同一委托重复落表」，
biz_order_no 防「业务侧同一计划重复下单」（§4.4(2)），两层都要有。

InMemoryLocalLedger 实现 contracts.LocalLedger 协议，单测 / 进程内常驻使用。

索引口径（评审三轮 EXEC-order-06 / storage-05）：
- 回报反查索引 _order_index 为【两级映射】 order_id → {target_trade_date → biz_order_no}：QMT order_id
  按交易日重置可【跨日复用】，单值映射在 load_from_db 全表读回时会被后到行覆盖、把当日回报串到历史单。
  两级映射下，按 order_id 反查取【最近交易日】那条（当日回报恒落当日单），绝不串改历史。
- 活跃单反查索引 _active_index 为 (target_trade_date, ts_code, strategy_family) → {biz}：find_active 据键定位
  候选再逐一【按 _by_biz 权威态复核】，避免全表线性扫。索引【只增不删】（终态行残留无害——find_active 复核
  state 会跳过非活跃项），保证它恒为活跃集的【超集】：绝不漏判活跃单（漏判会导致同一计划重复下单，最危险）。
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from ..contracts.enums import OrderState
from ..contracts.models import LedgerEntry


class InMemoryLocalLedger:
    """进程内本地下单台账。实现 contracts.LocalLedger 协议。

    幂等键：(target_trade_date, ts_code, strategy_family) → 是否存在「活跃单」。
    回报关联键：order_id（下单后由 xttrader 返回回填），两级映射防跨日复用串单。
    """

    def __init__(self, logger=None):
        self._by_biz: Dict[str, LedgerEntry] = {}
        # 回报反查索引（两级，评审三轮 EXEC-order-06）：order_id → {target_trade_date → biz_order_no}。
        self._order_index: Dict[int, Dict[date, str]] = {}
        # 活跃单反查索引（评审三轮 EXEC-storage-05）：键 (target_trade_date, ts_code, strategy_family) → {biz}。
        # 只增不删的超集：find_active 据键取候选再按 _by_biz 复核 state，残留终态行无害、绝不漏判活跃单。
        self._active_index: Dict[Tuple[date, str, str], Set[str]] = {}
        # 结构化日志（可选）：order_id 跨日冲突等告警；单测可不注入。
        self._logger = logger

    # ------------------------------------------------------------------
    # 索引维护内部方法
    # ------------------------------------------------------------------
    def _index_order_id(self, entry: LedgerEntry) -> None:
        """把 (order_id, target_trade_date) → biz 写入两级反查索引，并对同日同 order_id 不同 biz 告警。"""
        if entry.order_id is None:
            return
        day_map = self._order_index.setdefault(entry.order_id, {})
        prev_biz = day_map.get(entry.target_trade_date)
        # 跨日复用是【预期】（不同 target_trade_date 各占一格，不冲突）；只有【同日同 order_id 不同 biz】才是异常。
        if prev_biz is not None and prev_biz != entry.biz_order_no and self._logger is not None:
            self._logger.warn(
                "ledger_order_id_collision",
                order_id=entry.order_id,
                trade_date=str(entry.target_trade_date),
                prev_biz=prev_biz,
                new_biz=entry.biz_order_no,
            )
        day_map[entry.target_trade_date] = entry.biz_order_no

    def _index_active(self, entry: LedgerEntry) -> None:
        """把 biz 加入活跃单候选索引（只增不删的超集，键 (date,ts,family)）。"""
        key = (entry.target_trade_date, entry.ts_code, entry.strategy_family)
        self._active_index.setdefault(key, set()).add(entry.biz_order_no)

    def _resolve_biz(self, order_id: int) -> Optional[str]:
        """按 order_id 反查 biz：跨日复用时取【最近交易日】那条（当日回报恒落当日单，绝不串历史）。"""
        day_map = self._order_index.get(order_id)
        if not day_map:
            return None
        latest_day = max(day_map.keys())
        return day_map[latest_day]

    def has_active(self, target_trade_date: date, ts_code: str, strategy_family: str) -> bool:
        """同 (target_trade_date, ts_code, strategy_family) 是否已有未终结/已成单（§4.4(2)）。"""
        return self.find_active(target_trade_date, ts_code, strategy_family) is not None

    def find_active(
        self, target_trade_date: date, ts_code: str, strategy_family: str
    ) -> Optional[LedgerEntry]:
        """据活跃单索引按键取候选，再按 _by_biz 权威态逐一复核 state，返回首个活跃项（深拷贝）。

        索引是超集（只增不删）：候选里可能混入已终态的 biz，故必须复核 e.state in active；复核以 _by_biz
        为唯一权威，从根本上杜绝「索引漏判活跃单 → 同一计划重复下单」。
        """
        active = OrderState.active()
        key = (target_trade_date, ts_code, strategy_family)
        for biz in self._active_index.get(key, ()):  # 候选集为空则不进循环
            e = self._by_biz.get(biz)
            if (
                e is not None
                and e.target_trade_date == target_trade_date
                and e.ts_code == ts_code
                and e.strategy_family == strategy_family
                and e.state in active
            ):
                return copy.deepcopy(e)
        return None

    def insert(self, entry: LedgerEntry) -> None:
        """写入新计划单。重复 biz_order_no 直接覆盖（幂等：同号视为同一计划）。同步维护两套反查索引。"""
        self._by_biz[entry.biz_order_no] = copy.deepcopy(entry)
        self._index_order_id(entry)
        self._index_active(entry)

    def get(self, biz_order_no: str) -> Optional[LedgerEntry]:
        e = self._by_biz.get(biz_order_no)
        return copy.deepcopy(e) if e else None

    def get_by_order_id(self, order_id: int) -> Optional[LedgerEntry]:
        biz = self._resolve_biz(order_id)
        if biz is None:
            return None
        return self.get(biz)

    def update(self, biz_order_no: str, **fields) -> None:
        """按字段更新台账行。order_id 变化时同步维护两级反查索引（活跃索引键字段不可变，无需改）。"""
        e = self._by_biz.get(biz_order_no)
        if e is None:
            raise KeyError(f"ledger 无 biz_order_no={biz_order_no}")
        for k, v in fields.items():
            if not hasattr(e, k):
                raise AttributeError(f"LedgerEntry 无字段 {k}")
            setattr(e, k, v)
        # order_id 可能由 None 变为下单返回值，或被修正：重建该 entry 的 order_id 反查格。
        self._index_order_id(e)

    def sync_status(self, order_id: int, state: OrderState, msg: Optional[str] = None) -> None:
        """按 order_id 同步委托状态（on_stock_order 驱动）。台账无该 order_id 则忽略（防越权改写）。

        部成收口（§4.7/§4.9 硬口径）：部成单撤单后 QMT 该委托终态报 CANCELLED，但若本地已有
        成交（filled_volume>0），终态应落 PART_TRADED 而非 CANCELLED——已成部分是真实建仓，
        未成部分计买不进。仅完全未成（filled_volume==0）才落 CANCELLED。对 REJECTED 同理收口。

        不回退已成交态守卫（评审 doc/19 H-1）：on_stock_trade（成交，驱动 add_fill 置 TRADED）与
        on_stock_order（委托态，驱动本方法）是 QMT 两路异步回调，可能乱序——成交回报先到把单置 TRADED 后，
        迟到的 on_stock_order(部成/已报) 再到。成交是比委托态更强的事实（全成=plan_volume 已全部成交，
        add_fill 行 196 亦有「!=TRADED 才推进」的同口径守卫），故一旦 TRADED，本方法不再据委托态降级：
        否则已全成单被降级为 PART_TRADED/REPORTED → on_ttl_expired 误判在途 → 对已全成委托反复发撤单
        （券商无活动委托、无 CANCELLED 回报 → 每宽限期重撤至收盘）+ 收盘对账台账态非 TRADED 污染勾稽。
        边界：OrderState 无序数，无法靠枚举大小拦截降级，必须显式守卫；error_msg 仍按需记录（不改状态）。
        """
        biz = self._resolve_biz(order_id)
        if biz is None:
            return
        e = self._by_biz[biz]
        # 已 TRADED（全成事实已确认）→ 任何迟到的委托态回报都不降级，仅按需记 error_msg（H-1）。
        # 注：不用 OrderState.terminal() 作判据——它含 PART_TRADED，会放行「迟到部成降级已成单」，等于没修。
        if e.state == OrderState.TRADED:
            if msg is not None:
                e.error_msg = msg
            return
        # fill-aware：撤单/废单终态遇已有成交 → 收口为 PART_TRADED（不抹掉真实建仓事实）。
        if state in (OrderState.CANCELLED, OrderState.REJECTED) and e.filled_volume > 0:
            e.state = OrderState.PART_TRADED
        else:
            e.state = state
        if msg is not None:
            e.error_msg = msg

    def add_fill(
        self, order_id: int, traded_id, traded_volume: int, traded_price
    ) -> Optional[Tuple[str, str, int, Any]]:
        """累计成交（on_stock_trade 驱动）：更新 filled_volume / 成交均价，并推进状态。

        业务意图：只认 xttrader 回报才算建仓成功（§4.4(4)）。累计成交达计划量 → TRADED，
        0<累计<计划 → PART_TRADED。
        幂等（§6.5/§4.4(4)）：按 traded_id 去重——同一成交编号重投（断线重连后回调重放、券商重复推送）
        只计一次，绝不重复累计 filled_volume。traded_id 为 None（异常回报）时不去重但仍保守计入一次。
        边界：
        - 台账无该 order_id 直接忽略（手工单/非本系统单）；
        - traded_volume<=0（异常/撤单回报）忽略，不污染累计量与均价；
        - 不回退已 TRADED 态（达量后续帧不降级）。

        返回值（评审三轮 EXEC-order-01）：实际【新计入】一笔成交时返回 (biz, dedup_key, vol, traded_price)，
        供持久层把该笔写入 local_order_fill 明细表（崩溃重启按明细重算）；被去重/忽略/未知单时返回 None。
        """
        biz = self._resolve_biz(order_id)
        if biz is None:
            return None
        e = self._by_biz[biz]
        # 成交量下界保护：<=0 视为异常/无效回报，直接忽略（§low#2）。
        vol = int(traded_volume) if traded_volume is not None else 0
        if vol <= 0:
            return None
        # 成交去重键归一（评审 P1#7 / P1#8）：
        # - P1#7：traded_id 一律 str 化（miniQMT 实测常为 int，落盘统一 str，重启后须类型一致才能去重）。
        # - P1#8：traded_id 缺失时改用合成键 (order_id|vol|price) 兜底去重，避免同一帧重投把建仓量翻倍。
        dedup_key = (
            str(traded_id)
            if traded_id is not None
            else f"_noid_{order_id}_{vol}_{traded_price}"
        )
        if dedup_key in e.counted_trade_ids:
            return None
        e.counted_trade_ids.add(dedup_key)
        prev_vol = e.filled_volume
        prev_amt = (e.avg_filled_price or Decimal("0")) * Decimal(prev_vol)
        add_price = Decimal(str(traded_price)) if traded_price is not None else Decimal("0")
        new_vol = prev_vol + vol
        if new_vol > 0:
            e.avg_filled_price = (prev_amt + add_price * Decimal(vol)) / Decimal(new_vol)
        e.filled_volume = new_vol
        # 推进状态：达计划量 → TRADED，否则 PART_TRADED（不回退已是 TRADED 的态）。
        if e.state != OrderState.TRADED:
            if e.plan_volume and new_vol >= e.plan_volume:
                e.state = OrderState.TRADED
            elif new_vol > 0:
                e.state = OrderState.PART_TRADED
        # 返回新计入的明细，供持久层落 local_order_fill（仅本次真正去重通过的成交）。
        return (biz, dedup_key, vol, traded_price)

    def reconcile_fills_from_detail(
        self, biz_order_no: str, fills: List[Tuple[str, int, Any]]
    ) -> None:
        """以成交明细（权威）重算某计划单的 filled_volume/均价/已计 dedup_key 并再推进状态（评审三轮 EXEC-order-01）。

        用途：重启 load_from_db 后，整行台账快照的 filled_volume 可能停留在崩溃窗口前的旧值；这里按
        local_order_fill 明细行（append-only + 唯一键去重）重算累计，纠正快照偏差，并把 counted_trade_ids
        重建为明细的 dedup_key 集——使重启后同一 traded_id 二次回报在内存层也被去重，绝不二次累计。
        fills: List[(dedup_key, vol, price)]。
        """
        e = self._by_biz.get(biz_order_no)
        if e is None:
            return
        # 不回退已成交态守卫（评审 doc/19 H-1 对称漏洞，与 sync_status / add_fill 同口径）：启动重建若崩溃
        # 窗口 local_order_fill 明细未全量落盘，按明细重算出的 total_vol 可能 < plan_volume；此时绝不能把已
        # TRADED 的整行快照降级为 PART_TRADED（否则 on_ttl_expired 误判在途→对已全成单反复发撤单 + 收盘对账态
        # 污染，正是 H-1 的下游危害）。已 TRADED 单的 filled_volume/counted 快照已是终态全成、与其明细同事务落盘
        # （M-1 原子写），无需也不应据（可能不全的）明细重算下修；且 add_fill 对 TRADED 不再计入新成交，counted
        # 无需重建。故已 TRADED 直接跳过重算、保留全成快照，使台账三处状态收口口径全对齐「已成不回退」。
        if e.state == OrderState.TRADED:
            return
        total_vol = 0
        total_amt = Decimal("0")
        counted: Set[str] = set()
        for dedup_key, vol, price in fills:
            if dedup_key in counted:  # 明细表 PK 已去重，这里再防御一次
                continue
            counted.add(dedup_key)
            v = int(vol)
            total_vol += v
            total_amt += (Decimal(str(price)) if price is not None else Decimal("0")) * Decimal(v)
        e.counted_trade_ids = counted
        e.filled_volume = total_vol
        e.avg_filled_price = (total_amt / Decimal(total_vol)) if total_vol > 0 else None
        # 明细重算后按 filled vs plan 重新收口状态（与 add_fill / sync_status 同口径）：
        if e.state in (OrderState.CANCELLED, OrderState.REJECTED):
            # 终态撤/废：有成交则 fill-aware 收口 PART_TRADED，否则保持终态。
            if total_vol > 0:
                e.state = OrderState.PART_TRADED
        else:
            if e.plan_volume and total_vol >= e.plan_volume:
                e.state = OrderState.TRADED
            elif total_vol > 0:
                e.state = OrderState.PART_TRADED
            # else 保持装载态（SUBMITTED/PLANNED 等），不臆造

    def all_for_date(self, target_trade_date: date) -> List[LedgerEntry]:
        return [
            copy.deepcopy(e)
            for e in self._by_biz.values()
            if e.target_trade_date == target_trade_date
        ]

    def all(self) -> List[LedgerEntry]:
        return [copy.deepcopy(e) for e in self._by_biz.values()]
