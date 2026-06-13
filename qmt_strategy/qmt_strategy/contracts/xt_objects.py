"""xtquant 回报对象的 fake 实现（单测用 + 字段口径文档）。

业务意图：真实 XtTrade / XtOrder / XtAsset / XtPosition / XtOrderError / XtCancelError 仅 Windows
miniQMT 运行期可得，且字段集存在版本差异（§1.4 / §6.0：落地前须用 vars(obj)/dir(obj) 实测）。
这里用最小可变对象模拟其字段，供单测构造「带 / 不带可选字段」两组对象，验证 normalize 用
getattr(obj, name, None) 取值、缺失落 NULL 不抛 AttributeError（§6.3 版本兼容）。

注意：本类只用于测试与示意，绝不在真实下单链路引用；真实链路直接消费 xtquant 返回对象。
"""

from __future__ import annotations

from typing import Optional


class _Bag:
    """按 kwargs 设置任意属性的轻量容器，模拟 xtquant 对象的属性访问。"""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeXtTrade(_Bag):
    """模拟 XtTrade（成交回报，§6.2.1 on_stock_trade）。

    常见字段：account_id / account_type / stock_code / traded_id / order_id / order_sysid /
    offset_flag / order_type / traded_price / traded_volume / traded_amount / traded_time /
    strategy_name / order_remark。版本差异字段（如 traded_amount）可缺省。
    """


class FakeXtOrder(_Bag):
    """模拟 XtOrder（委托，§6.2.1 on_stock_order）。

    常见字段：account_id / stock_code / order_id / order_sysid / order_type / offset_flag /
    price_type / price / order_volume / traded_volume / traded_price / order_status /
    status_msg / order_time / strategy_name / order_remark。
    """


class FakeXtAsset(_Bag):
    """模拟 XtAsset（账户资产，§6.0：只有 6 字段、不给净值）。

    字段：account_id / cash / frozen_cash / market_value / total_asset（account_type 可选）。
    """


class FakeXtPosition(_Bag):
    """模拟 XtPosition（持仓，§6.0：不给浮动盈亏，float_profit/last_price 原生不返回）。

    字段：account_id / stock_code / volume / can_use_volume / open_price / market_value；
    版本可选：frozen_volume / on_road_volume / yesterday_volume / avg_price。
    """


class FakeXtOrderError(_Bag):
    """模拟 XtOrderError（下单失败，§6.2.1 on_order_error）。

    字段：order_id / error_id / error_msg（部分版本含 stock_code / order_remark）。
    """


class FakeXtCancelError(_Bag):
    """模拟 XtCancelError（撤单失败，§6.2.1 on_cancel_error）。

    字段：order_id / error_id / error_msg。
    """


class FakeStockAccount(_Bag):
    """模拟 StockAccount（账户对象，下单/查询入参）。字段：account_id / account_type。"""

    def __init__(self, account_id: str, account_type: Optional[int] = None):
        super().__init__(account_id=account_id, account_type=account_type)
