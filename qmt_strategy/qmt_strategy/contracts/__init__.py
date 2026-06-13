"""锁定契约层：枚举 / 数据结构 / 接口协议 / 异常 / xtquant fake 的单一来源。

各业务模块统一从 ``qmt_strategy.contracts`` 导入，禁止互相依赖对方实现。
"""

from __future__ import annotations

from .enums import (
    AuctionPhase,
    Board,
    CentroidTrend,
    DataSource,
    EntryAction,
    OrderPhase,
    OrderState,
    OrderStatus,
    PositionMode,
    PositionState,
    PriceSource,
    RiskVerdict,
    SellActionType,
    SnapshotType,
    StrEnum,
    TradeSide,
)
from .errors import (
    ConnectionNotReadyError,
    QmtStrategyError,
    RepositoryError,
    TickSourceError,
    WatchlistLoadError,
)
from .models import (
    AccountRecord,
    AuctionSnapshot,
    EntryDecision,
    LedgerEntry,
    OrderBook,
    OrderRecord,
    PlanRow,
    PositionRecord,
    PositionUnit,
    PriceBudget,
    RiskDecision,
    SealInfo,
    SelectedStockRow,
    SellAction,
    SignalPrior,
    TradableEntry,
    TradeRecord,
    WatchlistContext,
)
from .protocols import (
    Clock,
    DataWriter,
    LocalLedger,
    QmtRepository,
    RouterSink,
    SelectedStockSource,
    StructLogger,
    TickSource,
    TradeCalendar,
    XtDataLike,
    XtTraderLike,
)

__all__ = [
    # enums
    "AuctionPhase", "Board", "CentroidTrend", "DataSource", "EntryAction", "OrderPhase",
    "OrderState", "OrderStatus", "PositionMode", "PositionState", "PriceSource", "RiskVerdict",
    "SellActionType", "SnapshotType", "StrEnum", "TradeSide",
    # errors
    "ConnectionNotReadyError", "QmtStrategyError", "RepositoryError", "TickSourceError",
    "WatchlistLoadError",
    # models
    "AccountRecord", "AuctionSnapshot", "EntryDecision", "LedgerEntry", "OrderBook",
    "OrderRecord", "PlanRow", "PositionRecord", "PositionUnit", "PriceBudget", "RiskDecision",
    "SealInfo", "SelectedStockRow", "SellAction", "SignalPrior", "TradableEntry", "TradeRecord",
    "WatchlistContext",
    # protocols
    "Clock", "DataWriter", "LocalLedger", "QmtRepository", "RouterSink", "SelectedStockSource",
    "StructLogger", "TickSource", "TradeCalendar", "XtDataLike", "XtTraderLike",
]
