"""竞价四因子计算（§3.4 / §3.8 纯函数）。

业务意图：把每帧 ``get_full_tick`` 的原始 tick 字段加工成「竞价高开 / 竞价量能比 /
分时重心 / 虚拟封单」四个决策因子，供下游 entry_router 做买 / 弃判断。本模块只算数值、
不做阈值比较、不下单（阈值在 entry_router）。

设计取舍：
- 全部因子函数为纯函数，输入原始值、输出 Decimal / None，便于单测穷举边界。
- 价位一律用 Decimal，且从 tick 取出的浮点先经 ``str()`` 包装再入 Decimal，避免
  ``Decimal(0.1)`` 这种二进制误差污染比较（A 股价位 0.01 精度敏感）。
- 任一因子取不到数据时返回 None 并把原因码记入 data_quality（降级 A，§3.7），
  绝不臆造数值，下游对缺失因子按「中性 / 不加分」处理。
- 时间一律由调用方传入 UTC naive（经 common.time_utils 换算），本模块不自取系统时间、
  不做任何 ±8h。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

from ..contracts.enums import AuctionPhase, CentroidTrend
from ..contracts.models import AuctionSnapshot, PlanRow, SealInfo

# data_quality 原因码常量（与 §3.4 / §3.7 / §3.9 单测口径一致）。
DQ_NO_PRE_CLOSE = "NO_PRE_CLOSE"   # 昨收缺失 → open_pct=None
DQ_NO_BASE_VOL = "NO_BASE_VOL"     # 首板放量基准缺失 → auction_vol_ratio=None
DQ_NO_SEAL_VOL = "NO_SEAL_VOL"     # 已达涨停但买一量取不到 → 封单额无法计算
DQ_NO_TICK = "NO_TICK"             # 整帧 tick 缺失（本轮该 code 无任何行情）


def _to_decimal(value: object) -> Optional[Decimal]:
    """把 tick 里的原始值安全转 Decimal。

    业务意图：tick 字段在不同 xtdata 版本可能是 float / int / str / None，统一在此用
    ``str()`` 包装后入 Decimal，规避浮点二进制误差；无法解析（None/空/非数）返回 None，
    由各因子据此走降级，绝不抛 KeyError / InvalidOperation 拖垮主循环。
    """
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d


def _best_level(value: object) -> object:
    """从可能是五档数组的盘口字段取 best 档标量（评审 F4）。

    业务意图：真实 xtdata.get_full_tick 的 bidVol/bidPrice/askVol/askPrice 为五档 list（best 在 [0]），
    原实现把它当标量直接喂 _to_decimal → Decimal(str(list)) 抛 InvalidOperation 被吞→None，导致一字/
    竞价封板（bidVol 实为大数组）时虚拟封单恒为 0。这里统一取 best 档：list/tuple 取首元素、标量原样、
    空/None 返回 None。
    实测口径：目标机用 vars(tick) 核对 get_full_tick 实际键名与五档结构后固化（键名仍为占位）。
    """
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def open_pct(auction_price: Optional[Decimal], pre_close: Optional[Decimal]) -> Optional[Decimal]:
    """竞价高开幅度 = (虚拟成交价 − 昨收) / 昨收（§3.4 因子 1）。

    边界：pre_close 为 None 或 0（停牌复牌首日 / 数据缺失）→ 返回 None（无法定义涨跌幅），
    由调用方标 NO_PRE_CLOSE 走降级；auction_price 缺失同样返回 None。
    """
    if auction_price is None:
        return None
    if pre_close is None or pre_close == 0:
        return None
    return (auction_price - pre_close) / pre_close


def auction_volume_ratio(
    cum_vol: Optional[int], first_board_vol: Optional[int]
) -> Optional[Decimal]:
    """竞价量能比 = 竞价段累计撮合量 cum_vol / 首板信号日全天成交量 first_board_vol（§3.4 因子 2）。

    业务意图：竞价就放出首板爆量的相当比例 → 承接资金活跃（强开）。

    ⚠️ 量纲/时段不对等口径（评审三轮 EXEC-auction-05，务必固化）：
    - 分子 cum_vol = 竞价段（9:15–9:25 十分钟）累计撮合量（手）；
    - 分母 first_board_vol = 首板【信号日全天】成交量（手）。
    二者时段不对等（十分钟 vs 全天），比值结构性偏低（经验 ~1–5%），**仅作相对强弱加分参考，回测标定前
    不得单独构成硬弃门槛**（见 chase_auction_strong 弃条件已降级为 weak_vol 留痕）。
    TODO（终态）：分母改用「首板日【同时段 9:15–9:25】竞价量」使量纲对等——需 PlanRow 新增 first_board_auction_vol
    字段、由信号侧/分钟数据回填；量纲对等后再据标定结果把本因子重启用为硬门槛。

    边界：基准量 first_board_vol 为 None 或 0 → 返回 None（无基准不可比），标 NO_BASE_VOL；
    cum_vol 缺失 → 返回 None（竞价段拿不到撮合量，常见于降级 B）。
    """
    if first_board_vol is None or first_board_vol == 0:
        return None
    if cum_vol is None:
        return None
    return Decimal(str(cum_vol)) / Decimal(str(first_board_vol))


def auction_centroid(
    prev_ticks: List[dict], cur_tick: Optional[dict]
) -> Tuple[Optional[Decimal], CentroidTrend]:
    """分时重心（量加权均价）+ 趋势（§3.4 因子 3）。

    口径：对帧序列 (prev_ticks + cur_tick) 按相邻两帧累计量(volume)的增量 Δvol_i 加权，
        重心 = Σ(price_i × Δvol_i) / Σ(Δvol_i)，price_i 取该帧 lastPrice。
        Δvol_i 为「第 i 帧累计量 − 第 i-1 帧累计量」，序列首帧无前序故不计入（无增量）。
    趋势：末帧价 > 重心 → UP（越竞越高）；末帧价 < 重心 → DOWN（高开回落）；相等 → FLAT。

    业务意图：单看末帧高开会被「瞬时拉竞价」骗；重心 + 趋势能识别「先拉高再砸下」的诱多型竞价。
    边界：帧数不足 2、或全程累计量无正增量（ΣΔvol=0，如降级 B 拿不到 volume）→ 重心无法定义，
        返回 (None, FLAT)，下游按缺失处理。
    """
    # 合成完整帧序列：cur_tick 为本帧、prev_ticks 为历史帧，按时间先后排列。
    frames: List[dict] = list(prev_ticks)
    if cur_tick is not None:
        frames = frames + [cur_tick]

    # 数据不足：少于 2 帧无法构造任何增量。
    if len(frames) < 2:
        return None, CentroidTrend.FLAT

    weighted_sum = Decimal("0")  # Σ(price_i × Δvol_i)
    total_delta = Decimal("0")   # Σ(Δvol_i)
    last_price: Optional[Decimal] = None  # 末帧价，用于判趋势

    for i in range(1, len(frames)):
        prev_v = _to_decimal(frames[i - 1].get("volume"))
        cur_v = _to_decimal(frames[i].get("volume"))
        price_i = _to_decimal(frames[i].get("lastPrice"))
        # 末帧价取序列最后一帧的 lastPrice（可能为 None，留待后续判断）。
        if i == len(frames) - 1:
            last_price = price_i
        # 累计量增量：任一帧缺 volume 或为负增量（数据异常/重置）则跳过该帧权重。
        if prev_v is None or cur_v is None or price_i is None:
            continue
        delta = cur_v - prev_v
        if delta <= 0:
            continue
        weighted_sum += price_i * delta
        total_delta += delta

    # 全程无正增量（竞价量拿不到 / 量未变）→ 重心无法定义。
    if total_delta == 0:
        return None, CentroidTrend.FLAT

    centroid = weighted_sum / total_delta
    # 末帧价缺失时无法判趋势，重心仍可给出，趋势记 FLAT。
    if last_price is None:
        return centroid, CentroidTrend.FLAT
    if last_price > centroid:
        return centroid, CentroidTrend.UP
    if last_price < centroid:
        return centroid, CentroidTrend.DOWN
    return centroid, CentroidTrend.FLAT


def virtual_seal(
    tick: Optional[dict],
    limit_up_price: Optional[Decimal],
    float_mktcap: Optional[Decimal],
) -> SealInfo:
    """虚拟封单（§3.4 因子 4）。

    口径：虚拟成交价(lastPrice) ≥ 涨停价(limit_up_price) 视为「顶涨停」is_limit_up=True；
        封单额 = 买一量(bidVol) × 买一价(bidPrice)；封流比 = 封单额 / 流通市值。
    业务意图：一字 / 竞价即封涨停时，虚拟封单大小决定能否排进队；封单极大 → 大概率买不进。
    边界：
      - 未达涨停（或 limit_up_price/lastPrice 缺失无法判定）→ is_limit_up=False、封单额=0、
        封流比=None（正常情形，不算异常）。
      - 达涨停但 bidVol 取不到 → is_limit_up=True、封单额=0、标 NO_SEAL_VOL（数据缺口）。
      - float_mktcap 为 None / 0 → 封流比=None（无市值不可比），但封单额仍照算。
    """
    info = SealInfo()
    # tick 整帧缺失：既无价也无买档，记为未达涨停、无封单。
    if tick is None:
        return info

    last_price = _to_decimal(tick.get("lastPrice"))
    # 涨停价或最新价缺失 → 无法判定是否顶涨停，按未达涨停处理（封单额 0）。
    if limit_up_price is None or last_price is None:
        return info

    # 是否顶涨停：虚拟成交价 ≥ 涨停价。
    is_limit_up = last_price >= limit_up_price
    info.is_limit_up = is_limit_up
    if not is_limit_up:
        # 未达涨停：封单额 0、封流比 None，属正常态。
        return info

    # 已达涨停：尝试取买一档（一档即虚拟封单）。
    # 评审 F4：bidVol/bidPrice 真实为五档 list，先经 _best_level 取 best 档再转 Decimal，
    # 否则一字/竞价封板（数组）会被 _to_decimal 吞成 None、虚拟封单恒 0。
    bid_vol = _to_decimal(_best_level(tick.get("bidVol")))
    bid_price = _to_decimal(_best_level(tick.get("bidPrice")))
    if bid_vol is None or bid_price is None:
        # 达涨停但买一量/价取不到 → 封单额无法计算，标 NO_SEAL_VOL 走降级。
        info.data_quality.append(DQ_NO_SEAL_VOL)
        return info

    # 封单额 = 买一量 × 买一价（买一价竞价即封时等于涨停价）。
    info.virtual_seal_amount = bid_vol * bid_price
    # 封流比 = 封单额 / 流通市值；市值缺失 / 为 0 时比例不可得。
    if float_mktcap is not None and float_mktcap != 0:
        info.seal_to_float_ratio = info.virtual_seal_amount / float_mktcap
    return info


def compute_auction_factors(
    ts_code: str,
    tick: Optional[dict],
    prev_ticks: List[dict],
    phase: AuctionPhase,
    plan: PlanRow,
    now_utc: datetime,
) -> AuctionSnapshot:
    """组装单帧四因子聚合对象 AuctionSnapshot（§3.4 / §3.6）。

    业务意图：把当前帧 tick + 该股已采历史帧 + 计划行基准，整合为下游可直接消费的快照。
    降级口径（§3.7）：
      - tick 为 None → 仅产出能取到的字段（基本全 None），标 NO_TICK，snapshot 仍正常产出
        供「开盘后确认」（降级 B 的极端形态）。
      - pre_close（plan/tick 取昨收）缺失 → open_pct=None、标 NO_PRE_CLOSE。
      - first_board_vol 缺失 → auction_vol_ratio=None、标 NO_BASE_VOL。
      - 竞价量(volume)/买档拿不到（降级 B）→ 量能比 / 重心 / 封单各自为 None / 0，仅 open_pct 可得。
    幂等/留痕：tick_seq = 已采历史帧数 + 1（含本帧）；ts 取传入的 now_utc（UTC naive）。
    """
    data_quality: List[str] = []

    # —— 基础留痕字段 ——
    # 昨收：tick 的 lastClose 优先（实时帧权威），缺则无昨收。
    pre_close = _to_decimal(tick.get("lastClose")) if tick is not None else None
    # 当前帧虚拟成交价。
    last_price = _to_decimal(tick.get("lastPrice")) if tick is not None else None

    snap = AuctionSnapshot(
        ts_code=ts_code,
        phase=phase,
        ts=now_utc,
        last_price=last_price,
        pre_close=pre_close,
        tick_seq=len(prev_ticks) + 1,
    )

    # tick 整帧缺失：本轮该 code 无任何行情，只能产出空壳快照（降级 A 的极端态）。
    if tick is None:
        data_quality.append(DQ_NO_TICK)
        snap.data_quality = data_quality
        return snap

    # —— 因子 1：竞价高开 ——
    op = open_pct(last_price, pre_close)
    snap.open_pct = op
    if op is None:
        # 昨收缺失（或为 0）导致高开无法计算 → 标 NO_PRE_CLOSE，下游降级。
        data_quality.append(DQ_NO_PRE_CLOSE)

    # —— 因子 2：竞价量能比 ——
    cum_vol = tick.get("volume")
    cum_vol_int: Optional[int]
    try:
        cum_vol_int = int(cum_vol) if cum_vol is not None else None
    except (ValueError, TypeError):
        cum_vol_int = None
    ratio = auction_volume_ratio(cum_vol_int, plan.first_board_vol)
    snap.auction_vol_ratio = ratio
    if plan.first_board_vol is None or plan.first_board_vol == 0:
        # 首板放量基准缺失 → 量能比不可得，标 NO_BASE_VOL，下游不据此加分。
        data_quality.append(DQ_NO_BASE_VOL)

    # —— 因子 3：分时重心 + 趋势 ——
    centroid, trend = auction_centroid(prev_ticks, tick)
    snap.auction_centroid = centroid
    snap.centroid_trend = trend

    # —— 因子 4：虚拟封单 ——
    seal = virtual_seal(tick, plan.limit_up_price, plan.float_mktcap)
    snap.virtual_seal_amount = seal.virtual_seal_amount
    snap.seal_to_float_ratio = seal.seal_to_float_ratio
    snap.is_limit_up = seal.is_limit_up
    # 合并封单计算自带的数据缺口码（如 NO_SEAL_VOL）。
    data_quality.extend(seal.data_quality)

    snap.data_quality = data_quality
    return snap
