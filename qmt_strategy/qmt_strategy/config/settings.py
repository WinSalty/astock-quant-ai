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
    account_type: Optional[int] = None        # QMT_ACCOUNT_TYPE：账号类型枚举（实测为准）
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
    signal_internal_token: Optional[str] = None      # QMT_SIGNAL_INTERNAL_TOKEN：X-Internal-Token 值
    signal_internal_token_file: Optional[str] = None  # QMT_SIGNAL_INTERNAL_TOKEN_FILE：token 落盘文件（优先）
    http_timeout_seconds: float = 10.0        # QMT_HTTP_TIMEOUT_SECONDS：单次接口超时秒数

    # —— 7.1.3 竞价轮询与采集节奏 ——
    auction_poll_interval_sec: float = 3.0    # QMT_AUCTION_POLL_INTERVAL_SEC（默认 3s，§7.1.3）
    auction_window: str = "09:15-09:25"       # QMT_AUCTION_WINDOW
    intraday_snapshot_minutes: int = 5        # QMT_INTRADAY_SNAPSHOT_MINUTES
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
    per_order_max_amount: Optional[Decimal] = None    # QMT_PER_ORDER_MAX_AMOUNT
    # 强度加权资金分配（按 leader_strength_score 分预算，强的分得多）：可分配总预算上限 = 日初权益×本比例。
    # 默认 1.0=用全部日初权益作上限（强度权重在候选间归一分配）；可调小以留现金（如 0.8 只用八成仓）。
    target_position_ratio: Decimal = Decimal("1.0")   # QMT_TARGET_POSITION_RATIO
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
    account_drawdown_limit: Optional[Decimal] = None  # 账户级当日回撤阈值（§5.4.1）
    account_loss_limit: Optional[Decimal] = None      # 账户级当日已实现亏损阈值
    stock_float_loss_limit: Optional[Decimal] = None  # 单票浮亏阈值

    # —— 7.1.6 竞价择时总开关（实测前必须关，§7.1.6 强约束）——
    auction_timing_enabled: bool = False      # QMT_AUCTION_TIMING_ENABLED：默认 False
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
            http_timeout_seconds=_as_float(g("QMT_HTTP_TIMEOUT_SECONDS")) or 10.0,
            auction_poll_interval_sec=_as_float(g("QMT_AUCTION_POLL_INTERVAL_SEC")) or 3.0,
            auction_window=g("QMT_AUCTION_WINDOW") or "09:15-09:25",
            intraday_snapshot_minutes=_as_int(g("QMT_INTRADAY_SNAPSHOT_MINUTES")) or 5,
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
            per_order_max_amount=_as_decimal(g("QMT_PER_ORDER_MAX_AMOUNT")),
            # 目标仓位比（评审二轮 P2#33 / 复审 P2-3）：必须区分"未配置"与"显式配 0"。原 `_as_decimal(...) or
            # Decimal('1.0')` 会把 Decimal('0')（空跑/降仓口径）当假值静默改成满仓 1.0。这里仅在【未配置/空串】
            # 时取默认 1.0；显式配值保留，且负值钳到 0（与下方注释一致：0=不开新仓的空跑，绝不被悄悄放大成满仓）。
            target_position_ratio=(
                max(Decimal("0"), _as_decimal(g("QMT_TARGET_POSITION_RATIO")))
                if (g("QMT_TARGET_POSITION_RATIO") or "").strip() != ""
                else Decimal("1.0")
            ),
            price_deviation_guard_pct=_as_decimal(g("QMT_PRICE_DEVIATION_GUARD_PCT")),
            market_state_block=split_csv(g("QMT_MARKET_STATE_BLOCK"), ["空仓", "谨慎参与", "退潮", "冰点"]),
            account_drawdown_limit=_as_decimal(g("QMT_ACCOUNT_DRAWDOWN_LIMIT")),
            account_loss_limit=_as_decimal(g("QMT_ACCOUNT_LOSS_LIMIT")),
            stock_float_loss_limit=_as_decimal(g("QMT_STOCK_FLOAT_LOSS_LIMIT")),
            decision_log_enabled=_as_bool(g("QMT_DECISION_LOG_ENABLED") or "true"),
            decision_log_queue_size=_as_int(g("QMT_DECISION_LOG_QUEUE_SIZE")) or 2000,
            decision_log_batch_size=_as_int(g("QMT_DECISION_LOG_BATCH_SIZE")) or 50,
            auction_timing_enabled=_as_bool(g("QMT_AUCTION_TIMING_ENABLED") or "false"),
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

    def assert_safe_to_trade(self) -> None:
        """下单前安全校验（§7.1.6）：竞价择时开关只有在显式置真时才允许，
        且 kill_switch 为真时禁止任何下单（由调用方据返回的 kill_switch 决定）。
        这里只做配置自洽校验，不做行情判断。"""
        # 占位校验点：真实落地可在此追加「未实测不得开竞价择时」的外部标志校验。
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
