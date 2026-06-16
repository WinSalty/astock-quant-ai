"""回流落库前的字段规整纯函数（§6.3 / §6.6 / §6.8）。

业务意图：把 xtquant 回报对象（XtTrade / XtOrder / XtAsset / XtPosition，仅 Windows 运行期可得）
统一规整为四表落库记录（TradeRecord / OrderRecord / PositionRecord / AccountRecord），规整后再走
data_writer 的幂等 upsert。本模块为纯函数、无副作用、不持有任何外部连接，便于离线单测。

四类规整口径（§6.3）：
- 代码归一 ts_code：QMT stock_code 经 identity.resolve_code 归一为带交易所后缀的标准代码，
  同时保留原值 qmt_stock_code 便于排查。
- 方向 trade_side：由 order_type / offset_flag 经 side_resolver 映射为 BUY / SELL。
- 状态 order_status：QMT order_status 数值/字符串经 status_resolver 映射为 OrderStatus 枚举。
- 时间：traded_time / order_time 经 time_utils.qmt_ts_to_db 双写（UTC naive + 东八区 naive 原值）。

版本兼容（§6.3 关键约束）：xtquant 字段集存在版本差异，本模块一律用 getattr(obj, name, None) 取值，
缺失字段落 None（DDL 已设可空），绝不因 AttributeError 崩溃。

价位口径：traded_price / open_price / avg_price 等一律转 Decimal（A 股 0.01 精度，禁 float 比较/累加）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from ..common import identity
from ..common.order_remark import parse_order_remark
from ..common.time_utils import qmt_ts_to_db
from ..contracts.enums import DataSource, OrderStatus, SnapshotType, TradeSide
from ..contracts.models import (
    AccountRecord,
    OrderRecord,
    PositionRecord,
    TradeRecord,
)

# side_resolver / status_resolver 的可注入类型签名：
#   side_resolver(order_type, offset_flag) -> TradeSide
#   status_resolver(order_status_raw) -> OrderStatus
SideResolver = Callable[[Any, Any], TradeSide]
StatusResolver = Callable[[Any], OrderStatus]


# ---------------------------------------------------------------------------
# 取值/类型规整小工具（纯函数，无副作用）
# ---------------------------------------------------------------------------


def _to_decimal(v: Any) -> Optional[Decimal]:
    """把任意数值/字符串安全转 Decimal；None / 空串 / 非法值落 None（不臆造 0）。

    业务意图：价位与金额一律 Decimal 承载，杜绝 float 误差；脏数据不抛错，留 None 由 DDL 可空兜底。
    边界：先转 str 再构造 Decimal，避免 float→Decimal 引入二进制误差（如 Decimal(35.12)）。
    """
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    s = str(v).strip()
    if s == "":
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        # 非法数值不写错误值，落 None 交由对账/排查兜底（§6.3 不得因脏数据崩溃）。
        return None


def _to_int(v: Any) -> Optional[int]:
    """把任意数值/字符串安全转 int；None / 空串 / 非法值落 None。"""
    if v is None:
        return None
    if isinstance(v, bool):
        # bool 是 int 子类，单独拦截避免 True→1 的语义误用。
        return int(v)
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if s == "":
        return None
    try:
        # 兼容 "200" / "200.0" 两种字符串数量表达。
        return int(float(s)) if ("." in s or "e" in s or "E" in s) else int(s)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 默认方向 / 状态解析器（数值映射「待目标机实测」，详见下方注释）
# ---------------------------------------------------------------------------

# 【待实测】QMT/xtquant 的 order_type 数值含义随版本/券商存在差异，下表为一套「合理默认假设」，
#          目标机落地前须用 vars(obj)/dir(obj) 实测后再固化（§6.5 实测口径）。
#          常见约定：23=股票买入、24=股票卖出；同时兼容已是标准字符串 "BUY"/"SELL" 的直传。
_ORDER_TYPE_BUY = {23}
_ORDER_TYPE_SELL = {24}

# 【待实测】offset_flag 数值：常见 48='0' 开仓 / 49='1' 平仓（ASCII '0'/'1'），股票无开平仓概念，
#          故 offset_flag 仅作旁证、原值落库；方向最终以 order_type 为准。本表仅在 order_type
#          无法判定时作兜底参考，默认不依赖它判方向。

# 【待实测】QMT order_status 数值 → OrderStatus 枚举。xtconstant 常见取值（随版本核对）：
#          48 未报 / 49 待报 / 50 已报 / 51 已报待撤 / 52 部成待撤 / 53 部撤 / 54 已撤 /
#          55 部成 / 56 已成 / 57 废单。本表为合理默认，目标机实测后固化（§6.3 状态映射）。
_STATUS_NUM_MAP = {
    48: OrderStatus.REPORTED,      # 未报：尚未到交易所，归入「已报」语义的前态，落 REPORTED 留痕
    49: OrderStatus.REPORTED,      # 待报
    50: OrderStatus.REPORTED,      # 已报
    51: OrderStatus.REPORTED,      # 已报待撤（仍在途，未到终态）
    52: OrderStatus.PART_TRADED,   # 部成待撤
    53: OrderStatus.CANCELLED,     # 部撤（已撤销剩余，视为已撤终态）
    54: OrderStatus.CANCELLED,     # 已撤
    55: OrderStatus.PART_TRADED,   # 部成
    56: OrderStatus.TRADED,        # 已成
    57: OrderStatus.REJECTED,      # 废单（来自 on_stock_order 的拒单/废单）
    # 255 ORDER_UNKNOWN（国金对接核对补）：xtquant 另有未知状态 255。显式映射为 REJECTED 终态而非落
    # 兜底 REPORTED(在途)——否则某废单/异常单回 255 会被长期当在途、卖单 SELLING 迟迟不复位漏重挂。
    # 取终态使其促发 SELLING 复位/释放名额；残留风险（极少见的「255 实为活单被提前终态」）由成交回报
    # on_stock_trade（持仓权威源）+ 收盘持仓快照对账兜底，不会算错真实持仓。
    # ⚠️ 卖单侧短窗：255→REJECTED 会触发持仓 revert_selling→重挂；若该 255 卖单实际仍在券商挂着，存在
    # 「同票两笔活卖单」的短窗，由 can_use_volume / 券商超量拒单兜住（非 255 独有，是任何终态失败→revert
    # 的共性）。**真机须确认 255 是否可能为活单**（见待办 §A9），必要时对 SELL 的 255 复位加「确认券商无该
    # 活动委托」前置校验。
    255: OrderStatus.REJECTED,     # ORDER_UNKNOWN
}

# 已是标准字符串时的直传映射（大小写不敏感），兼容上游已规整场景。
_STATUS_STR_MAP = {s.value: s for s in OrderStatus}
_SIDE_STR_MAP = {s.value: s for s in TradeSide}


# 合成成交键前缀（评审三轮 EXEC-DW-01）：缺 traded_id 的成交用此前缀的合成键，便于回调侧识别并告警。
SYNTHETIC_TRADE_ID_PREFIX = "SYN|"


def _synthetic_trade_id(order_id: Any, traded_volume: Any, traded_price: Any, traded_time_east8: Any) -> str:
    """缺 traded_id 时的合成键（评审三轮 EXEC-DW-01）。

    用 (order_id, 量, 价, 东八区成交时间) 拼成稳定字符串：同一笔成交重投得同一键（幂等去重），不同笔不相撞，
    绝不落字面 "None" 撞 NOT NULL 唯一键把多笔成交折叠成一笔。一旦上线不可随意改格式（否则历史重投不再幂等）。
    """
    parts = "|".join(str(x) for x in (order_id, traded_volume, traded_price, traded_time_east8))
    return SYNTHETIC_TRADE_ID_PREFIX + parts


def default_side_resolver(order_type: Any, offset_flag: Any) -> TradeSide:
    """默认买卖方向解析（§6.3）。

    业务意图：把 QMT order_type/offset_flag 映射为标准 TradeSide。
    口径优先级：
      1) order_type 已是标准字符串 "BUY"/"SELL" → 直传（兼容上游已规整）；
      2) order_type 命中数值映射表（买 23 / 卖 24，【待实测】）→ 对应方向；
      3) 兜底：无法判定时返回 UNKNOWN（评审三轮 EXEC-DW-09），**绝不臆造为 BUY**——默认 BUY 会把方向不明的
         卖出成交误判为买入凭空建仓、算反持仓与资金流向。返回 UNKNOWN 由 _apply_trade_to_position 拒绝改持仓
         + 强告警，便于实测期快速发现映射表缺口。
    边界：order_type 为 None 时走 UNKNOWN；不抛错（§6.3 不得因脏数据崩溃）。
    """
    # 1) 标准字符串直传（大小写不敏感）
    if isinstance(order_type, str):
        key = order_type.strip().upper()
        if key in _SIDE_STR_MAP:
            return _SIDE_STR_MAP[key]
    # 2) 数值映射（待目标机实测固化）
    ot = _to_int(order_type)
    if ot is not None:
        if ot in _ORDER_TYPE_BUY:
            return TradeSide.BUY
        if ot in _ORDER_TYPE_SELL:
            return TradeSide.SELL
    # 3) 兜底：方向不可判定 → UNKNOWN（不臆造 BUY，不凭空建仓）
    return TradeSide.UNKNOWN


def default_status_resolver(order_status: Any) -> OrderStatus:
    """默认委托状态解析（§6.3）。

    业务意图：把 QMT order_status 数值/字符串映射为 OrderStatus 枚举。
    口径优先级：
      1) 已是标准字符串（REPORTED/PART_TRADED/TRADED/CANCELLED/REJECTED/ERROR）→ 直传；
      2) 命中数值映射表（_STATUS_NUM_MAP，【待实测】）→ 对应枚举；
      3) 兜底：无法判定时落 REPORTED（在途留痕，不臆造终态），避免把未知态误当已成/已撤。
    边界：None / 非法值走兜底，不抛错。
    """
    # 1) 标准字符串直传（大小写不敏感）
    if isinstance(order_status, str):
        key = order_status.strip().upper()
        if key in _STATUS_STR_MAP:
            return _STATUS_STR_MAP[key]
    # 2) 数值映射（待目标机实测固化）
    st = _to_int(order_status)
    if st is not None and st in _STATUS_NUM_MAP:
        return _STATUS_NUM_MAP[st]
    # 3) 兜底：未知态落 REPORTED（在途），不臆造终态
    return OrderStatus.REPORTED


# ---------------------------------------------------------------------------
# 四类记录规整
# ---------------------------------------------------------------------------


def normalize_trade(
    xt: Any,
    *,
    account_id: str,
    trade_date: date,
    data_source: DataSource,
    side_resolver: SideResolver,
    status_resolver: Optional[StatusResolver] = None,
) -> TradeRecord:
    """成交回报 XtTrade → TradeRecord（§6.2.1 on_stock_trade / §6.3）。

    业务意图：成交是唯一不可丢的事实源，规整后走 upsert_trade。
    关键规整：
      - ts_code = identity.resolve_code(stock_code)（归一后），qmt_stock_code 保留原值；
      - traded_id 强制 str（DDL 为字符串，跨日唯一性以实测为准，§6.5）；
      - trade_side 经 side_resolver(order_type, offset_flag)；
      - traded_time 双写经 qmt_ts_to_db → (UTC naive, 东八区 naive 原值)。
    版本兼容：所有字段经 getattr(..., None)，缺失落 None 不抛 AttributeError（§6.3）。
    注：status_resolver 形参在成交规整中保留以对齐统一签名，成交记录本身无 order_status 列，不使用。
    """
    qmt_stock_code = getattr(xt, "stock_code", None)
    # 代码归一：归一失败（脏代码/取不到 6 位）时 ts_code 落 None，由对账/降级兜底，不臆造。
    ts_code = identity.resolve_code(qmt_stock_code)
    # 方向：由 order_type / offset_flag 经注入的解析器统一映射（默认见 default_side_resolver）。
    order_type = getattr(xt, "order_type", None)
    offset_flag = getattr(xt, "offset_flag", None)
    trade_side = side_resolver(order_type, offset_flag)
    # 时间双写：东八区 Unix 时间戳 → (UTC naive 入 traded_time, 东八区 naive 原值入 traded_time_east8)。
    traded_time, traded_time_east8 = qmt_ts_to_db(getattr(xt, "traded_time", None))
    # signal_trade_date 落库阶段回填（§6.8 主路）：从 order_remark 解析信号日 T；
    # order_remark 缺失/非法时落 None，由对账阶段经交易日历反推兜底（reconcile.backfill_signal_trade_date）。
    order_remark = getattr(xt, "order_remark", None)
    signal_trade_date = parse_order_remark(order_remark)
    # traded_id 缺失兜底（评审三轮 EXEC-DW-01）：原实现 str(getattr(...,None)) 产出字面 "None" 字符串，会撞
    # 成交唯一键 (account_id, trade_date, traded_id) 把同日所有缺 id 的成交折叠成一笔（成交是唯一不可丢事实源）。
    # 缺失时改用合成键 SYN|order_id|量|价|时间：同一笔重投得同一键（幂等），不同笔不相撞，绝不落字面 "None"。
    tid_raw = getattr(xt, "traded_id", None)
    order_id_val = _to_int(getattr(xt, "order_id", None))
    traded_volume_val = _to_int(getattr(xt, "traded_volume", None))
    traded_price_val = _to_decimal(getattr(xt, "traded_price", None))
    if tid_raw is not None:
        traded_id_val = str(tid_raw)
    else:
        traded_id_val = _synthetic_trade_id(
            order_id_val, traded_volume_val, traded_price_val, traded_time_east8
        )
    return TradeRecord(
        account_id=account_id,
        trade_date=trade_date,
        ts_code=ts_code,                       # 已归一（可能为 None）
        qmt_stock_code=qmt_stock_code,         # 原值保留
        traded_id=traded_id_val,               # 缺失走合成键（SYN| 前缀），绝不落字面 "None"
        trade_side=trade_side,
        traded_price=traded_price_val,
        traded_volume=traded_volume_val,
        traded_time=traded_time,
        traded_time_east8=traded_time_east8,
        account_type=_to_int(getattr(xt, "account_type", None)),
        order_id=order_id_val,
        order_sysid=getattr(xt, "order_sysid", None),
        offset_flag=_to_int(offset_flag),      # 原值落库供事后核对
        traded_amount=_to_decimal(getattr(xt, "traded_amount", None)),  # 版本可缺
        strategy_name=getattr(xt, "strategy_name", None),
        order_remark=order_remark,
        signal_trade_date=signal_trade_date,   # 由 order_remark 回填（§6.8）
        data_source=data_source,
    )


def normalize_order(
    xt: Any,
    *,
    account_id: str,
    trade_date: date,
    data_source: DataSource,
    side_resolver: SideResolver,
    status_resolver: StatusResolver,
) -> OrderRecord:
    """委托回报 XtOrder → OrderRecord（§6.2.1 on_stock_order / §6.3）。

    业务意图：委托状态变化（已报/部成/已成/已撤/废单）规整后走 upsert_order。
    关键规整：
      - order_status 经 status_resolver(order_status 原值) → OrderStatus 枚举；
      - ts_code 归一 + qmt_stock_code 原值；trade_side 经 side_resolver；
      - order_time 双写（同 traded_time 口径）。
    版本兼容：全字段 getattr(..., None)，缺失落 None。
    """
    qmt_stock_code = getattr(xt, "stock_code", None)
    ts_code = identity.resolve_code(qmt_stock_code)
    order_type = getattr(xt, "order_type", None)
    offset_flag = getattr(xt, "offset_flag", None)
    trade_side = side_resolver(order_type, offset_flag)
    # 状态映射：QMT order_status 数值/字符串 → OrderStatus（默认见 default_status_resolver）。
    order_status = status_resolver(getattr(xt, "order_status", None))
    # 委托时间双写。
    order_time, order_time_east8 = qmt_ts_to_db(getattr(xt, "order_time", None))
    # signal_trade_date 落库阶段回填（§6.8 主路），同 normalize_trade 口径。
    order_remark = getattr(xt, "order_remark", None)
    signal_trade_date = parse_order_remark(order_remark)
    return OrderRecord(
        account_id=account_id,
        trade_date=trade_date,
        ts_code=ts_code,
        qmt_stock_code=qmt_stock_code,
        order_id=_to_int(getattr(xt, "order_id", None)),
        trade_side=trade_side,
        order_volume=_to_int(getattr(xt, "order_volume", None)),
        order_status=order_status,
        traded_volume=_to_int(getattr(xt, "traded_volume", None)) or 0,  # 默认 0，对齐 DDL 缺省
        account_type=_to_int(getattr(xt, "account_type", None)),
        order_sysid=getattr(xt, "order_sysid", None),
        offset_flag=_to_int(offset_flag),
        price_type=_to_int(getattr(xt, "price_type", None)),
        order_price=_to_decimal(getattr(xt, "price", None)),
        traded_price=_to_decimal(getattr(xt, "traded_price", None)),
        status_msg=getattr(xt, "status_msg", None),
        error_id=_to_int(getattr(xt, "error_id", None)),
        error_msg=getattr(xt, "error_msg", None),
        order_time=order_time,
        order_time_east8=order_time_east8,
        strategy_name=getattr(xt, "strategy_name", None),
        order_remark=order_remark,
        signal_trade_date=signal_trade_date,   # 由 order_remark 回填（§6.8）
        data_source=data_source,
    )


def normalize_position(
    xt: Any,
    *,
    account_id: str,
    trade_date: date,
    snapshot_type: SnapshotType,
    data_source: DataSource = DataSource.QUERY,
) -> PositionRecord:
    """持仓回报/查询 XtPosition → PositionRecord（§6.2 / §6.3）。

    业务意图：盘中回调落 INTRADAY，收盘定时拉取落 CLOSE（净值/持仓复盘只认 CLOSE）。
    版本兼容：float_profit/last_price 原生不返回、avg_price/frozen_volume/on_road_volume/
              yesterday_volume 版本可缺，一律 getattr(..., None)（§6.3）。
    """
    qmt_stock_code = getattr(xt, "stock_code", None)
    ts_code = identity.resolve_code(qmt_stock_code)
    return PositionRecord(
        account_id=account_id,
        trade_date=trade_date,
        ts_code=ts_code,
        qmt_stock_code=qmt_stock_code,
        snapshot_type=snapshot_type,
        volume=_to_int(getattr(xt, "volume", None)) or 0,
        can_use_volume=_to_int(getattr(xt, "can_use_volume", None)) or 0,
        account_type=_to_int(getattr(xt, "account_type", None)),
        frozen_volume=_to_int(getattr(xt, "frozen_volume", None)),      # 版本可缺
        on_road_volume=_to_int(getattr(xt, "on_road_volume", None)),    # 版本可缺
        yesterday_volume=_to_int(getattr(xt, "yesterday_volume", None)),  # 版本可缺
        open_price=_to_decimal(getattr(xt, "open_price", None)),
        avg_price=_to_decimal(getattr(xt, "avg_price", None)),          # 版本可缺
        market_value=_to_decimal(getattr(xt, "market_value", None)),
        last_price=_to_decimal(getattr(xt, "last_price", None)),        # 原生不返回，常为 None
        float_profit=_to_decimal(getattr(xt, "float_profit", None)),    # 原生不返回，常为 None
        profit_rate=_to_decimal(getattr(xt, "profit_rate", None)),
        data_source=data_source,
    )


def normalize_account(
    xt: Any,
    *,
    account_id: str,
    trade_date: date,
    snapshot_type: SnapshotType,
    data_source: DataSource = DataSource.QUERY,
) -> AccountRecord:
    """账户资产回报/查询 XtAsset → AccountRecord（§6.2 / §6.3）。

    业务意图：盘中回调刷 INTRADAY、收盘定时拉取落 CLOSE（净值曲线唯一来源）。
    版本兼容：XtAsset 仅 total_asset/cash/frozen_cash/market_value 等基础字段，不给净值/日盈亏，
              缺失字段落 None / 默认 0（日盈亏由对账阶段据 prev_total_asset 计算，本处不臆造）。
    """
    return AccountRecord(
        account_id=account_id,
        trade_date=trade_date,
        total_asset=_to_decimal(getattr(xt, "total_asset", None)),
        cash=_to_decimal(getattr(xt, "cash", None)),
        snapshot_type=snapshot_type,
        frozen_cash=_to_decimal(getattr(xt, "frozen_cash", None)) or Decimal("0"),
        market_value=_to_decimal(getattr(xt, "market_value", None)) or Decimal("0"),
        account_type=_to_int(getattr(xt, "account_type", None)),
        data_source=data_source,
    )
