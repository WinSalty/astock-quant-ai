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
import random
import time
from datetime import date, time as dtime
from typing import Optional, Tuple

from ..adapters import xt_real
from ..auction.tick_source import XtdataTickSource
from ..common.logger import StructLoggerImpl
from ..common.time_utils import SystemClock, east8_trade_date
from ..common.trade_calendar import StaticTradeCalendar, WeekdayTradeCalendar
from ..config.settings import Settings
from ..connection.connection_guard import ConnectionGuard
from ..storage.local_stack import LocalStorage
from .main import Engine, EngineDeps, build_engine
from .scheduler import DailyScheduler, parse_hhmm


def _build_signal_client(settings: Settings, logger, *, purpose: str = "unified"):
    """构造信号侧 HTTP 客户端（base_url + token + 超时）。

    按接口选 token（评审三轮 XCUT-01）：信号侧支持 watchlist 导出与 qmt 回流写两套独立 token，执行侧据此分接口
    取 token——purpose='watchlist'→resolve_watchlist_token、'ingest'→resolve_ingest_token、其余→统一 signal token；
    三者缺省都回落统一 signal_internal_token，故默认配置仍自洽、运维隔离权限时各端用各自 token 不互相 401。
    缺 base_url → 返回 None。未配 token 时记告警但仍返回 client（信号侧以 401 兜底，便于连通性排查）。
    """
    if not settings.signal_base_url:
        return None
    from ..common.http_client import SignalHttpClient

    if purpose == "watchlist":
        token = settings.resolve_watchlist_token()
    elif purpose == "ingest":
        token = settings.resolve_ingest_token()
    else:
        token = settings.resolve_signal_token()
    if not token:
        logger.warn(
            "signal_client_no_token",
            purpose=purpose,
            note="对应接口 token 未配置（QMT_SIGNAL_*_TOKEN(_FILE) / QMT_SIGNAL_INTERNAL_TOKEN(_FILE)），将被信号侧 401",
        )
    return SignalHttpClient(settings.signal_base_url, token, settings.http_timeout_seconds, logger)


def _build_decision_emitter(settings: Settings, logger, account_id: str, clock):
    """构造并启动决策采集器（复盘用、best-effort、与交易热路径物理隔离）。

    口径：用独立的信号侧 HTTP 客户端做 sink（不与盘后 RemoteSyncJob 的 client 共享），逐行 POST
        qmt_decision_log；失败由采集器内部 warn 后丢弃（绝不重试到死、绝不影响交易/四表回流/对账）。
        缺 signal_base_url（无回流通道）或 decision_log_enabled=False → 返回 no-op 采集器（不起线程）。
    """
    from ..decision.decision_emitter import DECISION_TABLE, DecisionEmitter
    from ..storage.http_ingest_repository import INGEST_PATH

    # 决策日志走回流 ingest 端点 → 用 ingest token（评审三轮 XCUT-01）。
    client = _build_signal_client(settings, logger, purpose="ingest")

    def _sink(rows):
        # 逐行 POST 决策记录；首个失败上抛 → 采集器 _flush 捕获并 warn 后丢弃该批（数据可丢失）。
        for row in rows:
            client.post_json(
                INGEST_PATH,
                {
                    "account_id": row.get("account_id"),
                    "trade_date": row.get("trade_date"),
                    "records": [{"table": DECISION_TABLE, "data": row}],
                },
            )

    emitter = DecisionEmitter(
        account_id, clock, logger,
        sink=(_sink if client is not None else None),
        enabled=settings.decision_log_enabled,
        queue_size=settings.decision_log_queue_size,
        batch_size=settings.decision_log_batch_size,
    )
    emitter.start()
    return emitter


def _build_remote_repo(settings: Settings, logger):
    """构造盘后回流的「远端写端」（实现 contracts.QmtRepository），供 RemoteSyncJob 注入。

    选路（doc/07）：
    1) 优先 **HTTP 回流**：配了 signal_base_url → HttpIngestQmtRepository（POST /api/internal/qmt/ingest）；
       盘中不连远端、回流逐行幂等 POST，是当前默认方案。
    2) 回落 **直连 MySQL**（旧方案 A）：仅配了 QMT_MYSQL_DSN（未配 signal_base_url）时启用。
    3) 都没配 → None（只本地、暂不同步，sync_to_remote 跳过并告警）。
    """
    # 1) HTTP 回流优先（POST /internal/qmt/ingest → 用 ingest token，评审三轮 XCUT-01）。
    client = _build_signal_client(settings, logger, purpose="ingest")
    if client is not None:
        from ..storage.http_ingest_repository import HttpIngestQmtRepository

        return HttpIngestQmtRepository(client, logger)

    # 2) 回落直连 MySQL（旧方案 A）：当前未落地，配 DSN 即启动期 fail-closed 拒启（国金对接核对 F12）。
    #    背景：原实现 conn_factory 抛 NotImplementedError 被推迟到盘后首行 upsert 才触发，导致「配了
    #    QMT_MYSQL_DSN 却整条回流静默全失败、无 error 告警、运维以为正常」。生产默认走 HTTP 回流
    #    (signal_base_url)，本直连通道未实现；显式配 DSN 即在装配期清晰报错，避免误以为 MySQL 回落可用。
    if settings.mysql_dsn:
        raise RuntimeError(
            "QMT_MYSQL_DSN 指定了 MySQL 直连回落通道，但该通道(MySqlQmtRepository.conn_factory)尚未落地。"
            "请改用 HTTP 回流：配置 QMT_SIGNAL_BASE_URL + ingest token；或先实现 conn_factory(解析 DSN→"
            "pymysql.connect，仅 qmt_* 四表写权限独立账号、敏感信息不入日志)后再启用本通道。"
        )

    # 3) 未配任何远端写端。
    return None


def _make_session_id_provider(settings: Optional[Settings] = None):
    """每次重连返回单调递增的新 session_id（旧 session 不复用，§2.2 / 评审三轮 EXEC-sched-07）。

    业务意图：起点不能用秒级 int(time.time())——同秒重启/多进程会与历史 session 区间重叠，违反
    「每次重连必须用全新且不与历史重叠的 session」（复用已断 session 可能订阅静默失效却无回报）。
    口径：基底取「毫秒低位（同秒重启高度可分）+ 随机位」并收口到正 int32（< 2^31），再 itertools 单调
    自增。**必须收口到正 int32**：xtquant 的 session_id 是 C 层整型量级，若用 ms<<高位 的大整数会被
    静默截断、丢掉高位单调性反而更易碰撞（评审三轮 P0-1）。
    - 毫秒低 15 位：同秒重启（毫秒差 <1000ms < 32768）必然可分；
    - 15 位随机：同一毫秒窗内的并发/重启再加一层离散，两次启动区间重叠概率 ~N/2^15（N=单进程重连数，极小）；
    - QMT_SESSION_ID：显式配置时作偏置叠加（接入原死配置，便于人工指定基底排查）。
    边界（TODO 实测）：xtquant 实际接受的位宽以目标机为准，必要时调整掩码（当前取正 int32 0x7FFFFFFF 保守安全）。
    """
    ms = int(time.time() * 1000)
    base = ((ms & 0x7FFF) << 15) | random.getrandbits(15)  # 30 位：毫秒低位 + 随机位
    if settings is not None and settings.session_id:
        base += int(settings.session_id)
    base &= 0x7FFFFFFF  # 收口到正 int32，杜绝高位溢出被 C 层截断
    seq = itertools.count(base)
    # 自增也夹到正 int32（长周期重连防越界回绕；单日重连量级远不及，仅兜底）。
    return lambda: next(seq) & 0x7FFFFFFF


def _load_trade_calendar_days(path: str) -> list:
    """从交易日清单文件读取交易日（每行一个 ISO 日期 YYYY-MM-DD，# 开头或空行忽略）。

    文件由信号侧 a_trade_calendar（is_open=1）导出/同步而来，含法定节假日，是执行侧 T+1/名单键/
    对账日推算的权威来源。解析失败的行跳过；得到空集时由调用方按"无可用日历"处理。
    """
    days: list = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                days.append(date.fromisoformat(s))
            except ValueError:
                continue
    return days


def _write_trade_calendar_days(path: str, days) -> None:
    """把全量交易日写回本地清单文件（doc/29 J-3：盘前从信号侧补取后落盘，下次启动可用）。

    与 _load_trade_calendar_days 同格式（每行一个 ISO 日期，首行注释）；升序去重写出。整体覆盖写，
    使本地文件始终是「内存日历的最新全量快照」。调用方对写失败仅告警不阻断（内存日历本轮已更新）。
    """
    ordered = sorted({d for d in days})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# 交易日历(执行侧本地副本)：J-3 盘前自动补取自信号侧 /api/internal/trade_calendar；每行一个 ISO 日期\n")
        for d in ordered:
            fh.write(d.isoformat() + "\n")


def _build_calendar(settings: Settings, logger):
    """装配交易日历（评审 P0-E1/3.1：生产禁止静默用周末近似日历）。

    口径（fail-closed）：
    1) 配了 trade_calendar_file 且能读到 ≥1 个交易日 → StaticTradeCalendar（与信号侧 a_trade_calendar
       同源、含节假日），日推算正确；
    2) 未配/读空：
       - allow_weekday_calendar=True（仅离线/测试显式开）→ WeekdayTradeCalendar + 强告警；
       - 否则 → 直接抛 RuntimeError 拒绝启动，强制生产提供真实交易日历，绝不让"节假日当交易日"的
         近似日历静默进实盘（会致 T+1 可卖日偏早、跨假日名单键/对账错位）。
    """
    path = settings.trade_calendar_file
    if path:
        try:
            days = _load_trade_calendar_days(path)
        except OSError as exc:
            raise RuntimeError(f"交易日历文件无法读取: {path} ({exc})") from exc
        if days:
            logger.info("trade_calendar_loaded", source=path, days=len(days))
            return StaticTradeCalendar(days)
        logger.error("trade_calendar_file_empty", source=path)

    if settings.allow_weekday_calendar:
        logger.warn(
            "trade_calendar_weekday_fallback",
            reason="未提供 QMT_TRADE_CALENDAR_FILE，退化为仅排周末的近似日历（节假日会被当交易日，仅限测试）",
        )
        return WeekdayTradeCalendar()

    raise RuntimeError(
        "未配置真实交易日历（QMT_TRADE_CALENDAR_FILE 缺失/为空）。生产禁止用周末近似日历"
        "（会把节假日当交易日，致 T+1 可卖日偏早/名单键/对账错位）。请提供与信号侧 a_trade_calendar"
        "同源的交易日清单文件；仅离线测试可设 QMT_ALLOW_WEEKDAY_CALENDAR=true 显式放行。"
    )


def _build_prior_provider(stack: LocalStorage, logger):
    """构造续板先验取数 (ts_code, today) -> Optional[SignalPrior]（阶段0-C T0.2）。

    业务意图：从本机当日 watchlist（SelectedStockRow，target_trade_date=today）映射出续板先验，供持仓
    续持决策（position_manager.refresh_state 定 SIGNAL_DRIVEN/TECH_EXIT、sell_decider 吃 continuation_prob）。
    取不到（持仓不在今日名单=次日没续板/退出信号池）→ 返回 None = 纯技术退出（安全方向正确）。

    按日缓存：先验在盘前 refresh_state 与盘中每轮卖出评估被【逐票】调用，若每次重查全表开销大；这里按
    target_trade_date 缓存一次当日名单映射（{norm_code: SignalPrior}）。调用方全在【调度线程】单线程串行
    （prewarm / run_sell_pass 均由 DailyScheduler.run 驱动），故无需加锁。
    边界：fetch 异常 → 当日按空名单处理（降级为全 TECH_EXIT，不抛、不拖垮调度）；代码归一后匹配。
    """
    from ..common.identity import resolve_code
    from ..contracts.models import SignalPrior

    cache: dict = {"day": None, "by_code": {}}

    def _provider(ts_code: str, today: date):
        # 当日名单未缓存或跨日 → 重建一次（fetch 当日 target_trade_date=today 的 SelectedStockRow）。
        if cache["day"] != today:
            by_code: dict = {}
            try:
                rows = stack.watchlist_source.fetch(today)
            except Exception as exc:  # noqa: BLE001 取名单失败按空名单降级(全 TECH_EXIT)，不拖垮调度
                logger.warn("prior_provider_fetch_failed", today=str(today), error=str(exc))
                rows = []
            for r in rows:
                code = resolve_code(r.ts_code)
                if code is None:
                    continue
                by_code[code] = SignalPrior(
                    ts_code=code,
                    trade_date=r.trade_date,
                    target_trade_date=r.target_trade_date,
                    continuation_prob=r.continuation_prob,
                    boost=r.boost,
                    fail_conditions=list(r.fail_conditions or []),
                    market_state=r.market_state,
                    role=r.role,
                    strategy=r.strategy,
                    # 口径变更（2026-06-21）：原此处把信号侧缺测标记搬进 SignalPrior 供 B3「缺测持仓强卖」，
                    # 现 B3 已下线——卖出完全交由执行侧 xtdata 实时盘口扳机裁决，先验不再携带缺测字段。
                    # 缺测仅在买入侧拦截（SelectedStockRow.data_missing → buy_prefilter/entry_router 放弃买入，B2 保留）。
                )
            cache["day"] = today
            cache["by_code"] = by_code
        return cache["by_code"].get(resolve_code(ts_code))

    return _provider


def build_real_engine(settings: Settings, logger=None) -> Tuple[Engine, LocalStorage, ConnectionGuard, SystemClock, object]:
    """装配真实引擎：本地 SQLite 栈 + xtquant 适配器 + Engine + 连接守护。

    返回 (engine, stack, guard, clock, calendar)——后两者供 main 装配调度器复用同一时钟/日历。

    仅目标机可成功（xtquant 工厂在此触发）；缺 xtquant 时 make_stock_account/import_xtdata 抛清晰 RuntimeError。
    """
    logger = logger or StructLoggerImpl()
    clock = SystemClock()
    # —— 启动期 fail-closed 配置校验（go-live 审计 / 国金对接核对 F06/F07）——
    # 参照交易日历 fail-closed 模式：account_id / mini_path 是「真金账户」与「miniQMT 连接」的根口径，
    # 缺配绝不静默回落。原 `account_id or "UNKNOWN"` 会把假账户号贯穿台账/持仓单元键/qmt_* 四表/回流信封/
    # 对账，造成真实资金口径污染、串账户；mini_path 缺配传空串则建 trader 行为不可控。缺失直接拒启动，
    # 强制由 env（run_qmt.bat）显式提供。注：本校验先于本地栈/xtquant 触发，缺配即快速失败。
    if not (settings.account_id and settings.account_id.strip()):
        raise RuntimeError(
            "未配置 QMT_ACCOUNT_ID（QMT 资金账号）。缺配会用假账户号污染台账/qmt_* 四表/回流/对账，拒绝启动；"
            "请在 env（run_qmt.bat）显式配置真实资金账号。"
        )
    if not (settings.mini_path and settings.mini_path.strip()):
        raise RuntimeError(
            "未配置 QMT_MINI_PATH（miniQMT userdata_mini 路径）。缺配会以空路径建 trader、连接行为不可控，"
            "拒绝启动；请配置如 D:\\国金证券QMT交易端\\userdata_mini。"
        )
    # 竞价择时未实测放行 fail-closed（§7.1.6）：auction_timing_enabled=True 但未 verified 即拒启动。
    settings.assert_safe_to_trade()
    # account_loss_limit 接线缺口显式告警（评审 F06）：QMT_ACCOUNT_LOSS_LIMIT(当日已实现亏损闸)配置项存在、
    # gate 内有对应击穿分支，但生产买入/卖出路径均喂 account_realized_loss=None → 该闸配了也永不触发，运维易误以为
    # 已有「当日已实现亏损」独立熔断。当前设计由 total_asset 日内回撤闸(QMT_ACCOUNT_DRAWDOWN_LIMIT)综合承载
    # 已实现+浮动亏损，故已实现亏损不单列接线。这里在配了该项时启动期强告警，避免误信双闸而把回撤闸配松。
    if settings.account_loss_limit is not None:
        logger.warn(
            "account_loss_limit_not_independently_wired",
            value=str(settings.account_loss_limit),
            note="QMT_ACCOUNT_LOSS_LIMIT 当前未独立接线(已由 total_asset 回撤闸 QMT_ACCOUNT_DRAWDOWN_LIMIT 综合承载)，"
                 "单配本项不会独立触发已实现亏损熔断；如需账户级日内止损请配置 QMT_ACCOUNT_DRAWDOWN_LIMIT。",
        )
    account_id = settings.account_id

    # —— 本地 SQLite 数据栈：建表 + 起写线程 + 重建台账（重启幂等）——
    remote = _build_remote_repo(settings, logger)
    # 武装写队列健壮性（评审 F07/F09）：从配置注入 max_queue/看门狗，使溢出熔断与 hang 检测在生产真正生效。
    stack = LocalStorage(
        settings.local_db_path, logger, account_id, remote_repo=remote,
        write_queue_max=settings.write_queue_max,
        write_queue_stuck_seconds=settings.write_queue_stuck_seconds,
    )
    # 传 east8 当日启用台账窗口装载（评审三轮 EXEC-storage-05）：只重建近 N 日活跃单，防跨日只增不减。
    stack.start(today=east8_trade_date(clock.now_utc()))

    # —— TraderHolder：引擎始终指向当前 trader（重连换实例不影响下单/查询）——
    holder = xt_real.TraderHolder()
    account = xt_real.make_stock_account(account_id)            # 触发 xtquant（缺则清晰报错）
    tick_source = XtdataTickSource(xt_real.import_xtdata())     # 触发 xtquant
    calendar = _build_calendar(settings, logger)

    # —— 决策采集器（复盘用、best-effort、与交易热路径物理隔离，整条崩溃也不影响交易）——
    decision_emitter = _build_decision_emitter(settings, logger, account_id, clock)

    # prior_provider（阶段0-C T0.2）：读本机当日 watchlist 得续板先验（SignalPrior），喂持仓续持决策；
    # 取不到（次日没续板/退出信号池）→ None = 纯技术退出。不接则所有持仓恒 TECH_EXIT、续板增强不生效。
    prior_provider = _build_prior_provider(stack, logger)
    deps = EngineDeps(
        settings=settings, clock=clock, logger=logger, calendar=calendar,
        trader=holder, account=account, account_id=account_id,
        tick_source=tick_source, selected_source=stack.watchlist_source,
        repository=stack.repository, ledger=stack.ledger, flush_hook=stack.flush,
        decision_emitter=decision_emitter, prior_provider=prior_provider,
    )
    engine = build_engine(deps)

    # —— 存储 fail-closed 接线（评审二轮 P0#2）——
    # 写线程死亡 / 关键落盘失败 → on_failure 回调 engine.on_storage_failure 置"存储不健康"→停开新仓 + 强告警；
    # 并注入健康探测器供调度周期体检（静默死亡且当轮无 submit 时的兜底发现）。Engine 在 LocalStorage 之后
    # 装配，故二者均在此装配末尾接线。
    stack.set_on_failure(engine.on_storage_failure)
    # 周期体检的 latching 永久 fail-closed 只认【持续性致命故障】（执行-4 修正 2026-06-22）：用
    # is_persistently_failed（线程死/熔断/卡死）而非 is_healthy（含瞬时 degraded）——否则一次无害的瞬时写抖动
    # 撞上 15 秒一次的体检就会把存储永久置不可用、冻结当天剩余全部开仓直到重启。瞬时 degraded 仍由逐单
    # 关键落盘检查（可恢复、不 latch）把关。
    engine.set_storage_health_checker(lambda: not stack.is_persistently_failed())

    # —— 连接守护：trader_factory 顺手 set 进 holder；回调用 ABI 适配壳包住引擎 ExecCallback ——
    callback = xt_real.make_trader_callback(engine.callback)    # 触发 xtquant
    trader_factory = xt_real.make_trader_factory(settings.mini_path, holder)
    guard = ConnectionGuard(
        trader_factory=trader_factory, account=account, callback=callback,
        clock=clock, logger=logger, session_id_provider=_make_session_id_provider(settings),
        on_reconnect_backfill=engine.on_reconnect_backfill,     # 重连就绪后触发当日 query_* 补采
        # 解冻/冻结下单闸的权威源（评审三轮 EXEC-sched-02）：连接就绪→report_trade_conn(True)、
        # 断线/连接失败→report_trade_conn(False)，不再由一次注定失败的补采独占管理。
        on_connection_state=engine.report_trade_conn,
    )
    # 断线钩子延迟回填（评审三轮 EXEC-sched-01）：Engine（含 ExecCallback）先于 guard 装配，构造期拿不到
    # guard 引用；此处 guard 已就位，回填 callback 的断线钩子为「先经 guard 换新 session 重连」——
    # 真实断线链路 xt_real._RealCallback.on_disconnected → ExecCallback.on_disconnected → guard.on_disconnected
    # → guard.reconnect（换新 session、connect/subscribe 成功）→ guard 触发 engine.on_reconnect_backfill 补采。
    engine.callback.set_on_disconnected_hook(guard.on_disconnected)
    # 心跳探到「静默死亡」首次冻结时请求重连（评审三轮 P1-1）：把它当断线交 guard 换 session 重连重订阅，
    # 让连接就绪事件接管解冻，避免心跳冻结后无解冻路径而整日永久冻结。
    engine.set_reconnect_requester(guard.on_disconnected)
    return engine, stack, guard, clock, calendar


# 主循环退避/告警常量（评审三轮 EXEC-sched-09）。
_RECONNECT_BACKOFF_BASE = 5.0    # 未就绪首次重连后的退避基数秒
_RECONNECT_BACKOFF_MAX = 60.0    # 退避上限秒（指数退避封顶，避免 busy-loop 也避免过久不重试）
_NOT_READY_ALERT_THRESHOLD = 6   # 连续未就绪达该次数即强告警「连接长期未就绪」


def _supervise_once(guard, logger, state: dict) -> Tuple[bool, object, dict]:
    """主循环一轮连接监督（评审三轮 EXEC-sched-03/09）：纯逻辑、可单测（不真正 run_forever）。

    业务意图：原主循环未就绪时仅 sleep(5) 空转，从不主动重连/告警，盘前一次性建连失败即整日无连接。
    这里取 guard 的一致快照 (ready, trader)：
    - 就绪 → 返回 (True, trader, 清零计数)，由调用方对该 trader run_forever（本函数不阻塞）；
    - 未就绪 → 主动退避重连 guard.reconnect()、未就绪计数 +1、达阈值强告警，返回 (False, None, 新计数)。
    把「连接未就绪」与「watchlist 未装载」分开：后者属 scheduler.prewarm 的 fired 语义，不在此混判。
    """
    ready, trader = guard.snapshot()
    if ready and trader is not None:
        return True, trader, {"not_ready_streak": 0}
    streak = state.get("not_ready_streak", 0) + 1
    try:
        # 主动退避重连（guard 内部重走全序、幂等；与 scheduler PREWARM 的建连叠加亦安全，TODO 实测口径）。
        guard.reconnect()
    except Exception as e:  # noqa: BLE001 重连异常不终止主循环
        logger.error("run_supervise_reconnect_error", error=repr(e))
    if streak >= _NOT_READY_ALERT_THRESHOLD:
        logger.error(
            "run_loop_persistently_disconnected",
            not_ready_streak=streak,
            note="连接长期未就绪：已退避主动重连仍未成功，请人工排查 miniQMT 链路/账号",
        )
    return False, None, {"not_ready_streak": streak}


def main() -> None:
    """真实进程入口骨架（仅目标机运行）。具体时点触发由 Windows 任务计划 / 调度线程驱动（TODO）。"""
    settings = Settings.from_env(os.environ)
    logger = StructLoggerImpl()
    logger.info("engine_boot", config=settings.redacted())     # 脱敏后才打印（§7.1 安全口径）

    engine, stack, guard, clock, calendar = build_real_engine(settings, logger)

    # —— 盘前 watchlist 拉取钩子（doc/07）：配了 signal_base_url 才启用——
    # PREWARM 时先从信号侧 GET /internal/watchlist 拉当日名单落本机 SQLite，再让 engine.prewarm 装载。
    watchlist_prefetch = None
    # GET /internal/watchlist → 用 watchlist token（评审三轮 XCUT-01）。
    signal_client = _build_signal_client(settings, logger, purpose="watchlist")
    if signal_client is not None:
        from ..watchlist.remote_watchlist import WatchlistPrefetcher

        prefetcher = WatchlistPrefetcher(signal_client, calendar, stack.save_watchlist, logger)
        watchlist_prefetch = prefetcher.prefetch

    # 盘前交易日历校验/补取（doc/29 J-3）：复用 watchlist 同源 signal_client（同 token）。本地日历不足时拉信号侧
    # /api/internal/trade_calendar 补取并入【共享 calendar 对象】+ 落回 trade_calendar_file（配了才落盘）。
    # 缺 signal_client（未配信号侧）→ refresher=None，调度跳过：保持「只用本地静态日历」的原行为，耗尽仍走 engine
    # 的 trading_days_left fail-closed 兜底（J-3 前的口径不退化）。
    calendar_refresh = None
    if signal_client is not None:
        from ..watchlist.trade_calendar_refresh import TradeCalendarRefresher

        _cal_file = settings.trade_calendar_file
        _persist_fn = (
            (lambda days, _p=_cal_file: _write_trade_calendar_days(_p, days)) if _cal_file else None
        )
        calendar_refresh = TradeCalendarRefresher(
            signal_client, calendar, persist_fn=_persist_fn, logger=logger
        ).ensure_coverage

    # —— 启动调度线程：按东八区钟点触发 盘前装载 / 竞价 / 盘中巡检 / 收盘对账 / 盘后同步（设计 §7.5）——
    # 调度细节全在 DailyScheduler；本入口只负责装配与「主线程 run_forever 接收回调」。
    # 卖出链接线（阶段0-C T0.1）+ 生产门控（fail-closed，国金对接核对 B1/H1）：
    #   注入 engine.build_sell_books（由 xtdata 实时盘口派生 {ts_code: OrderBook}）后，盘中/竞价段才会真正跑
    #   run_sell_pass（止损/弱开/秒板续持/尾盘了结，§5.3）。但最小版跨帧破位/炸板未保真、且不在今日 watchlist
    #   的真封板隔夜票会被误判非封板 → **默认门控关（sell_pass_live=False）即 provider=None，回退到接线前的
    #   安全行为（盘中只 sweep_ttl、不自动卖）**，杜绝误清仓/批量误清；待阶段1 T1.2 保真 + 真机实测后，显式配
    #   QMT_SELL_PASS_LIVE=true 开启（届时 assert_safe_to_trade 已强制要求配单票浮亏止损）。
    if settings.sell_pass_live:
        sell_books_provider = engine.build_sell_books
        logger.warn(
            "sell_pass_live_enabled",
            note="盘中卖出链已开启(QMT_SELL_PASS_LIVE=true)；确认已完成 T1.2 跨帧盘口保真 + 真机实测",
        )
    else:
        sell_books_provider = None
        logger.info(
            "sell_pass_gated_off",
            note="卖出链已接线但生产门控默认关(QMT_SELL_PASS_LIVE=false)，盘中不自动卖出；T1.2 保真+实测后再开",
        )
    scheduler = DailyScheduler(
        engine, guard, stack, clock, logger, calendar=calendar,
        close_time=parse_hhmm(settings.close_snapshot_time, dtime(15, 5)),
        sell_books_provider=sell_books_provider,
        watchlist_prefetch=watchlist_prefetch,
        calendar_refresh=calendar_refresh,
    )
    scheduler.start_thread()
    logger.info("scheduler_started")

    # —— 主线程常驻：取连接一致快照，就绪则 run_forever 接收回调，未就绪则主动退避重连+告警（§2.2）——
    # 注：run_forever / 断线重连(on_disconnected→guard.reconnect 换 trader) 的交互依 QMT 运行期行为，
    #     具体阻塞/返回语义须目标机实测，这里给出常驻骨架（TODO(实测)）；监督逻辑抽到 _supervise_once 可单测。
    supervise_state: dict = {"not_ready_streak": 0}
    while True:
        ready, trader, supervise_state = _supervise_once(guard, logger, supervise_state)
        if ready and trader is not None:
            try:
                trader.run_forever()  # 阻塞接收回调；返回（断线）后回到循环，由 _supervise_once 退避重连
            except Exception as e:  # noqa: BLE001 run_forever 异常不应直接终止进程
                logger.error("run_forever_error", error=repr(e))
            time.sleep(1)  # run_forever 返回后短暂让步再监督（断线由钩子触发重连，这里兜底再探）
        else:
            # 未就绪：按连续未就绪次数指数退避（带上限），避免 busy-loop 又避免长久不重试。
            streak = supervise_state.get("not_ready_streak", 1)
            backoff = min(_RECONNECT_BACKOFF_BASE * (2 ** (streak - 1)), _RECONNECT_BACKOFF_MAX)
            time.sleep(backoff)


if __name__ == "__main__":
    main()
