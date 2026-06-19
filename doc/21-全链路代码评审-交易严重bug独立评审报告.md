# 21 · 全链路代码评审 — 交易严重 bug 独立评审报告

> 评审日期：2026-06-19。范围：执行侧 `astock-quant-ai/qmt_strategy` 全模块 + 信号侧 `stock-ah-premium-ai` 交易闭环相关代码（watchlist 导出 / qmt 回流 / 龙头打分·回测·情绪闸）。
> 口径：**只记代码逻辑 bug 与交易严重 bug**，不记样式/命名/可读性等无关紧要项，不记正向评价。
> 方法：按子系统并行精读 → 逐条对抗式核验（回读真实代码、构造触发路径、核对是否被其它层兜底）→ 评审人独立二次确认。共 18 条候选，**核验通过 14 条**（6 严重 / 8 中等），**证伪/降级 4 条**（见附录 A）。
> 高层状态另见 [`评审与修复状态概要.md`](评审与修复状态概要.md)；本报告为评审处理文档，处理完后按 [`CLAUDE.md`](../CLAUDE.md) 归档。

---

## 0. 总览与「门控前提」说明（务必先读）

当前生产为**模拟上线 collect-only**：`QMT_KILL_SWITCH=true`（通道关、`place` 空转不下单）+ `QMT_SELL_PASS_LIVE=false`（卖出链不接 provider、`run_sell_pass` 不执行）+ `QMT_AUCTION_TIMING_ENABLED=false`（竞价单只采集）。

下表多条**严重 bug 的危害目前被这三道门控暂时遮住**，但它们都是**真实的逻辑缺陷而非门控问题**——一旦按 go/no-go 计划开 `KILL_SWITCH`/`QMT_SELL_PASS_LIVE` 即暴露。门控只推迟暴露、不修复逻辑。每条都标注了「暴露前提」。

| # | 标题 | 严重度 | 子系统 | 暴露前提 |
|---|---|---|---|---|
| B1 | 部成单(PART_TRADED)被撤后无限重复撤单 + 死量预算永久超额承诺 | 严重 | 订单执行 | 开 `KILL_SWITCH`（盘中买单部成后撤剩余，打板极常见） |
| B2 | 同步下单失败的负哨兵 order_id 被写入反查索引、击穿"未知单忽略"防线 | 中等 | 订单/持久化 | 开 `KILL_SWITCH` + 券商推负 order_id 回报（后者无代码证据） |
| P1 | 盘中重连重建对 SELLING 单元只校准量、不清在途/不复位 → 整日卡死漏卖 | 严重 | 持仓卖出 | 开 `QMT_SELL_PASS_LIVE` + 盘中断线重连 |
| P2 | 竞价段正封涨停但封流比不可得 → 误判"弱开"清仓、砸自家封板 | 严重 | 卖出决策 | 开 `QMT_SELL_PASS_LIVE` + float_mktcap 缺失 |
| P3 | 分时段同源：正封涨停但封流比不可得 → 落"尾盘了结"清仓自家封板 | 中等 | 卖出决策 | 开 `QMT_SELL_PASS_LIVE` + float_mktcap 缺失 |
| P4 | `_is_seal_strong` 仅判 `ratio>0`、非战法强封阈值 → 弱封/烂封票被误判稳封拒卖 | 中等 | 卖出决策 | 开 `QMT_SELL_PASS_LIVE` + T1.2 跨帧接线 |
| E1 | 建仓幂等锁先于编排层风控/限价闸 → 单帧瞬时拒单致该票全天再不评估（该买不买） | 严重 | 建仓路由 | 开 `KILL_SWITCH` |
| E2 | `EntryRouter._decided` 跨交易日永不重置 → 昨日成交标的次日被永久锁死不建仓 | 严重 | 建仓路由 | 进程跨日常驻 + 开 `KILL_SWITCH` |
| E3 | `AuctionPoller._history` 跨交易日永不重置 → 内存无界增长 + tick_seq 留痕失真 | 中等 | 竞价采集 | 进程跨日常驻 |
| C1 | 交易日历末日越界：买入回写吞 ValueError → 已成交持仓被静默丢出状态机、隔夜裸奔；重建路径级联致全天调度瘫痪 | 严重 | 时间日历 | 静态日历文件未及时外延 + 当日有买入成交 |
| R1 | 对账：台账侧 ERROR 卖单行未排除 → 弱关联抢走真实卖单回报，误置/掩盖次日开仓闸 | 中等 | 对账 | 开 `QMT_SELL_PASS_LIVE` + 同票当日多笔卖单 |
| R2 | 对账：缺 order_id 的卖单弱关联仅按 ts_code+方向取首条 → 多笔卖单张冠李戴 | 中等 | 对账 | 开 `QMT_SELL_PASS_LIVE` + 崩溃恢复 + 同票多笔卖单 |
| R3 | 对账：有成交无委托的"孤儿成交"被四类勾稽全部忽略、不报警不补采 | 中等 | 对账 | 委托回报漏采时序（任意门控下） |
| S1 | 信号侧导出契约无 `is_st` 字段 → 执行侧"显式 is_st"分支恒空，禁买 ST 退化为单点 name 判定 | 中等 | 契约 | 每次正常导出（常态） |

**上线阻断口径**：B1、E1、C1 在开 `KILL_SWITCH` 后即可触发；P1、P2 在开 `QMT_SELL_PASS_LIVE` 后即可触发。这 5 条应作为开对应门控前的**硬阻断项**优先修复并补测。其余按依赖顺序跟进。

---

## 1. 严重问题（开门控前必须修复）

### B1 · 部成单(PART_TRADED)被撤后无限重复撤单 + 死量预算永久超额承诺

- **位置**：`order_executor.py:1046-1092`（`on_ttl_expired`）、`local_ledger.py:158-160`（`sync_status` fill-aware 收口）、`enums.py:66,71`（`PART_TRADED` 同时属 `terminal()` 与 `active()`）。
- **逻辑**：买单部成（`0 < filled_volume < plan_volume`）后 TTL 到期，`on_ttl_expired` 发撤单置 `CANCELLING` 并在 `:1092` 无条件续写 `_ttl_deadline=now+grace`。随后 `CANCELLED` 回报到达，`sync_status` 在 `:159-160` 把"`CANCELLED/REJECTED` 且 `filled>0`"主动收口为 `PART_TRADED`（不抹真实建仓事实，有意设计）。但 `PART_TRADED` 既在 `on_ttl_expired:1046` 的可撤集合 `{SUBMITTED,REPORTED,PART_TRADED}` 内、又因 `filled` 永远达不到 `plan_volume` 而无法被 `add_fill` 推进到 `TRADED`。`H-1` 的"不回退已成交"守卫只保护 `TRADED`（`local_ledger.py:154`），**对 `PART_TRADED` 没有对称守卫**。
- **触发**：每个 `cancel_grace_seconds`（默认 30s）周期 `sweep_expired` 都重新触达该单 → 对一个券商侧早已撤销的委托反复发 `cancel_order_stock`、反复续写 deadline，**到收盘为止的无界重复废撤单**。
- **交易后果**：① 部成后撤剩余（打板/连板极常见）的单触发整日重复无效撤单，日志洪泛、对券商持续发废撤单可能触发风控限频；② `committed_amount`/`_committed_for_code`（`order_executor.py:771-775,826-828`）对 `PART_TRADED` 仍按 `filled_amt + plan_price×(plan_volume−filled)` 计承诺，其中 `remaining` 段是一笔永不会再成交的**死量**，却持续占用 `max_total_exposure` 总敞口与单票敞口，挤占其它龙头预算（少买）。
- **门控**：与卖出链/竞价门控无关，由盘中 `sweep_ttl`（`main.py:979`）驱动；开 `KILL_SWITCH`（下真实买单）后即暴露。
- **修复方向**：撤单成功进入 `CANCELLING` 后，对"已撤且 `filled>0`"收口为 `PART_TRADED` 的部成单视为**终态**——新增"部成撤单完成"语义（如 `PART_CANCELLED`）或在首次进入 `CANCELLING` 后不再对 `PART_TRADED` 续写 deadline，并使其退出 `on_ttl_expired` 可撤集合与 `active()` 的预算口径。
- **验收**：单测覆盖"部成 → TTL 撤单 → `CANCELLED` 回报收口 `PART_TRADED` → 下一轮 `sweep` 不再重撤"；并验证 `committed_amount` 对部成撤单单元不再计入死量 `remaining`。

### P1 · 盘中重连重建对 SELLING 单元只校准量、不清在途/不复位 → 整日卡死漏卖

- **位置**：`position_manager.py:645-651`（`rebuild_from_broker_positions` 非 SOLD 分支）；调用链 `main.py:1238`（`on_reconnect_backfill` → `_rebuild_positions_from_broker`）。
- **逻辑**：`rebuild_from_broker_positions` 对"已有且非 SOLD"单元一律用券商权威覆写 `volume/can_use_volume` 后 `continue`，**`SELLING` 单元同样命中该分支**：既不清 `on_road_sell_volume`、不复位 `SELLING`，也不像盘前 `reconcile_stuck_selling` 那样查券商委托终态。而 `reconcile_stuck_selling` 只在盘前 `prewarm`（`main.py:386`）调用，**重连路径不调用它**。
- **触发**：盘中断线→重连，期间某 `SELLING` 单元的卖单已被券商撤/废且撤单回执在断线期丢失。重连重建只把量校回券商权威（仍持有），单元仍停 `SELLING`、`on_road` 仍为旧冻结量 → `sellable_remaining = can_use − on_road = 0`，且 `_evaluate_and_sell_unit` 对 `SELLING` 一律 `return False`（`main.py:1077`，浮亏止损在其后不可达）。
- **交易后果**：该票当日永久卡 `SELLING`、可卖余量算成 0，整个交易日无法再卖出，止损/破位/炸板清仓全部失效（漏卖裸奔），要等次日盘前 `reconcile_stuck_selling` 才复位。
- **门控**：`SELLING` 态只能经 `mark_selling` 产生，受 `QMT_SELL_PASS_LIVE` 门控（默认关）；开门控后暴露。
- **修复方向**：`on_reconnect_backfill` 在 `rebuild` 之后补调 `_reconcile_stuck_selling`（复用券商委托终态对账口径）；或 `rebuild_from_broker_positions` 命中 `SELLING` 单元时同跑终态对账（零成交终态→`revert_selling` 清 `on_road` 复位），并校验 `on_road <= can_use`。

### P2 · 竞价段正封涨停但封流比不可得 → 误判"弱开"清仓、砸自家封板

- **位置**：`sell_decider.py:136-174`（`decide_auction`）、`:371-382`（`_is_seal_strong`）。
- **逻辑**：`doc/19 C-2` 的封板续持保护 `if book.is_sealed and self._is_seal_strong(book): return HOLD` 只在 `_is_seal_strong` 为真时生效；而 `_is_seal_strong` 要求 `seal_to_float_ratio` 非空且 `>0`。封流比 = 封单额/流通市值，`float_mktcap` 来自信号侧、是 `Optional` 且常缺（`remote_watchlist.py:147` 透传可空、`auction_factors.py:205-206` 缺市值则 `ratio=None`）。而 `is_sealed`（`auction_factors.py:186` 仅比 `last_price>=limit_up_price`，与市值解耦）此时已正确为 `True`。
- **触发**：标的当日一字/秒封涨停（`is_sealed=True`、`open_pct≈+10%`）但 `float_mktcap` 缺失 → `ratio=None` → `_is_seal_strong=False` → 封板续持分支被跳过 → `_is_weak_open`（`open_pct>0`）跳过、续持分支（先验弱）跳过 → 落 `:167 if not prior_strong: return _reduce_or_clear(reason="弱开")` → 先验弱 → `CLEAR`。
- **交易后果**：对一只**正封涨停、当日上涨约 10%** 的持仓产出 `CLEAR`（先验弱/`TECH_EXIT` 占绝大多数隔夜未再涨停的票），`_place_sell` 以 `book.last_price`(=涨停价)挂卖砸向自家封板买一队列：轻则把仍在续板的强势仓在涨停高点主动了结，重则封单薄时砸开自家涨停板触发炸板引发跟风抛压；理由还标成"弱开"污染复盘归因。这正是 C-2 想修的场景，但因保护门挂在常缺的封流比上而失效。
- **门控**：`QMT_SELL_PASS_LIVE`（默认关）；开门控后暴露。注意与 `main.py:1013-1016` 的已知限制 B1（plan 缺失→`is_sealed=False`）是**不同缺口**——此处 `is_sealed` 已为 `True`，仅封流比算不出。
- **修复方向**：封板续持判定不应只押在封流比上——`book.is_sealed=True` 时即便 `seal_to_float_ratio` 缺失也应至少 `HOLD`（不主动卖正封涨停板）；区分"封流比不可得"（保守续持）与"封流比明确偏低"（才允许减/清）；并给"弱开"分支加 `not book.is_sealed` 前置。

### E1 · 建仓幂等锁先于编排层风控/限价闸 → 单帧瞬时拒单致该票全天再不评估

- **位置**：`entry_router.py:117-130`（`on_auction_snapshot` 在产出非 SKIP 决策时立即 `_decided.add`）；`main.py:876,897-905,929-939`（`_router_sink` 随后才跑 `_open_blocked_by_risk` 与 `_limit_price_sane`，两条拒单分支都直接 `return`、**不调用 `release`**）；`main.py:925`（`release` 只在"竞价段 + `auction_timing_enabled=false` 只采集"分支调用）。
- **逻辑**：幂等锁在"下单 gating 之前"就提交，而真正的拒单闸晚于本调用执行。`_open_blocked_by_risk` 依赖每帧刷新的 `_market_feed_ok`/`_trade_conn_ok`/账户回撤查询（任一瞬时失败即返 True），`_limit_price_sane` 在 `snap.last_price` 缺失时 fail-closed 拒发——二者都可能在**产出 BUY 的那一帧**恰好为真而拒单，且都不 `release`。
- **触发（最干净路径）**：`phase>=SETTLED` 的顶板 `OPENING` 买决策被锁后，`_account_drawdown` 调 `query_stock_asset` 单次 RPC 抖动抛异常返回 None（`main.py:562-564`），叠加 `_day_open_equity>0` → `main.py:598-600` fail-closed 返 True 拒单、不 `release`。资产查询独立于行情 tick，下帧即恢复，但该票已被永久锁死、当日再不评估。
- **交易后果**：竞价/定盘窗内最强龙头因一次单帧瞬时故障被永久放弃（该买不买）；典型的 fail-then-stuck。
- **门控**：当前由 `KILL_SWITCH=true`（`place` 空转）掩盖，开通道后暴露。
- **修复方向**：把幂等锁的提交时机后移到"订单实际 `place` 成功"之后；或在 `_router_sink` 的 `risk_block`/`limit_guard` 等**所有非终态拒单分支**补 `self._entry.release(decision.ts_code)`。

### E2 · `EntryRouter._decided` 跨交易日永不重置 → 昨日成交标的次日被永久锁死不建仓

- **位置**：`entry_router.py:102,118-119,128-129`（`_decided` 构造/短路/写入）；`main.py:363-402`（`prewarm` 重置了 `_market_feed_ok`/`_auction_collect_logged`/`_reconcile_blocked` 等，**独独未重置 `_entry._decided`**，全仓 grep 无 `reset/clear`）。
- **逻辑**：`_decided` 在 `__init__` 建一次，对任一非 SKIP 决策永久写入；`release` 只覆盖竞价段只采集分支，对实际下单的 `OPENING` 决策无释放点。`EntryRouter` 随 `Engine` 一次性构造（`main.py:150`），进程经 `run.py` `while True` + `DailyScheduler` 按 date 跨日常驻。
- **触发**：某标的第 N 日产出有效 `OPENING` BUY（`dip_buy_ma`/`leader_pullback` 无条件出 `OPENING`；`chase_*` 在 `phase>=SETTLED` 出 `OPENING`，正是默认配置下唯一能真成交的路径）后，第 N+1 日同 `ts_code` 以连板身份再入选 watchlist（`_plan_map` 不按持仓排除），`on_auction_snapshot` 直接 `return None`、永不路由。
- **交易后果**：打板战法核心的"强龙头连续接力买入"被系统性漏掉（跨日该买不买）。佐证：`_strength_budget_volume`（`main.py:746-762` 注释）显式预期"跨日复买同一连板龙头"，与 `_decided` 永久锁死自相矛盾，证明是缺陷而非设计。
- **修复方向**：在 `Engine.prewarm` 每个交易日清空 `EntryRouter._decided`/`_last_recorded_action`（新增 `reset_day()` 并在 `prewarm` 调用），保证幂等集仅当日有效。

### C1 · 交易日历末日越界：买入回写吞 ValueError → 持仓被丢出状态机隔夜裸奔；重建路径级联致全天调度瘫痪

- **位置**：`common/trade_calendar.py:30-35`（`StaticTradeCalendar.next_open` 越界即 `raise ValueError`）、`position_manager.py:115`（`mark_position_on_fill` 在加锁/建单元之前第一行即调 `next_open(today)`）、`main.py:824-830`（`_apply_trade_to_position` 宽 `except` 吞掉异常仅记 `position_writeback_failed`）、`position_manager.py:661-668`（`rebuild` 的 `LOCKED_T1` 分支同样 `next_open`，循环内无 per-item try）。
- **逻辑**：生产用 `StaticTradeCalendar`，交易日集合进程启动一次性从静态文件读入，全仓无 reload/外延机制；`next_open` 在 `today>=` 已知最后一个交易日时抛 `ValueError`。`_build_calendar`（`run.py`）的 fail-closed 只对"文件缺失/读空"生效，**对"文件非空但末日==today"的陈旧日历完全不校验**。
- **触发**：日历文件未及时外延、其最后一个交易日 = 当前交易日 `today`，且当日发生买入成交（或盘中重连/收盘对账触发 `rebuild` 当日新建仓单元，券商报 `can_use==0` 命中 `LOCKED_T1` 分支）。
- **交易后果**：① 买入回写抛 `ValueError` 被 `:824` 吞掉，已真实成交的买入**永不进 `PositionManager`** → 无 T+1 管理、无止损/炸板/破位评估 → 隔夜裸奔扛单（正是该回写方法注释明示要防的"裸奔扛单"）；② `rebuild` 路径的 `ValueError` 不被局部吞、穿透 `prewarm` → `scheduler` 顶层，导致 `PREWARM` 永不标 `fired` → 当日 `RUN_AUCTION/INTRADAY` 被 `decide` 门控掐断 → 全仓卖出管理整日瘫痪。本质是"日历=权威、未来日缺失本应 fail-closed（拒开新仓+强告警）"被偷换成 fail-open。
- **门控**：触发前提是运维未及时外延静态日历文件（操作性前提）；"已成交买入被静默丢出状态机 + rebuild 级联瘫痪"独立于卖出门控。
- **修复方向**：① 买入回写处对 `next_open` 越界单独捕获，至少以保守 `earliest_sellable_date` 占位写入状态机（标"日历待补、禁卖前复核"）+ 升级强告警/熔断，绝不让已成交买入丢出状态机；② 启动期增加日历覆盖度校验（`max(open_days)` 距 `today` 不足 N 个交易日则拒启/强告警）；③ `apply_position_snapshot`/`rebuild` 循环改 per-item try/except，单票日历异常不连累整批；④ 运维侧把 `a_trade_calendar` 外延纳入盘前 prefetch 同步链路。

---

## 2. 中等问题

### B2 · 同步下单失败的负哨兵 order_id 被写入反查索引、击穿"未知单忽略"防线

- **位置**：`order_executor.py:547-552`（买）、`:716-721`（卖）把 `order_stock` 同步失败返回的负值（`protocols.py:109` 约定 `<0`=同步失败）原样写入 `order_id` 字段；`local_ledger.py:49-64`（`_index_order_id` 仅在 `order_id is None` 时跳过、**不拦负值**）。
- **逻辑**：负值是失败哨兵而非真实委托号。`(-1, today)→biz` 被注册进两级反查索引 → `_resolve_biz(-1)` 必命中最近一笔失败单；`add_fill`（`:210` 仅守卫 `TRADED`，对 `ERROR` 态无守卫）会累计 `filled_volume` 并推进 `PART_TRADED/TRADED`。"`_resolve_biz` 返回 None → 未知单忽略"这条防线对 `-1` 被静默击穿。`load_from_db` 重启再注册，缺陷跨进程存活。
- **确定可达后果**：当日多笔同步失败在 `_order_index[-1][today]` 互相覆盖（仅打 collision 告警），留痕被串改。
- **依赖未证实前提的最严后果**：若券商推送 `order_id=-1` 的异步回报，会把一笔本应 `ERROR` 终态的失败单误累计成持仓（凭空多报）。但负值意味委托未被受理，正常语义下不应再有该委托回报——本仓无代码证据，故维持中等。
- **门控**：当前 `KILL_SWITCH=true` 使 `order_stock` 不被调用、负哨兵写入不可达；开通道后暴露。
- **修复方向**：同步失败保持 `order_id=None`（失败码另存 `error_msg`/独立字段），使 `_index_order_id` 跳过、`_resolve_biz` 永不命中负值；并在 `add_fill`/`sync_status` 对 `ERROR` 终态加守卫不再累计成交。

### P3 · 分时段同源缺陷：正封涨停但封流比不可得 → 落"尾盘了结"清仓自家封板

- **位置**：`sell_decider.py:222-254`（`decide_intraday`）。与 P2 同源。
- **逻辑**：`:222 if book.is_sealed and self._is_seal_strong(book): return HOLD("秒板续持")` 同样依赖封流比。`float_mktcap` 缺失 → `_is_seal_strong=False` → 秒板续持跳过；一字封板下 `price_volume_diverge=False`、`near_close_weak` 在 `cross_frame_builder=None`（T0.1 现状）恒 `False` → 落 `:247 兜底`，先验弱 → `_reduce_or_clear(prior_strong=False)` 返回 `CLEAR("尾盘了结")`。
- **交易后果**：盘中对正封涨停持仓以涨停价挂卖砸自家封板，等同 P2 在分时段的延续。
- **门控**：`QMT_SELL_PASS_LIVE`（默认关）；开门控且 `float_mktcap` 缺失/隔夜票时暴露。
- **修复方向**：同 P2——`is_sealed=True` 时即便封流比缺失也不走兜底清仓。建议与 P2 合并修复（共改 `_is_seal_strong`/续持门槛口径）。

### P4 · `_is_seal_strong` 仅判 `ratio>0`、非战法强封阈值 → 弱封/烂封票被误判稳封拒卖

- **位置**：`sell_decider.py:222-223,136-137,371-382`。
- **逻辑**：`is_sealed=(last_price>=limit_up_price)`（只比价，不要求真有封单），`_is_seal_strong` 仅判 `seal_to_float_ratio is not None and >0`。系统其实存在封流比阈值概念 `seal_ratio_min`（`settings.py`），**但卖出侧 `_is_seal_strong` 完全没读它**。只要现价触及涨停价且 `float_mktcap` 非空且买一档有任意量，`ratio` 即为正 → 直接 `HOLD`，吞掉本帧减/清信号（封流比阈值被退化为裸 `>0` 而非战法标定的"强封"）。
- **交易后果**：触及涨停价但封板质量很弱（瞬间打板即开板/弱封/烂封）的隔夜票被判"秒板续持"返回 `HOLD`，跳过弱开/背离/尾盘了结减清逻辑，只剩单帧浮亏止损兜底；炸板前的减仓窗口被错过（该卖不卖）。
- **门控**：`QMT_SELL_PASS_LIVE` + T1.2 跨帧 builder 注入（使 `price_volume_diverge`/`near_close_weak` 真正产出非默认值）后才暴露完整危害。注：与 P2/P3 是同一函数 `_is_seal_strong` 的两个失败方向（P2/P3=封流比缺失误清；P4=封流比微正误持），**建议一并按战法标定阈值重构**：`ratio` 缺失→保守 `HOLD`、`ratio` 明确低于强封阈值才允许减/清、`ratio` 达强阈值才"稳封续持"。

### E3 · `AuctionPoller._history` 跨交易日永不重置 → 内存无界增长 + tick_seq 留痕失真

- **位置**：`auction_poller.py:76`（`_history` 仅 `__init__` 建一次）、`:147-148`（`poll_once` 每轮 `setdefault().append()` 只增不减，全仓无 `clear/reset`）。`prewarm`（`main.py:363-402`）未触碰 `_poller._history`。
- **交易后果**：进程跨日常驻（systemd daemon）时 `_history` 按 `票数×每日帧数` 线性增长（内存泄漏，长期不重启可致 OOM 影响进程稳定性）；`compute_auction_factors` 的 `tick_seq=len(prev_ticks)+1` 被前几日帧数抬高、留痕/归因失真（`tick_seq` 仅留痕、不入任何交易决策，故只审计失真）；`auction_centroid` 跨日负增量被 `delta<=0` 跳过、大体自愈，但每 `poll` 遍历帧列表随天数线性变重（性能退化）。**不触及订单/持仓/风控正确性**。
- **修复方向**：在 `Engine.prewarm`（或 `AuctionPoller.run` 入口按 `trade_date` 变化）清空 `_history`。

### R1 · 对账：台账侧 ERROR 卖单行未排除 → 弱关联抢走真实卖单回报

- **位置**：`reconcile.py:346-405`（`_match_ledger_to_order`）。
- **逻辑**：弱关联只对**回报侧**排除 ERROR（`:380`），对**台账侧**处于 ERROR 态的行不排除。卖单同步下单失败时台账行为 `state=ERROR`、`order_id` 负/None（`order_executor.py:716-721`），强关联落空后进入卖单弱关联（`:401-404`，仅按归一 `ts_code`+方向）→ 会命中同股任意一笔真实卖单回报并 `return`。强关联路径（`:362-365`）还**不检查 `consumed_order_ids`**。
- **交易后果**：对账为只读、不直接下错单，但其 report 驱动次日开仓硬闸（`main.py:1276-1289`：`missing_report`/`manual_order`/`trade_discrepancies`→`reconcile_blocked`→次日只守仓）：误产 `missing_report` 会无故封死次日整日开仓（spurious fail-closed），或把真实下单失败/漏单掩盖成 matched（fail-open）。
- **兜底现状**：`P2-1` 的 `consumed` 集（`:384`）+ "成功卖单台账行带 order_id 走强关联优先消费"挡住了最典型的"先成功 REDUCE 后 CLEAR 同步失败"链路；只有存在"未被强关联消费的同股真实卖单回报"时残留缺陷才暴露。门控：`QMT_SELL_PASS_LIVE`（同票多笔卖单前提）。
- **修复方向**：`_match_ledger_to_order` 入口对台账行 `state==ERROR` 直接 `return None`；强关联路径同样校验 `consumed_order_ids`。

### R2 · 对账：缺 order_id 的卖单弱关联仅按 ts_code+方向取首条 → 多笔卖单张冠李戴

- **位置**：`reconcile.py:401-405`。
- **逻辑**：卖单弱关联只用归一 `ts_code`+方向取第一条未消费回报；同票当日多笔卖出（先 REDUCE 后 CLEAR，两条回报）且某条台账行 `order_id` 回填缺失时，会抢走碰巧排在前面的那条回报。真正根因是强关联分支不读 `consumed`+`all_for_date` 按插入序无"强关联优先"排序。
- **交易后果**：被错配后，真正属于它的台账行落 `missing_report`，未被认领的回报被标 `manual_order`，经 `main.py:1276-1289` 误置 `reconcile_blocked` 无故阻断次日开仓（偏保守、不亏钱），或反向掩盖真实漏单。
- **触发前提（较窄）**：卖单成功路径 `order_id` 在 `order_executor.py:732` 同步回填、走强关联正常匹配；只有进程在 `order_stock` 返回有效 id 之后、`update(order_id, SUBMITTED)` 落盘之前崩溃产生"孤儿"（`:733` 注释承认此窗口），叠加同票多笔卖单才暴露。门控：`QMT_SELL_PASS_LIVE`。
- **修复方向**：卖单弱关联在同标的多候选时结合 `plan_volume`/卖量、`order_time` 邻近度、`order_remark` 语义做更窄匹配，或多候选无法唯一确定时显式落歧义告警而非取首条。

### R3 · 对账：有成交无委托的"孤儿成交"被四类勾稽全部忽略、不报警不补采

- **位置**：`reconcile.py:415-457`（`_reconcile_trades`）。
- **逻辑**：先按 `t.order_id` 汇总成交进 `trade_vol_by_order`，随后只遍历 `orders` 反查（`:436-440`）。若某条 `qmt_trade` 的 `order_id` 在 `qmt_order` 中根本不存在（委托回报漏采、成交回报到了），该成交不会被任何 order 反查到，也不在 `_reconcile_orders`/`_reconcile_slippage` 中出现 → 三类只读勾稽对孤儿成交全部视而不见，无任何 discrepancy 落地；其金额虽进 `_reconcile_assets.net_flow`，但资产偏差已降为仅告警不阻断（`main.py:1280-1285`）且多条件可被跳过/容差吞没。
- **交易后果**：真实的委托回报漏采不被对账捕获、不触发补采也不阻断，使持仓/资金事实源出现未被发现的缺口；属"最后一道闸"失效（审计漏报，不直接下错单）。任意门控下均存在。
- **修复方向**：`_reconcile_trades` 末尾对"出现在 `trade_vol_by_order` 却不在 `orders_by_id`"的 `order_id` 补一类"孤儿成交/委托漏采"偏差并 error 告警（`needs_backfill=True`），触发 `query_stock_orders` 补采或纳入阻断判定。

### S1 · 信号侧导出契约无 `is_st` 字段 → 执行侧"显式 is_st"分支恒空，禁买 ST 退化为单点 name 判定

- **位置**：信号侧 `schemas/limit_up_watchlist.py:25-90`（`LimitUpWatchlistItem` 无 `is_st`）、`db/models/notification.py:228-328`（`LimitUpSelectedStock` 无 `is_st` 列）、`services/limit_up_push_service.py:767-817`（构造时不写 `is_st`，尽管 `:734 universe_ctx.evaluate` 内部 `universe_filter.py:194` 已算出权威 `is_st_on_date`——**算出即丢弃**）；执行侧 `remote_watchlist.py:123 is_st=_to_bool(item.get("is_st"))` 恒 None。
- **逻辑**：执行侧三层 ST 禁买闸的"显式 is_st"分支因信号侧从不下发该字段而永久走不到（`order_executor.py:331` 第三道闸甚至只传 `is_st` 不传 name，依赖 `entry_router.py:324` 算定的 `decision.is_st`=`is_st_name(name)`），全部 ST 证据最终单点回落到 name 文本匹配。与 `doc/README.md:50` 等文档宣称的"is_st 经 HTTP→SQLite→PlanRow 可靠透传、不再单点押 name"**不符**。
- **交易后果**：信号侧 `name` 可空（`schema name: str|None`），名称口径/空格/大小写偶发差异时 name 匹配会漏判。若某 ST 标的绕过信号侧写入前过滤进入名单且 name 缺失/不规范，执行侧将拿不到任何 ST 证据而放行买入 ST 股，触碰"绝不买入 ST"红线。
- **兜底现状**：被信号侧两道写入前 ST 过滤（`limit_up_push_service.py:734` 数据驱动 `AStockSt` point-in-time + `:1811/4603` Tushare 取数阶段 name 剔除）+ 执行侧 name 兜底三道收窄，故定中等。任意门控下每次正常导出即触发（属常态）。
- **修复方向（二选一）**：① 在 `LimitUpSelectedStock` 增 `is_st` 列（写入时落 `universe_filter` 已查得的 `is_st_on_date`）并在 `LimitUpWatchlistItem` 暴露 `is_st` 透传给执行侧（修复成本极低，权威布尔已算出只是没持久化）；② 在执行侧文档与代码注释中如实降级为"name 单点判 ST + 信号侧写入前已过滤 ST"，去除"可靠透传 is_st"的不实承诺。**当前代码与文档/契约宣称严重不一致，必须修正其一。**

---

## 3. 修复建议的依赖与分组（无工时口径，按依赖顺序）

- **第一批（开 `KILL_SWITCH` 前硬阻断）**：B1、E1、C1。三者都在通道打开后即可触发，且 B1/C1 直接关联资金/持仓正确性。B2 可与 B1 同批（同在 `order_executor`/`local_ledger`）。
- **第二批（开 `QMT_SELL_PASS_LIVE` 前硬阻断）**：P1、P2，连带 P3、P4（P2/P3/P4 同源于 `_is_seal_strong`，建议一次重构封板质量判定口径）。R1、R2 属对账侧、与卖出链同期暴露，随第二批跟进。
- **第三批（常驻/审计健壮性，可独立推进）**：E2、E3（`prewarm` 增 `reset_day` + 清 `_history`，可一次提交）；R3（对账补孤儿成交检测）；S1（信号侧补 `is_st` 列 + 契约字段，或文档如实降级）。
- **共性根因（一次改受益多条）**：
  - `_decided` 释放/重置（E1+E2）：统一在"下单成功后才登记锁 + `prewarm` 每日重置"。
  - `_is_seal_strong` 封板质量口径（P2+P3+P4）：按战法标定阈值重构、区分缺失/弱/强三态。
  - `PART_TRADED` 双重归属（B1）：理清 `terminal()`/`active()`/可撤集合对部成收口态的口径。
  - 对账关联（R1+R2+R3）：入口排除 ERROR 台账行、强关联校验 consumed、补孤儿成交检测。

每条修复落地后须补对应单测（已在各条"验收/修复方向"标注关键断言点），并在 `评审与修复状态概要.md` 续写本轮处理状态。

---

## 附录 A · 已核验并排除的候选（4 条，非问题）

| 候选 | 排除理由（回读真实代码后） |
|---|---|
| 量权威单元上同日真实加仓被当"迟到重复回报"丢弃 | 真实存在分支但**良性**：卖量由 `can_use_volume`/`sellable_remaining` 而非 `volume` 钳定，当日加仓股 T+1 锁定（`can_use=0`）本不可卖；低报不跨日存活，次日 `prewarm rebuild` 以券商权威覆盖/重建。不触及超卖/漏卖/T+1。 |
| 竞价强开追 overheat/abandon 阈值缺失时 fail-open | **不可达**：进 `decide` 的 `PlanRow.reasonable_open_high` 经 `board_rules.budget_prices` 对 tradable 票恒为正 Decimal（信号上沿或本地现算涨停价），`MISSING` 票已在 loader 转观察名单不进 `decide`；且到达该闸前 `op` 非 None 已保证 `pre_close` 非 None/0 → `overheat_pct` 必非 None。与 `dip_buy_ma` 低吸档的差异是有意设计（低吸无回退故 fail-closed）。 |
| 竞价段卖出取盘口失败不置行情不健康 | **不致错单**：能真卖的前提（`build_sell_books` 返回 books）恰意味取数成功=行情健康；取数失败时由"无 books→不卖"间接兜底。`_market_feed_ok` 残留 True 永不是是否卖出的决定因素，且 intraday 轮会正确置位。仅损纵深防御对称性。 |
| 收盘资产快照重试耗尽抛异常致 CLOSE 持仓快照被跳过 | **被调度层整批重试兜底**：`close_batch` 不包裹 `run_close` 异常 → 冒泡 → `dispatch` 跳过 `_mark(CLOSE_BATCH)` → `decide` 在 `t>=close_time` 下个 poll（默认 15s）重新返回 `CLOSE_BATCH` → 幂等重跑直至成功/收市；瞬时抖动不会永久缺失，次日持仓走 `rebuild_from_broker_positions`。 |

## 附录 B · 潜在风险（当前不可达，留痕备查）

- **`try_next_best` 转次优绕过限价偏离/法定涨停价闸**：`_limit_price_sane`（含偏离护栏 + 超法定涨停价上界 + 限价非正校验）只在 `_router_sink` 调用，`OrderExecutor.try_next_best` → `place(nxt)` 直接进 `place`、绕过该闸。**但全仓无任何处构造 `EntryDecision(next_best=...)` 带内容**（`next_best` 恒空、`try_next_best` 恒走"空→放弃"分支），故当前不可达。若未来接线 `next_best` 序列，须同步把 `_limit_price_sane` 校验下沉到 `OrderExecutor.place`（唯一下单点），否则次优候选会绕过偏离/涨停闸。

## 附录 C · 评审范围与方法

- **执行侧**精读：`order_executor`/`local_ledger`/`position_manager`/`sell_decider`/`entry_router` 及五策略/`risk`/`buy_prefilter`/`board_rules`/`auction_factors`/`auction_poller`/`auction_window`/`storage` 全栈/`data_writer` 全栈/`reconcile`/`trade_calendar`/`time_utils`/`scheduler`/`config.settings`/`connection_guard`/`xt_real`/`watchlist` 全栈/`app/main`（引擎装配/资金分配/卖出编排/对账编排）。
- **信号侧**精读：`routes_watchlist_export`/`watchlist_service`/`schemas`、`routes_qmt_ingest`/`qmt_ingest_service`/`db/models/qmt`、`limit_up_leader_scoring_service`/`limit_up_backtest_service`/`sentiment_gate`。
- **方法**：18 个子系统并行精读找候选 → 逐条对抗式核验（独立回读源码、构造触发输入、核对是否被三层买入前置过滤/风控 fail-closed/`KILL_SWITCH`/`QMT_SELL_PASS_LIVE`/装配期 assert/唯一键幂等等兜底）→ 评审人对每条严重项做第二次独立确认（已逐行核对 `enums.active()/terminal()`、`sync_status` 收口、`prewarm` 重置项、买入回写吞异常、`_decided` 释放点、对账匹配/孤儿成交等）。
- **本仓库代码状态提示**：评审时 `doc/` 工作树存在 **25 处未提交的文档删除**（`doc/16`–`doc/20` 及 `已归档-完成不再维护/` 整目录从工作区删除但未 commit，`git ls-files` 仍跟踪）。本报告未触碰这些删除，仅新增本文件。
