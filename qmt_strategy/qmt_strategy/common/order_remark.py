"""order_remark 解析（§4.4(5) / §6.8 单一来源）。

业务意图：下单时执行侧把信号来源标识写入 order_remark，固定格式 ``LUP|<signal_trade_date T>|<ts_code>``
（如 ``LUP|2026-06-11|600036.SH``）。落库（normalize）与对账（reconcile）阶段都据此解析回填
signal_trade_date，使 qmt_trade/qmt_order 能与 limit_up_selected_stock 按 signal_trade_date+ts_code 直接 join。

本模块为唯一解析入口（normalize 与 reconcile 共用，避免两处口径漂移）。
"""

from __future__ import annotations

from datetime import date
from typing import Optional

# order_remark 透传前缀（与 order_executor.build_order_remark 一致）。
REMARK_PREFIX = "LUP"


def parse_order_remark(remark: Optional[str]) -> Optional[date]:
    """从 order_remark 解析信号日 T（§6.8）。

    边界：remark 为空 / 前缀非 LUP / 段数不足 / 日期非法 → 返回 None（由调用方走交易日历兜底）。
    ts_code 段可被超长截断（§4.4(5)），故只要求前两段（LUP 前缀 + T）。
    """
    if not remark:
        return None
    parts = str(remark).split("|")
    if len(parts) < 2 or parts[0] != REMARK_PREFIX:
        return None
    t_str = parts[1].strip()
    try:
        # 严格 ISO 日期；非法格式（'2026/06/11'、空串等）落 ValueError 返回 None。
        return date.fromisoformat(t_str)
    except (ValueError, TypeError):
        return None
