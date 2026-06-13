"""执行侧自定义异常（锁定契约层）。"""

from __future__ import annotations


class QmtStrategyError(Exception):
    """执行侧错误基类。"""


class TickSourceError(QmtStrategyError):
    """get_full_tick 取数失败（§3.8）：由 auction_poller 主循环捕获走降级（§3.7）。"""


class WatchlistLoadError(QmtStrategyError):
    """watchlist 契约取数失败（§2.6）：由 loader 捕获后进降级态，不抛出拖垮常驻进程。"""


class ConnectionNotReadyError(QmtStrategyError):
    """连接未就绪（connect/subscribe 未返 0，或断线未重连成功，§2.6）。"""


class RepositoryError(QmtStrategyError):
    """回流写库失败（§6.2）：由 data_writer 捕获并按退避重试 + 告警。"""
