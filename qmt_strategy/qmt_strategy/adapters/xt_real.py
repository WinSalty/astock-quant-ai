"""真实 xtquant 适配器（仅 Windows + miniQMT 可运行）。

定位：把真实 xtquant 对象「翻译」成本引擎的 Protocol 接口（contracts.XtTraderLike / XtDataLike）。
**这是整个工程唯一 import xtquant 的地方。**

跨平台安全：xtquant 全部【惰性 import】（只在工厂/构造函数被调用时才触发）。因此：
- 在任意平台（含 macOS/Linux）import 本模块都不会报错（不破坏现有单测/工具导入）；
- 在缺 xtquant 的机器上调用任何工厂会抛【清晰的 RuntimeError】，提示「请在目标机实测后运行」，
  而非深层难懂的 ImportError。

==================== 待实测清单（到目标 Windows 机用 vars(obj)/dir(obj)/print 核对后固化）====================
- xtconstant 各值：STOCK_BUY / STOCK_SELL / FIX_PRICE(限价) 及 order_status 数值映射
  （目前 order_executor.XT_ORDER_TYPE_BUY=23 / XT_PRICE_TYPE_FIX=11、normalize._STATUS_NUM_MAP 均为占位）；
- order_stock 真实参数顺序与含义；StockAccount 构造签名（account_id[, account_type]）；
- 回报对象 XtTrade/XtOrder/XtAsset/XtPosition/XtOrderError/XtCancelError 的字段名（normalize 用 getattr 容错，但需核对）；
- XtQuantTraderCallback 实际回调方法名/签名（版本差异）；
- xtdata.get_full_tick 返回字典的键名（lastPrice/lastClose/volume/bidVol/bidPrice... 见 auction_factors）。
=============================================================================================================
"""

from __future__ import annotations

from typing import Any, Callable

from ..contracts.errors import ConnectionNotReadyError


def _require_xtquant() -> None:
    """校验 xtquant 可用；不可用时抛清晰错误（仅 Windows + miniQMT 环境可用）。"""
    try:
        import xtquant  # noqa: F401
    except Exception as e:  # noqa: BLE001 任何 import 失败都视为「非目标环境」
        raise RuntimeError(
            "xtquant 不可用：真实适配器只能在 Windows + miniQMT 环境运行；"
            f"请在目标机安装并登录 miniQMT 后再运行（import xtquant 失败：{e!r}）"
        ) from e


class RealXtTrader:
    """实现 contracts.XtTraderLike：包住 xtquant.xttrader.XtQuantTrader，逐方法转发。

    强约束（§2.2）：connect() 返回 0 视为成功；不依赖 on_connected（xtquant 有该回调，但靠 connect()
    返回值判就绪，§连接守护）；断开不自动重连；须 run_forever 常驻。
    """

    def __init__(self, userdata_path: str, session_id: int):
        _require_xtquant()
        from xtquant.xttrader import XtQuantTrader  # 惰性 import

        # userdata_path = QMT 客户端 userdata_mini 目录；session_id 每次重连须换新（旧 session 不复用）。
        self._t = XtQuantTrader(userdata_path, session_id)

    def register_callback(self, callback: Any) -> None:
        self._t.register_callback(callback)

    def start(self) -> None:
        self._t.start()

    def connect(self) -> int:
        return self._t.connect()  # 0=成功

    def subscribe(self, account: Any) -> int:
        return self._t.subscribe(account)  # 0=成功

    def run_forever(self) -> None:
        self._t.run_forever()

    def stop(self) -> None:
        # 停掉 trader 及其后台线程（评审 doc/19 H-2）：ConnectionGuard 重连换新 trader 前调本方法回收旧实例，
        # 防反复断线下后台线程/句柄泄漏。TODO(实测)：核对目标机 xtquant 版本 XtQuantTrader.stop() 的真实语义
        # （是否幂等、对已断线实例是否安全）；ConnectionGuard._stop_trader_quietly 已对异常做 best-effort 吞错。
        self._t.stop()

    def order_stock(
        self, account: Any, stock_code: str, order_type: int, order_volume: int,
        price_type: int, price: float, strategy_name: str = "", order_remark: str = "",
    ) -> int:
        # TODO(实测)：确认真实 order_stock 参数顺序，与 xtconstant.STOCK_BUY / FIX_PRICE 取值。
        return self._t.order_stock(
            account, stock_code, order_type, order_volume, price_type, price, strategy_name, order_remark
        )

    def cancel_order_stock(self, account: Any, order_id: int) -> int:
        return self._t.cancel_order_stock(account, order_id)

    def query_stock_asset(self, account: Any) -> Any:
        return self._t.query_stock_asset(account)

    def query_stock_positions(self, account: Any) -> Any:
        return self._t.query_stock_positions(account)

    def query_stock_orders(self, account: Any) -> Any:
        return self._t.query_stock_orders(account)

    def query_stock_trades(self, account: Any) -> Any:
        return self._t.query_stock_trades(account)


class TraderHolder:
    """XtTraderLike 代理：始终委托给「当前」trader 实例。

    业务意图（重连正确性）：ConnectionGuard 断线重连会用新 session_id 重建 trader 实例，而
    OrderExecutor / SnapshotJob 在引擎装配时拿到的是固定引用。若直接把某个 trader 实例交给引擎，
    重连后下单 / 查询会发给【已失效的旧 trader】。本 holder 让引擎始终指向当前活跃 trader：
    把 trader_factory 包成「创建后顺手 set 到 holder」，引擎持有 holder 即可。

    边界：未建连（current 为 None）时调用交易方法抛 ConnectionNotReadyError（不静默发给 None）。
    """

    def __init__(self) -> None:
        self._current: Any = None

    def set(self, trader: Any) -> Any:
        """更新当前 trader（trader_factory 创建后调用），返回该 trader 以便链式使用。"""
        self._current = trader
        return trader

    @property
    def current(self) -> Any:
        return self._current

    def _t(self) -> Any:
        if self._current is None:
            raise ConnectionNotReadyError("trader 未就绪（连接守护尚未成功建连）")
        return self._current

    # —— XtTraderLike 全方法委托当前 trader ——
    def register_callback(self, callback: Any) -> None:
        self._t().register_callback(callback)

    def start(self) -> None:
        self._t().start()

    def connect(self) -> int:
        return self._t().connect()

    def subscribe(self, account: Any) -> int:
        return self._t().subscribe(account)

    def run_forever(self) -> None:
        self._t().run_forever()

    def order_stock(self, account, stock_code, order_type, order_volume, price_type, price,
                    strategy_name="", order_remark=""):
        return self._t().order_stock(account, stock_code, order_type, order_volume, price_type,
                                     price, strategy_name, order_remark)

    def cancel_order_stock(self, account, order_id):
        return self._t().cancel_order_stock(account, order_id)

    def query_stock_asset(self, account):
        return self._t().query_stock_asset(account)

    def query_stock_positions(self, account):
        return self._t().query_stock_positions(account)

    def query_stock_orders(self, account):
        return self._t().query_stock_orders(account)

    def query_stock_trades(self, account):
        return self._t().query_stock_trades(account)


def make_trader_factory(userdata_path: str, holder: "TraderHolder") -> Callable[[int], RealXtTrader]:
    """返回 ConnectionGuard 用的 trader_factory: (session_id) -> XtTraderLike。

    每次重连换新 session_id 创建新 RealXtTrader，并顺手 set 到 holder（使引擎始终指向当前 trader）。
    """

    def _factory(session_id: int) -> RealXtTrader:
        trader = RealXtTrader(userdata_path, session_id)
        holder.set(trader)
        return trader

    return _factory


def make_stock_account(account_id: str) -> Any:
    """构造 xtquant StockAccount（下单 / 查询入参）。"""
    _require_xtquant()
    from xtquant.xttype import StockAccount  # 惰性 import

    # TODO(实测)：StockAccount 构造参数（account_id[, account_type]）以目标机 xtquant 版本为准。
    return StockAccount(account_id)


def make_trader_callback(exec_callback: Any) -> Any:
    """把本引擎的 ExecCallback 适配成 xtquant 要求的 XtQuantTraderCallback 子类实例（回调 ABI 适配）。

    业务意图：trader.register_callback 要求传入 XtQuantTraderCallback 的子类实例；本引擎的 ExecCallback
    是普通类（便于跨平台单测），故在此惰性定义一个转发子类。
    """
    _require_xtquant()
    from xtquant.xttrader import XtQuantTraderCallback  # 惰性 import

    class _RealCallback(XtQuantTraderCallback):
        # TODO(实测)：核对 XtQuantTraderCallback 实际回调方法名/签名（不同版本可能有差异）。
        def on_stock_trade(self, trade):
            exec_callback.on_stock_trade(trade)

        def on_stock_order(self, order):
            exec_callback.on_stock_order(order)

        def on_order_error(self, err):
            exec_callback.on_order_error(err)

        def on_cancel_error(self, err):
            exec_callback.on_cancel_error(err)

        def on_stock_asset(self, asset):
            exec_callback.on_stock_asset(asset)

        def on_stock_position(self, position):
            exec_callback.on_stock_position(position)

        def on_disconnected(self):
            exec_callback.on_disconnected()

    return _RealCallback()


def import_xtdata() -> Any:
    """返回真实 xtdata 模块（其 get_full_tick / subscribe_quote 即 XtDataLike）。

    用法：``XtdataTickSource(import_xtdata())`` 注入给 AuctionPoller。
    TODO(实测)：核对 xtdata.get_full_tick(code_list) 签名与返回字典键名。
    """
    _require_xtquant()
    from xtquant import xtdata  # 惰性 import

    return xtdata
