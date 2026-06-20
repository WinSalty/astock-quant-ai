"""执行侧配置（§7.1 配置项清单）。

安全口径（与项目 AGENTS.md 一致）：账户 / token / DSN 等敏感项不硬编码、不入库、不写日志；
从外部环境变量注入（Windows 本地 .env / 系统环境）。``Settings.redacted()`` 用于安全打印
（脱敏后才可进日志）。

强约束：``auction_timing_enabled`` 默认 False——在 §7.2 竞价数据能力真实交易日实测通过前不可开
（详见 §7.1.6）。``kill_switch`` 为全局熔断：True 则只采集不下单。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import List, Mapping, Optional

# 敏感字段名集合：redacted() 打印时一律脱敏，禁止进日志（§7.1 / AGENTS.md）。
_SENSITIVE = {
    "account_id",
    "mysql_dsn",
    "mini_path",
    "writeback_token",
    "ingest_token",
    "writeback_base_url",
    "signal_internal_token",
}


def _as_bool(v) -> bool:
    """环境变量字符串转 bool：'1'/'true'/'yes'/'on'（大小写不敏感）为真。"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _as_decimal(v) -> Optional[Decimal]:
    if v is None or str(v).strip() == "":
        return None
    return Decimal(str(v))


def _as_int(v) -> Optional[int]:
    if v is None or str(v).strip() == "":
        return None
    return int(str(v))


def _as_float(v) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    return float(str(v))


@dataclass
class Settings:
    """执行侧运行配置。字段名为业务语义；对应环境变量见每项后注释。"""

    # —— 7.1.1 账户与连接（敏感，不入库/不进日志）——
    account_id: Optional[str] = None          # QMT_ACCOUNT_ID：QMT 资金账号（落 qmt_*.account_id）
    # QMT_ACCOUNT_TYPE：账号类型（保留·当前未接入 StockAccount 构造）。是否需透传待 §A10 国金真机确认——
    # 纯现货账户单参 StockAccount(account_id) 即可（account_type 默认 'STOCK'）；两融账户需传 'CREDIT'。
    # 注意官方第二参是字符串('STOCK'/'CREDIT')，与此处 int 解析口径不一致，启用前须一并改为字符串映射（见 doc/16 T0.5）。
    account_type: Optional[int] = None        # QMT_ACCOUNT_TYPE
    mini_path: Optional[str] = None           # QMT_MINI_PATH：userdata_mini 路径
    session_id: Optional[int] = None          # QMT_SESSION_ID：交易 session id（重连须换新）
    mysql_dsn: Optional[str] = None           # QMT_MYSQL_DSN：仅 qmt_* 四表写权限的独立账号连接串

    # —— 7.1.2 信号侧写回接口（仅 B 方案启用，默认空）——
    writeback_base_url: Optional[str] = None  # QMT_WRITEBACK_BASE_URL
    writeback_token: Optional[str] = None     # QMT_WRITEBACK_TOKEN
    ingest_token: Optional[str] = None        # QMT_INGEST_TOKEN（/ingest 服务端校验）

    # —— 交易日历（评审 P0-E1/3.1）：生产须用与信号侧 a_trade_calendar 同源的真实交易日 ——
    # trade_calendar_file：交易日清单文件路径（每行一个 ISO 日期 YYYY-MM-DD，从信号侧 a_trade_calendar
    #   导出/同步）。配置后用 StaticTradeCalendar 读取，含法定节假日，T+1/名单键/对账才正确。
    # allow_weekday_calendar：未提供日历文件时是否允许退化为「仅排周末」的 WeekdayTradeCalendar。
    #   默认 False=fail-closed（拒绝启动），强制生产提供真实日历；仅离线/测试可显式置 True。
    trade_calendar_file: Optional[str] = None  # QMT_TRADE_CALENDAR_FILE
    allow_weekday_calendar: bool = False       # QMT_ALLOW_WEEKDAY_CALENDAR

    # —— watchlist 契约来源（§1.2.1 二选一，默认 A 直读表）——
    watchlist_source: str = "DB"              # QMT_WATCHLIST_SOURCE：DB（A）/ HTTP（B）
    watchlist_api_url: Optional[str] = None   # QMT_WATCHLIST_API_URL：B 方案只读接口地址

    # —— 信号侧 HTTP 内网接口（doc/07：盘前 GET watchlist + 盘后 POST 回流，两接口同源同 token）——
    signal_base_url: Optional[str] = None     # QMT_SIGNAL_BASE_URL：信号侧服务根，如 http://1.2.3.4:8000
    signal_internal_token: Optional[str] = None      # QMT_SIGNAL_INTERNAL_TOKEN：X-Internal-Token 值（统一回落）
    signal_internal_token_file: Optional[str] = None  # QMT_SIGNAL_INTERNAL_TOKEN_FILE：token 落盘文件（优先）
    # 按接口独立 token（评审三轮 XCUT-01）：信号侧支持 watchlist 导出与 qmt 回流写两套独立 token；执行侧据此
    # 也支持分接口配置——缺省回落统一 signal_internal_token，需隔离权限时分别配置。绝不让一个接口的 401 拖垮另一个。
    signal_watchlist_token: Optional[str] = None       # QMT_SIGNAL_WATCHLIST_TOKEN：GET /internal/watchlist 专用
    signal_watchlist_token_file: Optional[str] = None  # QMT_SIGNAL_WATCHLIST_TOKEN_FILE
    signal_ingest_token: Optional[str] = None          # QMT_SIGNAL_INGEST_TOKEN：POST /internal/qmt/ingest 专用
    signal_ingest_token_file: Optional[str] = None     # QMT_SIGNAL_INGEST_TOKEN_FILE
    http_timeout_seconds: float = 10.0        # QMT_HTTP_TIMEOUT_SECONDS：单次接口超时秒数

    # —— 7.1.3 竞价轮询与采集节奏 ——
    # 注：原 auction_window / intraday_snapshot_minutes 两项已删除（国金对接核对 F09）——真实竞价时段
    # 由 common/auction_window.py 硬编码（交易所固定结构、不可配），盘中快照节奏由调度 poll 驱动，二者
    # 从无消费者，属「假可配置」误导运维，故移除。
    auction_poll_interval_sec: float = 3.0    # QMT_AUCTION_POLL_INTERVAL_SEC（默认 3s，§7.1.3）
    close_snapshot_time: str = "15:05"        # QMT_CLOSE_SNAPSHOT_TIME

    # —— 7.1.4 各战法阈值与开关（对齐竞价观察清单口径）——
    auction_abandon_pct: Optional[Decimal] = None   # QMT_AUCTION_ABANDON_PCT：弱于该幅度放弃
    auction_lowbuy_pct_low: Optional[Decimal] = None  # QMT_AUCTION_LOWBUY_PCT_LOW
    auction_lowbuy_pct_high: Optional[Decimal] = None  # QMT_AUCTION_LOWBUY_PCT_HIGH
    auction_overheat_pct: Optional[Decimal] = None   # QMT_AUCTION_OVERHEAT_PCT：超该幅度警惕
    leader_strength_min: Optional[Decimal] = None    # QMT_LEADER_STRENGTH_MIN：龙头强度分下限
    # 打板跟买的封流比下限（评审二轮 P2#41 + 复审 P2-1）：封流比(封单额/流通市值)有值且低于此值视为封单不稳→弃。
    # 原硬编码 0.0 使该护栏恒不触发(对任何顶板无条件跟买)，现改为【可配置】。
    # 默认 0（关闭）：执行侧 seal_to_float_ratio 依赖 bidVol 的"手/股"量纲(auction_factors.virtual_seal)，
    # 在目标机实测确认 bidVol 量纲(是否需 ×100)并据此标定阈值前，不默认激活——避免量纲偏差把真实强板误杀在
    # 真金买入路径上。实测确认后用 QMT_SEAL_RATIO_MIN 配一个正阈值(如 0.005)启用本护栏。
    seal_ratio_min: Decimal = Decimal("0")            # QMT_SEAL_RATIO_MIN
    # 打板因子消费阈值（执行侧 E2，消费 watchlist 1.2.0 的封板时序/位置因子）：
    # 【默认即生效（业务方决策 2026-06-19）】，env 可覆盖；这三个是【未回测的起步值】，日后按回测/真机标定调整。
    # 与 plan 因子双守卫（阈值 is not None 且 plan 因子 is not None 才判弃），缺数据/老契约零误杀。
    # 关停某闸：配一个永不触发的大值（见各行）；⚠️ open_times 配 0=`>=0` 恒成立=全弃，属误配，关闭请配大值。
    forbid_open_times_max: Optional[int] = 1          # QMT_FORBID_OPEN_TIMES_MAX：open_times>=本值→弃。默认1=只打稳封(0炸板)；关闭配大值如999（勿配0）
    high_return_pct_limit: Optional[Decimal] = Decimal("50")  # QMT_HIGH_RETURN_PCT_LIMIT：return_5d_pct>=本值(%)→弃。默认50=近5日涨超50%规避；关闭配大值如9999
    pullback_entry_deadline_hm: Optional[str] = "10:30"  # QMT_PULLBACK_ENTRY_DEADLINE_HM(HH:MM)：龙回头 first_limit_time>本值→弃。默认10:30；关闭配如23:59
    strategy_enabled: dict = field(default_factory=dict)  # QMT_STRATEGY_<NAME>_ENABLED 汇总

    # —— 7.1.5 风控阈值（执行侧硬约束，下单前生效）——
    max_position_per_stock: Optional[Decimal] = None  # QMT_MAX_POSITION_PER_STOCK（金额上限）
    max_total_exposure: Optional[Decimal] = None      # QMT_MAX_TOTAL_EXPOSURE
    max_orders_per_day: Optional[int] = None          # QMT_MAX_ORDERS_PER_DAY（下单「次数」上限，含转次优/卖出）
    # 单日建仓「只数」上限（不同标的被买入的数量，区别于 max_orders 的下单次数）：
    # 默认 5——当日最多买入 5 只不同标的。机制：强度优选 top-N 进入买入universe（权重在 N 只内归一、
    # 满额部署不闲置现金），叠加 order_executor 下单层硬闸（占名额口径：在途/部成/已成占名额，
    # 终态零成交=没买进不占）双保险。设 0/负数或极大值可放宽（QMT_MAX_POSITIONS_PER_DAY 覆盖）。
    max_positions_per_day: Optional[int] = 5          # QMT_MAX_POSITIONS_PER_DAY
    # 禁买四板及以上阈值（doc/18 买入前置过滤层硬规则）：连板高度 board_level >= 本值即禁买（默认 4=四板及以上）。
    # 由 buy_prefilter 在 loader/entry_router/order_executor 三层统一消费；信号侧 tier==HIGH_BOARD 兜底拦 4+ 板
    # （board_level 缺失时）。调低（如 3）拦更多板，调大/置 0 放宽（0=关闭高板口径，仅特殊场景）。
    forbid_board_level_min: int = 4                   # QMT_FORBID_BOARD_LEVEL_MIN
    per_order_max_amount: Optional[Decimal] = None    # QMT_PER_ORDER_MAX_AMOUNT
    # 强度加权资金分配（按 leader_strength_score 分预算，强的分得多）：可分配总预算上限 = 日初权益×本比例。
    # 默认 1.0=用全部日初权益作上限（强度权重在候选间归一分配）；可调小以留现金（如 0.8 只用八成仓）。
    target_position_ratio: Decimal = Decimal("1.0")   # QMT_TARGET_POSITION_RATIO
    # 允许 plan_volume 缺失时回退 _plan_volume 现算（评审三轮 EXEC-entry-02）：默认 False=对真实 BUY 主链
    # fail-closed。真实 BUY 的仓位必须由 EntryRouter 的强度加权 position_sizer 给出 plan_volume，缺失视为
    # 装配/限价异常，拒单留痕而非用【不含强度份额】的 _plan_volume 退化口径绕过强度与名额约束。仅离线/无
    # sizer 的兼容场景显式置 True 才启用回退（QMT_ALLOW_PLAN_VOLUME_FALLBACK）。
    allow_plan_volume_fallback: bool = False          # QMT_ALLOW_PLAN_VOLUME_FALLBACK
    # 限价相对参考价偏离护栏（评审 doc/19 H-3 升级为装配期 fail-closed）：默认 None；assert_safe_to_trade
    # 要求生产显式配置(如 0.10=偏离盘口现价10%即拒发)，缺配拒启动——杜绝「忘配=偏离护栏静默放行」。显式关停
    # 配大值(如 0.99，实际永不触发)而非 0(0=零容忍最严)。
    price_deviation_guard_pct: Optional[Decimal] = None  # QMT_PRICE_DEVIATION_GUARD_PCT
    # QMT_MARKET_STATE_BLOCK：禁开仓集合（评审 2.5 口径修正）。
    # 信号侧 watchlist 的 market_state 只有三档：空仓 / 谨慎参与 / 参与（六档情绪周期已在信号侧
    # _CYCLE_TO_STATE 折叠进这三档；退潮/冰点 → 空仓，分歧/启动 → 谨慎参与，发酵/高潮 → 参与）。
    # 原默认 ["退潮","冰点","空仓"] 的核心问题：退潮/冰点 是六档周期名、在 market_state 列永不出现
    # （禁开仓死代码），且「谨慎参与」漏挡照常开仓。修复：
    #   - 真实三档：加入「谨慎参与」（已确认口径：谨慎参与=禁开仓，仅「参与」开新仓）；
    #   - 保留 退潮/冰点 作为防御性冗余（正常不出现在 market_state，但契约漂移时仍兜底禁开，无副作用）。
    market_state_block: List[str] = field(
        default_factory=lambda: ["空仓", "谨慎参与", "退潮", "冰点"]
    )
    # 账户级当日回撤阈值（§5.4.1；评审 doc/19 H-3 升级为装配期 fail-closed）：默认 None；assert_safe_to_trade
    # 要求生产显式配置(如 0.05=日内回撤5%熔断禁开新仓)，缺配拒启动——杜绝「忘配=回撤熔断静默形同虚设」。显式关停
    # 配大值(如 0.99，实际永不触发)而非 0(0=任何亏损即熔断)。配了即生效：盘前日初权益基线抓取失败时 fail-closed
    # 禁开新仓(不冻结卖出，见 main._open_blocked_by_risk 的 F15 分支)。
    account_drawdown_limit: Optional[Decimal] = None  # 账户级当日回撤阈值（§5.4.1）
    account_loss_limit: Optional[Decimal] = None      # 账户级当日已实现亏损阈值
    stock_float_loss_limit: Optional[Decimal] = None  # 单票浮亏阈值

    # —— 收盘资产对账容差（评审 F01）——
    # 原实现把「成交净额(Σtraded_amount，不含佣金/印花税/过户费)」与「可用现金变动(含全部费用+冻结)」硬比、
    # 阈值硬编码 1000 元不可配不随规模缩放 → 高换手/大账户日因费用噪声误报 asset_discrepancy。改为费用一致的
    # 相对容差：tolerance = max(abs_floor, 当日成交额 × rel_rate)；偏差超容差才记偏差（且已降为告警不阻断开仓）。
    reconcile_asset_abs_floor: Decimal = Decimal("1000")   # QMT_RECONCILE_ASSET_ABS_FLOOR：绝对容差下限(元)
    reconcile_asset_rel_rate: Decimal = Decimal("0.003")   # QMT_RECONCILE_ASSET_REL_RATE：相对容差(×成交额，覆盖费用)

    # —— 7.1.6 竞价择时总开关（实测前必须关，§7.1.6 强约束）——
    auction_timing_enabled: bool = False      # QMT_AUCTION_TIMING_ENABLED：默认 False
    # 竞价择时实测放行标记（fail-closed）：仅当在国金真机完成竞价数据能力实测（§A4）后显式置 True，才允许
    # auction_timing_enabled=True 真实启动；否则 assert_safe_to_trade 启动期拒启，杜绝「未实测就开竞价择时」。
    auction_timing_verified: bool = False     # QMT_AUCTION_TIMING_VERIFIED：默认 False
    # 盘中卖出链生产放行门控（阶段0-C 接线 + fail-closed，国金对接核对 B1/H1）：默认 False=不接 provider、
    # 生产不自动卖出（与接线前安全行为一致）。**必须保持关，直到阶段1 T1.2 跨帧盘口保真（隔夜持仓 is_sealed/
    # 封流比补数、破位/炸板帧历史）完成 + 真机实测**——否则最小版会把「不在今日 watchlist 的真封板隔夜票」误判
    # 非封板、且 watchlist 取数失败时批量误清仓。开启时 assert_safe_to_trade 强制要求配单票浮亏止损（唯一价位安全网）。
    sell_pass_live: bool = False              # QMT_SELL_PASS_LIVE：默认 False（生产卖出门控）
    kill_switch: bool = False                 # QMT_KILL_SWITCH：True 则只采集不下单

    # —— 下单/台账参数 ——
    order_ttl_seconds: int = 60               # 单最长存活时限（开盘单），竞价单到 9:25 定盘
    repository_unique_with_trade_date: bool = True  # §6.5 加固：明细唯一键是否纳入 trade_date
    # 下单通道主动探活连续失败阈值（评审三轮 EXEC-risk-05）：盘中心跳 query_stock_asset 连续失败达该
    # 次数才置 _trade_conn_ok=False（FREEZE），避免单次抖动误冻；单次成功即清零。
    trade_conn_heartbeat_fail_threshold: int = 3  # QMT_TRADE_CONN_HEARTBEAT_FAIL_THRESHOLD
    # 撤单回执丢失的 CANCELLING 单宽限期秒数（评审三轮 EXEC-order-08）：超过宽限仍 CANCELLING 即幂等
    # 重发 cancel/续二次截止，防撤单回执断线丢失导致单永久卡 CANCELLING 占名额占预算。
    cancel_grace_seconds: int = 30            # QMT_CANCEL_GRACE_SECONDS

    # —— 本地化数据栈（doc/05 单进程+SQLite）——
    local_db_path: str = "qmt_local.db"       # QMT_LOCAL_DB_PATH：本机 SQLite 库路径（回流/台账/名单）
    # 写队列长度硬上限（评审 F07）：>0 启用溢出熔断——写线程永久挂死(如 fsync/NFS 卡死)时防无界堆积 OOM，
    # 超限即丢弃任务 + on_failure 告警(→停开新仓)。原生产装配未传(默认0=不启用)，该保护形同虚设；这里默认武装 5万。
    write_queue_max: int = 50000              # QMT_WRITE_QUEUE_MAX
    # 写线程「卡死」看门狗阈值秒（评审 F09）：有积压(pending>0)但连续该秒数无任何任务推进 → is_healthy 转 False
    # (→fail-closed 停开新仓)。覆盖「写线程卡在永不返回的 I/O、commit 不抛错、_last_write_ok 误停在 True」盲区。
    write_queue_stuck_seconds: float = 30.0   # QMT_WRITE_QUEUE_STUCK_SECONDS

    # —— 决策链路采集（复盘用、best-effort、与交易热路径物理隔离，不影响真实交易）——
    # 默认开：在各决策点非阻塞采集「信号达标→下单/未买→卖出」决策事件，盘后回流信号侧 qmt_decision_log。
    # 关掉（QMT_DECISION_LOG_ENABLED=false）即降级 no-op（不建队列线程），是该功能的一键回滚开关。
    decision_log_enabled: bool = True         # QMT_DECISION_LOG_ENABLED：默认 True
    decision_log_queue_size: int = 2000       # QMT_DECISION_LOG_QUEUE_SIZE：有界队列容量（满即丢）
    decision_log_batch_size: int = 50         # QMT_DECISION_LOG_BATCH_SIZE：回流攒批大小

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        """从环境变量映射构造配置。

        业务意图：所有 QMT_* 配置统一在此解析，缺省走 dataclass 默认；
        ``strategy_enabled`` 收集所有形如 QMT_STRATEGY_<NAME>_ENABLED 的开关。
        边界：数值/布尔做容错解析（空串视为未配置，走默认）。
        """
        g = env.get
        # 汇总各战法开关：键为 <NAME> 小写，值为 bool。
        strat: dict = {}
        for k, v in env.items():
            if k.startswith("QMT_STRATEGY_") and k.endswith("_ENABLED"):
                name = k[len("QMT_STRATEGY_"):-len("_ENABLED")].lower()
                strat[name] = _as_bool(v)

        def split_csv(val: Optional[str], default: List[str]) -> List[str]:
            if val is None or val.strip() == "":
                return list(default)
            return [s.strip() for s in val.split(",") if s.strip()]

        return cls(
            account_id=g("QMT_ACCOUNT_ID") or None,
            account_type=_as_int(g("QMT_ACCOUNT_TYPE")),
            mini_path=g("QMT_MINI_PATH") or None,
            session_id=_as_int(g("QMT_SESSION_ID")),
            mysql_dsn=g("QMT_MYSQL_DSN") or None,
            writeback_base_url=g("QMT_WRITEBACK_BASE_URL") or None,
            writeback_token=g("QMT_WRITEBACK_TOKEN") or None,
            ingest_token=g("QMT_INGEST_TOKEN") or None,
            watchlist_source=(g("QMT_WATCHLIST_SOURCE") or "DB").upper(),
            trade_calendar_file=g("QMT_TRADE_CALENDAR_FILE") or None,
            allow_weekday_calendar=_as_bool(g("QMT_ALLOW_WEEKDAY_CALENDAR") or "false"),
            watchlist_api_url=g("QMT_WATCHLIST_API_URL") or None,
            signal_base_url=g("QMT_SIGNAL_BASE_URL") or None,
            signal_internal_token=g("QMT_SIGNAL_INTERNAL_TOKEN") or None,
            signal_internal_token_file=g("QMT_SIGNAL_INTERNAL_TOKEN_FILE") or None,
            signal_watchlist_token=g("QMT_SIGNAL_WATCHLIST_TOKEN") or None,
            signal_watchlist_token_file=g("QMT_SIGNAL_WATCHLIST_TOKEN_FILE") or None,
            signal_ingest_token=g("QMT_SIGNAL_INGEST_TOKEN") or None,
            signal_ingest_token_file=g("QMT_SIGNAL_INGEST_TOKEN_FILE") or None,
            http_timeout_seconds=_as_float(g("QMT_HTTP_TIMEOUT_SECONDS")) or 10.0,
            auction_poll_interval_sec=_as_float(g("QMT_AUCTION_POLL_INTERVAL_SEC")) or 3.0,
            close_snapshot_time=g("QMT_CLOSE_SNAPSHOT_TIME") or "15:05",
            auction_abandon_pct=_as_decimal(g("QMT_AUCTION_ABANDON_PCT")),
            auction_lowbuy_pct_low=_as_decimal(g("QMT_AUCTION_LOWBUY_PCT_LOW")),
            auction_lowbuy_pct_high=_as_decimal(g("QMT_AUCTION_LOWBUY_PCT_HIGH")),
            auction_overheat_pct=_as_decimal(g("QMT_AUCTION_OVERHEAT_PCT")),
            leader_strength_min=_as_decimal(g("QMT_LEADER_STRENGTH_MIN")),
            # 用 is-not-None 守卫（复审 P1-1）：配 0 是"显式关闭"，不能被 `or 默认` 当假值覆盖（安全阀须能关）。
            seal_ratio_min=(
                _v if (_v := _as_decimal(g("QMT_SEAL_RATIO_MIN"))) is not None else Decimal("0")
            ),
            # 打板因子消费阈值（E2，默认即生效，env 可覆盖）：is-not-None 守卫——显式配（含极端值关停）优先，
            # 未配/空串回退起步默认 1 / 50 / "10:30"。注：配 0 是显式值（open_times>=0 全弃，误配自负），不被守卫吞。
            forbid_open_times_max=(
                _v if (_v := _as_int(g("QMT_FORBID_OPEN_TIMES_MAX"))) is not None else 1
            ),
            high_return_pct_limit=(
                _v if (_v := _as_decimal(g("QMT_HIGH_RETURN_PCT_LIMIT"))) is not None else Decimal("50")
            ),
            pullback_entry_deadline_hm=(g("QMT_PULLBACK_ENTRY_DEADLINE_HM") or "10:30"),
            strategy_enabled=strat,
            max_position_per_stock=_as_decimal(g("QMT_MAX_POSITION_PER_STOCK")),
            max_total_exposure=_as_decimal(g("QMT_MAX_TOTAL_EXPOSURE")),
            max_orders_per_day=_as_int(g("QMT_MAX_ORDERS_PER_DAY")),
            # 默认 5；显式配置（含 0/负=放宽不限只数）优先，未配则取默认 5。
            max_positions_per_day=(
                _as_int(g("QMT_MAX_POSITIONS_PER_DAY"))
                if g("QMT_MAX_POSITIONS_PER_DAY")
                else 5
            ),
            # 禁买四板及以上阈值（doc/18）：is-not-None 守卫——显式配 0（关闭高板口径）不被 `or 默认` 吞成 4。
            forbid_board_level_min=(
                _v if (_v := _as_int(g("QMT_FORBID_BOARD_LEVEL_MIN"))) is not None else 4
            ),
            per_order_max_amount=_as_decimal(g("QMT_PER_ORDER_MAX_AMOUNT")),
            # 目标仓位比（评审二轮 P2#33 / 复审 P2-3）：必须区分"未配置"与"显式配 0"。原 `_as_decimal(...) or
            # Decimal('1.0')` 会把 Decimal('0')（空跑/降仓口径）当假值静默改成满仓 1.0。这里仅在【未配置/空串】
            # 时取默认 1.0；显式配值保留，且负值钳到 0（与下方注释一致：0=不开新仓的空跑，绝不被悄悄放大成满仓）。
            target_position_ratio=(
                max(Decimal("0"), _as_decimal(g("QMT_TARGET_POSITION_RATIO")))
                if (g("QMT_TARGET_POSITION_RATIO") or "").strip() != ""
                else Decimal("1.0")
            ),
            allow_plan_volume_fallback=_as_bool(g("QMT_ALLOW_PLAN_VOLUME_FALLBACK") or "false"),
            price_deviation_guard_pct=_as_decimal(g("QMT_PRICE_DEVIATION_GUARD_PCT")),
            market_state_block=split_csv(g("QMT_MARKET_STATE_BLOCK"), ["空仓", "谨慎参与", "退潮", "冰点"]),
            account_drawdown_limit=_as_decimal(g("QMT_ACCOUNT_DRAWDOWN_LIMIT")),
            account_loss_limit=_as_decimal(g("QMT_ACCOUNT_LOSS_LIMIT")),
            stock_float_loss_limit=_as_decimal(g("QMT_STOCK_FLOAT_LOSS_LIMIT")),
            # 资产对账容差（评审 F01）：未配走默认（绝对下限 1000 元、相对 0.3% 成交额）。
            reconcile_asset_abs_floor=(
                _v if (_v := _as_decimal(g("QMT_RECONCILE_ASSET_ABS_FLOOR"))) is not None else Decimal("1000")
            ),
            reconcile_asset_rel_rate=(
                _v if (_v := _as_decimal(g("QMT_RECONCILE_ASSET_REL_RATE"))) is not None else Decimal("0.003")
            ),
            decision_log_enabled=_as_bool(g("QMT_DECISION_LOG_ENABLED") or "true"),
            decision_log_queue_size=_as_int(g("QMT_DECISION_LOG_QUEUE_SIZE")) or 2000,
            decision_log_batch_size=_as_int(g("QMT_DECISION_LOG_BATCH_SIZE")) or 50,
            auction_timing_enabled=_as_bool(g("QMT_AUCTION_TIMING_ENABLED") or "false"),
            auction_timing_verified=_as_bool(g("QMT_AUCTION_TIMING_VERIFIED") or "false"),
            sell_pass_live=_as_bool(g("QMT_SELL_PASS_LIVE") or "false"),
            kill_switch=_as_bool(g("QMT_KILL_SWITCH") or "false"),
            order_ttl_seconds=_as_int(g("QMT_ORDER_TTL_SECONDS")) or 60,
            trade_conn_heartbeat_fail_threshold=(
                _as_int(g("QMT_TRADE_CONN_HEARTBEAT_FAIL_THRESHOLD")) or 3
            ),
            cancel_grace_seconds=_as_int(g("QMT_CANCEL_GRACE_SECONDS")) or 30,
            repository_unique_with_trade_date=_as_bool(
                g("QMT_UNIQUE_WITH_TRADE_DATE") or "true"
            ),
            local_db_path=g("QMT_LOCAL_DB_PATH") or "qmt_local.db",
            # 写队列健壮性（评审 F07/F09）：默认武装 max_queue=5万、看门狗 30s；显式配 0 关闭上限/0 关看门狗。
            write_queue_max=(
                _v if (_v := _as_int(g("QMT_WRITE_QUEUE_MAX"))) is not None else 50000
            ),
            # is-not-None 守卫（与 write_queue_max/seal_ratio_min 同口径）：显式配 0 是「关看门狗」，绝不能被
            # `or 30.0` 把 0.0(falsy) 吞成默认 30——否则文档承诺的「配 0 关看门狗」逃生口不可达（review 修正）。
            write_queue_stuck_seconds=(
                _v if (_v := _as_float(g("QMT_WRITE_QUEUE_STUCK_SECONDS"))) is not None else 30.0
            ),
        )

    def resolve_signal_token(self) -> Optional[str]:
        """解析信号侧内网接口 token：优先落盘文件（不入 env/日志），其次环境变量；均无返回 None。

        与 AGENTS.md 安全口径一致：token 不硬编码、不进日志（redacted() 已脱敏 signal_internal_token）；
        生产建议用 QMT_SIGNAL_INTERNAL_TOKEN_FILE 指向本机文件，避免明文落 .env。
        """
        token_file = self.signal_internal_token_file
        if token_file:
            try:
                with open(token_file, encoding="utf-8") as fh:
                    token = fh.read().strip()
                if token:
                    return token
            except OSError:
                # 文件不存在/不可读：不抛，回落环境变量（缺则 None，由调用方按未配置处理）。
                pass
        if self.signal_internal_token:
            return self.signal_internal_token.strip()
        return None

    @staticmethod
    def _read_token(token_file: Optional[str], token_value: Optional[str]) -> Optional[str]:
        """按「文件优先、环境变量兜底」读取 token（文件不可读不抛、回落 env）。"""
        if token_file:
            try:
                with open(token_file, encoding="utf-8") as fh:
                    token = fh.read().strip()
                if token:
                    return token
            except OSError:
                pass
        if token_value:
            return token_value.strip()
        return None

    def resolve_watchlist_token(self) -> Optional[str]:
        """GET /internal/watchlist 专用 token（评审三轮 XCUT-01）：专用文件/env→回落统一 signal token。"""
        return (
            self._read_token(self.signal_watchlist_token_file, self.signal_watchlist_token)
            or self.resolve_signal_token()
        )

    def resolve_ingest_token(self) -> Optional[str]:
        """POST /internal/qmt/ingest 专用 token（评审三轮 XCUT-01）：专用文件/env→回落统一 signal token。"""
        return (
            self._read_token(self.signal_ingest_token_file, self.signal_ingest_token)
            or self.resolve_signal_token()
        )

    def assert_safe_to_trade(self) -> None:
        """启动期配置自洽校验（§7.1.6 fail-closed）：竞价择时未实测放行不得开启。

        业务意图：竞价择时（auction_timing_enabled）是真金下单的高风险开关，红线为「§A4 真机竞价数据
        能力实测通过前必须关」。本方法把该红线从「仅靠默认 False + 文档」升级为启动期强制校验：
        若 auction_timing_enabled=True 但 auction_timing_verified=False（未显式标记实测通过）→ 抛
        RuntimeError 拒绝启动，杜绝「误配 QMT_AUCTION_TIMING_ENABLED=true 就直接开竞价真金下单」。
        由 build_real_engine 装配期调用（离线/单测直接构造 Engine 不经此，不影响既有用例）。
        边界：只做配置自洽校验，不做行情判断；kill_switch 的全局熔断由 order_executor.place 直接消费。
        """
        if self.auction_timing_enabled and not self.auction_timing_verified:
            raise RuntimeError(
                "竞价择时已开(QMT_AUCTION_TIMING_ENABLED=true)但未标记实测通过"
                "(QMT_AUCTION_TIMING_VERIFIED 未置真)。竞价数据能力须先在目标机(国金 miniQMT)完成 §A4 "
                "实测，确认后显式配 QMT_AUCTION_TIMING_VERIFIED=true 方可开启；拒绝启动以防未实测即真金竞价下单。"
            )
        # 卖出链放行 fail-closed（国金对接核对 H1）：最小版跨帧破位/炸板止损未保真，单帧浮亏止损是开启生产
        # 卖出时唯一的价位安全网；若开了卖出链却没配单票浮亏止损阈值，深套位将无任何价位止损，故强制必配。
        if self.sell_pass_live and self.stock_float_loss_limit is None:
            raise RuntimeError(
                "盘中卖出链已开(QMT_SELL_PASS_LIVE=true)但未配单票浮亏止损(QMT_STOCK_FLOAT_LOSS_LIMIT)。"
                "最小版跨帧破位/炸板止损未保真(待 T1.2)，单帧浮亏止损是唯一价位安全网，须显式配置(如 0.05)"
                "方可开启生产卖出；拒绝启动以防卖出链开启却无价位止损兜底。"
            )
        # 账户回撤闸 / 限价偏离闸装配期强校验（评审 doc/19 H-3）：这两道是 README「风控五道闸」中的资金/价位护栏，
        # 原实现默认 None → risk._breached / _limit_price_sane 对 None 整段放行（fail-open）、且无任何启动期提醒，
        # 运维易误以为护栏已生效。这里把它们升级为与竞价择时/卖出链同级的【启动期 fail-closed 强校验】：
        # 缺配即拒启，强制运维显式做出风控决策，杜绝「忘配=两道护栏静默形同虚设」。
        # 关停口径：这两闸的观测值（回撤/偏离）恒 >=0，配 0 表示「零容忍」(最严，非关停)，故【显式关停】
        # 应配一个大到实际永不触发的阈值（如回撤 0.99=99%、偏离 0.99=99%），而非 0；这样既满足「显式配置」，
        # 又把「我确实不要这道闸」变成一次有痕可审计的主动决策（值进 redacted() 启动快照可核对）。
        if self.account_drawdown_limit is None:
            raise RuntimeError(
                "账户级当日回撤闸未配置(QMT_ACCOUNT_DRAWDOWN_LIMIT 缺失)。它是「风控五道闸」之一，"
                "未配时回撤熔断对开新仓零作用(爆亏当天仍可继续开仓至现金耗尽)。须显式配置(如 0.05=回撤5%熔断)；"
                "若确需关停，配一个实际永不触发的大值(如 0.99)以留下有痕的主动决策。拒绝启动以防账户级止损静默失效。"
            )
        if self.price_deviation_guard_pct is None:
            raise RuntimeError(
                "限价偏离护栏未配置(QMT_PRICE_DEVIATION_GUARD_PCT 缺失)。它是「风控五道闸」之一，"
                "未配时价位口径错位/追高失控只剩「超法定涨停价」一道兜底。须显式配置(如 0.10=偏离盘口现价10%即拒)；"
                "若确需关停，配一个实际永不触发的大值(如 0.99)以留下有痕的主动决策。拒绝启动以防限价护栏静默失效。"
            )
        return None

    def redacted(self) -> dict:
        """脱敏后的配置快照（供日志/排查）。敏感字段一律打码（§7.1 安全口径）。"""
        out = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if f.name in _SENSITIVE and val:
                out[f.name] = "***REDACTED***"
            else:
                out[f.name] = val
        return out
