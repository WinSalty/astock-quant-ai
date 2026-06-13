"""真实进程入口（仅 Windows + miniQMT 实际运行）：装配 SQLite 本地栈 + xtquant 适配器 + Engine 并拉起。

跨平台安全：xtquant / pymysql 等真实依赖经适配器惰性 import，故本模块在任意平台可 import（供测试/工具），
但 ``build_real_engine`` / ``main`` 调用 xtquant 工厂时只能在目标机成功运行。

装配关系（关键）：用 TraderHolder 让 OrderExecutor / SnapshotJob 始终指向「当前」trader——
ConnectionGuard 重连换新 session/trader 时，trader_factory 顺手把新 trader set 进 holder，引擎无感知。

调度（盘前重连+装载 / 竞价轮询 / TTL 巡检 / 卖出 / 收盘快照+对账 / 盘后同步）由 Windows 任务计划触发或
主循环驱动，属环境相关，下方 main() 以 TODO 标注调用序列（设计 §1.4 / §7.5）。
"""

from __future__ import annotations

import itertools
import os
import time
from datetime import date, time as dtime
from typing import Optional, Tuple

from ..adapters import xt_real
from ..auction.tick_source import XtdataTickSource
from ..common.logger import StructLoggerImpl
from ..common.time_utils import SystemClock
from ..common.trade_calendar import WeekdayTradeCalendar
from ..config.settings import Settings
from ..connection.connection_guard import ConnectionGuard
from ..storage.local_stack import LocalStorage
from .main import Engine, EngineDeps, build_engine
from .scheduler import DailyScheduler, parse_hhmm


def _build_signal_client(settings: Settings, logger):
    """构造信号侧 HTTP 客户端（base_url + token + 超时），供盘后回流与盘前 watchlist 拉取共用。

    缺 base_url → 返回 None（不走 HTTP 通道）。token 经 resolve_signal_token（文件优先）解析；
    未配 token 时记告警但仍返回 client（信号侧会以 401 兜底，便于本机连通性排查）。
    """
    if not settings.signal_base_url:
        return None
    from ..common.http_client import SignalHttpClient

    token = settings.resolve_signal_token()
    if not token:
        logger.warn(
            "signal_client_no_token",
            note="QMT_SIGNAL_INTERNAL_TOKEN(_FILE) 未配置，HTTP 调用将被信号侧 401",
        )
    return SignalHttpClient(settings.signal_base_url, token, settings.http_timeout_seconds, logger)


def _build_remote_repo(settings: Settings, logger):
    """构造盘后回流的「远端写端」（实现 contracts.QmtRepository），供 RemoteSyncJob 注入。

    选路（doc/07）：
    1) 优先 **HTTP 回流**：配了 signal_base_url → HttpIngestQmtRepository（POST /api/internal/qmt/ingest）；
       盘中不连远端、回流逐行幂等 POST，是当前默认方案。
    2) 回落 **直连 MySQL**（旧方案 A）：仅配了 QMT_MYSQL_DSN（未配 signal_base_url）时启用。
    3) 都没配 → None（只本地、暂不同步，sync_to_remote 跳过并告警）。
    """
    # 1) HTTP 回流优先。
    client = _build_signal_client(settings, logger)
    if client is not None:
        from ..storage.http_ingest_repository import HttpIngestQmtRepository

        return HttpIngestQmtRepository(client, logger)

    # 2) 回落直连 MySQL（保留旧通道，仅当显式配 DSN）。
    if settings.mysql_dsn:
        from ..data_writer.repository import MySqlQmtRepository

        def conn_factory():
            import pymysql  # 惰性 import（CI/测试不强依赖）

            # TODO(实测/落地)：解析 settings.mysql_dsn → pymysql.connect(host/port/user/password/db, ...)。
            #   用仅对 qmt_* 四表有 INSERT/UPDATE/SELECT 权限的独立账号；敏感信息不入库/不进日志（§6.10）。
            raise NotImplementedError("TODO(落地)：由 QMT_MYSQL_DSN 解析并返回 pymysql.connect(...)")

        return MySqlQmtRepository(
            conn_factory, unique_with_trade_date=settings.repository_unique_with_trade_date
        )

    # 3) 未配任何远端写端。
    return None


def _make_session_id_provider():
    """每次重连返回单调递增的新 session_id（旧 session 不复用，§2.2）。"""
    seq = itertools.count(int(time.time()))
    return lambda: next(seq)


def build_real_engine(settings: Settings, logger=None) -> Tuple[Engine, LocalStorage, ConnectionGuard, SystemClock, object]:
    """装配真实引擎：本地 SQLite 栈 + xtquant 适配器 + Engine + 连接守护。

    返回 (engine, stack, guard, clock, calendar)——后两者供 main 装配调度器复用同一时钟/日历。

    仅目标机可成功（xtquant 工厂在此触发）；缺 xtquant 时 make_stock_account/import_xtdata 抛清晰 RuntimeError。
    """
    logger = logger or StructLoggerImpl()
    clock = SystemClock()
    account_id = settings.account_id or "UNKNOWN"

    # —— 本地 SQLite 数据栈：建表 + 起写线程 + 重建台账（重启幂等）——
    remote = _build_remote_repo(settings, logger)
    stack = LocalStorage(settings.local_db_path, logger, account_id, remote_repo=remote)
    stack.start()

    # —— TraderHolder：引擎始终指向当前 trader（重连换实例不影响下单/查询）——
    holder = xt_real.TraderHolder()
    account = xt_real.make_stock_account(account_id)            # 触发 xtquant（缺则清晰报错）
    tick_source = XtdataTickSource(xt_real.import_xtdata())     # 触发 xtquant
    # TODO(实测/落地)：换成读信号侧 a_trade_calendar 的 DbTradeCalendar；WeekdayTradeCalendar 会把节假日当交易日。
    calendar = WeekdayTradeCalendar()

    # TODO(落地)：prior_provider 读当日本机 watchlist 表得续板先验（SignalPrior）；缺省 None=纯技术退出。
    deps = EngineDeps(
        settings=settings, clock=clock, logger=logger, calendar=calendar,
        trader=holder, account=account, account_id=account_id,
        tick_source=tick_source, selected_source=stack.watchlist_source,
        repository=stack.repository, ledger=stack.ledger, flush_hook=stack.flush,
    )
    engine = build_engine(deps)

    # —— 连接守护：trader_factory 顺手 set 进 holder；回调用 ABI 适配壳包住引擎 ExecCallback ——
    callback = xt_real.make_trader_callback(engine.callback)    # 触发 xtquant
    trader_factory = xt_real.make_trader_factory(settings.mini_path or "", holder)
    guard = ConnectionGuard(
        trader_factory=trader_factory, account=account, callback=callback,
        clock=clock, logger=logger, session_id_provider=_make_session_id_provider(),
        on_reconnect_backfill=engine.on_reconnect_backfill,     # 重连后触发当日 query_* 补采
    )
    return engine, stack, guard, clock, calendar


def main() -> None:
    """真实进程入口骨架（仅目标机运行）。具体时点触发由 Windows 任务计划 / 调度线程驱动（TODO）。"""
    settings = Settings.from_env(os.environ)
    logger = StructLoggerImpl()
    logger.info("engine_boot", config=settings.redacted())     # 脱敏后才打印（§7.1 安全口径）

    engine, stack, guard, clock, calendar = build_real_engine(settings, logger)

    # —— 盘前 watchlist 拉取钩子（doc/07）：配了 signal_base_url 才启用——
    # PREWARM 时先从信号侧 GET /internal/watchlist 拉当日名单落本机 SQLite，再让 engine.prewarm 装载。
    watchlist_prefetch = None
    signal_client = _build_signal_client(settings, logger)
    if signal_client is not None:
        from ..watchlist.remote_watchlist import WatchlistPrefetcher

        prefetcher = WatchlistPrefetcher(signal_client, calendar, stack.save_watchlist, logger)
        watchlist_prefetch = prefetcher.prefetch

    # —— 启动调度线程：按东八区钟点触发 盘前装载 / 竞价 / 盘中巡检 / 收盘对账 / 盘后同步（设计 §7.5）——
    # 调度细节全在 DailyScheduler；本入口只负责装配与「主线程 run_forever 接收回调」。
    scheduler = DailyScheduler(
        engine, guard, stack, clock, logger, calendar=calendar,
        close_time=parse_hhmm(settings.close_snapshot_time, dtime(15, 5)),
        # TODO(落地)：sell_books_provider 注入「由 xtdata 实时盘口构造 {ts_code: OrderBook}」的函数后，
        #   盘中才会跑 run_sell_pass（卖出决策需实时盘口，§5.3）；缺省只跑 sweep_ttl（超时撤单巡检）。
        sell_books_provider=None,
        watchlist_prefetch=watchlist_prefetch,
    )
    scheduler.start_thread()
    logger.info("scheduler_started")

    # —— 主线程常驻：等连接就绪（调度器 PREWARM 阶段会建连）后 run_forever 接收回调；进程退出即丢推送（§2.2）——
    # 注：run_forever / 断线重连(on_disconnected→guard.reconnect 换 trader) 的交互依 QMT 运行期行为，
    #     具体阻塞/返回语义须目标机实测，这里给出常驻骨架（TODO(实测)）。
    while True:
        if guard.ready and guard.current_trader is not None:
            try:
                guard.current_trader.run_forever()  # 阻塞接收回调；返回（断线）后回到循环等待重连
            except Exception as e:  # noqa: BLE001 run_forever 异常不应直接终止进程
                logger.error("run_forever_error", error=repr(e))
        time.sleep(5)


if __name__ == "__main__":
    main()
