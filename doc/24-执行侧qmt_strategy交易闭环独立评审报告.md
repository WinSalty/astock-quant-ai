# 24 · 执行侧 `qmt_strategy` 交易闭环独立评审报告

> 评审对象：执行侧 `astock-quant-ai/qmt_strategy`（Windows/单进程/SQLite，约 15.4k 行）。
> 评审范围：下单执行 / 本地台账 / 持仓卖出 / 建仓路由与五策略 / 集合竞价 / 风控护栏与买入前置过滤 / 异步持久化与回流幂等 / 对账与回流落库 / 调度与连接守护 / 契约模型与配置门控与时间口径。
> 配套：信号侧评审见 `25-信号侧打板推送链独立评审报告.md`；两侧联合契约评审见 `26-两侧联合交易整体评审报告.md`。

> 严重度：P0=直接错单/资金损失/漏卖漏风控/数据错乱；P1=特定条件下交易行为偏离设计；P2=较小边界/健壮性缺陷；待确认=需进一步核实。

## 统计

- 确认问题：10（P0×1，P1×5，P2×4）
- 待确认：9
- 复核排除误报：含 by-design 项（详见复核补记）

---

## 确认问题 · P0

### E-1 [幂等] 写队列溢出会丢弃「发单前 PLANNED 关键落盘」，而 `flush_confirm`/`is_healthy` 仍报成功 → 崩溃重启重复下单

- 位置：`qmt_strategy/qmt_strategy/storage/write_queue.py:116-119`（溢出静默丢弃）、`:142-158`（`is_healthy`/`_is_stuck` 不含 overflow）、`:266-274`（屏障超时）；`qmt_strategy/qmt_strategy/storage/sqlite_ledger.py:199-217`（insert→submit 丢弃、flush_confirm 透传）；`qmt_strategy/qmt_strategy/order/order_executor.py:120-138`、`:501-518`（persist 闸在「超时+健康」下放行）。
- 触发：写线程被慢/卡 I/O 拖累，队列堆积达 `max_queue`（默认 50000）；下单热路径 `insert` 一笔 PLANNED 行时 `submit` 命中 `qsize() >= max_queue` 分支，直接 `return` 丢弃该写任务（未入队、无重投、不 set `_failed`、不改 `_last_write_ok`）。
- 影响：发单前关键落盘被静默丢弃，但 `flush_confirm` 因 5 万积压屏障超时返回 False → 改判 `is_healthy`；溢出既不置 `_failed` 也不改 `_last_write_ok`，且「写线程慢但仍逐个推进」时 `_is_stuck()` 为 False（每任务刷新进展），故 `is_healthy=True` → `_persist_critical_before_order` 判为「纯超时/健康」放行 `order_stock`。结果：券商已收委托，本机 SQLite 无 PLANNED 行；溢出触发的 `_storage_ok=False` 来得太晚，只能挡后续单、挡不住当前这笔。崩溃重启 `load_from_db` 读不到该行 → `has_active` 失效 → 同一计划重复下单（真金重复建仓）。这正是 P0-C3 要堵的窗口本身。
- 限定：写线程「完全 hang」时 stuck 看门狗（默认 30s）会使 `is_healthy` 转 False、走 fail-closed 挡住；此缺陷限定在「慢但未死」子场景，无任何兜底。

---

## 确认问题 · P1

### E-2 [状态机] 龙回头策略 `LEADER_PULLBACK` 在信号侧真实取值域下永不被路由（死策略）

- 位置：`qmt_strategy/qmt_strategy/entry/entry_router.py:283-285`（龙回头分支不可达）；根因取值域见信号侧 `limit_up_leader_scoring_service.py:548-557`、`:82`。
- 触发：信号侧 `strategy_family∈{DABAN,BANLU,DIXI}`、`setup∈{首板/连板/高位}×{打板/半路/低吸}`，二者均不含「龙/龙回头/高位回踩/分歧」。
- 影响：`_select_action` 龙回头分支条件 `("龙回头" in family) or ("龙" in family and ("高位回踩" in setup or "分歧" in setup))` 对任何真实 payload 恒 False；且低吸分支（`:280`）排在其前，DIXI/高位低吸先命中 `DIP_BUY_MA`。`leader_pullback.decide`（强度+续板+首封时间承接判定）永不执行，五策略之一形同空挂。
- 复核：信号侧 `_strategy_and_action` 是 `strategy_family/setup` 唯一赋值点，全仓「龙回头」仅出现在注释；执行侧 `_normalize_family` 仅把三枚举译为「打板/半路/低吸」，`hay` 恒不含「龙」。跨侧确认见 `26` 文档 J-2。

### E-3 [取数口径] 账户已实现亏损闸 `QMT_ACCOUNT_LOSS_LIMIT` 配置后永不触发（独立闸 fail-open）

- 位置：`qmt_strategy/qmt_strategy/risk/risk.py:104-108`（gate 击穿分支）、`:188-190`（`_breached` 对 None 短路）；`qmt_strategy/qmt_strategy/app/main.py:635-642`（开仓硬编码 `account_realized_loss=None`）、`:1132-1136`（卖出未传该参数）；`qmt_strategy/qmt_strategy/app/run.py:284-290`（仅 warn 不阻断）。
- 触发：运维仅配 `QMT_ACCOUNT_LOSS_LIMIT`（当日已实现亏损熔断）而未配 `QMT_ACCOUNT_DRAWDOWN_LIMIT`。
- 影响：gate 有完整击穿分支，但全部生产调用方把观测值硬编码/默认为 None，叠加 `_breached(observed is None)→False`，使该闸永不命中。全仓 `.gate(` 非测试调用方仅 `main.py:635`/`:1132` 两处。运维若误以为已开通独立的已实现亏损止损而漏配回撤闸，则账户级日内已实现亏损熔断完全 fail-open，仅启动期一条 warn。

### E-4 [异常] `is_healthy`「单次成功即清除不健康」，间歇写失败下健康位在 True/False 抖动，漏检台账与券商不一致

- 位置：`qmt_strategy/qmt_strategy/storage/write_queue.py:188-218`（成功路径无条件复位、失败仅累加达阈值才 set）、`:142-149`（`is_healthy` 直读瞬时 `_last_write_ok`）；`qmt_strategy/qmt_strategy/app/main.py:288-290`（瞬时轮询）；`qmt_strategy/qmt_strategy/storage/sqlite_ledger.py:244-272`（成交/状态写走异步 submit、其后无 `flush_confirm` 兜底）。
- 触发：写线程交替遇失败与成功（磁盘空间临界/锁竞争间歇），每次成功 commit 都 `_consecutive_fail=0` + `_last_write_ok=True`。
- 影响：只要连续失败数没攒到 `fail_after`（默认 5）就触发 `_failed.set()`，单次偶发成功即把 `is_healthy` 拉回 True、计数清零。间歇性持续丢数据下 `is_healthy` 长期抖动、永不 fail-closed；期间失败的成交/状态写被 rollback 丢弃（write-behind 不重投），台账与券商不一致却无人察觉（漏卖/对账不平）。`storage_health_tick` 瞬时轮询恰落「刚成功」即误报健康。

### E-5 [安全] `redacted()` 脱敏白名单漏 `signal_watchlist_token`/`signal_ingest_token`，启动日志明文泄露内网写接口 token

- 位置：`qmt_strategy/qmt_strategy/config/settings.py:18-26`（`_SENSITIVE` 漏列）、`:91/93`（字段定义）、`:251/253`（from_env 直取明文）、`:433-442`（redacted 非递归仅打码 `_SENSITIVE`）；`qmt_strategy/qmt_strategy/app/run.py:391`（`logger.info("engine_boot", config=settings.redacted())`）；`qmt_strategy/qmt_strategy/common/logger.py:18-25`（`_scrub` 不递归嵌套 config dict）。
- 触发：运维用 `QMT_SIGNAL_WATCHLIST_TOKEN`/`QMT_SIGNAL_INGEST_TOKEN` 直接以环境变量（非 `_FILE`）配置分接口 token。
- 影响：`_SENSITIVE` 仅含 `signal_internal_token`，两个分接口 token 不命中脱敏分支，被原值写进 `engine_boot` 启动日志；日志层 `_scrub` 顶层 key 为 `config`、非递归不命中嵌套键。POST `/internal/qmt/ingest` 的写权限凭据明文落日志，违反 `AGENTS.md`「token 不进日志」红线。跨侧确认见 `26` 文档 J-7。

### E-6 [幂等] 无 `traded_id` 时合成去重键含原始 `traded_price`（浮点），碎单/重投可致漏去重（超量）或真实独立成交被键吞（漏计→漏卖）

- 位置：`qmt_strategy/qmt_strategy/order/local_ledger.py:200-207`（合成键 `f"_noid_{order_id}_{vol}_{traded_price}"` 既作内存去重键又落 `local_order_fill`）、`:180`（docstring 与实际行为矛盾）。
- 触发：券商回报缺 `traded_id`（异常回报，代码显式将其作为真实场景处理）。
- 影响（两向风险）：① 同一笔无 id 回报在同会话内被券商重推但价格浮点表示有别（如 `10.5` vs `10.5000001`）→ 合成键不同 → 去重失败、`filled_volume` 翻倍（超量建仓）；② 同委托同价同量两笔真实独立成交（打板碎单常见）均缺 `traded_id` → 合成键完全相同 → 第二笔被 `counted_trade_ids` 去重丢弃 → `filled_volume` 少计（漏卖/对账不平）。
- 复核：docstring `:180`「traded_id 为 None 时不去重但仍保守计入一次」与 `:197-207`（P1#8 用合成键去重）自相矛盾；实际行为是合成键去重。

---

## 确认问题 · P2

### E-7 [配置] `from_env` 多处 `_as_int(...) or 默认` / `_as_float(...) or 默认`，显式配 `0` 被当假值静默吞成默认

- 位置：`qmt_strategy/qmt_strategy/config/settings.py:255-256、313-314、319-323`（`http_timeout_seconds` / `auction_poll_interval_sec` / `decision_log_queue_size` / `decision_log_batch_size` / `order_ttl_seconds` / `trade_conn_heartbeat_fail_threshold` / `cancel_grace_seconds`）。
- 触发：运维显式把上述任一 `QMT_*` 配为 `0`（如 `QMT_ORDER_TTL_SECONDS=0` 想表达「不设存活时限」）。
- 影响：`_as_int/_as_float` 对 `"0"` 返回 `0`（非 None），但 `0` 为 falsy 被 `or` 吞回默认（如 `order_ttl` 0→60、poll 0→3.0、heartbeat 0→3）。与同文件 `seal_ratio_min`/`write_queue_max`/`write_queue_stuck_seconds`/`target_position_ratio` 等已专门改用 `is not None` 守卫以保留「显式配 0」语义的口径自相矛盾，同份配置有的项配 0 生效、有的被悄悄改默认，无任何提示。

### E-8 [异常] `from_env` 对非空非法数值环境变量无防护，`int()/float()/Decimal()` 直接抛异常使引擎启动崩溃，且注释承诺「容错解析」与行为不符

- 位置：`qmt_strategy/qmt_strategy/config/settings.py:36-51`（`_as_int/_as_float/_as_decimal` 仅对空串容错）、`:214-337`（from_env 全程无 try/except）、`:220`（注释「数值/布尔做容错解析」）；`qmt_strategy/qmt_strategy/app/run.py:387-389`（入口无外层捕获）。
- 触发：任一数值型 `QMT_*` 配成非空非法串（如 `QMT_SESSION_ID=abc`、`QMT_MAX_ORDERS_PER_DAY=10,000`、`QMT_HTTP_TIMEOUT_SECONDS=10s`）。
- 影响：非法串分别抛 `ValueError`/`InvalidOperation`，异常冒泡使 `Settings.from_env` 失败、引擎无法启动；属启动期 fail-closed（不错单），但与文档承诺的容错口径相悖，且报错位置不易定位是哪个 `QMT_*` 配错。

### E-9 [契约] `TradeCalendar` Protocol 未声明 `trading_days_left`，依赖 duck-typing 的日历耗尽预警在自定义日历实现上会静默失效

- 位置：`qmt_strategy/qmt_strategy/contracts/protocols.py:45-59`（Protocol 仅声明 `is_open/next_open/prev_open`）；`qmt_strategy/qmt_strategy/app/main.py:383-384`（`getattr+callable` 静默兜底）；`qmt_strategy/qmt_strategy/common/trade_calendar.py:6`（docstring 预示未来注入 `DbTradeCalendar`）。
- 触发：未来注入一个实现了 Protocol 三方法但未实现 `trading_days_left` 的真实日历（Protocol 不要求该方法，类型/契约检查不报缺）。
- 影响：`trading_days_left` 是日历耗尽前的覆盖度预警入口（doc/21 C1），仅由 `StaticTradeCalendar`/`WeekdayTradeCalendar` 各自实现；一旦真实日历漏实现，`getattr` 取到 None 后整段预警被静默跳过，日历悄悄耗尽时 `next_open` 越界走 fail-closed 占位（可卖日不精确）却无提前告警。当前两实现均含该方法，故为潜在缺陷。

### E-10 [状态机·latent] 卖出门控关（生产默认）时，INTRADAY 每轮 `report_market_feed(False)` 把全局 `_market_feed_ok` 置 False，把「有意不接卖出」误编码为「行情断流」

- 位置：`qmt_strategy/qmt_strategy/app/scheduler.py:141-145`（provider 为 None + `manage_feed=True` → `report(False)`）、`:249`（INTRADAY 每轮调用）；`qmt_strategy/qmt_strategy/app/run.py:421`（`QMT_SELL_PASS_LIVE=false` → provider=None）；`qmt_strategy/qmt_strategy/app/main.py:301-311`（`report_market_feed` 置 `_market_feed_ok`）、`:637`/`:1134`（`risk.gate` 第 1 层消费）。
- 触发：生产默认 `QMT_SELL_PASS_LIVE=false`，调度进入 INTRADAY（09:30–收盘）每一轮。
- 影响：把「卖出盘口源缺失（有意不接卖出）」与「行情通道断流」合并成同一个全局健康位，盘中一开始就让 FREEZE 安全默认对开新仓恒成立。当前因「9:30+ 无盘中买入轮询」（设计如此）而不致实际漏/错单，但属语义混淆 + latent landmine：任何未来新增的盘中开仓逻辑或其它依赖 `_market_feed_ok` 的判定会被静默 FREEZE。

---

## 待确认（真机量纲 / 外部时序依赖，静态不可坐实）

- **UE-1 [P2]** `place` 下单 `order_stock` **抛异常**时跳过计数 `+1`（而返回 `<0` 的路径会计数），`order_executor.py:523-542`。若券商已受理委托后才断连抛错，该笔真实委托不占 `max_orders_per_day` → 超频窗口。依赖 `order_stock` 抛错时券商是否可能已受理（同步接口语义）。
- **UE-2 [P2]** `rebuild_runtime_state` 对 AUCTION 在途单统一 `now+order_ttl_seconds` 截止，丢「原应存活到 9:25 定盘」语义，`order_executor.py:196-227`。若崩溃重启发生在竞价段且 `order_ttl_seconds` 短，竞价在途单可能定盘前被 sweep。代码已自述为保守取舍。
- **UE-3 [待确认]** 封流比 `seal_to_float_ratio` 的 `bidVol` 量纲（手/股）未核，`auction/auction_factors.py:195-206`。若真机 bidVol 为「手」而市值为「元」，封单额低估 100 倍；配正 `QMT_SEAL_RATIO_MIN` 会把强封板误判炸板弃买。默认 `0` 时护栏休眠故暂不致错，代码已门控「先实测再启用」。
- **UE-4 [待确认]** 竞价高开 `open_pct` 仅取 `tick.lastClose`、无 plan 兜底，`auction/auction_factors.py:233,253-257`；缺 `lastClose` 时强开票被标 `NO_PRE_CLOSE` 而 SKIP（且 `NO_PRE_CLOSE` 不计入降级 B、不走 OPENING 兜底）。依赖真机 tick 是否稳定带 `lastClose`。
- **UE-5 [待确认]** `qmt_ts_to_db` 对毫秒级 `traded_time/order_time` 越界抛 ValueError → 静默返回 `(None, None)`，成交时间整列丢失无告警，`common/time_utils.py:30-32` + `data_writer/normalize.py:230,297`。文档与常规约定为秒级（指向误报），需真机 `vars(obj)` 实测量纲排除毫秒/浮点。
- **UE-6 [待确认]** 交易日历文件**部分损坏行**（带额外列/BOM/编码异常）被 `except ValueError: continue` 静默跳过，只要剩 ≥1 天即通过 fail-closed 启动 → 稀疏日历使 T+1 可卖日/signal_T 反推/对账日全部漂移，无行数/区间下界校验，`app/run.py`（`_load_trade_calendar_days`）。
- **UE-7 [待确认]** 资产对账 cash 差额基线未排除收盘时点仍冻结/在途买单现金，相对容差按双边成交额放大（`turnover×0.003`），高换手日真实资金外划恰落容差内可被漏报；已 warn-only 不阻断，`reconcile/reconcile.py:539-591`。
- **UE-8 [待确认→部分自愈]** 收盘 `run_close` 中明细兜底先于 CLOSE 资产/持仓快照，明细 query 抛错会连带跳过「隔日不可补」的快照，`data_writer/snapshot_job.py:206-212`；但瞬时失败会被调度 `decide→dispatch` 逐轮重派自愈，仅「持续失败至日终」才永久缺失（从 P1 降为耦合/健壮性弱点）。
- **UE-9 [待确认→误报倾向]** `prefetch` 落库前不校验 today 是否交易日，非交易日仍按 `prev_open` 拉名单，`watchlist/remote_watchlist.py:188-249`；但唯一生产调用方 `scheduler.decide()` 已先 `is_open(today)` 否则 IDLE，且 loader 二次兜底，实际不可达。

---

## 复核补记（二次回代码核验的定级调整）

下列 5 条问题经二次回执行侧真实文件核验后调整定级：

| 初判 | 二次核验结论 |
|---|---|
| `local_ledger.py:200-207` 合成去重键（待确认） | **确认 P1**（E-6） |
| `order_executor.py:219-227` rebuild 把 SELL 计入下单次数配额（待确认） | **误报 / by-design**：`_orders_count_by_date` 在买（`:540`）与卖（`:716`）路径均自增，配额本就是「总下单次数」上限（README「含…卖出」），rebuild 计入卖单与在线行为一致 |
| `order_executor.py:523-542` 异常时计数不 +1（待确认） | **待确认 P2**（UE-1）：异常路径确跳过 `:540` 的 +1，而返回 `<0` 路径会计数，依赖券商语义 |
| `order_executor.py` AUCTION 在途单 TTL 语义（待确认） | **待确认 P2**（UE-2） |
| `app/scheduler.py` 卖出关时 INTRADAY 污染 `_market_feed_ok`（待确认） | **确认机制 P2·latent**（E-10）：当前因无盘中买入而 benign |
