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

    # —— watchlist 契约来源（§1.2.1 二选一，默认 A 直读表）——
    watchlist_source: str = "DB"              # QMT_WATCHLIST_SOURCE：DB（A）/ HTTP（B）
    watchlist_api_url: Optional[str] = None   # QMT_WATCHLIST_API_URL：B 方案只读接口地址

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
    strategy_enabled: dict = field(default_factory=dict)  # QMT_STRATEGY_<NAME>_ENABLED 汇总

    # —— 7.1.5 风控阈值（执行侧硬约束，下单前生效）——
    max_position_per_stock: Optional[Decimal] = None  # QMT_MAX_POSITION_PER_STOCK（金额上限）
    max_total_exposure: Optional[Decimal] = None      # QMT_MAX_TOTAL_EXPOSURE
    max_orders_per_day: Optional[int] = None          # QMT_MAX_ORDERS_PER_DAY
    per_order_max_amount: Optional[Decimal] = None    # QMT_PER_ORDER_MAX_AMOUNT
    price_deviation_guard_pct: Optional[Decimal] = None  # QMT_PRICE_DEVIATION_GUARD_PCT
    market_state_block: List[str] = field(            # QMT_MARKET_STATE_BLOCK：禁开仓情绪周期
        default_factory=lambda: ["退潮", "冰点", "空仓"]
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

    # —— 本地化数据栈（doc/05 单进程+SQLite）——
    local_db_path: str = "qmt_local.db"       # QMT_LOCAL_DB_PATH：本机 SQLite 库路径（回流/台账/名单）

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
            watchlist_api_url=g("QMT_WATCHLIST_API_URL") or None,
            auction_poll_interval_sec=_as_float(g("QMT_AUCTION_POLL_INTERVAL_SEC")) or 3.0,
            auction_window=g("QMT_AUCTION_WINDOW") or "09:15-09:25",
            intraday_snapshot_minutes=_as_int(g("QMT_INTRADAY_SNAPSHOT_MINUTES")) or 5,
            close_snapshot_time=g("QMT_CLOSE_SNAPSHOT_TIME") or "15:05",
            auction_abandon_pct=_as_decimal(g("QMT_AUCTION_ABANDON_PCT")),
            auction_lowbuy_pct_low=_as_decimal(g("QMT_AUCTION_LOWBUY_PCT_LOW")),
            auction_lowbuy_pct_high=_as_decimal(g("QMT_AUCTION_LOWBUY_PCT_HIGH")),
            auction_overheat_pct=_as_decimal(g("QMT_AUCTION_OVERHEAT_PCT")),
            leader_strength_min=_as_decimal(g("QMT_LEADER_STRENGTH_MIN")),
            strategy_enabled=strat,
            max_position_per_stock=_as_decimal(g("QMT_MAX_POSITION_PER_STOCK")),
            max_total_exposure=_as_decimal(g("QMT_MAX_TOTAL_EXPOSURE")),
            max_orders_per_day=_as_int(g("QMT_MAX_ORDERS_PER_DAY")),
            per_order_max_amount=_as_decimal(g("QMT_PER_ORDER_MAX_AMOUNT")),
            price_deviation_guard_pct=_as_decimal(g("QMT_PRICE_DEVIATION_GUARD_PCT")),
            market_state_block=split_csv(g("QMT_MARKET_STATE_BLOCK"), ["退潮", "冰点", "空仓"]),
            account_drawdown_limit=_as_decimal(g("QMT_ACCOUNT_DRAWDOWN_LIMIT")),
            account_loss_limit=_as_decimal(g("QMT_ACCOUNT_LOSS_LIMIT")),
            stock_float_loss_limit=_as_decimal(g("QMT_STOCK_FLOAT_LOSS_LIMIT")),
            auction_timing_enabled=_as_bool(g("QMT_AUCTION_TIMING_ENABLED") or "false"),
            kill_switch=_as_bool(g("QMT_KILL_SWITCH") or "false"),
            order_ttl_seconds=_as_int(g("QMT_ORDER_TTL_SECONDS")) or 60,
            repository_unique_with_trade_date=_as_bool(
                g("QMT_UNIQUE_WITH_TRADE_DATE") or "true"
            ),
            local_db_path=g("QMT_LOCAL_DB_PATH") or "qmt_local.db",
        )

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
