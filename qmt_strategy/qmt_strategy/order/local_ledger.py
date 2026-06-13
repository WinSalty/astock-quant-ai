"""本地下单台账（§4.4 / §4.8 / §6.7 对账事实源之一）。

业务意图：每次 order_stock 计划单落盘，承载业务级幂等（biz_order_no 去重）与对账事实源
（台账 vs xttrader 回报）。与 qmt_order 的 DB 级唯一键互补：DB 级防「同一委托重复落表」，
biz_order_no 防「业务侧同一计划重复下单」（§4.4(2)），两层都要有。

InMemoryLocalLedger 实现 contracts.LocalLedger 协议，单测 / 进程内常驻使用。
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from ..contracts.enums import OrderState
from ..contracts.models import LedgerEntry


class InMemoryLocalLedger:
    """进程内本地下单台账。实现 contracts.LocalLedger 协议。

    幂等键：(target_trade_date, ts_code, strategy_family) → 是否存在「活跃单」。
    回报关联键：order_id（下单后由 xttrader 返回回填）。
    """

    def __init__(self):
        self._by_biz: Dict[str, LedgerEntry] = {}
        # order_id → biz_order_no，便于回调侧按 order_id 反查台账
        self._order_index: Dict[int, str] = {}

    def has_active(self, target_trade_date: date, ts_code: str, strategy_family: str) -> bool:
        """同 (target_trade_date, ts_code, strategy_family) 是否已有未终结/已成单（§4.4(2)）。"""
        return self.find_active(target_trade_date, ts_code, strategy_family) is not None

    def find_active(
        self, target_trade_date: date, ts_code: str, strategy_family: str
    ) -> Optional[LedgerEntry]:
        active = OrderState.active()
        for e in self._by_biz.values():
            if (
                e.target_trade_date == target_trade_date
                and e.ts_code == ts_code
                and e.strategy_family == strategy_family
                and e.state in active
            ):
                return copy.deepcopy(e)
        return None

    def insert(self, entry: LedgerEntry) -> None:
        """写入新计划单。重复 biz_order_no 直接覆盖（幂等：同号视为同一计划）。"""
        self._by_biz[entry.biz_order_no] = copy.deepcopy(entry)
        if entry.order_id is not None:
            self._order_index[entry.order_id] = entry.biz_order_no

    def get(self, biz_order_no: str) -> Optional[LedgerEntry]:
        e = self._by_biz.get(biz_order_no)
        return copy.deepcopy(e) if e else None

    def get_by_order_id(self, order_id: int) -> Optional[LedgerEntry]:
        biz = self._order_index.get(order_id)
        if biz is None:
            return None
        return self.get(biz)

    def update(self, biz_order_no: str, **fields) -> None:
        """按字段更新台账行。order_id 变化时同步维护反查索引。"""
        e = self._by_biz.get(biz_order_no)
        if e is None:
            raise KeyError(f"ledger 无 biz_order_no={biz_order_no}")
        for k, v in fields.items():
            if not hasattr(e, k):
                raise AttributeError(f"LedgerEntry 无字段 {k}")
            setattr(e, k, v)
        if e.order_id is not None:
            self._order_index[e.order_id] = e.biz_order_no

    def sync_status(self, order_id: int, state: OrderState, msg: Optional[str] = None) -> None:
        """按 order_id 同步委托状态（on_stock_order 驱动）。台账无该 order_id 则忽略（防越权改写）。

        部成收口（§4.7/§4.9 硬口径）：部成单撤单后 QMT 该委托终态报 CANCELLED，但若本地已有
        成交（filled_volume>0），终态应落 PART_TRADED 而非 CANCELLED——已成部分是真实建仓，
        未成部分计买不进。仅完全未成（filled_volume==0）才落 CANCELLED。对 REJECTED 同理收口。
        """
        biz = self._order_index.get(order_id)
        if biz is None:
            return
        e = self._by_biz[biz]
        # fill-aware：撤单/废单终态遇已有成交 → 收口为 PART_TRADED（不抹掉真实建仓事实）。
        if state in (OrderState.CANCELLED, OrderState.REJECTED) and e.filled_volume > 0:
            e.state = OrderState.PART_TRADED
        else:
            e.state = state
        if msg is not None:
            e.error_msg = msg

    def add_fill(self, order_id: int, traded_id, traded_volume: int, traded_price) -> None:
        """累计成交（on_stock_trade 驱动）：更新 filled_volume / 成交均价，并推进状态。

        业务意图：只认 xttrader 回报才算建仓成功（§4.4(4)）。累计成交达计划量 → TRADED，
        0<累计<计划 → PART_TRADED。
        幂等（§6.5/§4.4(4)）：按 traded_id 去重——同一成交编号重投（断线重连后回调重放、券商重复推送）
        只计一次，绝不重复累计 filled_volume。traded_id 为 None（异常回报）时不去重但仍保守计入一次。
        边界：
        - 台账无该 order_id 直接忽略（手工单/非本系统单）；
        - traded_volume<=0（异常/撤单回报）忽略，不污染累计量与均价；
        - 不回退已 TRADED 态（达量后续帧不降级）。
        """
        biz = self._order_index.get(order_id)
        if biz is None:
            return
        e = self._by_biz[biz]
        # 成交量下界保护：<=0 视为异常/无效回报，直接忽略（§low#2）。
        vol = int(traded_volume) if traded_volume is not None else 0
        if vol <= 0:
            return
        # traded_id 去重：已计入则直接返回，不重复累计（§low#1/§6.5）。
        if traded_id is not None:
            if traded_id in e.counted_trade_ids:
                return
            e.counted_trade_ids.add(traded_id)
        prev_vol = e.filled_volume
        prev_amt = (e.avg_filled_price or Decimal("0")) * Decimal(prev_vol)
        add_price = Decimal(str(traded_price)) if traded_price is not None else Decimal("0")
        new_vol = prev_vol + vol
        if new_vol > 0:
            e.avg_filled_price = (prev_amt + add_price * Decimal(vol)) / Decimal(new_vol)
        e.filled_volume = new_vol
        # 推进状态：达计划量 → TRADED，否则 PART_TRADED（不回退已是 TRADED 的态）。
        if e.state == OrderState.TRADED:
            return
        if e.plan_volume and new_vol >= e.plan_volume:
            e.state = OrderState.TRADED
        elif new_vol > 0:
            e.state = OrderState.PART_TRADED

    def all_for_date(self, target_trade_date: date) -> List[LedgerEntry]:
        return [
            copy.deepcopy(e)
            for e in self._by_biz.values()
            if e.target_trade_date == target_trade_date
        ]

    def all(self) -> List[LedgerEntry]:
        return [copy.deepcopy(e) for e in self._by_biz.values()]
