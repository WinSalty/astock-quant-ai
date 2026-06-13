"""执行侧全链路接口协议（锁定契约层）。

业务意图：把 xtquant（Windows-only）、MySQL、信号侧取数、交易日历等所有外部依赖
抽象为 Protocol，使各业务模块只依赖接口而非实现——既能在 macOS/Linux 用 fake 跑全部
单测，又便于真实落地时注入 xtquant / PyMySQL 实现。所有方法签名对齐 doc/03 各节伪逻辑。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from .enums import OrderState, OrderStatus, SnapshotType
from .models import (
    AccountRecord,
    AuctionSnapshot,
    LedgerEntry,
    OrderRecord,
    PositionRecord,
    SelectedStockRow,
    TradeRecord,
)


@runtime_checkable
class Clock(Protocol):
    """时钟抽象：所有「现在几点」一律经此取，便于单测注入固定时刻（禁直接 datetime.now）。"""

    def now_utc(self) -> datetime:
        """返回当前 UTC naive 时间（tzinfo=None）。"""
        ...


@runtime_checkable
class StructLogger(Protocol):
    """结构化本地日志接口（§6.1 logger）。不写敏感信息（账户/口令/token）。"""

    def info(self, event: str, **fields: Any) -> None: ...

    def warn(self, event: str, **fields: Any) -> None: ...

    def error(self, event: str, **fields: Any) -> None: ...


@runtime_checkable
class TradeCalendar(Protocol):
    """交易日历（对齐信号侧 a_trade_calendar；所有日推算经此，禁自然日 ±1）。"""

    def is_open(self, d: date) -> bool:
        """d 是否为交易日（is_open=1）。"""
        ...

    def next_open(self, d: date) -> date:
        """d 之后的下一交易日（trade_cal_next，跳过周末/节假日）。"""
        ...

    def prev_open(self, d: date) -> date:
        """d 之前的上一交易日（pretrade_date，供 signal_trade_date 反推）。"""
        ...


@runtime_checkable
class SelectedStockSource(Protocol):
    """watchlist 契约取数源（§2.3 路径 A 直读 MySQL / 路径 B 只读接口，二者同契约）。"""

    def fetch(self, target_trade_date: date) -> List[SelectedStockRow]:
        """整批取 target_trade_date=today 的信号行；失败抛异常由 loader 兜底。"""
        ...


# ---------------------------------------------------------------------------
# xtquant 抽象（xttrader / xtdata），真实实现仅 Windows 可用
# ---------------------------------------------------------------------------


@runtime_checkable
class XtTraderLike(Protocol):
    """xttrader（XtQuantTrader）交易接口抽象（§2.2 / §6.2）。

    强约束：无 on_connected；connect() 返回 0 视为成功；断开不自动重连；须 run_forever 常驻。
    """

    def register_callback(self, callback: Any) -> None: ...

    def start(self) -> None: ...

    def connect(self) -> int:
        """返回 0 视为连接成功，非 0 失败。"""
        ...

    def subscribe(self, account: Any) -> int:
        """订阅账户推送，返回 0 成功。"""
        ...

    def run_forever(self) -> None: ...

    def order_stock(
        self,
        account: Any,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        """下单，返回 QMT order_id（<0 视为同步失败）。"""
        ...

    def cancel_order_stock(self, account: Any, order_id: int) -> int:
        """撤单，返回 0 受理。"""
        ...

    def query_stock_asset(self, account: Any) -> Any: ...

    def query_stock_positions(self, account: Any) -> List[Any]: ...

    def query_stock_orders(self, account: Any) -> List[Any]: ...

    def query_stock_trades(self, account: Any) -> List[Any]: ...


@runtime_checkable
class XtDataLike(Protocol):
    """xtdata 行情接口抽象（§3.2 自采竞价/盘口）。"""

    def get_full_tick(self, codes: List[str]) -> Dict[str, dict]:
        """批量取全推 tick，返回 {ts_code: 原始 tick dict}。"""
        ...

    def subscribe_quote(self, code: str, period: str = "tick") -> int: ...


@runtime_checkable
class TickSource(Protocol):
    """竞价/盘中 tick 取数源（封装 xtdata.get_full_tick，§3.8）。"""

    def get_full_tick(self, codes: List[str]) -> Dict[str, dict]:
        """失败抛 TickSourceError，由 poller 主循环捕获走降级（§3.7）。"""
        ...


# ---------------------------------------------------------------------------
# 回流写端 / 仓储 / 台账
# ---------------------------------------------------------------------------


@runtime_checkable
class QmtRepository(Protocol):
    """qmt_* 四表仓储（§6.4 写入路径抽象：InMemory / 直连 MySQL / /ingest）。

    写入语义：INSERT ... ON DUPLICATE KEY UPDATE，后到覆盖为终态，
    但已回填的 signal_trade_date / *_east8 不被空值覆盖（COALESCE 口径，§6.5）。
    """

    def upsert_trade(self, rec: TradeRecord) -> None: ...

    def upsert_order(self, rec: OrderRecord) -> None: ...

    def upsert_position(self, rec: PositionRecord) -> None: ...

    def upsert_account_daily(self, rec: AccountRecord) -> None: ...

    def mark_cancel_failed(
        self, account_id: str, order_id: int, error_id: Optional[int], error_msg: Optional[str]
    ) -> None:
        """on_cancel_error：在既有委托行追加 cancel_failed=1 + error_*，不改 order_status 终态。"""
        ...

    # —— 对账只读 ——
    def get_orders(self, account_id: str, trade_date: date) -> List[OrderRecord]: ...

    def get_trades(self, account_id: str, trade_date: date) -> List[TradeRecord]: ...

    def get_account_daily(
        self, account_id: str, trade_date: date, snapshot_type: SnapshotType = SnapshotType.CLOSE
    ) -> Optional["AccountRecord"]:
        """取账户日快照（供资产对账，§6.7 第三类）。无该日该类型快照返回 None。"""
        ...


@runtime_checkable
class DataWriter(Protocol):
    """data_writer 落库写端（§6.2/§6.3）。callbacks / snapshot_job 依赖本接口。

    与 QmtRepository 的区别：DataWriter 负责字段规整后再 upsert；snapshot_type 在此处带入。
    """

    def upsert_trade(self, rec: TradeRecord) -> None: ...

    def upsert_order(self, rec: OrderRecord) -> None: ...

    def upsert_position(self, rec: PositionRecord, snapshot_type: SnapshotType) -> None: ...

    def upsert_account_daily(self, rec: AccountRecord, snapshot_type: SnapshotType) -> None: ...

    def mark_cancel_failed(
        self, account_id: str, order_id: int, error_id: Optional[int], error_msg: Optional[str]
    ) -> None: ...


@runtime_checkable
class LocalLedger(Protocol):
    """本地下单台账（§4.8 / §6.1）。order_executor 写、reconcile 读。"""

    def has_active(self, target_trade_date: date, ts_code: str, strategy_family: str) -> bool:
        """幂等判定：同 (target_trade_date, ts_code, strategy_family) 有未终结单则 True。"""
        ...

    def insert(self, entry: LedgerEntry) -> None: ...

    def get(self, biz_order_no: str) -> Optional[LedgerEntry]: ...

    def get_by_order_id(self, order_id: int) -> Optional[LedgerEntry]: ...

    def find_active(
        self, target_trade_date: date, ts_code: str, strategy_family: str
    ) -> Optional[LedgerEntry]: ...

    def update(self, biz_order_no: str, **fields: Any) -> None: ...

    def sync_status(self, order_id: int, state: OrderState, msg: Optional[str] = None) -> None: ...

    def add_fill(
        self, order_id: int, traded_id: Any, traded_volume: int, traded_price: Any
    ) -> None:
        """累计成交：按 traded_id 去重（重复回报/断线重连重投不重复累计，§6.5/§4.4(4)）。"""
        ...

    def all_for_date(self, target_trade_date: date) -> List[LedgerEntry]: ...


# 进程内回调：auction_poller 把每帧 AuctionSnapshot 推给 entry_router（§3.6 push_to_router）
RouterSink = Callable[[AuctionSnapshot], None]
