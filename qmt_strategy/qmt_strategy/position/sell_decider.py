"""卖出/连板决策 sell_decider（§5.3）。

业务意图：在「守 T+1 / 风控闸门」两道前置约束都放行后，把『信号侧先验（方向性基调）』与
『执行侧 xtdata 实时盘口（秒级扳机）』两路融合为一个定性卖出动作（HOLD / REDUCE / CLEAR）。
本模块只产出「动作 + 触发理由」，**价位一律不在此处**——由 QMT 下单层结合实时盘口与滑点控制
自定（§5.3 末 / §5.7）。

融合原则（§5.3.1「先验定基调、盘口定扳机」）：
- 先验强（continuation_prob 高、未命中 fail_conditions / SIGNAL_DRIVEN）→ 基调倾向续持（HOLD），
  但盘口一旦命中 fail_conditions / 破位 / 炸板 → 实时盘口一票否决，转减 / 清。
- 先验弱或缺失（prior 为空、TECH_EXIT）→ 基调倾向了结，纯盘口驱动，破位即出（保守安全默认）。

不可推翻的口径（与 doc/03 §5.3 一致）：
- 守 T+1 短路（§5.3.4）：unit.state == LOCKED_T1 → 连决策都不进入实质分支，直接返回 HOLD
  （reason='不可卖(守T+1)'）。卖出只对昨日及更早持仓生效。
- 风控联动（§5.4）：risk_verdict == FREEZE → HOLD（暂停新卖出，安全默认宁可不卖）；
  risk_verdict == SELL_ONLY_HOLD（空仓闸门）只锁买不锁卖，**不影响卖出**，存量仍按规则正常卖。
- 盘口来源（§5.8）：实时决策盘口一律取执行侧 xtdata 的 OrderBook，**不取信号侧 last_price 快照**。
  本模块只读 OrderBook 字段做判定，绝不读信号侧 last_price 进决策路径。

时间口径：本模块为无状态定性判定，不直接落库；如需记录决策时刻一律经 clock 取 UTC naive
（禁直接 datetime.now / 手工 ±8h，§6.6）。

依赖边界：只依赖锁定契约层（enums / models）、配置（Settings）、时钟 / 日志协议；
不 import 任何 xtquant；概率 / 比例一律用 Decimal 比较（禁 float）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from qmt_strategy.config.settings import Settings
from qmt_strategy.contracts.enums import (
    PositionMode,
    PositionState,
    RiskVerdict,
    SellActionType,
)
from qmt_strategy.contracts.models import OrderBook, PositionUnit, SellAction, SignalPrior
from qmt_strategy.contracts.protocols import Clock, StructLogger

# —— 续板概率「高」判定阈值（执行侧可调，§5.3 末「阈值在执行侧按战法定参并固化到执行侧文档」）——
# 业务意图：先验是否足够强以支撑「续持基调」。Settings 暂未提供该项专用阈值（见 contract_gaps），
# 故以构造参数注入、给一个偏保守的默认值（0.6），便于单测固定阈值、真实落地再按战法覆写。
_DEFAULT_CONTINUATION_HIGH = Decimal("0.6")

# —— fail_conditions 关键词分类表（结构化失败条件 → 盘口比对类别）——
# 业务意图：信号侧 fail_conditions 是结构化字符串（如「竞价弱于X% / 炸板未回封 / 题材退潮 / 量价背离」），
# 本模块只做「类别命中」判断（命中即作废续持），不解析具体阈值（阈值在执行侧盘口信号里已加工成 bool）。
# 背离类：竞价高开但量价不匹配（虚高 / 骤缩），对应 book.price_volume_diverge。
_FAIL_KW_DIVERGE = ("背离", "量价", "虚高", "骤缩", "缩量")
# 弱开类：平开 / 低开 / 竞价走弱，对应 open_pct 弱。
_FAIL_KW_WEAK_OPEN = ("弱", "低开", "平开", "退潮", "走弱")
# 破位 / 炸板类：盘中破位、炸板未回封等，对应 below_support / broke_board。
_FAIL_KW_BREAK = ("破位", "破", "炸板", "开板", "封不", "回封")


class SellDecider:
    """卖出 / 连板决策器（§5.3）。

    构造依赖：
    - settings：执行侧配置（情绪周期口径等，本模块当前主要透传，不直接读阈值）。
    - clock：统一时钟（落决策日志时刻用，禁直接 datetime.now / 手工 ±8h，§6.6）。
    - logger：结构化日志，卖出决策留痕（§5.10 第 7 条要求卖出动作落决策日志）。
    - continuation_high：续板概率「高」阈值（默认 0.6，执行侧可按战法覆写）。

    无状态：decide_* 为纯函数式判定，同入参恒得同结果，重跑 / 回放安全（§5.8 幂等友好）。
    """

    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        logger: StructLogger,
        continuation_high: Decimal = _DEFAULT_CONTINUATION_HIGH,
        decision_emitter: Optional[Any] = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._logger = logger
        # 续板概率「高」阈值：先验是否强到支撑续持基调（§5.3.1）。
        self._continuation_high = continuation_high
        # 决策采集器（可选）：注入则采集 SELL_HOLD（续持）决策；与交易热路径物理隔离。
        self._decision_emitter = decision_emitter

    # ==================================================================
    # 次日竞价定夺（可卖日 9:15–9:25，§5.3.2）
    # ==================================================================
    def decide_auction(
        self,
        unit: PositionUnit,
        prior: Optional[SignalPrior],
        book: OrderBook,
        risk_verdict: Optional[RiskVerdict] = None,
    ) -> SellAction:
        """次日竞价定夺：信号先验 + xtdata 实时竞价盘口 → HOLD / REDUCE / CLEAR。

        业务意图（§5.3.2 决策表）：
        - 高开强且量价匹配（open_pct 落合理高开区间、未背离）且 prior 强且未命中 fail_conditions
          → 续持（HOLD，进入分时定夺 5.3.3）。
        - 高开但量价背离（book.price_volume_diverge 或命中 fail_conditions 背离类）→ 竞价/开盘减或清，
          reason='竞价背离'。
        - 平开 / 低开走弱（open_pct 弱 或命中 fail_conditions 弱开类）→ 开盘减或清，reason='弱开'。

        前置短路（守 T+1 + 风控，优先级最高，安全默认在前）：
        - unit.state == LOCKED_T1 → 不可卖，返回 HOLD（连实质分支都不进，§5.3.4）。
        - risk_verdict == FREEZE → 暂停新卖出，返回 HOLD（§5.4）。
        - risk_verdict == SELL_ONLY_HOLD 不拦截卖出（空仓闸门只锁买，§5.4.2）。

        边界：
        - 盘口一律取 OrderBook（执行侧 xtdata），绝不读信号侧 last_price 快照（§5.8）。
        - prior 为空（TECH_EXIT / 次日没涨停）→ 无续持基调，纯盘口驱动，弱开 / 背离即减清。
        """
        # —— 前置短路：守 T+1 + 风控（任一命中即不进竞价实质分支）——
        guard = self._pre_guard(unit, risk_verdict)
        if guard is not None:
            return guard

        prior_strong = self._is_prior_strong(unit, prior)

        # —— 盘口缺失安全默认（评审三轮 EXEC-position-08）：盘口竞价信息整体缺失（open_pct 为 None 且各结构
        # 布尔全 False、open_times 0）时，原兜底分支会对先验弱标的以「弱开」口径误清仓——违背 §5.4.3「没有可信
        # 盘口不做实时卖出决策」与 main.py「book is None 不卖」口径。这里显式区分「盘口缺失→HOLD」与「open_pct<=0
        # 真实弱开→reduce_or_clear」：盘口缺失一律安全默认 HOLD，不在无可信盘口下卖在竞价低点。
        if self._is_book_missing(book):
            return self._hold(unit, reason="盘口缺失,安全默认不卖", phase="auction")

        # —— 竞价封板续持（评审 doc/19 C-2）：实时已封涨停且封流比强 → 一票续持（最强实时盘口信号）——
        # 业务意图：竞价一字 / 秒封涨停是最强的实时盘口信号；先验弱只代表「无延续预期」，不代表要在涨停高点
        # 主动放弃一只仍在封板的强势仓（更不该以现价≈涨停价挂卖砸开自家封板）。与分时 decide_intraday 的
        # 「秒板稳封→HOLD」(本文件 _is_seal_strong 同口径) 对齐，修复原 decide_auction 全程不读 is_sealed、
        # 把正封涨停的隔夜【弱先验】票走兜底误判「弱开」清仓的缺陷。置于背离/弱开判定之前：实时封板一票否决。
        # 边界：封流比未知/过低（_is_seal_strong=False，多因 plan 缺 float_mktcap）→ 封板质量不可信、不据此
        # 续持，落到下方原有分支处理（与 intraday「seal 不强则不进续持分支」一致），不过度保护误持弱质封板。
        if book.is_sealed and self._is_seal_strong(book):
            return self._hold(unit, reason="竞价封板续持", phase="auction")

        # —— 扳机一：量价背离（盘口背离 或 命中 fail_conditions 背离类）→ 一票否决续持 ——
        # 业务意图：高开但量能虚高 / 骤缩，封板预期落空，即便先验强也转减 / 清（盘口一票否决，§5.3.1）。
        if book.price_volume_diverge or self._hit_fail(prior, _FAIL_KW_DIVERGE):
            return self._reduce_or_clear(
                unit,
                prior_strong=prior_strong,
                reason="竞价背离",
                phase="auction",
            )

        # —— 扳机二：平开 / 低开走弱（open_pct 弱 或 命中 fail_conditions 弱开类）→ 开盘减 / 清 ——
        if self._is_weak_open(book) or self._hit_fail(prior, _FAIL_KW_WEAK_OPEN):
            return self._reduce_or_clear(
                unit,
                prior_strong=prior_strong,
                reason="弱开",
                phase="auction",
            )

        # —— 续持分支：高开强且量价匹配且先验强且未命中 fail → HOLD（进分时）——
        # 业务意图：先验定基调——只有先验强 + 盘口高开未背离才续持；先验弱 / 缺失即便高开也不主动续持，
        # 保守落了结（安全默认：先验缺失 + 不确定，不做主动续持，§5.4.3）。
        if prior_strong and self._is_strong_open_matched(book):
            return self._hold(unit, reason="竞价高开续持", phase="auction")

        # —— 兜底：未命中明确续持条件（先验弱 / 高开不显著但未背离 / 未明确走弱）——
        # 先验弱 / 缺失（TECH_EXIT）→ 倾向了结（弱开口径减 / 清，不恋战）；
        # 先验强但盘口未达「强开匹配」→ 保守续持进分时，由分时定夺再裁。
        if not prior_strong:
            return self._reduce_or_clear(
                unit,
                prior_strong=False,
                reason="弱开",
                phase="auction",
            )
        return self._hold(unit, reason="竞价高开续持", phase="auction")

    # ==================================================================
    # 分时定夺（可卖日 9:30 起，§5.3.3）
    # ==================================================================
    def decide_intraday(
        self,
        unit: PositionUnit,
        prior: Optional[SignalPrior],
        book: OrderBook,
        risk_verdict: Optional[RiskVerdict] = None,
    ) -> SellAction:
        """分时定夺：秒板续持 / 冲高止盈 / 走弱破位 / 炸板出 / 烂板出 / 尾盘了结。

        业务意图（§5.3.3 决策表，按危险优先级从高到低判定，破位 / 炸板一票否决先验）：
        - 炸板（book.broke_board，已封后开板、封单瓦解）→ CLEAR，reason='炸板出'（开板即出不赌回封）。
        - 走弱破位（book.below_support 且 book.volume_surge，破支撑 + 放量下杀）→ CLEAR，reason='走弱破位'。
        - 烂板（book.open_times 多、封不实）→ CLEAR，reason='烂板出'（质量差宁可出）。
        - 秒板 / 稳封（book.is_sealed 且封流比高）→ HOLD，reason='秒板续持'（吃 continuation_prob 先验）。
        - 冲高 + 量能透支 / 上方压力（book.price_volume_diverge）→ REDUCE/CLEAR，reason='冲高止盈'。
        - 冲高不及预期（book.near_close_weak，全天弱于续板预期）→ REDUCE/CLEAR，reason='尾盘了结'。

        前置短路同 decide_auction：守 T+1 + 风控（FREEZE→HOLD；SELL_ONLY_HOLD 不拦卖出）。

        边界：
        - 破位 / 炸板 / 烂板属「盘口一票否决」，无论先验强弱一律出（§5.3.1 实时盘口一票否决）。
        - 盘口一律取 OrderBook（执行侧 xtdata），不读信号侧 last_price（§5.8）。
        """
        # —— 前置短路：守 T+1 + 风控 ——
        guard = self._pre_guard(unit, risk_verdict)
        if guard is not None:
            return guard

        prior_strong = self._is_prior_strong(unit, prior)

        # —— 一票否决组（破位 / 炸板 / 烂板，盘口硬扳机，先验再强也出）——
        # 炸板：已封涨停后开板、封单瓦解 → 开板即出，不赌回封（最危险，最先判）。
        if book.broke_board:
            return self._clear(unit, reason="炸板出", phase="intraday")
        # 走弱破位：跌破分时关键支撑 / 均价线 且 放量下杀（两条件齐备才算破位止损，避免误杀缩量回踩）。
        if book.below_support and book.volume_surge:
            return self._clear(unit, reason="走弱破位", phase="intraday")
        # 烂板：反复开板（open_times 多）、封不实 → 质量差宁可出。
        if self._is_messy_board(book):
            return self._clear(unit, reason="烂板出", phase="intraday")

        # —— 秒板 / 稳封续持：封涨停且封流比高 → 吃 continuation_prob 先验续持 ——
        # 业务意图：先验定基调——秒板稳封是最强续持信号，但仍要求「未命中上面任一破位/炸板/烂板」才进此分支。
        if book.is_sealed and self._is_seal_strong(book):
            return self._hold(unit, reason="秒板续持", phase="intraday")

        # —— 冲高止盈：涨幅显著但量能透支 / 上方筹码压力（量价背离）→ 减 / 清 ——
        if book.price_volume_diverge:
            return self._reduce_or_clear(
                unit,
                prior_strong=prior_strong,
                reason="冲高止盈",
                phase="intraday",
            )

        # —— 冲高不及预期：全天弱于先验续板预期 → 尾盘了结，不强行隔夜 ——
        if book.near_close_weak:
            return self._reduce_or_clear(
                unit,
                prior_strong=prior_strong,
                reason="尾盘了结",
                phase="intraday",
            )

        # —— 兜底：未命中任何明确扳机 ——
        # 先验强（SIGNAL_DRIVEN、continuation_prob 高、未命中 fail）→ 维持续持（HOLD）；
        # 先验弱 / 缺失（TECH_EXIT）→ 纯盘口驱动、不确定则保守了结（破位即出已在上面判过，
        # 此处为未破位的弱势震荡，按尾盘了结口径减 / 清，宁可出不恋战，§5.4.3 安全默认）。
        if prior_strong:
            return self._hold(unit, reason="盘口未触发，维持续持", phase="intraday")
        return self._reduce_or_clear(
            unit,
            prior_strong=False,
            reason="尾盘了结",
            phase="intraday",
        )

    # ==================================================================
    # 前置短路：守 T+1 + 风控（§5.3.4 / §5.4）
    # ==================================================================
    def _pre_guard(
        self, unit: PositionUnit, risk_verdict: Optional[RiskVerdict]
    ) -> Optional[SellAction]:
        """守 T+1 + 风控前置短路：命中则返回 HOLD（不进实质分支），否则返回 None 放行。

        业务意图（安全默认在前，§5.3.4 / §5.4.3）：
        1. 守 T+1：unit.state == LOCKED_T1 → 当日买入不可卖，连决策都不进实质分支，返回 HOLD。
           reason='不可卖(守T+1)'。这是 A 股 T+1 物理约束，最高优先。
        2. 风控冻结：risk_verdict == FREEZE → 暂停新卖出（没有可信盘口/通道宁可不卖），返回 HOLD。
           reason='风控冻结,暂停新卖出'。
        3. risk_verdict == SELL_ONLY_HOLD（空仓闸门）→ 只锁买不锁卖，**不拦截卖出**（返回 None 放行），
           存量仍按规则正常卖（§5.4.2 避免该出的票出不掉）。
        4. risk_verdict == ALLOW / None → 放行（返回 None）。
        """
        # —— 守 T+1：买入当日锁定，连实质分支都不进 ——
        if unit.state == PositionState.LOCKED_T1:
            return self._hold(unit, reason="不可卖(守T+1)", phase="guard")

        # —— 风控冻结：暂停新卖出（安全默认宁可不卖）——
        if risk_verdict is RiskVerdict.FREEZE:
            return self._hold(unit, reason="风控冻结,暂停新卖出", phase="guard")

        # —— SELL_ONLY_HOLD（空仓闸门）/ ALLOW / None：不拦卖出，放行 ——
        return None

    # ==================================================================
    # 先验强弱判定（§5.3.1「先验定基调」）
    # ==================================================================
    def _is_prior_strong(self, unit: PositionUnit, prior: Optional[SignalPrior]) -> bool:
        """先验是否「强」（足以支撑续持基调）。

        业务意图（§5.3.1）：
        - prior 为空（次日没涨停、退出信号池）→ 必然弱（纯技术退出 TECH_EXIT，无续持基调）。
        - unit.mode == TECH_EXIT → 纯技术退出基调，视为弱（即便偶然传入了 prior 也不吃续持）。
        - 否则要求 continuation_prob 非空且 >= 续板「高」阈值，才算强。
        边界：continuation_prob 为 None（先验缺失续板概率）→ 不确定，保守视为弱（安全默认）。
        """
        # 纯技术退出模式：无续持基调，直接判弱。
        if unit.mode == PositionMode.TECH_EXIT:
            return False
        if prior is None:
            return False
        cp = prior.continuation_prob
        if cp is None:
            # 续板概率缺失：不确定宁可不主动续持（保守判弱，§5.4.3 安全默认）。
            return False
        # Decimal 直接比较（禁 float），达到「高」阈值即视为强先验。
        return cp >= self._continuation_high

    def _hit_fail(self, prior: Optional[SignalPrior], keywords: tuple) -> bool:
        """fail_conditions 是否命中某一类别（按关键词匹配结构化失败条件）。

        业务意图（§5.3.1）：信号侧 fail_conditions 给出「什么情况作废续持」的结构化判据；
        本模块只判「类别命中」——任一条目命中该类别任一关键词即视为命中（命中即作废续持）。
        边界：prior 为空 / fail_conditions 为空 → 无失败条件，返回 False（不凭空作废续持）。
        """
        if prior is None or not prior.fail_conditions:
            return False
        for cond in prior.fail_conditions:
            if cond is None:
                continue
            text = str(cond)
            for kw in keywords:
                if kw in text:
                    return True
        return False

    # ==================================================================
    # 盘口形态判定（只读 OrderBook，§5.8 盘口来源）
    # ==================================================================
    def _is_strong_open_matched(self, book: OrderBook) -> bool:
        """竞价高开强且量价匹配：open_pct 为正且未背离。

        业务意图（§5.3.2）：高开强（open_pct > 0）且量价不背离（price_volume_diverge=False），
        是续持的盘口必要条件。具体「合理高开区间」阈值由执行侧战法定参；这里以「高开为正且未背离」
        作为已加工盘口信号的判据（盘口已把区间判断折叠进 open_pct / diverge 等 bool/数值字段）。
        边界：open_pct 为 None（盘口缺失）→ 不确定，保守判「非强开」（不主动续持）。
        """
        if book.price_volume_diverge:
            return False
        if book.open_pct is None:
            return False
        return book.open_pct > Decimal("0")

    def _is_book_missing(self, book: OrderBook) -> bool:
        """盘口竞价信息整体缺失判定（评审三轮 EXEC-position-08）。

        判据：open_pct 为 None（无高开幅度）且各结构布尔全 False（未背离/未封板/未炸板/未跌破支撑/未放量）
        且 open_times==0 且 last_price 缺失——即盘口对象存在但字段整体空白（数据未到/降级 B），与「确实弱开
        (open_pct<=0)」区分。缺失时安全默认 HOLD，避免无可信盘口误清仓。
        """
        return (
            book.open_pct is None
            and book.last_price is None
            and not book.price_volume_diverge
            and not book.is_sealed
            and not book.broke_board
            and not book.below_support
            and not book.volume_surge
            and book.open_times == 0
        )

    def _is_weak_open(self, book: OrderBook) -> bool:
        """平开 / 低开走弱：open_pct <= 0（高开幅度非正即视为弱开）。

        业务意图（§5.3.2）：平开 / 低开走弱不恋战，开盘减 / 清。
        边界：open_pct 为 None（盘口缺失）→ 不在此处判弱开（避免无数据误清），由调用方兜底分支处理。
        """
        if book.open_pct is None:
            return False
        return book.open_pct <= Decimal("0")

    def _is_seal_strong(self, book: OrderBook) -> bool:
        """封板是否「稳」：封流比高（封单额相对流通市值占比足够）。

        业务意图（§5.3.3 秒板续持）：is_sealed 仅表示当前封涨停，还需封流比高才算稳封，
        才支撑「吃 continuation_prob 续持」。封流比阈值由执行侧战法定参；这里以
        seal_to_float_ratio 存在且 > 0 作为已加工信号的判据（盘口已把强弱折叠进该值）。
        边界：seal_to_float_ratio 为 None → 封板质量未知，保守判「封不稳」（不续持）。
        """
        ratio = book.seal_to_float_ratio
        if ratio is None:
            return False
        return ratio > Decimal("0")

    def _is_messy_board(self, book: OrderBook) -> bool:
        """烂板判定：反复开板（open_times 多）、封不实。

        业务意图（§5.3.3 烂板出）：open_times 多代表反复开板封板、封不实、质量差，宁可出。
        阈值「多」由执行侧战法定参；这里取 open_times >= 2（出现两次及以上开板即视为烂板）
        作为保守口径，避免一次正常波动误判。
        """
        return book.open_times >= 2

    # ==================================================================
    # 动作构造 + 留痕
    # ==================================================================
    def _reduce_or_clear(
        self, unit: PositionUnit, *, prior_strong: bool, reason: str, phase: str
    ) -> SellAction:
        """按先验强弱选择减仓 / 清仓：先验强 → REDUCE（保留底仓博续板），先验弱 → CLEAR。

        业务意图（§5.3.1 先验定基调）：
        - 先验强（SIGNAL_DRIVEN、continuation_prob 高）但盘口触发减仓信号 → 减仓 REDUCE，
          保留部分底仓博后续（不一把清空，尊重先验续板预期）。
        - 先验弱 / 缺失（TECH_EXIT）→ 直接清仓 CLEAR，不恋战（安全默认了结）。
        价位不在此处：REDUCE 的具体卖量比例 reduce_ratio 置 None（= 策略默认），由 QMT 下单层自定。
        """
        if prior_strong:
            action = SellActionType.REDUCE
        else:
            action = SellActionType.CLEAR
        return self._emit(unit, action=action, reason=reason, phase=phase)

    def _hold(self, unit: PositionUnit, *, reason: str, phase: str) -> SellAction:
        """构造 HOLD（续持 / 不可卖）动作并留痕。"""
        return self._emit(unit, action=SellActionType.HOLD, reason=reason, phase=phase)

    def _clear(self, unit: PositionUnit, *, reason: str, phase: str) -> SellAction:
        """构造 CLEAR（清仓）动作并留痕。一票否决类（破位 / 炸板 / 烂板）一律清仓。"""
        return self._emit(unit, action=SellActionType.CLEAR, reason=reason, phase=phase)

    def _emit(
        self, unit: PositionUnit, *, action: SellActionType, reason: str, phase: str
    ) -> SellAction:
        """统一出口：构造 SellAction 并落决策日志（§5.10 第 7 条卖出动作均落决策日志）。

        业务意图：所有 decide_* 分支经此产出 SellAction 并留痕，便于复盘 / 闭环归因回挂——
        reason 后续透传到 qmt_order.order_remark（§5.7），供归因侧回挂卖出意图。
        时间口径：决策时刻经 clock 取 UTC naive（禁手工 ±8h，§6.6）。
        """
        decided_at = self._clock.now_utc()
        self._logger.info(
            "sell_decision",
            account_id=unit.account_id,
            ts_code=unit.ts_code,
            phase=phase,
            action=str(action),
            reason=reason,
            mode=str(unit.mode),
            state=str(unit.state),
            decided_at=str(decided_at),
        )
        # 决策采集：仅 HOLD（续持）在此采集——REDUCE/CLEAR 的实际卖出由 place_sell 采为 SELL_SUBMIT，
        # 避免重复；续持没有对应委托，是「为什么没卖」的唯一事实源。全程吞异常，绝不影响卖出决策。
        if action == SellActionType.HOLD:
            em = self._decision_emitter
            if em is not None:
                try:
                    em.emit(
                        decision_type="SELL_HOLD", decision_stage="SELL", action="HOLD",
                        ts_code=unit.ts_code, strategy_family="SELL", reason=reason,
                        reason_code=f"hold_{phase}", decided_at=decided_at,
                    )
                except Exception:  # noqa: BLE001 决策采集绝不影响卖出
                    pass
        # reduce_ratio 一律置 None（= 策略默认），具体减仓比例由 QMT 下单层自定（§5.7 价位不在此处）。
        return SellAction(ts_code=unit.ts_code, action=action, reason=reason, reduce_ratio=None)
