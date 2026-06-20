"""盘前 watchlist 拉取与落本机 SQLite（doc/07：执行侧 → 信号侧 GET /internal/watchlist）。

业务意图：执行侧盘前（PREWARM）一次性从信号侧只读接口拉取「当日买入清单」，落到本机 SQLite 的
    watchlist 表；盘中 watchlist_loader 只读本机库（不再跨网络），彻底规避盘中网络抖动波及交易。

口径对齐（关键）：信号侧 ``GET /api/internal/watchlist?date=T`` 以**信号日 T** 为查询键，返回的每条
    target_trade_date=T+1。执行侧要的是「今天买入(=今天 target)」的清单，故先用交易日历反推
    signal_T = prev_open(today)，再按 date=signal_T 拉取；落库时把 target_trade_date 统一对齐为 today
    （我们正是按「today 的前一交易日」拉的，构造上即今天买入；对齐后 loader 按 target=today 读得到）。

字段映射：信号侧 LimitUpWatchlistItem（对外契约）→ 执行侧 SelectedStockRow（盘中只读契约）。
    两侧字段命名/类型有差异，本模块是**唯一映射点**：tradable_flag 字符串→bool、role_tags 列表→role、
    close→signal_close、Decimal 兼容数值/字符串两种 JSON 形态；信号侧未给的价位/因子（limit_up_price、
    reasonable_open_*、first_board_vol、float_mktcap）留空，由执行侧 board_rules 按 signal_close+板块兜底现算。

失败口径（§4.1 / §2.6）：拉取/解析失败**不抛出、不崩溃**——记 warn 并返回 0，本机库保持原状；
    loader 盘前读本机库为空 → 自动降级「只守仓、不开新仓」。绝不让网络问题拖垮常驻进程。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, List, Optional

from ..common.http_client import SignalHttpError
from ..contracts.models import SelectedStockRow

# 信号侧 watchlist 只读导出接口路径（与 backend/app/api/routes_watchlist_export.py 一致，含 /api 前缀）。
WATCHLIST_PATH = "/api/internal/watchlist"

# 信号侧 tradable_flag 取该值视为「可交易」，其余（谨慎/放弃观察等）一律视为不可交易（只观察）。
_TRADABLE_VALUE = "TRADABLE"


def _to_decimal(v: Any) -> Optional[Decimal]:
    """JSON 数值/字符串 → Decimal（兼容 Pydantic 把 Decimal 序列化成 number 或 string 两种形态）。

    None/空串/非法 → None（不臆造）。用 str(v) 中转避免 float 二进制误差（Decimal(str(1.05))=1.05）。
    """
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    """JSON 数值/字符串 → int；None/空/非法 → None（不臆造）。

    评审 F3：first_board_vol 等整数量字段透传用；先经 Decimal 容忍 "123.0"/"1e3" 等浮点表示再取整。
    """
    d = _to_decimal(v)
    return int(d) if d is not None else None


def _to_date(v: Any) -> Optional[date]:
    """ISO 字符串/date → date；None/空 → None。"""
    if v is None:
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if s == "":
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _to_bool(v: Any) -> Optional[bool]:
    """JSON 布尔/数值/字符串 → Optional[bool]（禁买 ST 硬规则透传 is_st 用）。

    口径：None/空串 → None（未下发，保留三态由下游回退 name 判定）；'true'/'1'/'yes' 系真，其余假。
    保留 None 与 False 的区分：信号侧未给 is_st 时回退证券名识别 ST，不可把缺失当作显式 False。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s == "":
        return None
    return s in {"1", "true", "yes", "y", "t", "on"}


def _first_role(role_tags: Any) -> Optional[str]:
    """role_tags 列表 → 单一 role（取首个非空标签；空/非列表 → None）。

    SelectedStockRow.role 是单值，信号侧 role_tags 是列表（龙头/中军/补涨…）；取首个作主角色，
    其余标签不丢失语义（盘中下单主要按 strategy_family/setup 路由，role 仅用于画像/复盘）。
    """
    if isinstance(role_tags, list) and role_tags:
        first = role_tags[0]
        return str(first) if first is not None else None
    if isinstance(role_tags, str) and role_tags.strip():
        return role_tags.strip()
    return None


def watchlist_item_to_selected(item: dict, target_trade_date: date) -> SelectedStockRow:
    """信号侧 watchlist item 字典 → 执行侧 SelectedStockRow（唯一字段映射点）。

    边界：缺失字段一律 None（下游按降级口径处理，§2.6 单票价位缺失只降级单票）；
    target_trade_date 以入参对齐 today（见模块 docstring 口径说明），trade_date 取 item 的信号日 T。
    """
    tradable_raw = item.get("tradable_flag")
    # 信号侧为字符串枚举（默认 TRADABLE）：等于 TRADABLE 才算可交易，其余视为只观察。
    tradable = (tradable_raw == _TRADABLE_VALUE) if tradable_raw is not None else None
    return SelectedStockRow(
        ts_code=item.get("ts_code"),
        trade_date=_to_date(item.get("trade_date")),
        target_trade_date=target_trade_date,
        # 证券名称（评审二轮 P1#18/#63）：契约已含 name，透传供执行侧识别 ST/退市（主板 ST 涨停 5%）与 live 过滤。
        name=item.get("name"),
        # 显式 ST 标志（禁买 ST 硬规则 + F08）：信号侧契约若下发 is_st 布尔则透传（保留 None 三态），
        # 使执行侧「绝不买入 ST」三层闸拿到可靠显式信号，不再单点押在 name 文本上（name 偶发缺失会漏判）。
        is_st=_to_bool(item.get("is_st")),
        # 连板维度（doc/18 禁买四板及以上）：信号侧契约已下发 board_level（连板高度，可空）与 tier
        # （入选分层 FIRST_BOARD/CHAIN/HIGH_BOARD，恒非空）；透传供执行侧 buy_prefilter 做四板及以上前置过滤。
        # 缺字段（老契约）→ board_level=None / tier=None，buy_prefilter 无证据时放行（不无证据全拦）。
        board_level=_to_int(item.get("board_level")),
        tier=item.get("tier"),
        leader_strength_score=_to_decimal(item.get("leader_strength_score")),
        role=_first_role(item.get("role_tags")),
        # 信号侧只给 strategy_family/setup（无单值 strategy），strategy 留空，路由按 family+setup。
        strategy=None,
        market_state=item.get("market_state"),
        tradable_flag=tradable,
        continuation_prob=_to_decimal(item.get("continuation_prob")),
        next_day_premium_prob=_to_decimal(item.get("next_day_premium_prob")),
        boost=item.get("boost_conditions"),
        fail_conditions=item.get("fail_conditions"),
        signal_close=_to_decimal(item.get("close")),
        # 理论涨停价/合理高开区间：信号侧 watchlist 契约暂不含，留空由 board_rules 兜底现算。
        limit_up_price=None,
        reasonable_open_high_low=None,
        reasonable_open_high_high=None,
        # 评审 F3：竞价两因子分母——信号侧契约已补 first_board_vol/float_mktcap，这里透传（不再写死 None），
        # 使执行侧封流比/量能比因子不再结构性恒降级。缺字段(老契约/未回填)仍为 None → 因子走降级。
        first_board_vol=_to_int(item.get("first_board_vol")),
        float_mktcap=_to_decimal(item.get("float_mktcap")),
        strategy_family=item.get("strategy_family"),
        setup=item.get("setup"),
        # 打板因子（契约 1.2.0）：封板时序 + 位置/强度，透传供执行侧策略消费。缺字段(老契约)→None，策略侧降级不误杀。
        # 时刻为 HH:MM:SS 文本直取（不解析）；open_times 走 _to_int；三个比例走 _to_decimal（保 None 三态、不臆造）。
        first_limit_time=item.get("first_limit_time"),
        last_limit_time=item.get("last_limit_time"),
        open_times=_to_int(item.get("open_times")),
        volume_ratio=_to_decimal(item.get("volume_ratio")),
        return_5d_pct=_to_decimal(item.get("return_5d_pct")),
        return_10d_pct=_to_decimal(item.get("return_10d_pct")),
    )


class WatchlistPrefetcher:
    """盘前一次性拉取信号侧 watchlist 并落本机 SQLite（PREWARM 调用，非交易热路径）。

    依赖注入：
    - client：SignalHttpClient（带 base_url+token+超时）；
    - calendar：交易日历（prev_open 反推信号日 T；与信号侧 a_trade_calendar 同源，禁自然日 -1）；
    - save_fn：把 list[SelectedStockRow] 落本机 watchlist 表并返回写入行数（= LocalStorage.save_watchlist）；
    - logger：结构化日志（不记 token / 不记整行明细）。
    """

    def __init__(self, client, calendar, save_fn: Callable[[List[SelectedStockRow]], int], logger):
        self._client = client
        self._calendar = calendar
        self._save_fn = save_fn
        self._logger = logger

    def prefetch(self, today: date) -> int:
        """拉取「今天买入」的当日清单并落本机库。

        返回口径（评审 F11，区分真失败与合法空名单，供调度层决定是否重试）：
        - **>0**：成功写入 N 行；
        - **0**：合法空名单（信号侧 2xx 但当日无候选/报告未就绪）——**不重试**，避免空仓日 PREWARM 每个 poll
          周期反复重连+重抓基线打满频控；
        - **-1**：真失败（日历异常 / HTTP 非 2xx / 信封非对象 / 落库失败）——调度层据此重试。
        失败一律不抛、不崩溃（loader 读空本机库自动降级「只守仓」）。
        流程：prev_open(today)=signal_T → GET ?date=signal_T → map items → save_watchlist。
        """
        try:
            signal_t = self._calendar.prev_open(today)
        except Exception as exc:  # noqa: BLE001 日历异常不致命，降级为「无名单」由 loader 兜底
            self._logger.warn("watchlist_prefetch_calendar_failed", today=str(today), reason=repr(exc))
            return -1  # 真失败：调度层应重试（评审 F11）

        try:
            resp = self._client.get_json(WATCHLIST_PATH, {"date": signal_t.isoformat()})
        except SignalHttpError as exc:
            # 网络/非2xx：当日无名单 → loader 降级只守仓（doc/07 §失败口径）。不抛、不崩溃。
            self._logger.warn(
                "watchlist_prefetch_http_failed",
                today=str(today), signal_date=str(signal_t),
                status=exc.status, reason=str(exc),
            )
            return -1  # 真失败：调度层应重试（评审 F11）

        # 信封类型校验（评审 F12）：2xx 但 body 非 JSON 对象（list/标量/None）时，下方 (resp).get(...) 会抛
        # AttributeError 逸出。这里显式判失败留痕（非对象信封属契约异常，按真失败重试，不当合法空名单静默吞）。
        if resp is not None and not isinstance(resp, dict):
            self._logger.warn(
                "watchlist_prefetch_bad_envelope",
                today=str(today), signal_date=str(signal_t), resp_type=type(resp).__name__,
            )
            return -1

        # 供数降级感知（评审二轮 P2#58）：信号侧导出若有坏行被跳过(skipped_count>0)，可能漏掉可交易标的，
        # 这里强告警，使盘前装载不在"无声漏标的"下进行（运维可据此排查脏数据）。
        skipped = (resp or {}).get("skipped_count") or 0
        if skipped:
            self._logger.warn(
                "watchlist_prefetch_rows_skipped",
                today=str(today), signal_date=str(signal_t), skipped=skipped,
            )
        # 买入日一致性校验（评审二轮 P2#64/P3#80）：执行侧落库统一对齐 target_trade_date=today（按 prev_open(today)
        # 拉的，构造上即今天买入），但信号侧响应顶层 target_trade_date 应等于 today；不一致说明两侧交易日历不同步
        # （执行侧日历缺节假日等），原实现无条件覆盖会静默错配买入日。这里强告警暴露（仍按 today 落库，不静默吞）。
        resp_target = _to_date((resp or {}).get("target_trade_date"))
        if resp_target is not None and resp_target != today:
            self._logger.warn(
                "watchlist_prefetch_target_date_mismatch",
                today=str(today), signal_target=str(resp_target), signal_date=str(signal_t),
                note="两侧交易日历可能不同步，请核对执行侧 QMT_TRADE_CALENDAR_FILE 是否与信号侧 a_trade_calendar 同源",
            )
        items = (resp or {}).get("items") or []
        rows: List[SelectedStockRow] = []
        for it in items:
            try:
                rows.append(watchlist_item_to_selected(it, target_trade_date=today))
            except Exception as exc:  # noqa: BLE001 单条脏数据不影响整批，丢弃该条并告警
                self._logger.warn(
                    "watchlist_prefetch_item_skipped",
                    ts_code=(it or {}).get("ts_code"), reason=repr(exc),
                )
                continue

        if not rows:
            # 空清单是合法结果（当日尚无 READY 报告/无候选）：不删本机旧名单（save_watchlist 空入参返回0）。
            self._logger.info(
                "watchlist_prefetch_empty", today=str(today), signal_date=str(signal_t)
            )
            return 0

        try:
            written = self._save_fn(rows)
        except Exception as exc:  # noqa: BLE001 落库失败 → 降级无名单，不抛
            self._logger.warn("watchlist_prefetch_save_failed", today=str(today), reason=repr(exc))
            return -1  # 真失败：调度层应重试（评审 F11）

        self._logger.info(
            "watchlist_prefetch_done",
            today=str(today), signal_date=str(signal_t), pulled=len(items), saved=written,
        )
        return written
