# 10 · 信号侧打板报告与 watchlist 生成逻辑分析与优化建议

> 范围：信号侧 `stock-ah-premium-ai` 的「LLM 多阶段打板报告 + watchlist（`limit_up_selected_stock`）」生成链路。
> 目标：评估代码逻辑、提示词、整体步骤，以及是否需要补充 Tushare 资金流向 / 打板专题数据，并给出**经过对抗式核验、真实可落地**的优化建议。
> 产出口径：仅按阶段 + 验收 + 依赖顺序组织，不含工时/人天预估（沿用工作区文档约束）。
> 关联文档：本仓库 [`08-评审报告`](08-信号侧与执行侧量化交易代码评审报告.md)/[`09-修复台账`](09-两侧代码修复计划与执行清单.md)；信号侧 `stock-ah-premium-ai/resources/doc/` 内 `limit-up-llm-push-design.md`、`limit-up-multi-stage-analysis-refactor-plan.md`、`limit-up-analysis-improvement-implementation-summary.md`、`llm-tushare-on-demand-stock-data-plan.md`、`stock-selection-factor-design.md`。

---

## 1. 分析方法与可信度

本文结论来自两步：① 直接通读信号侧打板链路核心代码（`limit_up_push_service.py` 180KB 全部提示词/上下文装配/落表、`limit_up_leader_scoring_service.py` 六维打分、`tushare_stock_research_fetcher.py` 资金抓取、`sentiment_gate.py`、`universe_filter.py`、`db/models` 落表模型）；② 对每条候选建议做**多智能体对抗式核验**——独立读码复核「缺口是否真实存在、落地是否真有用、有无前视/越权/成本/口径错误」，苛刻判定，拿不准即否决。

共产出 29 条候选，**确认 27 条、否决 2 条**。核验过程纠正了若干「初稿看似成立但实则有坑」的提法（单位口径、`moneyflow` 是否每日批量、归一化、消费落点、字段是否真在该阶段输入里存在等），这些纠正已写入下文「⚠️ 口径纠正」，请落地时严格遵循，避免照搬错误版本。

---

## 2. 现状梳理

### 2.1 LLM 多阶段流水线（`_generate_multi_stage_llm_report`，`limit_up_push_service.py:2305`）

```
FIRST_BOARD(题材发酵·JSON)
   ├─ FIRST_BOARD_SELECTION(首板精选≤5·JSON)
   ├─ CHAIN_SELECTION(两/三连精选≤20·JSON)
   └─ HIGH_BOARD_SELECTION(高连板精选≤10·JSON)
        ↓ 对入选股(去重≤35只)补 cyq 筹码(_build_selected_stock_supplements)
   ├─ FIRST_BOARD_FOCUS / CHAIN_FOCUS / HIGH_BOARD_FOCUS
   │     (JSON-mode：html_fragment + 结构化先验 continuation_prob/next_day_premium_prob)
        ↓
   FINAL_REPORT(合成 HTML 复盘报告)
        ↓
   龙头六维打分 score_stocks  →  落 limit_up_selected_stock(watchlist, T→T+1)
        ↓
   INVESTMENT_ADVICE(报告 READY 后附加，生成高风险短线建议 HTML)
```

各阶段均有缓存（按 `trade_date+stage_key+model+prompt_version+input_hash`）与确定性兜底（LLM 失败仍能 READY）。

### 2.2 watchlist 契约（`LimitUpSelectedStock`，`db/models/notification.py:228`）

下发执行侧的只读因子：`leader_strength_score`、`strength_dim_json`(六维子分)、`role_tags`、`strategy_family`、`setup`、`action`、`sentiment_cycle`、`market_state`、`tradable_flag`、`continuation_prob`、`next_day_premium_prob`、`boost/fail_conditions`、`seal_ratio_pct`、`limit_order`、`turnover_rate`、`close`、`winner_rate`、`float_mktcap`、`first_board_vol`、`priority`、`selection_reason`。**全部为价量 / 筹码 / LLM 概率，无一个资金净流入 / 龙虎榜 / 游资 / 机构字段。**

### 2.3 当前接入打板链路的 Tushare 接口

| 接口 | 用途 |
|---|---|
| `kpl_list`(按 tag) | 涨停 / 炸板 / 跌停 / 昨涨停主池 |
| `limit_list_ths` / `limit_list_d` | 同花顺 / 通用涨停榜（补充字段，含 `limit_times`） |
| `limit_step` | 连板天梯（`nums`） |
| `limit_cpt_list` | 涨停题材最强板块统计 |
| `top_list` | 龙虎榜每日明细（净买额 `net_amount`） |
| `daily` / `daily_basic` | 行情 / 量比换手市值 |
| `cyq_perf` / `cyq_chips` | 入选股筹码分布（逐股抓） |

### 2.4 关键缺口（本文主线）

- **`moneyflow`（个股大单/超大单净额）已落 `a_moneyflow` 表**（`tushare_stock_research_fetcher.py:648-698`，含 `net_mf_amount`/`buy_elg_amount` 等 20 字段），**但仅在「个股研究/AI 问答」按需路径抓取**（`market_data_orchestrator.py` 带 14 天新鲜度触发），**打板报告、六维打分、watchlist 全链路零引用**。
- **六维打分的「资金」维（`_money_subscore`，`limit_up_leader_scoring_service.py:228`）是代理**：`0.6×量比分位 + 0.4×换手倒U`，注释自承「v1 暂不消费资金净流入」，却占 `WEIGHTS_V1` 中 0.15 权重。
- **`_capital_signals`（`:2051`）只把龙虎榜 `top_list` 按代码 inner-join**（`if not top: continue`），绝大多数涨停股不在龙虎榜 → 资金证据对全涨停池**系统性缺失**。
- **板块/大盘资金流向、游资席位(`hm_*`)、龙虎榜机构(`top_inst`)、概念人气(`kpl_concept`)** 全链路缺席。

> **一句话结论：** 当前链路「看封板强度 + 看连板高度 + 看筹码 + 让 LLM 凭文本判题材」，但**最关键的「谁在用真金买」这一维基本是空白或代理**。补齐「真实资金净流入」是性价比最高的优化主线；其次是把已有数据用足（龙虎榜身份零成本接入、分位池修正、闸门前移、提示词正向字段白名单）。

---

## 3. 数据接口优化建议（是否补 Tushare 资金/打板专题数据）

> 总原则：**信号侧只下发只读因子，不出仓位/买卖价**；所有数据为 T 日盘后定稿，用于 T+1，**无前视**；优先单次全市场调用，控制限频/积分成本。

### 3.1【P0·资金主线】个股资金流向 `moneyflow` 接入打分与上下文

**现状**：六维 money 维 = 量比+换手代理；`a_moneyflow` 有数据但不进打板链路。量比+换手只能刻画「活跃/分歧」，**无法区分「大单主动买进封板的强龙头」与「游资对倒放量出货的诱多封板」**——而后者正是打板跟买最怕的。

**建议**（合并自数据/步骤/打分三维的同源结论 `D1-moneyflow-stock` + `S1-moneyflow-into-compact` + `S1-money-proxy` + `S8`）：

1. **数据接入**：新增一次 `moneyflow` 按 `trade_date` 全市场拉取（与 `daily_basic` 现有「全市场单批 → 过滤 focus」同款，`limit_up_push_service.py:1678`），过滤到涨停池构建 `{ts_code: {net_mf_amount, net_elg, net_lg}}`；**单独成步，不要塞进需要 `ts_code` 的 `OPTIONAL_APIS` 通用循环**。
2. **并入候选行**：在 `_compact_stock_row`(`:1940`) 增 `net_mf_ratio`/`elg_net_ratio`（**归一化值**，见纠正）。
3. **主消费点是确定性打分**（核验结论：比喂 LLM 选股提示词更稳、可回测）：改 `_money_subscore` 为含「主力净额占比分位 / 超大单净买占比」子项，量比降权；权重显式标「回测前占位，待 walk-forward 校准」。
4. **次消费点**：把同字段并入三层 SELECTION 的 compact 行与 FOCUS 输入，让 `capital_signal` 维度有据（详见 §4.3、§5.3）。
5. **兜底统一**：确定性兜底排序 `_fallback_rank_stocks`(`:3099`) 同步纳入资金净额（原 `S9` 并入本项，不单独立项）。

> ⚠️ **口径纠正（必须遵守，否则会引入 1e4 级错配 bug）**
> - **单位**：Tushare `moneyflow` 金额口径为**万元**；`row['amount']`（来自 `daily.amount`）为**千元**；`daily_basic.circ_mv` 为**万元**。计算占比前必须统一换算到同一单位（如均转「元」：`moneyflow×1e4`、`amount×1e3`），并对换算后比值加合理上界保护（`|净占比|>1` 视为脏数据置缺）——参照已有的 `circ_mv×10000`（`:1957`）。
> - **归一化是硬要求**：不要塞原始 `net_mf_amount`（绝对净额与流通盘正相关，会机械抬高大市值股），要塞 `net_mf_amount/流通市值` 或 `净额/成交额` 或截面分位。
> - **缺测**：`None` 一律按中性（`imputed` 痕迹），**绝不当 0 抬分**。
> - **抓取可行性前置验证**：`moneyflow` 当前只在按 `ts_code` 的研究路径抓过（`fetcher` 有「15000 积分不做全市场扫描」的护栏，但那是 LLM 逐股研究护栏，`daily_basic` 全市场已在该档运行）。落地前先跑一次性脚本确认当前积分档**支持 `trade_date`-only 全市场取数**；若不支持，退化为「仅对入选 watchlist 少数票逐股抓」（复用 `_stock_supplement` 缓存钩子 `:3199`，成本 O(几十)）。
> - **覆盖率前置评估**：先抽几个历史信号日看 `a_moneyflow` 在涨停/入选池的真实覆盖率；若大面积 `None`，该子项收益受限，据此决定走全市场批还是 per-selected 抓。

**预期价值**：money 维从「活跃度代理」升级为「真实主力资金方向」，对「seal/height 强但资金实为净流出的高位诱多接力票」能有效降分，直接提升「看得准龙头」与次日溢价判断。

### 3.2【P1·资金主线】龙虎榜身份零成本接入 watchlist（`dragon_tiger`）

**现状**：watchlist 无任何资金/身份因子；执行侧竞价强开追只能凭封流比/量能比两个分母自算，看不到「T 日是否有资金接力」。

**建议**（`S2-watchlist-capital-fields` 的 P0 部分）：`top_list` **已每日抓取**（`OPTIONAL_APIS`），`_capital_signals` 已建 `top_by_code` 索引（`:2054`）。在 `LimitUpSelectedStock`/`LimitUpWatchlistItem` 新增 3 个**只读快照列**——`dragon_tiger_flag`(Bool)、`lhb_net_amount`(龙虎榜净买额, 元)、`lhb_net_rate`(净买占比)，落表点 `:602-660` 处按 `ts_code` join 填充。bump `WATCHLIST_SCHEMA_VERSION` 至 1.1.0，新列可空，执行侧按版本兼容旧行。**零新增抓取、无前视、无越权。**

> ⚠️ 纠正：`main_net_inflow`/`elg_net_ratio`（来自 `moneyflow`）**不是零成本**（依赖 §3.1 的批量抓取），列为 P1、与 §3.1 同批落地；不要宣称「复用已抓数据零成本」。

### 3.3【P1】游资席位 `hm_detail` + 名录 `hm_list` 提升接力辨识度

**现状**：`_capital_signals` 只有「净买额数字」，没有「谁在买」。同样 1 亿净买，知名游资接力 vs 普通营业部，对次日接力含义完全不同，而打板辨识度高度依赖席位身份。

**建议**（`D4-hm-list`，核验后收窄）：**仅做上下文/提示词侧（第一阶段，可逆低成本）**——
1. `hm_detail` 加入按 `trade_date` 全市场批量（复用 `_safe_query` 降级）；`hm_list` 静态低频（季度）拉取播种一份「席位名→知名度」人工字典（标 `imputed`、季度复核）。
2. `_capital_signals` 对命中票聚合 `famous_hm_buy`/`famous_hm_names`，缺测留痕不抬分；注入 SELECTION/FOCUS 辨识度维度（措辞见 §5.3），watchlist 入选理由可附 `famous_hm_names`。
3. **剔除 `top_inst`**：机构席位买连板票常是派发逆向信号而非接力认可，易引噪声，不纳入本捆绑。
4. **第二阶段（暂缓，待回测）**：是否在 `_recognition_subscore` 加硬加分。鉴于 recognition 权重仅 0.10 且为占位，盲目加分价值小、过拟合风险高，须回测确认 lift 再上。

> ⚠️ 风险：席位名→知名度映射需人工维护、会随营业部更名/游资轮换过时；游资席位「接力」≠次日必涨，只作辨识度加分线索，**不得诱导买点**（守信号/执行边界）。

### 3.4【P1】板块资金流向 `moneyflow_ind_dc` 量化题材发酵

**现状**：`_theme_summary`(`:2006`) 题材强弱只有「涨停家数」单维，缺资金动能。同样 5 只涨停，主力大幅净流入 vs 净流出，发酵质量天差地别，但现有上下文无法区分。

**建议**（`D2-moneyflow-ind`，核验后分两步、先验证映射再上提示词）：
1. **第一步（可独立验收）**：`moneyflow_ind_dc` 按 `trade_date` 全市场单次拉取入 `optional_payload`；**离线统计 KPL `theme` 集合与东财板块名集合的精确+别名映射命中率，作为 go/no-go 闸门**（命中率过低则只对主流题材提供资金证据，不全量）。
2. **第二步（命中率达标后）**：`_theme_summary` 给每个 theme bucket 增 `sector_net_amount`/`sector_net_rank`；**严格 imputed 范式——未命中题材标 `sector_flow_unmatched=true` 且不赋值、不参与排序惩罚，绝不把「未匹配」等同「净流出」**。提示词仅对已命中题材追加资金措辞，unmatched 回退原有家数+梯队判断。
3. **口径锁定**：只接东财 `_dc` 一条，**不并行 `_ths`/`_cnt`**（口径不同会打架）；只服务 FIRST_BOARD 题材发酵判断，不进六维打分。

> ⚠️ 落地难点是 KPL 细分题材 ↔ 东财标准板块名的映射（不同分类体系），是真正的工程量（一张需维护的映射表）。落地时确认 `_safe_query` 的 ST 过滤对板块行为 no-op。

### 3.5【P2】大盘资金流向 `moneyflow_mkt_dc` 佐证情绪周期

**现状**：情绪周期（启动/发酵/高潮/分歧/退潮/冰点）全靠涨停结构指标（炸板率/晋级率/昨涨停溢价），缺「全市场资金是流入还是撤离」的总量背景。

**建议**（`D3-moneyflow-mkt`，核验后降级为低成本可选）：
- 单独做一次 `moneyflow_mkt_dc` 近 5 日窗口请求（`start_date/end_date`，**不能塞进单日 `OPTIONAL_APIS` 循环**），落 `_emotion_cycle_metrics` 返回体新增 `market_net_amount`/`market_net_5d_trend`，在 `_final_report_prompt` 情绪周期措辞补「结合全市场主力净流入与近 5 日趋势佐证退潮/冰点 vs 启动/发酵」。
- **明确这是 LLM 叙事佐证，不进六维分、不进确定性闸门**（除非显式扩 `flatten_gate_inputs`，本期不做——缺阈值与回测依据）。
- `moneyflow_hsgt`（沪深港通）**不接入**：口径随北向额度披露规则变化、近年调整、对短线打板边际价值低。
- 因无确定性消费者且增益未经回测，建议先作情绪面观察项跑 N 期人工评估，再决定是否固化。

### 3.6 明确**不建议**接入的数据

| 接口 | 结论 | 原因 |
|---|---|---|
| `stk_auction`（集合竞价） | **红线·不接入** | T+1 集合竞价是 T+1 当天才有的数据 → **前视**；且竞价择时是执行侧盘中自采自决领域 → **越权**。代码现状已正确（watchlist 只下发 `float_mktcap`/`first_board_vol` 两个分母，把竞价比值计算留给执行侧）。注意 `kpl_list` 的 `bid_*` 字段是 **T 日**盘中数据、仅供内部上下文，不构成前视。 |
| `dc_hot` / `ths_hot`（个股人气榜） | **红线·不接入** | 散户关注度驱动、噪声大，纳入会抬高网红高位股辨识度，与打板「资金+卡位」克制原则相悖。建议写入数据接入口径文档作正式红线。 |
| `top_inst`（龙虎榜机构明细） | 不纳入资金/辨识度捆绑 | 机构买连板票常为派发逆向信号，对打板接力辨识度增益低且易噪声。 |
| `moneyflow_dc` / `moneyflow_ths`（东财/同花顺个股资金口径） | **不与原生 `moneyflow` 并行** | 三套口径对「大单/主力」阈值不同、`net` 值不可互换，混入同一分位分布会污染 money 子分。先用已落表的原生 `moneyflow`（改造面最小）；仅当回测证明口径不足，才用 `moneyflow_dc` 单口径**整体替换**（非叠加），并迁移 `a_moneyflow` 映射与 `v_stock_moneyflow_recent` 视图。**硬约束：money 子分任一时刻只允许单一资金口径进入分位分布。** |
| `kpl_concept` / `kpl_concept_cons`（概念人气） | 现阶段不建议 | 与已用的 `limit_cpt_list`（题材强度 `rank`/`cons_nums`）信息高度重叠，对「资金+卡位」核心无增量，ROI 低。其原拟「作板块资金 fallback」的前提（板块资金特征）本期不一定落地，不要预埋。 |

---

## 4. 步骤逻辑调整建议

### 4.1【P0】六维打分分位池修正：用全涨停池建分布（`S2-scoring-context-pool`）

**问题**：`_do_persist_selected_stocks`(`:580`) 调用 `score_stocks([row for _, row in tiered])`，`tiered` 仅 LLM 三层入选（≤35 只）。`build_scoring_context` 用**这同一批已被选过、且只有几十只的极小样本**建 `seal_dist`/`amount_ratio_dist`/`first_time_dist` 分位分布——所有入选股本就偏强，分位被压扁，区分度尽失；当日只有 1-2 只高连板时，height/seal 分位几乎恒为 0.5/1.0。这违背 `score_stocks` docstring 自己的警示（应传当日较大候选集）。

**建议**：`score_stocks` 拆分 `distribution_rows`（建分布用全涨停池 = `context['limit_up_stocks']`）与 `score_rows`（实际打分的入选股）两个参数；分位基准来自全市场，打分对象仅入选股。纯内部数据流，无新增接口，需同步更新单测分位基准。

### 4.2【P0】空仓闸门前移并注入提示词（`S7-gate-llm-decoupling`）

**问题**：执行顺序是「LLM 全阶段跑完 → 落表时才 `resolve_gate` 判 `market_state`」。LLM 各阶段只拿到 `emotion_cycle` 文本、不知确定性闸门结论；可能 FOCUS 给「重点观察/竞价参与」先验，而盘后闸门判「空仓」后落表把 `action` 强改「放弃观察」、`tradable_flag='BLOCKED'`——**报告正文与 watchlist 行级动作口径打架**，且 LLM 还为已被否决的日子白跑选股/cyq/FOCUS（无谓成本）。

**建议**：
1. **（P0 核心）** 把 `resolve_gate` 前移到 `_assemble_context` 之后、`_generate_llm_report` 之前（输入 `flatten_gate_inputs(market_emotion)` 全为 T 日盘后量，**无前视**），把 `market_state/sentiment_cycle/gate_reasons` 写入 `context['market_context']`，在 `_stage_system_prompt` 与各 FOCUS/FINAL 提示词显式声明「确定性闸门判定=空仓/谨慎/参与，空仓档只给观察不给参与建议」。**gate 只 resolve 一次，缓存进 context，落表段复用同一对象**（避免两处各算一遍漂移）。
2. **（P1 可选，不与 P0 绑死）** 空仓档短路 cyq 补数与 FOCUS 参与性分析，省 LLM/cyq 调用——但须保留「观察型降级报告」路径，走与 `_fallback_focus_stage` 一致的容错口径。属成本优化，价值低于「口径统一」，先做 P0 验证「报告↔watchlist 不再冲突」再单独评估。
3. 落表覆盖逻辑（`:632/635`）已实现，仅改为复用前置 gate 对象，不需改动。

### 4.3【P1】`_capital_signals` 改以资金净额为主、龙虎榜降为增强标注（`S3` + `S8`）

**问题**：`_capital_signals` inner-join 龙虎榜后 `if not top: continue`，丢弃所有非龙虎榜涨停股 → 长期稀疏甚至为空；`first_board_context`(`:1807`) 连这个都没有（**首板选股 prompt 列了资金信号档却收不到任何资金数据**）。

**建议**（依赖 §3.1 的 `moneyflow` 批量先打通）：
1. 重构 `_capital_signals`：以 `moneyflow` 主力净额为主体，**对每只涨停股**产出 `main_net_amount`/`net_mf_amount`/`net_elg`，排序键改 `main_net_amount`；`top_list` 改 **left join 增强标注**（在榜标 `dragon_tiger=true` 附 `lhb_net_amount`/`net_rate`/`reason`，不在榜置 false 且子字段缺省，**不再用龙虎榜做存在性过滤**）。
2. 把 `capital_signals` 补进 `first_board_context`（与 chain/high 对齐）；FOCUS 阶段按 `ts_code` 过滤后把对应资金子集带进 `stage_input`（否则 FOCUS prompt 即便加措辞也无数据可引用）。
3. **强制区分命名**：`lhb_net_amount`（龙虎榜成交净买额）vs `main_net_amount`（分单结构净额）口径不同，提示词同步说明；缺测标 `moneyflow_imputed` 不填 0。

> 「资金前置到选股、贵的逐股 cyq 留选后」这一拆分原则正确（`moneyflow` 全市场单次便宜，cyq 逐股贵）。本条只改喂 LLM 的上下文；money 子分消费净流入是 §3.1 的独立改动，勿混做。

### 4.4【P1】题材强弱补资金维（`S4-theme-summary`，分两档）

- **P1 低成本档（先做，零新增调用）**：`_theme_summary` 复用已加载的 `top_list` 龙虎榜净买，为每个 theme 增 `theme_lhb_net_amount`（题材内各股 join 求和，标注 coverage、缺测不置零），排序从单一 `stock_count` 改为「家数榜 + 净额榜双榜并存」交给 LLM 识别「家数中等但资金集中的真主线」。
- **P2 真资金/人气档（独立评估，勿并入 P1）**：`theme_main_net_amount`（逐股 `moneyflow` 求和）与 `theme_popularity`（`moneyflow_ind`/概念人气）都属对涨停池新增调用，**必须挂 settings 开关默认关、先解决题材名归一、回测验证优于 P1 免费代理后再开**；切勿以「复用资金数据=零调用」为由直接上线。

### 4.5【P1】FOCUS/SELECTION 阶段缓存的降级语义（`S6-stage-cache-key-degrade`）

核验后只保留**唯一硬问题**：JSON 阶段 `PARSE_FALLBACK`（LLM 输出 JSON 解析失败）时，`_fallback_focus_stage` 的**保守兜底先验（`continuation_prob`/`premium_prob`="极低"）被以 `status=READY` 写缓存**（`:2517-2530`），相同 `input_hash` 重跑直接复用、不再重试 LLM——与异常路径（`failed=True`、不被 READY 读）形成不对称。

**建议**：`PARSE_FALLBACK` 分支也按「内容降级」处理。最小改法：`_run_json_stage` 在 `parse_fallback` 时调 `_save_stage_cache(..., failed=True)`，与 `error_fallback` 对齐（兜底 payload 仍返回、报告照常 READY，但下次同输入不命中 READY、会重试 LLM）——这本质是把 JSON 路径补齐成 `_run_text_stage` 已有的口径（`:2588`）。

> ⚠️ 核验否决了原建议的另外两个子项：① 「upstream_degraded 进 FOCUS hash」收益极低——SELECTION 兜底的 `selection_reason`/`priority` 文案已天然扰动 FOCUS `input_hash`，正常/降级路径常态已分桶，误命中概率近零；② 「落表打 `prior_degraded` 列」已被现有实现覆盖——FOCUS 降级时 `stock_priors=[]` → 落表 `continuation_prob/premium_prob` 为 NULL，加上 `stage_quality` 已含降级状态，回测可直接据此剔除降级样本，无需新列。

### 4.6【P2】`_board_level` 连板高度解析增结构化兜底（`S5-board-level-fallback`）

**问题**：`_board_level`(`:1866`) 纯靠 `status` 文本正则匹配「N天M板」「N连」，KPL 文案漂移/字段为 None 时返回 0，该股被踢出所有分层池且情绪指标失真。

**建议**：正则解析失败时回退**结构化字段** `limit_list_d.limit_times`（连板次数整数，已在 `OPTIONAL_APIS` 拉取、字段已取但未用于兜底）；优先级「文本正则 → `limit_times` → 0」，缺测在 detail 留痕、暴露 `unrecognized` 占比告警。零新增调用。

> ⚠️ 核验纠正：原建议同时用 `limit_step.nums` 兜底，应**以 `limit_list_d.limit_times` 为准**（`nums` 是天梯记录数，语义不同）。

### 4.7【P1·用户决策】各层精选与最终入选数量砍半（收紧「少而精」）

**现状**：三层精选上限均为环境变量、纯配置项（`limit_up_push_service.py:2812/2899/2919` 与 `core/config.py:225-237`）：

| 层级 | 配置项 | 现状上限 |
|---|---|---|
| 首板精选 | `LIMIT_UP_PUSH_FIRST_BOARD_FOCUS_STOCK_LIMIT` | 5 |
| 两/三连精选 | `LIMIT_UP_PUSH_CHAIN_FOCUS_STOCK_LIMIT` | 20 |
| 高连板精选 | `LIMIT_UP_PUSH_HIGH_BOARD_FOCUS_STOCK_LIMIT` | 10 |
| 最终入选（三层并集 → watchlist `limit_up_selected_stock`） | 三层之和 | 35 |

**建议（数量砍半）**：

| 层级 | 现状 | 砍半后 |
|---|---|---|
| 首板精选 | 5 | 2~3 |
| 两/三连精选 | 20 | 10 |
| 高连板精选 | 10 | 5 |
| 最终入选（合计上限） | 35 | ~17~18 |

落地：**仅改上述三个环境变量即可**（纯配置、无代码改动、无新增接口、无迁移）；最终入选合计随三层上限自然减半，无需单独设项。

**理由（为何砍半合理）**：
1. 打板/龙头战法本质是「少而精、宁缺毋滥」：当前 35 只远多于执行侧单日建仓上限（`QMT_MAX_POSITIONS_PER_DAY` 默认 5、且只取强度 top-5），过宽的 watchlist 绝大部分是陪跑/噪声。
2. **两/三连 20 只尤其偏多**：A 股单日真正值得跟的两三连板通常远少于 20，过大名额会逼 LLM/确定性兜底「凑数」纳入边际弱票，稀释强票信号、并抬高下游六维打分里弱票占比。
3. 收窄入选池可把 FOCUS 阶段每只的分析深度/token 预算集中到真龙头，并直接**降低 cyq 逐股补数调用量**（`_build_selected_stock_supplements` 只对入选股抓，成本随入选数线性下降）。
4. 与现有 prompt 口径一致：首板选股 prompt 已明确「宁缺毋滥、不强制选满」（`:2818`），砍半是把这一口径在数量上坐实。

**⚠️ 落地纪律（避免砍半变成硬砍强票）**：
- 这些是**上限（≤）非固定值**，本就允许「不选满」；砍半是收紧上限。强票扎堆的大行情里仍要保留「按强度排序、宁缺毋滥」，必要时配合**强度阈值**（只留 `leader_strength_score` 达标者）而非纯数量截断，避免强势日误删边际强龙头。
- 高连板本就稀少（常 <5），10→5 影响很小；收紧主要落在两三连 20→10 与首板 5→3。
- 与 §4.1（分位池修正）**无冲突**：六维打分的分位分布应取**全涨停池**而非入选池（§4.1），故砍半入选数**不影响**分位质量。
- 可与空仓闸门/情绪周期联动（§4.2）：退潮/分歧/冰点期可在砍半基础上**进一步下调**（甚至首板归零、只留高度最高的少数），强势发酵期维持砍半上限即可。
- 因是纯配置项，建议先按砍半值灰度观察 N 期，对照 watchlist 命中率 / 回测 `leader_strength_ic` 再固化最终档位，而非一次写死。

---

## 5. 提示词细化建议

### 5.1【P1】防编造：把「可引用字段白名单」正向写进提示词（`P1-citable-fields`）

**问题**：各提示词只反向约束「不编造精确数值」，从不告诉模型输入里**有哪些字段可引用**，导致模型给「封板较强、资金活跃」这类无锚空泛结论。

**建议**：在 `_focus_json_contract()`（三个 FOCUS 复用，一处改三处生效）顶部加 **FOCUS 通用白名单**，**仅列 FOCUS 输入确有的字段**：
> 「分析每只标的时只能引用输入实际存在的以下字段作证据，引用须写明字段名与取值，缺失(null)则明确说『该项数据缺失』而非估算：封板质量看 `seal_ratio_pct`(封流比%)、`first_limit_time`(首封时间)、`open_times`(开板次数)；量能看 `turnover_rate`(换手率%)、`technical.amount_ratio_5d`(量比)；筹码看 `supplements[code].cyq_summary` 的 `upper_chip_pressure_pct`/`next_day_premium_bias`/`winner_rate_trend`/`chip_concentration`；板高/地位看 `board_level`、`theme_role`/`leader_role`、`selection.score_detail`。严禁输出输入中不存在的具体价格、涨幅、市值、资金额。」

> ⚠️ **核验纠正（重要）**：`capital_signals.net_amount/net_rate` **不在 FOCUS 输入里**（`market_context` 不含 `capital_signals`，FOCUS 只收 `market_context`+`selected_*_stocks`+`supplements`）。**资金字段白名单只能放进 `_chain_selection_prompt`/`_high_board_selection_prompt`**（只有这两个阶段输入带 `capital_signals`），**不要放进共享 FOCUS 契约和首板选股 prompt**——否则点名了输入里根本不存在的字段，反而使模型困惑。`_limit_up_system_prompt` 第9条可补「可引用字段以各阶段输入实际存在者为准」但**不写死具体字段名**（系统提示跨阶段复用）。落地前对每个阶段 `stage_input` 实际 dump 的 key 集合做断言校验，防重构后白名单失配。

### 5.2【P1】量化锚点：把代码已固化的经验阈值写进选股 prompt（`P3-quant-anchors`）

**问题**：`fermentation_value`/`seal_quality` 的强/中/弱无任何可度量阈值，模型主观判档，与确定性打分层（money 子分 5%-25% 换手区间、cyq `premium_bias` 阈值）各说各话。

**建议（核验后收窄——只写代码确有的常量，避免虚假对齐）**：在三个选股 prompt 追加【经验参照·非硬规则】：
> 「换手率(`turnover_rate`)：5%-25% 健康放量，<5% 偏缩量、>25% 偏天量分歧均封板质量降一档（与 money 子分同源）；上方筹码压力(`upper_chip_pressure_pct`)：≤25% 友好、25%-45% 中性、>45% 压力大判溢价不利（与 `premium_bias` 同源）。以上为经验参照，最终判档须结合周期定位。」
> 首封时间/开板次数保留**定性**表述（尾盘首封降档、多次开板降档），**不要伪造「30 秒」「≥3 次判弱」这类代码里并不存在的精确分桶**。全段标注「经验参照非硬规则」，落共享片段，回测得到更优阈值时与 `_money_subscore` **同步改两处**，避免再分叉。

### 5.3【P1】资金证据措辞 + 「缺测≠资金弱」纪律（`P4-capital-evidence`）

**问题**：选股 prompt 的「资金信号」档从不说来自哪个字段、缺失如何处理；首板选股阶段甚至收不到 `capital_signals`。

**建议（依赖 §4.3 先补数据管线）**：
- **（最高性价比，可单独做）** 在 `_chain_selection_prompt`/`_high_board_selection_prompt` 资金信号维度替换为：「优先引用 `capital_signals.net_amount`(龙虎榜净买额)/`net_rate`，净买为正且占比靠前→资金信号偏强；**绝大多数涨停股不上龙虎榜、无此字段属正常，缺失一律按『资金不明』中性处理，不得据缺失判定资金弱**。」
- `moneyflow`/`hm_detail` 的「若提供」措辞**判 P2 暂缓**：字段未进流水线前对模型零价值、且提前写死字段名会制造维护负担；等 §3.1/§3.3 真接入时**与字段一起加**。游资措辞限定「关注线索非买点」。
- 删除原建议「在 `_focus_json_contract` 登记字段名」——该方法是**输出契约**不是输入白名单（系误读）。

### 5.4【P1】可成交性/一字板提示（`P5-tradable-yiziban`）

**问题**：`_tradable_flag` 在打分层确定性判一字/秒封→`WATCH`/放弃，但所有 FOCUS 与建议 prompt 从不提一字/可成交性——HTML 叙事可能把一字板当「强势重点」推荐，与契约 `tradable_flag=WATCH` 自相矛盾，读者看不到「买不进」的关键风险。

**建议（核验后收敛触发条件，防 None 滥标）**：三个 FOCUS prompt 次日竞价观察清单后追加（用已有 `boost/fail_conditions` 承接）：
> 「判定可成交性：**仅当 `open_times` 真实为 0 且 `first_limit_time` 在开盘 30 秒内**时，标注『次日大概率一字/秒板，看得到买不进』，参与方式限定为『竞价未一字或开板回封时才考虑』，不得描述为可直接打板买入；**若 `open_times`/`first_limit_time` 缺失(None)则不臆断一字，按普通强封处理并注明数据不全**。首板尤其强调一字次日溢价兑现难。」
`_investment_advice_prompt` 候选分层追加：「对疑似一字/秒封标的，操作分层不得高于谨慎观察，高位连板一字直接放弃观察，提示『竞价若仍一字则放弃，需等打开』。」全部为参与条件/观察口径，**不出买价/仓位/止损**。

> 更稳妥的可选增量（P2）：把已算出的 `tradable_flag` 注入 FINAL/ADVICE 输入材料让叙事直接消费确定性结论，可根除两轨不一致——但需调整 `score_stocks` 调用时序（当前落表期才算），成本更高，非本条必须。

### 5.5【P1】周期约束写成可执行降级规则（`P6-cycle-risk-firstboard`）

**问题**：「用 `emotion_cycle` 约束」停留在口号，没有「读哪个子指标、过什么阈值、降几档」。`emotion_cycle` 实含 `broken_board_rate_pct`/`advancement.X进Y.rate_pct`/`prev_limit_up_premium` 等可读阈值，但 prompt 未指引。

**建议（核验后：复用确定性闸门结论而非在 prompt 里另立一套阈值）**：结合 §4.2 闸门前移——把已 resolve 的 `gate.sentiment_cycle/market_state` 作为**权威**注入 FOCUS/建议提示词，要求「分歧期所有接力评级下调一档；退潮期仅谨慎/放弃观察、不给激进接力；冰点期只输出观察清单」；首板与周期联动：「分歧/退潮/冰点期首板再降一档，原则上不进重点观察」。**避免在 prompt 里复制一套与 `sentiment_gate` 不一致的炸板率/溢价阈值**（否则散文与闸门又分叉）。

### 5.6【P1】合成/建议 prompt 收紧边界与防套话（`P7-final-advice-tighten`）

**核验后最强的真问题**：`_investment_advice_prompt` 自身正文写了「整体仓位态度」（`:1045`）、「合理低吸区间/止损」（`:1050-1051`），**主动诱导 LLM 越界到执行侧（仓位/价位/止损）**，违反「信号侧不出仓位/买卖价/止损」硬约束。

**建议**：
- **（P1 首要）** 收紧 `_investment_advice_prompt`：`:1045` 「整体仓位态度」改为「进攻/防守倾向（不含具体仓位比例）」；`:1050-1051` 删除或质性化低吸区间，「止损」改「失效条件」（用竞价相对强弱描述，不落绝对价格）；追加纪律句：「严禁任何仓位比例、买/卖价、止盈/止损价位、加减仓节奏（执行侧职责，越界即作废）；触发/失败条件只描述竞价相对强弱区间。同时禁止『需密切关注/控制仓位/注意风险』等空泛收尾，每条须落到可观察的具体信号。」
- **（P1 次要）** `_final_report_prompt`：禁止「市场有风险/注意控制风险」等无信息套话，要求每股结论挂具体失败条件；两/三连表格列固定为 `[股票|题材|连板状态|seal_ratio_pct|first_limit_time|upper_chip_pressure_pct|强弱观察]`（**只用 `_stocks_for_final_prompt` 实际下发的字段**）。
- ⚠️ 纠正：**删除原建议里「龙虎榜净买」作为 FINAL 表格列/证据**——该字段不在 `_stocks_for_final_prompt` 输入里，会导致编造。

---

## 6. 打分卡与 watchlist 契约建议

（§3.1 的 money 子分改造、§3.2 的 watchlist 资金/龙虎榜字段已在前文，这里是其余打分契约问题。）

### 6.1【P1】先验概率分 tier 校准（`S3-prob-band-uncalibrated`）

`continuation_prob`/`next_day_premium_prob` 由 LLM 档位经 `_PROB_BAND_TO_DECIMAL`（`:426`）硬映射成 高=0.75/中=0.5/低=0.25/极低=0.10，**单套、无校准、对所有 tier 同值**（首板续板基础概率远低于高位连板溢价，同套档值系统性高估首板），且 `DECIMAL(5,4)` 精度暗示「已校准概率」实则只有 4 个离散值。

**建议（核验后收窄）**：
- **该做**：`_PROB_BAND_TO_DECIMAL` 改为**按 tier 键控**（FIRST_BOARD/CHAIN/HIGH_BOARD 各一套档值，首板整体下调一档量级）；`strength_dim_json` 增写 `prob_calibration_version`；在列注释与设计文档澄清「这是离散档位代表值，非连续校准概率」。
- **纠正**：原始档位文字**已存于 `item_json` 的 prior**（`:652`），无需再「保留原文」，只缺 `calibration_version`。
- **移到中期**：用历史命中频率回填档值，依赖闭环归因物化表与隔日行情（尚未积累样本）；短期只做 tier 分层 + 保守缺省，闭环上线后再用实测频率刷新。校准待办的真实承接见信号侧 `qmt-trade-review-closed-loop-attribution-design.md`，本仓库登记见 [待办 §D](待办与上线验证清单.md)。

### 6.2【P1】角色/辨识度判定脆弱（`S4-role-substring-fragile`）

`_recognition_subscore`/`_theme_ladder_subscore`/`_determine_role` 把 `theme_role+leader_role` 拼成 `role_hint` 做**中文子串 `in` 匹配**。已确证的硬漏洞：high_board 的枚举「高辨识度」(`:2925`) 在消费侧关键词集 `{龙头,空间板,前排,跟风,卡位,助攻,补涨}` 里**零命中** → 静默退回中性 0.5/STRAGGLER 兜底；且「非龙头」「不是龙头」会误命中「龙头」判 0.9。

**建议（分两档）**：
- **P0（低成本、零缓存影响）**：① 给消费侧关键词集**补全覆盖 prompt 已声明的全部枚举**（至少补「高辨识度」命中分支）；② 判子串前剔除/短路「非/不是/未」否定前缀防误命中；③ 新增单测，断言三处 selection prompt 的每个枚举 token 都能在打分侧命中至少一个非中性分支（contract-drift 回归守护）。
- **P1（需 bump `prompt_version`，旧缓存走兼容兜底）**：`theme_role`/`leader_role` 改固定 ASCII enum 码（`LEADER`/`FRONT_ROW`/`FOLLOWER`、`SPACE_BOARD`/`THEME_LEADER`/`HIGH_RECOG`），消费侧 enum 精确匹配，中文由 `selection_reason` 承载展示，保留子串匹配作旧样本兜底并标 `role_match_fallback`。P0 即可拿到区分度收益绝大部分，P1 是稳健化收尾。

### 6.3【P1】缺测统一 0.5 中性稀释区分度（`S5-neutral-0.5-dilution`）

多维真实缺测时统一兜底 0.5，叠加后把综合分拉向 50 分中段（强票被压、弱票被抬）。

**建议（核验后分两块，纠正一处误读）**：
- **高价值低风险（建议做）**：算 `data_confidence`=非 imputed 子项权重占比，写入 `strength_dim_json` 并作 watchlist 只读列，供执行侧对低置信强票谨慎对待——纯描述性、不越权、可回测分桶。
- **中价值有风险（谨慎做且须纠错）**：聚合层缺测处理。⚠️ **不要照搬「把 `:197` 逻辑搬到顶层」**——`:197` 是加权平均、缺测项仍以 `(0.5,w)` 进入分子并未剔除，照搬不能消除稀释。真正要实现「缺测维度从分子分母同时剔除、按有效维度权重等比放大」，且必须设最低有效维度阈值（≥3 维或剩余权重≥0.5），不足则退回 0.5 口径并标低 `data_confidence`。任何改动 bump `limit_up_leader_scoring_version` 并与回测同批迁移，先用 `data_confidence` 分桶验证「剔除归一 vs 0.5 兜底」的真实增益，不显著则只保留 `data_confidence` 列、不动聚合。

### 6.4【P1】六维权重校准协议（`S6-weights-calibration-protocol`）

`WEIGHTS_V1`(`:35`) 注释自承占位；`score_stock` 可裸传匿名 `weights` 但与 `scoring_version` 解耦；`height` 正权与 `position` 内部 `height_ratio` 负向对冲（`:219` vs `:270`）对高位风险重复建模。

**建议（核验后：复用既有回测底座，删除「从零建校准」暗示）**：
- **已有底座**：`limit_up_backtest_service` 已严格用 `a_trade_calendar` 做 T→T+1→T+2 撮合（无前视）、已算 `leader_strength_ic`、已产 GO/NO_GO。真正缺的只是「按维度回归出权重」。
- **该做（P1）**：① 先把六维 subscores 随回测明细落库（当前只落综合分），否则无法做单维归因；② 以「次日续板 or 隔日溢价正收益」为标签，对各维子分做带单调约束的回归得 `WEIGHTS_V2`，连同样本期/标签/样本外 IC 写版本元数据。
- **护栏（P1，非当前 bug）**：建 `WEIGHTS_REGISTRY={version: weights}`，`score_stocks` 内部按 version 取权重、detail 记 `weights_hash`，匿名 `weights` 收为「仅测试/离线」。这是将来跑权重扫描前的可复现护栏（线上现已固定 `WEIGHTS_V1`，不是现存破口）。
- **建模口径**：回测调权前先解耦 `height` 正权与 `position` 负向对冲，二选一建模高位风险（建议保留 `position` 负向、`height` 只表达空间地位上限截断），避免回归时互相抵消。
- 首版校准前 `WEIGHTS_V1` 维持并标 `calibrated=false`；按行情周期分层校准 + 样本外验证，防小样本过拟合。

---

## 7. 明确否决的建议（避免后续重复提）

| id | 标题 | 否决结论 |
|---|---|---|
| D6 | 集合竞价 `stk_auction` 接入信号侧 | **缺口不存在、代码已合规**：接入会前视+越权，现状正确（见 §3.6）。属边界确认，无落地动作。 |
| P2 | 三套档位（强/中/弱、高/中/低/极低、重点/谨慎/放弃）prompt 交叉锚定 | **结构化契约层是伪命题**：三套档位在表里**正交分列**——选股 `score_detail` 只喂六维打分（数值输入）、FOCUS 概率档落独立 `*_prob` 列、watchlist `action` 是 `_strategy_and_action` 的**确定性**产出（LLM 从不撰写该列）。LLM 的「重点/谨慎/放弃」文字只存在于给人读的 HTML 散文里、不进表，无从「混档」。`leader_scoring.py:53-57` 已用共享中文枚举对齐。再往 prompt 里塞一套带阈值的经验映射反而会与确定性阈值产生**新的**散文-契约矛盾。 |

---

## 8. 落地优先级与依赖顺序

> 仅排优先级与依赖，不含工时。同优先级内按依赖箭头先后。

### P0（性价比最高，优先）
1. **资金主线·数据与打分**（§3.1）：`moneyflow` 全市场单次拉取 → 归一化并入 compact 行 → 改 `_money_subscore`。**先决：积分档支持 `trade_date`-only 验证 + 覆盖率抽样 + 单位换算口径固化。**
2. **分位池修正**（§4.1）：`score_stocks` 分布用全涨停池。（与 1 独立，可并行）
3. **闸门前移注入**（§4.2 核心）：`resolve_gate` 前移 + 注入提示词 + 落表复用同一对象。（独立，可并行）
4. **龙虎榜零成本只读列**（§3.2 P0）：`dragon_tiger_flag`/`lhb_net_amount`/`lhb_net_rate` 入 watchlist。（独立，可并行）

### P1（高价值，P0 后）
- 依赖资金主线：`_capital_signals` 改资金为主（§4.3）→ 资金证据措辞（§5.3）；watchlist `moneyflow` 字段（§3.2 P1）；兜底排序纳入资金（§3.1.5）。
- 独立可并行：正向字段白名单（§5.1）、量化锚点（§5.2）、一字板提示（§5.4）、周期降级规则（§5.5，依赖 §4.2）、合成/建议边界收紧（§5.6）、`PARSE_FALLBACK` 缓存语义（§4.5）、先验分 tier（§6.1）、角色枚举 P0 修复（§6.2 P0）、`data_confidence`（§6.3）、权重校准协议+子分落库（§6.4）。
- 题材 lhb 代理（§4.4 P1）、游资 `hm_detail` 上下文侧（§3.3 一期）、板块资金 `moneyflow_ind_dc` 映射验证（§3.4 一期）。
- **各层精选与最终入选数量砍半（§4.7，纯配置：首板 5→2~3、两三连 20→10、高连 10→5、合计 35→~18）**。

### P2（可选/待回测）
- 大盘资金 `moneyflow_mkt_dc` 情绪佐证（§3.5）、`_board_level` 结构化兜底（§4.6）、空仓档短路省算（§4.2.2）、题材真资金/人气档（§4.4 P2）、角色 enum 化（§6.2 P1）、聚合层缺测剔除归一（§6.3）、`tradable_flag` 注入叙事（§5.4 可选）。

---

## 9. 全局口径与落地纪律（务必遵守）

1. **无前视**：所有新接数据均为 T 日盘后定稿，用于 T+1。`stk_auction`(T+1 竞价) 绝不接入。
2. **信号/执行边界**：信号侧只下发只读因子，**不出仓位/买卖价/止盈止损**。新增资金/游资因子均为只读列；提示词同步清除诱导越界措辞（§5.6）。
3. **单位口径**：`moneyflow` 万元、`daily.amount` 千元、`circ_mv` 万元——做比值前**统一换算**，并加上界保护。新增时间字段沿用项目「UTC naive / 东八区」既有口径，不手工 ±8h。
4. **缺测纪律**：`None` 一律中性 + `imputed` 痕迹，**绝不当 0**（抬分或置流出）。
5. **成本/限频**：优先 `trade_date` 全市场单次；逐股抓只对入选股（≤几十只）；新增每日批量调用前评估积分/限频。
6. **回测校准**：权重/概率档/阈值改动均标占位、bump version、与回测同批迁移，复用 `limit_up_backtest_service` 的 T+1 撮合与 go/no-go，禁止小样本档值直接下发。
7. **契约升级**：watchlist 改列 bump `WATCHLIST_SCHEMA_VERSION`，新列可空，执行侧按版本兼容旧行。
8. **中文注释**：触碰逻辑须补/同步中文注释（业务意图/数据来源/单位/缺测/重跑口径）。

---

## 10. 关联与去重说明

本文与信号侧 `resources/doc/limit-up-*` 设计/改造文档不重复：那些记录的是**已落地**的多阶段改造、KPL 口径、情绪周期、F3 竞价分母等；本文聚焦**尚未做的**资金维补强、提示词正向化、分位/闸门/缓存/契约的增量优化。`llm-tushare-on-demand-stock-data-plan.md` 已证明「按需补数编排器 + `a_moneyflow` 表」基建成熟——故资金主线是「接线/扩消费面」而非从零搭建。

> 本文为处理中的分析/方案文档，落地推进后按本仓库文档分类规则整体移入归档目录。

---

## 11 · 落地取舍结论（可以做 / 没必要）—— 基于真实代码复核

### 11.0 方法说明

本结论基于通读真实代码（`limit_up_push_service.py` / `limit_up_leader_scoring_service.py` / `limit_up_backtest_service.py` / `db/models/notification.py` / `schemas/limit_up_watchlist.py` / `core/config.py` 等，含近期阶段 C/D/E 修复）+ 对抗式 triage 复核，回答「哪些可做、哪些没必要」。关键阶段背景须先记牢：① **实盘执行侧（miniQMT）未对接，无 `qmt_*` 回流闭环、无隔日真实标签**，凡需「真实频率校准 / 单调约束权重回归」的事都做不了，只能落结构占位；② **执行侧单日只按 `leader_strength_score` 取 top-5**（`QMT_MAX_POSITIONS_PER_DAY` 默认 5），当前三层精选合计 35 只里绝大多数被 top-5 截断后是陪跑/噪声——因此**「改的是确定性打分排序（直接改变 top-5 构成）」的项才有真价值，「只喂 LLM 散文叙事」的项普遍被 top-5 截断稀释、价值打折**。③ 全程恪守信号侧硬约束：**只下发只读因子，不出仓位、不出买卖价、不出止损**；凡涉及越界的判定在下文明确点出（见 §5.6）。

---

### 11.1 ✅ 建议做（高 ROI / 零成本 / 修明确 bug）

- **§4.5（=§3.1.5，复核改判合并）** PARSE_FALLBACK 写缓存补 `failed=True` —— 现 PARSE_FALLBACK 兜底以 `READY` 写缓存（`:2557` 未传 `failed=True`），与 error 分支 / `_run_text_stage` 不对称，导致一次 LLM 解析抖动产生的「极低先验」被同 `input_hash` 重跑长期复用、污染 watchlist。本文件一处改即修，零依赖。**（复核改判：§3.1.5 与 §4.5 实为同一行修复，合并为单条 P0，避免重复排期）。** 前置/依赖：无。
- **§6.2** 补「高辨识度」命中分支 + 否定前缀短路 + 契约漂移单测 —— HIGH_BOARD selection prompt 下发 `leader_role∈{空间板/题材龙头/高辨识度}`，但打分消费侧关键词集仅 `{龙头,空间板,前排,跟风,卡位,助攻,补涨}`，**「高辨识度」是孤儿 token，静默退中性 0.5/STRAGGLER**——最该给高分的高连板标的反被压成中性。直接改 recognition 子分与角色判定 → 改强度排序 → 改 top-5 构成，**不被 top-5 截断稀释**。未被阶段 C/D/E 触碰。前置/依赖：无。
- **§5.6** 清除合成/建议 prompt 越界诱导 —— 本批最强真问题（合规红线）：`_investment_advice_prompt` 正文确含「整体仓位态度」「合理低吸区间、过高开警惕点」「失败/止损条件」，**直接违反信号侧不出仓位/买卖价/止损硬约束**。改 prompt 文案：仓位态度→进攻/防守倾向（不含比例）、低吸区间质性化、止损→失效条件（用竞价相对强弱、不落绝对价格）、加禁越界纪律句、FINAL 表列固定为 `_stocks_for_final_prompt` 实有字段（禁加不在输入里的龙虎榜净买）。零数据依赖。前置/依赖：无。
- **§4.2** 空仓闸门 `resolve_gate` 前移至 LLM 之前 + 注入 `market_state`/`gate_reasons` 进 context/prompt + 落表复用同一 gate 对象 —— 现 `resolve_gate` 仅在落表期（`:571`）调一次、在 LLM 全 pipeline（`:401`）之后，LLM 全程拿不到 `market_state`，致**报告正文叙事 ↔ watchlist 行级动作两套口径冲突**。前移不引入前视（`market_emotion` 在 `_assemble_context` 已算好，T 日盘后量）。市场级闸门，不被 top-5 截断。前置/依赖：无（本条是 §5.5 的前置上游）。
- **§3.2（龙虎榜三列）** `dragon_tiger_flag` / `lhb_net_amount` / `lhb_net_rate` 零成本只读列入 watchlist —— `top_list` 已每日单批抓、`top_by_code` 索引已建，纯落表 join 加列。零新增抓取、零前视、零越权。前置/依赖：新列可空 + bump `WATCHLIST_SCHEMA_VERSION→1.1.0`，命名须与 §3.1 `main_net_amount`（分单结构净额）严格区分（lhb 是龙虎榜成交净买元口径），`dragon_tiger_flag` 缺测按「缺测≠资金弱」不置 0。`main_net_inflow/elg_net_ratio` 来自 moneyflow，不在本条，随 §3.1 同批落。
- **§4.7** 三层精选上限砍半（chain 20→10、high 10→5、合计 35→约 17-18）—— 纯 env 配置、零代码零迁移；当前 35 只里绝大多数被 top-5 截断后是陪跑/噪声，收紧主战场直接见效并顺带降逐股 `cyq` 调用量。与 §4.1 全池分位无冲突。前置/依赖：建议灰度 N 期对照 watchlist 命中率/回测 `leader_strength_ic` 再固化，强势日配合 `leader_strength_score` 阈值而非纯数量截断（避免误删强龙头）。
- **§5.1** 把「可引用字段白名单」正向写进 prompt（按阶段分写）—— 纯文案、零数据依赖，降「封板较强、资金活跃」类无锚空泛结论。**铁律：白名单按阶段分写，资金字段（net_amount/net_rate）只进 chain/high selection prompt，绝不放进共享 FOCUS 契约和首板 prompt**，否则点名输入里不存在的字段反致编造。前置/依赖：落地前对每阶段 `stage_input` 实际 key 集合加断言，防重构后失配。
- **§5.4（prompt 文案版）** 可成交性/一字板提示 —— `_tradable_flag` 在打分层已确定性判一字/秒封产 WATCH，但全部 FOCUS/建议 prompt 零引用，叙事可能把一字板当强势重点推荐，与 watchlist `tradable_flag=WATCH` 自相矛盾，读者看不到「买不进」关键风险。叙事护栏，与 top-5 不冲突。**铁律：仅当 `open_times==0` 且 `first_limit_time` 在开盘 30 秒内才标一字；`open_times/first_limit_time` 缺失（None）不臆断、按普通强封处理。** 前置/依赖：无（P2 的把 `tradable_flag` 注入 FINAL/ADVICE 叙事的时序版依赖 §4.2 打分前移，归暂缓）。
- **§5.2** 把打分层已固化经验阈值搬运进选股 prompt —— 纯文案对齐，只搬代码确有常量（5%-25% 健康换手、`upper_chip_pressure` ≤25/25-45/>45）。**铁律：首封时间/开板次数保留定性表述，严禁伪造「30 秒」「≥3 次判弱」这类代码里不存在的精确分桶；全段标注「经验参照非硬规则」。** 前置/依赖：无。
- **§3.6（红线文档治理）** 把五条不接入红线 + 「单一资金口径」硬约束写入数据接入口径文档 —— 零代码、纯文档治理，防后续迭代重复提接入、防误把多口径资金并行入分位分布。前置/依赖：无。

> 优先级速记（本组内）：**P0** = §4.5、§6.2、§5.6、§4.2；**P1** = §3.2、§4.7、§5.1、§5.4(文案版)；**P2** = §5.2、§3.6(文档)。

---

### 11.2 🟡 有条件做（需先满足前置闸门）

- **§3.1** moneyflow 全市场接入 + 改 `_money_subscore` 消费真实主力净额（资金维进分直接改 top-5 构成，**不被截断稀释**，有真价值；改的是盘前排序分位与 LLM 上下文，不依赖实盘回流）。**前置闸门：① 一次性脚本验证 moneyflow 在当前积分档支持 `trade_date-only` 全市场放行（daily_basic 与 5 个 OPTIONAL_APIS 已证同档可行，属高确定性但非零——不同接口积分门槛可不同，需单独确认）；② 涨停/入选池 moneyflow 覆盖率抽样达标；③ 单位口径固化（见下条，强制内嵌前置）；④ 归一化（净额/流通市值或截面分位）+ 缺测中性（imputed 不当 0）；⑤ 改 `_money_subscore` 须 bump `scoring_version` 并与回测同批迁移。**
- **单位口径**（§3.1 的强制内嵌前置纪律，非独立任务）moneyflow 万元 / `daily.amount` 千元 / `circ_mv` 万元，跨单位算净占比须统一换算到「元」+ 加 `|净占比|>1` 上界置缺保护。**现成范式可直接套用**：封流比 `free_float` 缺失时回退 `circ_mv_wan×Decimal('10000')` + `seal_ratio>100%` 置缺告警（`:1984-1995`）。当前无单位 bug（`daily.amount` 仅用于比值，单位自抵消）。依赖：§3.1（本条是其落地约束，不单独立项）。
- **§4.3** `_capital_signals` 改 left-join 增强 + 注入 moneyflow 主力净额 + 首板补 `capital_signals` 键（现 `if not top: continue` 纯 inner-join 丢弃全部非 LHB 涨停股；只改喂 LLM 上下文、不进六维分，被 top-5 截断稀释）。**前置：硬依赖 §3.1 全市场 moneyflow 批量管线先打通**（否则 left-join 也无主力净额可填），`lhb_net_amount` 与 `main_net_amount` 命名严格区分，缺测按「资金不明中性」。
- **§5.3** 资金信号档加字段措辞 + 「缺测≠资金弱」纪律 —— 可拆两半：**①** chain/high prompt 引用 `capital_signals.net_amount/net_rate` + 缺测中性纪律 = 零数据依赖当下可做（但因 `_capital_signals` 仍 LHB inner-join，短期多为「无数据可引用」，收益受龙虎榜稀疏性拖累）；**②** 首板也收资金 / 改 moneyflow 为主 = 硬依赖 §3.1+§4.3。属 LLM 散文护栏、不进 top-5 打分。
- **§5.5** 周期约束写成可执行降级规则（复用已 resolve 的 gate 结论注入 prompt，不另立阈值）—— 文案本身 easy 且 high value。**硬前置：必须先做 §4.2 把 `resolve_gate` 前移、resolve 一次并把 `market_state/sentiment_cycle` 缓存进 `context['market_context']`**，否则 prompt 无权威闸门结论可引用、直接写阈值会与 `sentiment_gate` 分叉。
- **§3.3** `hm_detail/hm_list` 游资席位接入 —— 一期注入 SELECTION/FOCUS 辨识度上下文喂 LLM 可做，二期进 `_recognition_subscore`（权重仅 0.10 且子分为占位）须回测确认 lift、当前无标签做不了。剔 `top_inst` 正确、无动作。**前置：① 一次性脚本验证 `hm_detail` 当前积分档全市场放行（与 moneyflow 同属未验证宽接口）；② 人工席位字典播种并标 imputed/季度复核（长期维护成本是主要 ROI 拖累）。**
- **§3.4** `moneyflow_ind_dc` 板块资金 + KPL 题材↔东财板块名映射 —— 只喂 FIRST_BOARD 题材发酵叙事、不进打分。**前置硬闸门：先离线统计 KPL theme 与东财板块名精确+别名映射命中率（go/no-go），达标才上提示词；未命中题材严格标 `sector_flow_unmatched` 不赋值/不惩罚（绝不把未匹配当净流出）；只接东财 `_dc` 单口径，禁并行 `_ths/_cnt`。** 映射表长期维护成本是主要 ROI 拖累。
- **§6.1** 概率档位映射 tier 分层骨架 + `calibration_version` + 列注释澄清「离散代表值≠连续校准概率」+ 首板系统性下调一档 —— 现 `_PROB_BAND_TO_DECIMAL` 单套硬映射、4 个离散值塞进 `DECIMAL(5,4)` 列。**骨架部分纯结构、零数据依赖、当前即改善首板高估，按 §6.4① 口径属可立即落（复核：骨架子项优先级对齐到「即落」）；真实频率回填须等隔日标签 + 闭环归因物化表，暂缓。** 有条件指必须同批 bump 元数据、不留裸值。依赖：真实校准依赖 §6.4 子分落库 + 实盘 `qmt_*` 回流。
- **§6.3** `data_confidence` 只读列 + 聚合层缺测剔除归一 —— 拆两半：**①** `data_confidence`（非 imputed 权重占比）纯描述性只读列、不越权、零依赖、立即可落；**②** `_aggregate_linear` 缺测维从分子分母同时剔除 + 按有效维等比放大 + 最低有效维阈值，须 bump `scoring_version` 与回测同批迁移，且**先用 `data_confidence` 分桶在回测验证「剔除归一 vs 0.5 兜底」真实增益**（不显著则只留列不动聚合）。依赖：聚合改造依赖 §6.4① 子分落库。
- **§6.4** 子分随回测落库（**子项 P0**）+ `WEIGHTS_REGISTRY` 护栏 + height/position 解耦（可先做结构）+ 真实 `WEIGHTS_V2` 回归（暂缓）—— `WEIGHTS_V1` 自承占位 TBD；`_height_subscore` 正权与 `_position_safety_subscore` 负向用同一 `height_ratio`，**同一高度既奖励又惩罚（重复建模），回归前会互相抵消**；回测 base dict 只取综合分、不读 `strength_dim_json`，**单维归因不可行**。子分落库纯数据搬运、零实盘依赖，是后续一切单维归因/校准的前置闸门。真实 `WEIGHTS_V2` 带单调约束的数值回归依赖次日续板/隔日溢价标签 + 实盘回流闭环，当前样本不足、只能落 `calibrated=false` 占位 → 暂缓。

---

### 11.3 ⏸️ 暂缓 / 待闭环（现在做没必要）

- **§3.5** `moneyflow_mkt_dc` 大盘资金 / `moneyflow_hsgt` 北向 —— 纯 LLM 情绪叙事佐证、无确定性消费链、增益未经回测，当前 ROI 低。须近 5 日窗口单独请求（不能塞进单日 OPTIONAL_APIS 循环）。结构可先在 `_emotion_cycle_metrics` 返回体留 `market_net_amount` 占位。**等什么：观察期 N 期人工对照评估真实增益。**
- **§4.4** `_theme_summary` 题材排序加资金维 —— P1 免费档（透传 top_list 求和 `theme_lhb_net_amount`）被龙虎榜稀疏性拖累（§4.3 inner-join 后常空，join 覆盖率低），且题材已有 `cpt` 衍生维（days/cons_nums/rank/top_stock）兜底，边际价值低；题材是上下文维度、不进六维打分、被 top-5 截断。**等什么：§3.4 `moneyflow_ind_dc` 管线就绪后连同 `theme_main_net_amount` 一起评估，不单独先做 lhb 代理。**
- **§4.6** `_board_level` 正则失败用 `limit_list_d.limit_times` 结构化兜底 + unrecognized 占比告警 —— 缺口真实（纯正则 `return 0`、`limit_times` 已取未用），但 `limit_times` 未按 `ts_code` merge 进 kpl/compact 行，兜底**非「读 row 现有字段」即可、需先建跨表 join**（非零代码）；只救 KPL status 文案漂移的边缘股、主路径多数可解析。**等什么：挂在 §4.3/§4.4 把 `limit_list_d` 已 merge 之后顺带做，不单独先建 join。**
- **§5.4（时序注入版）** 把已算 `tradable_flag` 注入 FINAL/ADVICE 输入根除两轨不一致 —— 须调 `score_stocks` 时序（当前落表期 `:571` 才算），成本更高。**等什么：§4.2 打分/闸门前移。**
- **§3.3 二期 / §6.1 真实校准 / §6.3 聚合改造 / §6.4③ WEIGHTS_V2** 凡需真实隔日标签的子项 —— **等什么：实盘 `qmt_*` 回流闭环样本 + 回测正收益标签管线（执行侧 miniQMT 未到位）。**

---

### 11.4 ❌ 没必要 / 不做（缺口不存在 / 已修复 / ROI 太低 / 越界 / 被 top-5 截断）

- **§4.1** 分位池用全涨停池构建 —— **已被阶段 C/D/E 修复落地**：`score_stocks` 已加 `context_rows` 参数（两参数解耦，全池构建分位、入选票打分），`push_service:603` `full_pool=context.get('limit_up_stocks') or [...]`、`:607 context_rows=full_pool`，且 `:601-602` 已把「读 `pipeline.get(limit_up_stocks)` 恒 None 静默回退入选子集」改为读顶层 context。**报告把它列 P0#2 的乐观假设已被现状推翻，无需再开发，只做回归确认**（断言 `context['limit_up_stocks']` 生产路径非空，避免 `or` 兜底退化）。
- **§3.6 `stk_auction` 集合竞价接入** —— 竞价择时是执行侧领域，接入即**前视（T+1 当天数据）+ 越权（违反信号侧只下只读因子）**，现状已合规（watchlist 只下 `float_mktcap`+`first_board_vol` 两个分母，比值计算留执行侧），零动作。
- **§3.6 `dc_hot` / `ths_hot` 散户人气榜** —— 散户人气是噪声逆向指标，与龙头强度负相关/无关，硬加只稀释区分度，列为红线、保持不接入。
- **§3.6 `top_inst` 机构席位** —— 打板主战场是游资接力，机构席位身份在该语境逆向/噪声，`recognition` 权重仅 0.10 且子分占位，过拟合风险高，不接入（与 §3.3 剔 `top_inst` 一致）。
- **§3.6 `moneyflow_dc` / `moneyflow_ths` 并行接入** —— 与原生 moneyflow 三套 `net` 口径不可互换，并行入同一截面分位分布会污染 money 子分，**硬约束「任一时刻只允许单一资金口径」**；§3.1 只走原生单口径，并行=负收益。
- **§3.6 `kpl_concept` 接入** —— 与已用 `limit_cpt_list`（rank/cons_nums/up_nums/top_stock 衍生维）信息高度重叠、无新增确定性消费者，长期维护成本换边际重复信息，不做。
- **§7 三套档位正交分列（角色 ROLE_* / 战法族 _STRATEGY_CN / 操作档 ACTION_*）** —— 三套枚举已正交分列、各落独立列（role_tags/strategy_family/action）、语义不混用，属现状确认型、**无缺口可改**（角色英文 enum 化是另一条 §6.2 P1，须 bump version、价值边际，归暂缓）。

---

### 11.5 总表（覆盖全部条目）

| 章节 | 建议 | 判定 | 优先级 | 一句话理由 | 前置/依赖 |
|---|---|---|---|---|---|
| §4.5（=§3.1.5） | PARSE_FALLBACK 补 `failed=True` | ✅ 做 | P0 | 防极低先验被 READY 缓存固化污染 watchlist；一行修复（复核合并去重） | 无 |
| §6.2 | 补「高辨识度」命中+否定短路+契约单测 | ✅ 做 | P0 | 孤儿 token 致高连板被压中性；直接改 top-5 构成、不被截断 | 无 |
| §5.6 | 清除越界诱导（仓位/低吸/止损） | ✅ 做 | P0 | 合规红线，prompt 主动诱导越界硬违约；纯文案 | 无 |
| §4.2 | `resolve_gate` 前移 + 注入 market_state | ✅ 做 | P0 | 消除报告↔watchlist 两套口径冲突；§5.5 上游 | 无 |
| §3.2 | 龙虎榜三只读列入 watchlist | ✅ 做 | P1 | 零新增抓取/前视/越权纯加列 | bump schema→1.1.0；命名与 main_net 区分；缺测不置 0 |
| §4.7 | 三层精选上限砍半 | ✅ 做 | P1 | 纯 env 配置；收紧被 top-5 截断的陪跑噪声 | 建议灰度，配合强度阈值非纯数量截断 |
| §5.1 | 阶段化字段白名单写进 prompt | ✅ 做 | P1 | 降空泛编造；资金字段只进 chain/high selection | 每阶段 stage_input key 加断言 |
| §5.4（文案版） | 一字板/可成交性提示 | ✅ 做 | P1 | 补读者看不到的「买不进」风险护栏 | 触发严限 open_times==0 且首封 30 秒内，None 不判 |
| §5.2 | 搬运已固化经验阈值进 prompt | ✅ 做 | P2 | 纯口径对齐，只搬代码确有常量 | 严禁伪造不存在的精确分桶 |
| §3.6（文档） | 红线/单口径硬约束写入口径文档 | ✅ 做 | P2 | 防后续重复提接入/多口径污染 | 无 |
| §3.1 | moneyflow 全市场接入 + 改 money 子分 | 🟡 有条件 | P1 | 资金维进分直接改 top-5、不被稀释 | 积分放行脚本 + 覆盖率 + 单位口径 + bump scoring_version |
| 单位口径 | 跨单位统一换算到元 + 上界置缺 | 🟡 有条件 | P1 | §3.1 强制内嵌前置；现成 circ_mv×10000 范式可套 | §3.1（不单独立项） |
| §4.3 | `_capital_signals` left-join + 注入主力净额 | 🟡 有条件 | P2 | 只改 LLM 上下文、被 top-5 稀释 | 强依赖 §3.1 管线；lhb/main_net 命名区分 |
| §5.3 | 资金档措辞+缺测纪律 / 首板收资金 | 🟡 有条件 | P2 | ①措辞当下可做但受 LHB 稀疏拖累；②收资金依赖管线 | §4.3、§3.1（②子项） |
| §5.5 | 周期降级规则注入 prompt | 🟡 有条件 | P1 | 复用权威 gate 结论、不另立阈值 | 强依赖 §4.2 闸门前移 |
| §3.3 | 游资席位 hm_detail/hm_list | 🟡 有条件 | P2 | 一期喂 LLM 辨识度；二期进分需回测 | 积分放行脚本 + 人工席位字典；二期依赖实盘标签 |
| §3.4 | 板块资金 moneyflow_ind_dc + 题材映射 | 🟡 有条件 | P2 | 只喂题材发酵叙事、不进打分 | 映射命中率 go/no-go 闸门；单口径 _dc |
| §6.1 | 概率档 tier 骨架 + calibration_version | 🟡 有条件 | P1（骨架即落） | 骨架零依赖改善首板高估；真实回填待闭环 | 真实校准依赖 §6.4①+实盘回流 |
| §6.3 | data_confidence 列 / 聚合剔除归一 | 🟡 有条件 | P1（列即落） | ①只读列零依赖；②聚合改造需回测验证增益 | 聚合改造依赖 §6.4① + 回测分桶 |
| §6.4 | 子分落库 / REGISTRY 护栏 / 解耦 / V2 回归 | 🟡 有条件 | P0（子分落库） | 子分落库是单维归因前置闸门；V2 待标签 | 真实 V2 依赖实盘 qmt_* 闭环 |
| §3.5 | 大盘/北向资金 moneyflow_mkt/hsgt | ⏸️ 暂缓 | P2 | 纯情绪叙事、无确定性消费、未回测 | 观察期 N 期人工评估 |
| §4.4 | 题材排序加资金维 | ⏸️ 暂缓 | P2 | P1 受 LHB 稀疏拖累、已有 cpt 维兜底 | 待 §3.4 管线就绪一起评估 |
| §4.6 | `_board_level` limit_times 结构化兜底 | ⏸️ 暂缓 | P2 | 需跨表 join、只救边缘股、ROI 低 | 复用 §4.3/§4.4 已 merge 的 limit_list_d |
| §5.4（时序版） | tradable_flag 注入 FINAL/ADVICE | ⏸️ 暂缓 | P2 | 须调打分时序、成本高 | §4.2 打分前移 |
| §4.1 | 全涨停池分位 | ❌ 不做 | - | 已被阶段 C/D/E 修复，只需回归确认 | 断言 limit_up_stocks 非空 |
| §3.6 | stk_auction 集合竞价 | ❌ 不做 | - | 接入即前视+越权，现状合规 | 无 |
| §3.6 | dc_hot / ths_hot 散户人气 | ❌ 不做 | - | 噪声逆向、稀释区分度（红线） | 无 |
| §3.6 | top_inst 机构席位 | ❌ 不做 | - | 打板语境逆向噪声、权重仅 0.10 占位 | 无 |
| §3.6 | moneyflow_dc / moneyflow_ths 并行 | ❌ 不做 | - | 多口径并行污染 money 子分位分布 | 无 |
| §3.6 | kpl_concept | ❌ 不做 | - | 与 limit_cpt_list 信息重叠、无增量 | 无 |
| §7 | 三套档位正交分列 | ❌ 不做 | - | 已正交分列、无缺口可改 | 无 |

---

### 11.6 最小落地子集

若只动手做最少的几项（全部纯逻辑/纯配置/纯文案、零实盘回流依赖、当前阶段即正收益，取自对抗复核 `overall_notes`），按顺序为：

1. **§4.5（=§3.1.5）** PARSE_FALLBACK 补 `failed=True` —— 一行修复，防极低先验固化污染 watchlist（P0，缓存纪律）。
2. **§6.2** 补「高辨识度」命中分支 + 否定前缀短路 + 契约漂移单测 —— 直接改 recognition 子分/角色判定→改强度排序→改 top-5 构成，少数不被截断稀释的确定性打分硬 bug（P0）。
3. **§5.6** 清除 advice prompt 越界诱导（仓位/低吸区间/止损）—— 合规红线，纯文案（P0）。
4. **§4.2** `resolve_gate` 前移 + `market_state` 注入 context —— 消除报告↔watchlist 两套口径冲突，且是 §5.5 前置上游（P0）。
5. **§4.7** 三层精选上限砍半 —— 纯 env 配置零代码，收紧被 top-5 截断的陪跑噪声（P1，建议灰度）。
6. **§3.2** 三个龙虎榜只读列 —— 零新增抓取、与 §3.1 解耦，价值取决于复盘看板是否消费，可与前述同批落但优先级低于前五条。

这六条在不依赖任何实盘回流/回测标签的前提下，覆盖**一处合规红线 + 一处确定性打分 bug + 一处口径冲突 + 一处缓存纪律 + 一处主战场收窄**，是当前阶段投入产出比最高的子集。§3.1/§4.3/§5.5/§6.4③ 等依赖 moneyflow 管线或实盘标签的条目排在这六条之后；§3.4/§3.5/§4.4/§4.6 等映射闸门/稀疏龙虎榜代理/边缘股兜底类 ROI 低，不进最小子集。

---

### 11.7 ⚠️ 报告写但代码已实现/已修复，无需再做（避免重复劳动）

- **§4.1（报告列 P0#2）分位池全涨停池解耦** —— 已被阶段 C/D/E 修复完整落地：`context_rows` 参数、`full_pool` 全池分位、读顶层 context 修掉「恒 None 静默回退入选子集」。**报告的乐观假设已被现状推翻，不要再当开发任务排期，只做回归确认。**
- **§3.6 `stk_auction` 现状已合规** —— watchlist 只下 `float_mktcap`+`first_board_vol` 两个分母，竞价比值留执行侧，无需任何代码动作（接入反而引入前视+越权）。
- **§3.1.5 与 §4.5 是同一行（`:2557`）修复** —— 复核已确认重复计数且优先级 P1/P0 分叉，**已合并为单条 P0，不要按两条不同优先级重复排期同一改动**。
- **§5.1/§5.3 自纠正确** —— 不要在 `_focus_json_contract` 登记输入字段名（该方法是输出契约非输入白名单）；资金字段不要写进共享 FOCUS 契约和首板 prompt。
- **§4.5 附注否决子项** —— `upstream_degraded` 进 hash、新增 `prior_degraded` 列经核验确属冗余（FOCUS 降级 `stock_priors=[]`→落表经 `_prob_band_to_decimal`→NULL，回测可据 NULL+stage_quality 剔除），不做。

---

### 11.8 ✅ 落地实现状态（本轮已实现，信号侧 `stock-ah-premium-ai`）

> 本轮按 §11 取舍，把「✅ 建议做」全部 + 「🟡 有条件做」中无外部前置、当前阶段即可见效的子项落地实现；经两轮多智能体对抗式代码评审 + 修复，信号侧测试 **466 passed**（无回归）、改动文件零新增 ruff 违规。详细实现设计见信号侧 `resources/doc/limit-up-doc10-landing-implementation-plan.md`。

**本轮已实现：**

| 章节 | 实现要点 | 类型 |
|---|---|---|
| §4.5 | `_run_json_stage` 的 PARSE_FALLBACK 兜底改 `failed=True` 写缓存，不再以 READY 复用「极低先验」污染 watchlist | 逻辑修复 |
| §6.2 | 角色关键词桶集中化 + 补「高辨识度」命中分支 + 否定前缀（非/不/未/无/没）守卫 + contract-drift 单测 | 逻辑修复 |
| §6.1 | `_PROB_BAND_TO_DECIMAL` 改按 tier(FIRST/CHAIN/HIGH) 键控（首板下调一档量级）+ `prob_calibration_version` 落 `strength_dim_json` + 列注释澄清「离散档值非连续概率」（**骨架**；真实频率回填待闭环） | 逻辑/契约内容 |
| §6.3① | `data_confidence`（非缺测子项权重占比）写入 `strength_dim_json`，纯只读不参与打分（**无迁移**） | 描述性增列 |
| §4.2 | `resolve_gate` 前移至 `_assemble_context`，权威 `market_state` 注入 `market_context`（含三个 SELECTION board_context 的精简闸门视图）；落表段对同一不可变 `market_emotion` 确定性 re-resolve、逐字节一致、不缓存对象 | 流程结构 |
| §5.6 | `_investment_advice_prompt`/`_final_report_prompt`/`_limit_up_system_prompt`/`_focus_json_contract` 清除诱导越界措辞（仓位态度→进攻/防守倾向、低吸区间质性化、止损→失效条件）+ 禁空泛套话纪律句 | 提示词文案 |
| §5.1 | FOCUS 通用「可引用字段白名单」（仅 FOCUS 实有字段、绝不含 capital_signals）+ chain/high 选股 prompt 资金字段白名单 + 系统提示「字段以输入实际存在者为准」 | 提示词文案 |
| §5.3① | chain/high 选股 prompt 资金信号措辞 + 「缺测≠资金弱」中性纪律 | 提示词文案 |
| §5.4 | FOCUS 契约一字板/可成交性提示（仅 `open_times==0` 且首封 30 秒内才标，缺测不臆断）+ advice 分层不高于谨慎观察 | 提示词文案 |
| §5.2 | 选股 prompt 换手经验参照（5%-25%）+ FOCUS 契约上方筹码压力参照（≤25/25-45/>45），均标「经验参照非硬规则」、只搬代码确有常量 | 提示词文案 |
| §5.5 | 系统提示复用权威 `market_state` 写可执行周期降级规则（不另立阈值，依赖 §4.2） | 提示词文案 |
| §4.7 | 三层精选上限默认值砍半：chain 20→10、high 10→5、first 5→3（合计 35→约 18），纯配置 | 配置 |
| §3.2 | watchlist 新增龙虎榜三只读列 `dragon_tiger_flag`/`lhb_net_amount`/`lhb_net_rate`（Alembic 0056、可空、`WATCHLIST_SCHEMA_VERSION`→1.1.0）；落表按 `top_list` 索引填充，**抓取失败(FAILED)→None 未知、不臆断「不在榜」**，缺测不置 0 | 契约/迁移 |
| §3.6 | 五条数据不接入红线 + 「money 子分单一资金口径」硬约束写入数据接入口径文档（`resources/doc/limit-up-data-access-redlines.md`） | 文档治理 |

**本轮暂未做（有外部硬前置 / 待闭环样本，记录承接）：**

- **§3.1 moneyflow 资金主线**（及其依赖链 §4.3 资金为主、§5.3② 首板收资金）：前置=一次性脚本验证当前积分档支持 `moneyflow` 的 `trade_date`-only 全市场放行 + 涨停/入选池覆盖率抽样 + 单位换算固化 + bump `scoring_version` 与回测同批迁移。需联网核验积分档，未在本轮。
- **§3.3 游资 / §3.4 板块资金 / §3.5 大盘资金 / §4.4 题材资金维 / §4.6 board_level 跨表兜底**：映射命中率闸门 / 长期人工字典 / 跨表 join，ROI 低或前置重。
- **§6.1 真实频率回填 / §6.3② 聚合层剔除归一 / §6.4 子分落库+WEIGHTS_V2**：依赖实盘 `qmt_*` 回流闭环样本与回测正收益标签（执行侧 miniQMT 未到位）。本轮只落结构/只读列骨架。
