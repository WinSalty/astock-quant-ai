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
from datetime import date
from typing import Optional, Tuple

from ..adapters import xt_real
from ..auction.tick_source import XtdataTickSource
from ..common.logger import StructLoggerImpl
from ..common.time_utils import SystemClock, east8_trade_date
from ..common.trade_calendar import WeekdayTradeCalendar
from ..config.settings import Settings
from ..connection.connection_guard import ConnectionGuard
from ..storage.local_stack import LocalStorage
from .main import Engine, EngineDeps, build_engine


def _build_remote_repo(settings: Settings, logger):
    """构造远端 MySQL 仓储（仅盘后同步用，写 qmt_* 四表）。

    缺 DSN → 返回 None（只本地、暂不同步，sync_to_remote 会跳过并告警）。
    安全口径（doc/05 §4.3）：盘中不连远端；同步用仅 qmt_* 四表写权限的独立账号。
    """
    if not settings.mysql_dsn:
        return None
    from ..data_writer.repository import MySqlQmtRepository

    def conn_factory():
        import pymysql  # 惰性 import（CI/测试不强依赖）

        # TODO(实测/落地)：解析 settings.mysql_dsn → pymysql.connect(host/port/user/password/db, charset, ...)。
        #   用仅对 qmt_* 四表有 INSERT/UPDATE/SELECT 权限的独立账号；敏感信息不入库/不进日志（§6.10）。
        raise NotImplementedError("TODO(落地)：由 QMT_MYSQL_DSN 解析并返回 pymysql.connect(...)")

    return MySqlQmtRepository(conn_factory, unique_with_trade_date=settings.repository_unique_with_trade_date)


def _make_session_id_provider():
    """每次重连返回单调递增的新 session_id（旧 session 不复用，§2.2）。"""
    seq = itertools.count(int(time.time()))
    return lambda: next(seq)


def build_real_engine(settings: Settings, logger=None) -> Tuple[Engine, LocalStorage, ConnectionGuard]:
    """装配真实引擎：本地 SQLite 栈 + xtquant 适配器 + Engine + 连接守护。返回 (engine, stack, guard)。

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
    return engine, stack, guard


def main() -> None:
    """真实进程入口骨架（仅目标机运行）。具体时点触发由 Windows 任务计划 / 调度线程驱动（TODO）。"""
    settings = Settings.from_env(os.environ)
    logger = StructLoggerImpl()
    logger.info("engine_boot", config=settings.redacted())     # 脱敏后才打印（§7.1 安全口径）

    engine, stack, guard = build_real_engine(settings, logger)

    # —— 盘前：建连重订阅（connect 返 0 才就绪，§2.2）+ 装载当日名单（需信号侧已盘前同步入本机 SQLite）——
    if not guard.connect_and_subscribe():
        logger.error("engine_not_ready_exit", reason="connect/subscribe 未就绪")
        stack.stop()
        return
    today = east8_trade_date(SystemClock().now_utc())
    engine.prewarm(today)

    # ============================ 调度（TODO：环境相关，由任务计划 / 调度线程驱动）============================
    # 设计 §1.4/§7.5 的时点序列（东八区）；run_forever 接收回调须常驻，故下列时点动作应在【独立线程/定时器】触发：
    #   ~09:00  盘前：guard 重连 + engine.prewarm(today)
    #   09:15-09:25  竞价：engine.run_auction()（仅 QMT_AUCTION_TIMING_ENABLED 实测通过后才真正下竞价单）
    #   盘中周期  engine.sweep_ttl()（超时撤单巡检）
    #   可卖日盘中  engine.run_sell_pass(today, books, session=...)（books 由 xtdata 实时盘口构造，TODO）
    #   15:05/15:30  收盘：engine.close_batch(today)（先 flush 再对账）
    #   盘后  stack.sync_to_remote(today)（本机 SQLite → 远端 MySQL 幂等同步，须当日完成）
    #   盘后  stack.flush() / stack.stop()（停机 drain）
    # TODO(落地)：实现上述调度（推荐：一个调度线程按东八区钟点触发 + Windows 任务计划每日开盘前拉起/重连）。
    # =======================================================================================================

    # —— 常驻：接收 xttrader 推送（成交/委托/资产/持仓回调），进程退出即丢推送（§2.2）——
    # connect_and_subscribe 成功后 current_trader 已就绪；run_forever 阻塞驻留接收回调。
    trader = guard.current_trader
    if trader is not None:
        trader.run_forever()


if __name__ == "__main__":
    main()
