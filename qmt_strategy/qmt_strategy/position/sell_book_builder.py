"""卖出盘口构造（阶段0-C T0.1 最小可用版 / 国金对接核对）。

业务意图：把执行侧 xtdata 单帧 tick 加工成 sell_decider 消费的 OrderBook。**复用买入侧 auction_factors
的同构纯函数**（`_to_decimal`/`_best_level`/`open_pct`/`virtual_seal`）派生【单帧可判】字段；【跨帧状态量】
（炸板/破位/放量/量价背离/开板次数/尾盘走弱）在本最小版**保守置默认（False/0）**，待阶段1真机 tick 帧历史
（T1.2 SellBookBuilder）实现。

字段口径（与 contracts.OrderBook 对齐）：
- 单帧可立即落地：`last_price`←tick.lastPrice、`open_pct`←(lastPrice-lastClose)/lastClose、
  `seal_amount`/`seal_to_float_ratio`/`is_sealed`←virtual_seal（需 PlanRow 提供今日理论涨停价 limit_up_price
  + 流通市值 float_mktcap；plan 缺失则封单类字段降级为默认，decider 视封板质量未知→不据此续持）。
- 跨帧（须真机 tick 帧历史，本版保守默认）：`broke_board`/`below_support`/`volume_surge`/
  `near_close_weak`/`price_volume_diverge`=False、`open_times`=0。

安全口径：纯函数、无副作用、**绝不抛异常拖垮整批构造**（单票坏数据只产出降级 OrderBook，由调用方按需取舍）；
时间无关（单帧）。缺关键字段（无现价/无涨停价）→ 相应字段 None/默认，下游 decider 走 HOLD/兜底安全默认。
"""

from __future__ import annotations

from typing import Callable, Optional

# 复用买入侧竞价四因子的同构纯函数（单一实现，避免口径漂移；评审 F4 的五档 best 取值同样复用）。
from ..auction.auction_factors import _best_level, _to_decimal, open_pct, virtual_seal  # noqa: F401
from ..contracts.models import OrderBook, PlanRow

# data_quality 原因码：与 auction_factors 同源语义，便于复盘排查盘口降级原因。
DQ_NO_TICK = "NO_TICK"            # 整帧 tick 缺失
DQ_NO_PLAN = "NO_PLAN"            # 无 PlanRow（持仓不在今日 watchlist）→ 封单类字段降级
DQ_CROSS_FRAME_ERR = "CROSS_FRAME_ERR"  # 跨帧字段计算器抛错（降级为该票跨帧字段保守默认）

# 跨帧盘口字段计算器类型（评审 doc/19 C-3 接线扩展点）：签名 (ts_code, tick, ob) -> None，原地填充 ob 的
# 跨帧状态量（broke_board/below_support/volume_surge/near_close_weak/price_volume_diverge/open_times）。
# T0.1 不注入（=None）→ 这些字段保守默认（False/0）。真机固化 tick 键名/量纲后（T1.2），实现一个【有状态、
# 逐 code 帧历史】的 builder 注入即可让「炸板/破位/烂板/尾盘了结」四类硬扳机真正生效——无需改本函数与下游 decider。
CrossFrameBuilder = Callable[[str, dict, OrderBook], None]


def build_order_book(
    ts_code: str,
    tick: Optional[dict],
    plan: Optional[PlanRow],
    cross_frame_builder: Optional[CrossFrameBuilder] = None,
) -> OrderBook:
    """单帧 tick + 计划行（+ 可选跨帧计算器）→ OrderBook。

    边界：
    - tick 为 None → 返回空壳 OrderBook（仅 ts_code + NO_TICK），各字段默认；调用方（build_sell_books）
      据此【不纳入 books】，使 run_sell_pass 对该票走「无盘口安全默认不卖」（book is None）。
    - plan 为 None（持仓不在今日 watchlist，无今日涨停价/流通市值基准）→ 封单类字段保持默认（封板质量未知）。
    - bidVol/bidPrice 为五档 list → 经 _best_level 取 best 档（评审 F4，与买入侧同口径）。
    - cross_frame_builder（评审 doc/19 C-3）：注入则用其填充跨帧状态量；None（T0.1）则跨帧字段保守默认。
    """
    ob = OrderBook(ts_code=ts_code)
    if tick is None:
        ob.data_quality.append(DQ_NO_TICK)
        return ob

    # —— 单帧可判字段：现价 + 高开幅度 ——
    last_price = _to_decimal(tick.get("lastPrice"))
    pre_close = _to_decimal(tick.get("lastClose"))
    ob.last_price = last_price
    ob.open_pct = open_pct(last_price, pre_close)   # 昨收缺/为 0 → None（decider 据此判盘口缺失/弱开）

    # —— 封单类字段（需 PlanRow 今日涨停价 + 流通市值）——
    if plan is not None:
        seal = virtual_seal(tick, plan.limit_up_price, plan.float_mktcap)
        ob.seal_amount = seal.virtual_seal_amount
        ob.seal_to_float_ratio = seal.seal_to_float_ratio
        # is_sealed 为单帧可判：虚拟成交价≥今日理论涨停价(plan)且有封一档 → 当前封涨停。
        ob.is_sealed = seal.is_limit_up
        ob.data_quality.extend(seal.data_quality)
    else:
        ob.data_quality.append(DQ_NO_PLAN)

    # —— 跨帧状态量：broke_board / below_support / volume_surge / near_close_weak / price_volume_diverge / open_times ——
    # 评审 doc/19 C-3：这些需保留 per-code 帧历史 + 真机 tick 键名（涨停价/均价/成交量量纲）才能保真。
    # - cross_frame_builder 为 None（T0.1 现状）→ 保守默认（OrderBook dataclass 默认 False/0），**故意未填、非遗漏**；
    #   此时破位/炸板细粒度止损缺失，由 main._unit_stop_loss_breached 的【单帧浮亏止损】兜底（见 doc/16）。
    # - 注入 builder（T1.2 真机固化键名后）→ 由其原地填充上述字段，使四类硬扳机生效；异常降级为该票保守默认 + 留痕。
    if cross_frame_builder is not None:
        try:
            cross_frame_builder(ts_code, tick, ob)
        except Exception:  # noqa: BLE001 跨帧计算异常绝不拖垮整批盘口构造，降级为该票跨帧字段保守默认
            ob.data_quality.append(DQ_CROSS_FRAME_ERR)
    return ob
