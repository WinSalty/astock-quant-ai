# 15 · 打板推送模块 watchlist 供数评审

> 评审对象：信号侧 `stock-ah-premium-ai` 的**打板推送模块**（多阶段 LLM 选股 → 龙头增强 → 空仓闸门 → universe 过滤 → `limit_up_selected_stock` 落表 → `/api/internal/watchlist` 只读导出）能否满足执行侧（QMT）当前 watchlist 供数需求、是否存在致命 bug。
> 评审纪律（遵循本项目约定）：**只记致命（FATAL）与直接阻断/严重降低供数质量（SUPPLY_DEGRADING）的问题**；正向实现、风格瑕疵、不影响供数质量的小问题一律不收录。
> 方法：沿供数链路分 7 个维度并行评审 → 每条候选发现经**三视角对抗式核验**（复现 / 生产可达性 / 供数影响），只保留「多数核验未被驳回 且 仍达供数门槛」的发现。

---

## 0. 结论速览

| 维度 | 结论 |
|---|---|
| 是否存在**无条件致命 bug**（必崩 / 必错买 / 每日空集） | **未发现**。 |
| **正常链路能否满足 watchlist 供数需求** | **能**。契约字段在正常 pipeline 下结构性齐备、单位口径正确；空仓闸门确定性可复算；落表为原子 latest-wins；READY 早退有补落自愈。 |
| **已确认的供数缺口**（条件触发，须上线前处理） | **1 条**：早盘生成调度真空，KPL 数据晚到日 → 当日整日空 watchlist（执行侧只守仓、漏当日全部打板信号）。等级 SUPPLY_DEGRADING（非致命：不错买、不崩溃、优雅降级）。 |
| **潜在健壮性缺口**（机制真实、当前生产路径不可达，建议预防性加固） | 2 条：落表前缺跨 tier 去重；补落静默依赖 `context_json` 含 pipeline。 |
| **上线前须校准/确认的运维口径**（非代码 bug，但影响供数质量） | 3 条：空仓闸门 veto 阈值仍为占位先验；universe 依赖 `a_stock_basic.list_date` 完整性且缺数据静默丢票；导出接口 token/IP 白名单未配置则拉不到。 |

> 总判断：**打板推送模块在数据按时就绪、依赖表完整、闸门阈值已校准的前提下，可满足当前 watchlist 供数需求；唯一会"整天供不上数"的现实风险是早盘生成与执行侧盘前拉取之间的调度真空（见 §2.1），属上线前必须闭合的供数缺口。**

---

## 1. 评审范围与方法

### 1.1 覆盖文件
- `backend/app/jobs/limit_up_push_jobs.py`（早盘轮询 / 周末复推调度）
- `backend/app/services/limit_up_push_service.py`（核心：多阶段 LLM 选股、`_do_persist_selected_stocks` 落表、补落、字段转换）
- `backend/app/services/limit_up_leader_scoring_service.py`（龙头六维打分 → 强度/角色/可成交性）
- `backend/app/services/sentiment_gate.py`（确定性空仓闸门）
- `backend/app/services/universe_filter.py`（主板/创业板/ST/次新 落表前过滤）
- `backend/app/api/routes_watchlist_export.py` + `backend/app/schemas/limit_up_watchlist.py`（只读导出契约）
- `backend/app/db/models/notification.py`（`LimitUpSelectedStock` 表结构 / 唯一键）
- `backend/app/core/config.py`（调度时点 / 闸门阈值 / universe 阈值默认值）

### 1.2 评审维度与核验
7 个维度：① 落表事务 / latest-wins / 补落；② 供数字段派生 / 单位 / 结构性缺失；③ T+1 与交易日历 / 调度时点；④ 龙头打分完整性 / 资金加权；⑤ 空仓闸门 / universe 过滤；⑥ 导出接口 / 契约字段覆盖；⑦ 多阶段 LLM / 降级兜底。
每条候选发现由 3 个独立核验者分别从**复现、生产可达性、供数影响**三视角对抗式复核（默认倾向「证据不足即驳回」）。

> 维度 ②④⑥⑦ 及⑤的闸门/过滤部分**未产出达门槛的致命/降级发现**——其载荷不变量（见 §4）经核验成立，是「正常链路可满足供数」结论的依据。维度 ③ 产出本报告唯一确认发现（§2.1）；维度 ① 产出 2 条经核验为「当前不可达」的健壮性缺口（§3）。

---

## 2. 已确认问题（须上线前处理）

### 2.1 【SUPPLY_DEGRADING】早盘生成调度真空 → KPL 数据晚到日，当日整日空 watchlist

**位置**：`app/jobs/limit_up_push_jobs.py:23-36`、`app/core/config.py:202-206`；空集出口 `app/api/routes_watchlist_export.py:109-111`；未就绪即不落表 `app/services/limit_up_push_service.py:354-357`。

**机制（均经读码与跨仓核验坐实）**：
1. 生成任务为 APScheduler cron，`hour=limit_up_push_poll_hours`（默认 `"8-9"`）× `minute=limit_up_push_poll_minutes`（默认 `"31,36,41,46,51,56"`），时区 `Asia/Shanghai`。展开后**开盘前触发点仅为 8:31 / 8:36 / 8:41 / 8:46 / 8:51 / 8:56**，下一个触发点是 **9:31**——即 **9:01–9:30 之间没有任何生成触发**（minute 全部 ≥31）。`misfire_grace_time=600` 只能让 8:56 那次最晚补到约 9:06，仍覆盖不到 9:06–9:31。
2. KPL 涨停池为对中转 Tushare 的**实时查询**（`_build_context_snapshot` → `_safe_query(kpl_list, required=True)`）。KPL 未落地时 `data_ready=False`，`ensure_analysis_for_trade_date` 直接 `return None`、**当日不落选股行、无自动重试**（下一次尝试只能等下一个 cron tick）。代码注释把 KPL 名义更新时点视为「次日 8:30」，8:31–8:56 是仅 6 次、约 25 分钟的安全余量。
3. 执行侧（QMT）盘前装载并非「一次性拉取」：其 `DailyScheduler` 自 **8:55** 起每约 15s 重拉 `GET /api/internal/watchlist?date=T`，到 **9:15（集合竞价起点）即定稿**——此后整日 watchlist 锁定（拉到空集就锁定为空）。
4. 因此真实竞态窗口为 **KPL 落地于约 8:56–9:15**：信号侧 8:56 之后要等到 9:31 才再生成（已晚于执行侧 9:15 放弃重试），执行侧 8:55–9:15 期间每轮都拉到 `count=0`、9:15 定稿空集。

**供数后果**：命中该窗口时，当日 `limit_up_selected_stock` 无行、`export_watchlist` 走 `routes_watchlist_export.py:109-111` 返回 `200 / count=0` 空集；执行侧按契约降级「**只守仓、不开新仓**」，**当日全部打板信号丢失**。注意后果是**优雅降级**：不产生错值、不错买、不崩溃——故定级 **SUPPLY_DEGRADING 而非 FATAL**；但「数据其实在竞价前已可用，却因调度真空被拖到开盘后才生成」构成一整天的结构性供数空洞。

**不触发条件**（边界）：KPL ≤8:56 落地 → 8:56（或更早）那次 cron 已生成、执行侧重试循环内即能拉到，不触发；KPL >9:15 落地 → 数据本就赶不上集合竞价，非调度可解。正常 8:30 名义口径下不触发；**节假日后首日、Tushare 延迟/限流**使 KPL 落在 8:56–9:15 是现实可发生场景。

**根因**：信号侧生成调度与执行侧盘前拉取是两套独立时钟，之间**无握手、无「开盘前最后一次紧凑补跑」**保证「生成早于执行侧 9:15 定稿」；且 9:01–9:30 这段最关键的盘前窗口恰好落在 cron 触发真空里。

**整改方向**（择一或组合，具体方案另行设计）：
- 收窄/补齐盘前轮询：在 9:01–9:14 之间增加生成触发点（如 minute 步长改为覆盖整段盘前），使 KPL 晚到也能在执行侧 9:15 定稿前完成生成落表。
- 数据就绪事件驱动：`data_ready=False` 时改为短间隔自重试直至成功或到截止时点，而非干等下一个稀疏 cron tick。
- 两侧握手：执行侧定稿前若仍为空集，给信号侧一个「兜底立即生成」的盘前触发（仅生成、不改契约语义）。

**验收标准**：模拟 KPL 在 8:56–9:14 任一分钟落地，信号侧能在执行侧 9:15 定稿前完成当日选股落表、`/api/internal/watchlist?date=T` 返回 `count>0`；并补充覆盖该窗口的回归用例。

**依赖**：本项可独立处理；与执行侧定稿时点（9:15）口径相关，整改若涉及放宽执行侧定稿须与执行侧协同（跨仓）。

---

## 3. 潜在健壮性缺口（机制真实、当前生产路径不可达，建议预防性加固）

> 以下两条经三视角核验**一致判定「当前生产定时路径不可达」**（故未列入 §2），但其失效机制真实、一旦上游条件改变即可能变为可达，且加固成本低、直接关系供数完整性，按工程责任在此留痕。**它们不是当前的致命 bug。**

### 3.1 落表前缺跨 tier 去重 → 批内重复 `ts_code` 会唯一键冲突、回滚整批

**位置**：`limit_up_push_service.py:661-674`（三层入选原样拼接 `tiered`，无去重）、`:728-824`（落表循环亦无 `(trade_date, ts_code, prompt_version)` 去重）；唯一键 `uk_limit_up_selected_once` 见 `notification.py:245-251`。

**机制**：若 `add_all` 的批内出现两行同 `(trade_date, ts_code, prompt_version)`，`begin_nested` 退出 flush 时抛 `IntegrityError` → `_persist_selected_stocks` 的 `except` 回滚整个 savepoint（当批 0 行落库），而外层仍把报告置 READY → 当日空集；补落从同一 `context_json` 还原同一 pipeline，会**重演冲突、不自愈**（已用 SQLite 复现该后果链路）。

**为何当前不可达**：三层 source 是按 `board_level` 的**互斥分区**（`max_level=1` / `levels={2,3}` / `min_level>=4`，`_stocks_by_board_level`），`_board_level` 对单行是确定性纯函数；`compact_rows` 由**单次 `kpl_list?tag=涨停` 单日查询**1:1 派生（其余 tag 只进 `optional_payload`、不入 `compact_rows`），单日单 tag 下一只票仅一条涨停状态；`_select_stage_stocks` 又只在本层 `by_code` 内映射 + `seen` 层内去重。故跨层重复无产生路径——除非上游对同一 `ts_code` 返回两条板级冲突行（数据损坏）。

**建议**：`add_all` 前对 `tiered` 加一次显式跨层 `ts_code` 去重（同码保留更高板/更高优先），把「上游脏数据 → 整日空集且不自愈」的尾部风险消除在落表层。

### 3.2 补落静默依赖 `context_json` 含 pipeline，缺失即 `warning` 后跳过

**位置**：`limit_up_push_service.py:585-592`（`persist_context` 无非空 `pipeline` → `logger.warning` 后 `return`，不落表、不抛错）；早退入口 `:543-555`、`:332`。

**机制**：READY 早退补落从 `analysis.context_json` 还原 pipeline；若该字段缺 pipeline，则补落恒跳过、该信号日恒空集，仅人工 `force` 重生成可修复。

**为何当前不可达**：git 历史核验——写 `context["pipeline"]` 的多阶段路径（2026-06-05）早于消费 pipeline 的落表/补落特性（2026-06-13），且 READY 的唯一赋值点紧邻「用含 pipeline 的 context 重新 dump `context_json`」。故补落特性上线后，所有会被自动调度命中（`latest_a_trade_date` 仅取近期交易日）的存量 READY 行**都含 pipeline**；无 pipeline 的旧行只挂在数周前的历史日期、自动路径永不回查。

**建议**：跳过分支当前是 `warning` 级、对外仅表现为静默空集。建议升级为可观测告警（与导出 `skipped_count` 一致的「当日应有却为空」信号），使任何「READY 但选股缺失且补不进」的异常能被及时发现，而非靠人工巡检。

---

## 4. 「正常链路可满足供数」的关键不变量（结论依据）

> 以下为本次核验确认成立、支撑「正常链路可满足 watchlist 供数」结论的载荷不变量；列此用于回答「能否满足供数需求」，非正向评价。

| 不变量 | 核验结论 |
|---|---|
| **热字段结构闭合**：`float_mktcap`/`first_board_vol`/`seal_ratio_pct`/`limit_order`/`turnover_rate`/`close` 等执行侧依赖字段在正常 pipeline 下非结构性缺失 | 成立。`_compact_stock_row` 产出 → `*_board_context["stocks"]` → `_select_stage_stocks` 以 `{**source_row, "selection": item}` 保留全字段 → `pipeline["selected_*_stocks"]` → `_do_persist_selected_stocks` 落表，链路闭合。 |
| **单位口径**：封流比分母 `float_mktcap` 为「元」、量能比分母 `first_board_vol` 为「手」 | 成立。`circ_mv`（万元）回退路径已 `×10000` 换算为元、并有封流比 >100% 上界保护置缺；`first_board_vol` 取技术指标 `vol`、经 `_vol_to_int` 取整（修正了曾用 `_int_or_none` 恒判 None 的缺口）。 |
| **龙头打分一一对应**：`score_stocks` 输出与入选 `tiered` 顺序/条数严格对齐 | 成立。落表 `zip(tiered, scores, strict=True)`；打分基于全涨停池 `context_rows=full_pool` 取分位、注入 `supplement` 筹码，不改条数/顺序。 |
| **空仓闸门确定性且口径一致**：落表 `market_state` 与注入 prompt 的结论同源 | 成立。落表处对**同一不可变 `context["market_emotion"]`** + 同一 `settings` 阈值再 `resolve_gate` 一次；`flatten_gate_inputs` 的取键（`limit_up_count`/`emotion_cycle.broken_board_rate_pct`/`highest_chain[_change]`/`advancement.1进2.rate_pct+prev_count`/`prev_limit_up_premium.avg_pct_chg`）与生产者 `_market_emotion`/`_emotion_cycle_metrics` 实际产物逐键对齐。 |
| **契约必填字段恒赋值**：导出契约中非可空字段（`trade_date`/`target_trade_date`/`ts_code`/`tier`/`tradable_flag`/`schema_version`/`model`/`prompt_version`/`advice_degraded`）落表恒非空 | 成立。`_do_persist_selected_stocks` 对上述字段均赋确定值；坏行不会整请求 500，且以 `skipped_count` 对外可观测。 |
| **降级兜底不伪装可交易**：LLM 阶段失效时仍产出 watchlist 且显式降级 | 成立。fallback 路径仍写 `pipeline` 三键（`tiered` 不空）；`_select_fallback_reason` 来源候选落表强制 `tradable_flag=BLOCKED` + `action=放弃观察`；`_select_stage_stocks` 仅在本层 `by_code` 内映射，幻觉/串号代码被拒，不会落表为可交易标的。 |
| **落表原子 latest-wins**：整组 delete-then-insert 在同一 savepoint 内 | 成立。无 T+1 / 无候选时亦整组清旧行，不留陈旧标的；savepoint 写失败仅回滚选股行、不阻断报告 READY，并由 READY 早退补落兜回。 |

---

## 5. 上线前须校准/确认的运维口径（非代码 bug，但影响供数质量）

1. **空仓闸门 veto 阈值仍为占位先验**：`limit_up_gate_*` 默认值（如 `limit_up_count_veto=15`、`broken_rate_veto=45`、`premium_veto=-3.0`）在 `config.py` 与 `sentiment_gate.py` 注释中均标注「占位 TBD，回测对账后定稿」。当前口径下「当日涨停家数 ≤15」即硬否决为空仓、全行 `BLOCKED`——若上线前未经回测校准，**偏弱但仍可交易的日子可能整批 `BLOCKED`**（供数虽非空但全不可开仓）。须纳入 go/no-go 校准项，定稿后 bump `threshold_version`。
2. **universe 依赖 `a_stock_basic.list_date` 完整性，且缺数据 fail-closed 静默丢票**：`evaluate_universe` 对 `list_date is None` 返回 `NO_LIST_DATE` 落选。这是「宁可错杀」的安全设计，但若该参考表未同步/有空洞，相关标的会被**静默逐票丢弃且无聚合告警**（极端下接近空集仅表现为 `count` 偏小）。须确保 `a_stock_basic` 在每个信号日前已同步完整。
3. **导出接口可用性**：`watchlist_export` 内网 token 未配置 → `503`；`watchlist_export_ip_whitelist` 配置后非白名单来源 → `403`。两者任一错配都会让执行侧**拉不到 watchlist**（表现等同空集 → 只守仓）。须在上线 checklist 中确认 token 已配、执行侧出口 IP 在白名单内。

---

## 6. 整改优先级与依赖顺序

> 按本项目约定不含工时/周期预估，仅排优先级、验收标准与依赖。

| 优先级 | 事项 | 验收标准 | 依赖 |
|---|---|---|---|
| **P0（上线前必处理）** | §2.1 早盘生成调度真空 | KPL 在 8:56–9:14 落地时，执行侧 9:15 定稿前 `/api/internal/watchlist` 返回 `count>0`；补该窗口回归用例 | 若整改涉及执行侧定稿时点，须与执行侧协同 |
| **P1（预防性加固）** | §3.1 落表前加跨 tier `ts_code` 去重 | 注入同码跨层重复行时落表不再抛唯一键冲突、当批正常落库；补单测 | 无 |
| **P1（可观测性）** | §3.2 补落跳过升级为告警 | `context_json` 缺 pipeline 导致补落跳过时产生可监控告警，而非静默空集 | 无 |
| **校准（go/no-go 门槛）** | §5.1 空仓闸门 veto 阈值回测定稿 | 阈值经历史样本回测校准、bump `threshold_version`，并验证不会在正常交易日误判空仓 | 依赖回测样本（执行侧回流闭环） |
| **运维 checklist** | §5.2 `a_stock_basic` 完整性、§5.3 token/IP 白名单 | 上线前自检：参考表已同步、token 已配、执行侧出口 IP 在白名单 | 无 |

---

> 评审与修复进展请同步至 [`评审与修复状态概要.md`](评审与修复状态概要.md)；本报告与 [`10-信号侧打板报告与watchlist生成逻辑分析与优化建议.md`](10-信号侧打板报告与watchlist生成逻辑分析与优化建议.md) 互补（doc/10 侧重 LLM 报告/选股逻辑与数据缺口，本报告侧重「打板推送 → watchlist 落表/导出」供数链路的致命性与可供数性）。
