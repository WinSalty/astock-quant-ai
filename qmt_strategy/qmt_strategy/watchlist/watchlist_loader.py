"""watchlist 装载流程（§2.3 装载流程 / §2.4 方法签名 / §2.6 异常兜底）。

业务意图：盘前（约 9:00 前）由调度触发一次，整批装载当日名单，产出常驻内存的
``WatchlistContext`` 供盘中只读（盘中下单决策不再回库，降低延迟、规避盘中 DB 抖动）。

核心安全口径（§2.6 总原则）：**宁可少做不可错做**——任何上游不确定（取契约失败 / 非交易日 /
market_state 缺失 / 单票价位缺失），一律退化为「只守仓、不开新仓」，绝不在契约残缺时盲开新仓。
loader 装载失败**不得抛出致使常驻连接进程退出的异常**：失败一律落到「降级只守仓」上下文。

只读、不写库。只依赖契约层 / common 工具 / 兄弟实现 sources.py，绝不 import xtquant / MySQL。
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Tuple

from ..common import board_rules, buy_prefilter, identity, universe_filter
from ..config.settings import Settings
from ..contracts.errors import WatchlistLoadError
from ..contracts.models import (
    PriceBudget,
    SelectedStockRow,
    TradableEntry,
    WatchlistContext,
)
from ..contracts.enums import PriceSource

# 空仓情绪周期标识（§2.6）：market_state == 该值 → 判为空仓日，禁开新仓。
# 口径修正（评审 2.5）：信号侧 watchlist 的 market_state 只有三档——空仓 / 谨慎参与 / 参与
# （六档情绪周期 冰点/退潮/分歧/启动/发酵/高潮 在信号侧已经 _CYCLE_TO_STATE 折叠进这三档，
#  且只落在 sentiment_cycle 另一列、不在 market_state）。禁开仓集合以 settings.market_state_block
# 为单一口径（默认 {空仓, 谨慎参与}，仅「参与」开新仓），本常量仅作空仓单值的语义锚点。
_EMPTY_POSITION_STATE = "空仓"


class WatchlistLoader:
    """盘前一次性装载当日可交易 / 观察名单与价位契约（§2.3–2.6）。

    口径：target_trade_date=今日；两路取数二选一（A 直读 MySQL / B 只读接口）经
    SelectedStockSource 协议屏蔽差异；复跑信号侧 universe_filter 前缀白名单兜底；
    按 tradable_flag 拆名单；按 board 预算今日涨停价与合理高开区间；读 market_state 决定
    是否空仓（空仓只守仓不开新仓）。只读、不写库。

    依赖注入：
    - primary：主取数源（按 settings.watchlist_source 选 A=DB / B=HTTP，由装配层决定）；
    - fallback：备路取数源（A↔B 互为备份，§2.6；可为 None 表示不配备路）；
    - calendar：交易日历（is_open 校验，禁自然日推算）；
    - logger：结构化日志（降级 / 单票剔除时告警留痕）；
    - settings：执行侧配置（透传，便于后续按需读取阈值，本流程暂只读不强依赖）。
    """

    def __init__(
        self,
        primary,
        calendar,
        logger,
        settings: Settings,
        fallback=None,
    ):
        self._primary = primary
        self._fallback = fallback
        self._calendar = calendar
        self._logger = logger
        self._settings = settings

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def load(self, today: date) -> WatchlistContext:
        """主入口：装载当日全量契约（§2.3 / §2.6）。

        流程与兜底分支：
        1) 非交易日（calendar.is_open(today)==False）→ 直接空载、禁开新仓（防御，理论上不应被拉起）；
        2) 取数失败（主路 + 备路均失败）→ degraded 上下文（禁开新仓、tradable={}）、记 warn 告警、
           **不抛异常**（loader 失败不得拖垮常驻进程）；
        3) 逐行归一 / universe 兜底 / tradable_flag 拆名单 / 价位预算 → 拆 tradable / watch_only；
        4) 读 market_state 判空仓闸门 → open_new_position_allowed。
        """
        # —— 1) 交易日校验：非交易日空载，盘中不开新仓（§2.6 防御性兜底）——
        if not self._calendar.is_open(today):
            self._logger.info("watchlist_skip_non_trading_day", trade_date=str(today))
            return WatchlistContext(
                trade_date=today,
                is_open=False,
                open_new_position_allowed=False,
                tradable={},
                watch_only=[],
            )

        # —— 2) 取数（主路失败切备路，均失败进降级态，不抛异常）——
        # 兜底捕获非约定异常（评审二轮 P3#79）：_fetch_selected_stocks 内仅约定抛 WatchlistLoadError，但取数源
        # 实现（HTTP/SQLite）可能逸出其它异常（连接、解码、属性等），若不兜底会冒出 load() 拖垮常驻进程。
        # 这里把任何非约定异常也收敛为降级"只守仓"（与契约失败同口径），绝不让异常逸出。
        try:
            rows = self._fetch_selected_stocks(today)
        except WatchlistLoadError as exc:
            # 取契约失败：禁开新仓、只守仓；tradable={}；记告警；返回降级上下文（不向上抛）。
            reason = str(exc)
            self._logger.warn(
                "watchlist_degraded",
                trade_date=str(today),
                reason=reason,
            )
            return WatchlistContext(
                trade_date=today,
                is_open=True,
                open_new_position_allowed=False,
                tradable={},
                watch_only=[],
                degraded=True,
                degraded_reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 非约定异常也降级，绝不逸出 load() 拖垮常驻进程（评审二轮 P3#79）
            reason = f"watchlist 取数未预期异常: {exc!r}"
            self._logger.error("watchlist_degraded_fetch_unexpected", trade_date=str(today), reason=reason)
            return WatchlistContext(
                trade_date=today,
                is_open=True,
                open_new_position_allowed=False,
                tradable={},
                watch_only=[],
                degraded=True,
                degraded_reason=reason,
            )

        # —— 3) 逐行处理 + 4) 空仓闸门：整段再包一层异常防护（§2.6）——
        # 业务意图：任何未预期异常（脏数据致价位计算抛错、市场状态解析异常等）一律回落到
        # 「降级只守仓」，绝不让异常逸出 load() 拖垮常驻进程（§2.6/§2.8 硬口径）。单票级异常已在
        # _split_by_tradable 内逐行隔离，这里是整批兜底的最后一道防线。
        try:
            tradable, watch_only = self._split_by_tradable(rows, today)
            market_state = self._resolve_market_state(rows)
            # 开新仓需「交易日 + 非降级 + 非空仓」全部成立；本分支已是交易日且取数成功（非降级），
            # 故 open_new_position_allowed 仅取决于空仓闸门（§2.6）。
            open_new_position_allowed = self._resolve_open_gate(market_state)
        except Exception as exc:  # noqa: BLE001 - loader 失败一律降级，绝不抛出拖垮常驻进程
            reason = f"watchlist 处理异常: {exc!r}"
            self._logger.error("watchlist_degraded_unexpected", trade_date=str(today), reason=reason)
            return WatchlistContext(
                trade_date=today,
                is_open=True,
                open_new_position_allowed=False,
                tradable={},
                watch_only=[],
                degraded=True,
                degraded_reason=reason,
            )

        self._logger.info(
            "watchlist_loaded",
            trade_date=str(today),
            tradable_count=len(tradable),
            watch_only_count=len(watch_only),
            market_state=market_state,
            open_new_position_allowed=open_new_position_allowed,
        )
        return WatchlistContext(
            trade_date=today,
            is_open=True,
            open_new_position_allowed=open_new_position_allowed,
            tradable=tradable,
            watch_only=watch_only,
        )

    # ------------------------------------------------------------------
    # §2.4 私有方法（对齐伪签名）
    # ------------------------------------------------------------------
    def _fetch_selected_stocks(self, today: date) -> List[SelectedStockRow]:
        """整批取 target_trade_date=today 的信号行（§2.4）。

        路径 A 直读 MySQL / 路径 B 只读端点，两路同契约（差异已由 SelectedStockSource 屏蔽）。
        兜底（§2.6 A↔B 互为备份）：
        - 主路成功 → 直接返回（空列表也算成功，表示当日无候选）；
        - 主路抛 WatchlistLoadError 且配置了备路 → 切备路再试；
        - 备路仍失败 → 抛 WatchlistLoadError 交由 load() 走降级（本方法不静默吞）。
        """
        try:
            return self._primary.fetch(today)
        except WatchlistLoadError as primary_exc:
            if self._fallback is None:
                # 无备路：主路失败即整体失败，原样上抛走降级。
                raise
            # 主路失败、切备路：记 warn 留痕（不含敏感信息），再试备路。
            self._logger.warn(
                "watchlist_primary_failed_switch_fallback",
                trade_date=str(today),
                reason=str(primary_exc),
            )
            try:
                return self._fallback.fetch(today)
            except WatchlistLoadError as fallback_exc:
                # 主备双失败：合并原因上抛，由 load() 落降级态。
                raise WatchlistLoadError(
                    f"primary and fallback both failed: primary={primary_exc}; "
                    f"fallback={fallback_exc}"
                ) from fallback_exc

    def _split_by_tradable(
        self, rows: List[SelectedStockRow], today: date
    ) -> Tuple[dict, List[TradableEntry]]:
        """逐行处理并拆 (可交易名单 dict, 观察名单 list)（§2.3 第 3–5 步 / §2.4 _split_by_tradable）。

        单行判定顺序（任一不满足即转观察名单，单票降级不影响整批，§2.6）：
        1) 代码归一：identity.resolve_code(ts_code)；归一失败（None，脏代码/无法判交易所）→ 观察名单；
        2) universe 前缀白名单兜底：_apply_universe_filter；不在白名单 → 观察名单；
        3) 价位预算：_budget_prices；price_source==MISSING（signal_close 与涨停价/区间全缺）→ 观察名单；
        4) tradable_flag 为假（一字/秒封先验买不进等）→ 观察名单；
        其余进可交易名单，key=norm_code（盘中 O(1) 查）。
        """
        tradable: dict = {}
        watch_only: List[TradableEntry] = []

        for row in rows:
            # 单票异常隔离（§2.6 单票降级不影响整批）：脏数据（如价位字段被投成非法类型导致
            # budget_prices 抛 TypeError/InvalidOperation）只丢弃该票并告警，绝不让单票异常拖垮整批。
            try:
                self._process_row(row, today, tradable, watch_only)
            except Exception as exc:  # noqa: BLE001 - 单票降级：记 warn 并跳过该票，整批继续
                self._logger.warn(
                    "watchlist_row_skipped_on_error",
                    raw_ts_code=getattr(row, "ts_code", None),
                    trade_date=str(today),
                    reason=repr(exc),
                )
                continue

        return tradable, watch_only

    def _process_row(
        self, row: SelectedStockRow, today: date, tradable: dict, watch_only: List[TradableEntry]
    ) -> None:
        """处理单行信号（§2.3 第 3–5 步）：归一 → universe → 价位 → tradable_flag 拆名单。

        判定顺序见 _split_by_tradable docstring。异常由调用方 _split_by_tradable 逐行捕获隔离。
        """
        # 1) 归一代码（与 qmt_trade 关联键一致，§6.3）。
        norm = identity.resolve_code(row.ts_code)
        if norm is None:
            # 脏代码 / 无法判定交易所：无法形成关联键，转观察名单留痕。
            self._logger.warn(
                "watchlist_drop_unresolved_code",
                raw_ts_code=row.ts_code,
                trade_date=str(today),
            )
            watch_only.append(
                self._to_entry(
                    row, norm_code=str(row.ts_code), price=self._budget_prices(row), today=today
                )
            )
            return

        # 价位预算（先算好，无论进哪个名单都透传，便于观察名单复盘对照）。
        price = self._budget_prices(row)

        # 2) universe 前缀白名单兜底（冗余防线，防信号侧规则漂移）：仅以代码前缀过滤科创/北交/B股等非目标段
        #    （§2.3 第 3 步 / universe_filter）。注：SelectedStockRow 现已含 name/is_st（契约补齐），ST/退市判定
        #    在紧随其后的 2.1 步用 is_st_stock 统一处理；停牌/退市等其余 live 名称判定仍可后续在盘中路径补强。
        if not self._apply_universe_filter(norm):
            self._logger.info(
                "watchlist_universe_reject",
                norm_code=norm,
                trade_date=str(today),
            )
            watch_only.append(self._to_entry(row, norm_code=norm, price=price, today=today))
            return

        # 2.1) 买入前置过滤层（doc/18 第 1 层）：统一委托 buy_prefilter 跑「禁买」硬规则集——命中任一
        #      （ST/退市整理 或 四板及以上）即剔出可交易名单、转观察（不下单）。ST 口径不变（显式 is_st=True
        #      或证券名含 ST/退）；四板及以上按 board_level>=阈值 或 tier==HIGH_BOARD 兜底。
        #      「绝不买入」由本层(loader 前置过滤) + entry_router(_should_skip) + order_executor(place) 三层叠加兜底。
        verdict = buy_prefilter.evaluate(
            buy_prefilter.CandidateView(
                ts_code=norm,
                name=row.name,
                is_st=getattr(row, "is_st", None),
                board_level=row.board_level,
                tier=row.tier,
            ),
            high_board_min_level=self._settings.forbid_board_level_min,
        )
        if not verdict.allowed:
            self._logger.info(
                "watchlist_prefilter_reject",
                norm_code=norm,
                name=row.name,
                trade_date=str(today),
                rule_code=verdict.rule_code,
                reason=verdict.reason,
            )
            watch_only.append(self._to_entry(row, norm_code=norm, price=price, today=today))
            return

        # 3) 价位全缺（MISSING）：无法预算今日价位，单票降级转观察名单（§2.6）。
        if price.price_source == PriceSource.MISSING:
            self._logger.info(
                "watchlist_price_missing",
                norm_code=norm,
                trade_date=str(today),
            )
            watch_only.append(self._to_entry(row, norm_code=norm, price=price, today=today))
            return

        entry = self._to_entry(row, norm_code=norm, price=price, today=today)

        # 4) tradable_flag 为假（含 None）：可成交性不成立 → 观察名单（盘中只跟踪不下单）。
        if not row.tradable_flag:
            watch_only.append(entry)
            return

        # 通过全部闸门：进可交易名单，key=norm_code。
        tradable[norm] = entry

    def _apply_universe_filter(self, norm_code: str) -> bool:
        """universe 前缀白名单兜底（§2.4 _apply_universe_filter）。

        复用信号侧同一套 universe 规则（闭式 allow-list 前缀白名单：仅放行
        600/601/603/605/000/001/002/003/300/301），其余一律剔除（科创 688/689、北交 8xx/920/4xx、
        B 股等非目标段）。这是冗余防线——信号侧理应已过滤，执行侧再过一遍防规则漂移 / 脏数据。

        边界：loader 阶段只做前缀白名单；ST/停牌/退市的实时名称判定不在此处（loader 无名称数据），
        留待盘中 live 路径按当日实时行名称判定（§2.3 ST 口径）。
        """
        return universe_filter.is_allowed_prefix(norm_code)

    def _budget_prices(self, row: SelectedStockRow) -> PriceBudget:
        """按 board 预算今日价位（§2.4 _budget_prices，直接复用 common.board_rules）。

        优先采用信号侧 limit_up_price / reasonable_open_high_*（price_source=SIGNAL）；
        缺失则以 signal_close + board 规则（主板±10% / 创业板±20%，不复权，四舍五入到 0.01）
        兜底现算并标 price_source=LOCAL_CALC；signal_close 与价位字段全缺 → MISSING（由上层转观察名单）。
        """
        return board_rules.budget_prices(row)

    def _resolve_open_gate(self, market_state: Optional[str]) -> bool:
        """读 market_state 判空仓闸门（§2.4 _resolve_open_gate / §2.6 保守口径）。

        - market_state 取不到（None，含取数列缺失 / 不可解析）→ 返回 False（按空仓保守处理，禁开新仓）；
        - market_state ∈ settings.market_state_block（默认 {空仓, 谨慎参与}）→ 返回 False（禁开新仓）；
        - 其余（参与）→ 返回 True（允许按可交易名单开新仓）。

        评审 2.5：禁开仓集合统一用 settings.market_state_block（与 entry_router._should_skip /
        risk.is_open_blocked 同一口径），不再只硬判「空仓」一值——否则信号侧「谨慎参与」日会漏挡、
        照常开新仓（已确认口径：谨慎参与=禁开仓，仅「参与」开仓）。
        该闸门**只关「开新仓」**，不影响卖出 / 守仓（T+1 已买入持仓仍按既定纪律处置，§2.6）。
        """
        if market_state is None:
            return False
        if market_state in set(self._settings.market_state_block or []):
            return False
        return True

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _resolve_market_state(self, rows: List[SelectedStockRow]) -> Optional[str]:
        """取当日 market_state：行集中首个非 None 值（§2.3 第 6 步「当日情绪周期统一口径」）。

        说明：market_state 是当日全局情绪周期，理应全行一致；取首个非 None 容错（个别行缺失不影响判定）。
        全行皆 None（取不到）→ 返回 None，交由 _resolve_open_gate 按空仓保守处理。
        """
        for row in rows:
            if row.market_state is not None:
                return row.market_state
        return None

    def _to_entry(
        self, row: SelectedStockRow, *, norm_code: str, price: PriceBudget, today: date
    ) -> TradableEntry:
        """把信号行 + 归一代码 + 价位预算组装为 TradableEntry（§2.5 透传字段）。

        透传字段用于回流期与 qmt_trade / limit_up_selected_stock join（§2.5）：
        - target_trade_date = today（今日 = 信号 T 的 T+1 买入日；以装载日为准，不盲信行内值）；
        - signal_trade_date = row.trade_date（T，便于回流期 join limit_up_selected_stock）；
        - market_state / role / strategy / 各先验分 / 路由维度（strategy_family/setup/
          first_board_vol/float_mktcap）原样透传，供下单决策与复盘归因。
        """
        return TradableEntry(
            norm_code=norm_code,
            target_trade_date=today,
            signal_trade_date=row.trade_date,
            market_state=row.market_state,
            tradable_flag=row.tradable_flag,
            role=row.role,
            strategy=row.strategy,
            leader_strength_score=row.leader_strength_score,
            continuation_prob=row.continuation_prob,
            next_day_premium_prob=row.next_day_premium_prob,
            price=price,
            strategy_family=row.strategy_family,
            setup=row.setup,
            first_board_vol=row.first_board_vol,
            float_mktcap=row.float_mktcap,
            # ST 识别透传（禁买 ST 硬规则）：name/is_st 一路带到 PlanRow/EntryDecision，供 entry_router 与
            # order_executor 的禁买 ST 闸复核——即便本票漏过 universe 层（如 DB 直读源未走本拆名单），下游仍拦得住。
            name=row.name,
            is_st=row.is_st,
            # 连板维度透传（doc/18 禁买四板及以上）：board_level/tier 一路带到 PlanRow/EntryDecision，
            # 供 entry_router 与 order_executor 的禁买四板及以上闸复核（与 ST 同样三层兜底）。
            board_level=row.board_level,
            tier=row.tier,
            # 打板因子透传（契约 1.2.0）：封板时序 + 位置/强度，带到 PlanRow 供策略消费（默认不改行为，配阈值才生效）。
            first_limit_time=row.first_limit_time,
            last_limit_time=row.last_limit_time,
            open_times=row.open_times,
            volume_ratio=row.volume_ratio,
            return_5d_pct=row.return_5d_pct,
            return_10d_pct=row.return_10d_pct,
        )
