# 量化交易系统代码评审与优化建议 - codex

评审日期：2026-06-14  
评审对象：

- `/Users/salty/codeProject/ai/coding/astock-quant-ai`
- `/Users/salty/codeProject/ai/coding/stock-ah-premium-ai`

重点阅读入口：

- `astock-quant-ai/doc/README.md`
- `astock-quant-ai/qmt_strategy/README.md`
- `stock-ah-premium-ai/backend/app/api/routes_watchlist_export.py`
- `stock-ah-premium-ai/backend/app/api/routes_qmt_ingest.py`
- `stock-ah-premium-ai/backend/app/services/limit_up_push_service.py`
- `stock-ah-premium-ai/backend/app/services/limit_up_backtest_service.py`
- `stock-ah-premium-ai/frontend/src/pages/LimitUpPushPage.tsx`

## 一、总体结论

当前系统的方向是合理的：信号侧 `stock-ah-premium-ai` 负责涨停池、LLM 选股、watchlist 导出和 QMT 回流入库；执行侧 `astock-quant-ai/qmt_strategy` 负责本机 SQLite、QMT 适配、盘前拉取、盘中执行、盘后回流。两边通过 `GET /api/internal/watchlist` 和 `POST /api/internal/qmt/ingest` 解耦，符合“信号生产”和“真实交易执行”分离的架构要求。

但以量化交易系统的上线标准看，当前更接近“可联调骨架 + 局部闭环”，还不建议直接打开真实交易。主要风险集中在执行侧真实 QMT 适配未实测、实盘日历仍使用工作日近似、默认熔断不是安全默认、空 watchlist 会留下旧本地名单、QMT 错误回报与信号侧表约束不兼容，以及开仓风控配置没有全部进入下单前硬闸。

建议上线顺序：先完成只采集不下单的影子运行，再完成小资金实盘沙盒，再打开真实交易。每一步都应有可观测看板和硬性回滚条件。

## 二、验证记录

本次评审执行了以下检查：

- `astock-quant-ai/qmt_strategy`：`../.venv/bin/python -m pytest -q`，全部通过。
- `stock-ah-premium-ai/backend`：使用临时 venv `/tmp/stock-ah-premium-review-venv` 安装 `.[dev]` 后执行 `python -m pytest -q tests/test_qmt_ingest.py tests/test_limit_up_backtest.py tests/test_limit_up_focus_json.py`，32 个用例通过，1 个 Starlette/httpx2 迁移警告。
- `stock-ah-premium-ai/frontend`：`npm run build` 通过；Vite 提示 `index`、`charts`、`antd` 等 chunk 较大。

本次没有连接真实 miniQMT、xtquant、券商账户或生产 MySQL，因此所有真实交易 ABI、行情字段、订单常量仍必须在目标 Windows 环境单独验收。

## 三、做得好的部分

1. 架构边界清晰。`astock-quant-ai/doc/README.md` 明确 watchlist 只表达“关注什么”，不承载仓位、买卖点、止盈止损或盘口数据，避免信号侧越权控制交易。

2. 执行侧把 `xtquant` 限定在 `qmt_strategy/qmt_strategy/adapters/xt_real.py`，其余模块依赖 Protocol，可在 macOS/Linux 跑单测，工程可测性较好。

3. 执行侧本地化方案比较完整：SQLite 本地库、异步写队列、盘后回流、重启重建台账、`RemoteSyncJob` 起点 flush 和 synced 守卫都已经覆盖到测试。

4. 信号侧 QMT ingest 有白名单、内部 token、按唯一键 upsert、日期/Decimal/Boolean 反序列化，避免了机器接口直接暴露通用写库能力。

5. 信号侧打板回测明确使用 `a_trade_calendar`、不复权行情、版本化撮合口径，并把空仓日、买不进、对照组分开处理，回测可复现性优于临时脚本。

6. 代码总体中文注释较多，尤其是新增 QMT 链路、回测和风控模块，能看出业务意图、边界和重跑口径。

## 四、关键问题

### P0：真实交易入口仍是未完成骨架

执行侧 README 描述“QMT 执行引擎已落地”，但真实交易主入口仍有多处上线阻断项：

- `qmt_strategy/qmt_strategy/app/run.py:75-77` 的直连 MySQL fallback 仍直接 `raise NotImplementedError`。
- `qmt_strategy/qmt_strategy/app/run.py:113-114` 真实引擎仍装配 `WeekdayTradeCalendar()`。
- `qmt_strategy/qmt_strategy/app/run.py:159-161` `sell_books_provider=None`，盘中卖出决策没有真实盘口输入。
- `qmt_strategy/qmt_strategy/app/run.py:167-176` `run_forever` 常驻语义仍标注目标机实测。
- `qmt_strategy/qmt_strategy/adapters/xt_real.py:11-18` 明确列出 xtconstant、order_stock、StockAccount、回调 ABI、xtdata 字段均待实测。
- `qmt_strategy/qmt_strategy/order/order_executor.py:35-40` 和 `qmt_strategy/qmt_strategy/app/main.py:62-64` 的买卖方向、限价常量仍是占位。

影响：真实账户下单可能因常量错误、回调签名不匹配、字段缺失或交易日判断错误而产生废单、漏单、误同步或 T+1 口径错误。

建议：

- 增加 `QMT_LIVE_TRADING_ENABLED` 和 `QMT_XTQUANT_VERIFIED` 双开关；未同时开启时，真实适配器允许采集但禁止调用 `order_stock`。
- 增加启动前自检：xtconstant 实测快照、回调 ABI 快照、StockAccount 构造、order_stock dry-run/模拟账户、xtdata 字段样例、真实交易日历加载。
- 自检结果写入本地 `runtime_health` 或日志，并在信号侧看板展示“允许实盘/只采集/禁止启动”。

### P0：默认熔断不是安全默认

`qmt_strategy/qmt_strategy/config/settings.py:107-109` 中 `kill_switch` 默认 `False`，`from_env` 在 `qmt_strategy/qmt_strategy/config/settings.py:173-174` 也默认关闭。虽然 `auction_timing_enabled` 默认关闭，开盘后 `OPENING` 决策仍可能进入 `OrderExecutor.place`，只要环境漏配 `QMT_KILL_SWITCH` 就具备下单能力。

影响：部署新机器或重启服务时，少配一个环境变量就可能进入真实下单路径。这不符合量化执行系统“默认不交易”的上线原则。

建议：

- `kill_switch` 默认改为 `True`。
- 新增显式 `QMT_ALLOW_NEW_ORDERS=true`，并要求账户、mini_path、日历、token、自检全部通过才允许下单。
- `Settings.redacted()` 中输出 live mode 状态，但不输出 token 和账号敏感信息。

### P0：信号侧空名单成功返回时，执行侧不会清理本地旧名单

`stock-ah-premium-ai/backend/app/api/routes_watchlist_export.py:88-90` 将“当日无 READY 报告”返回 200 空集，这是协议允许的合法响应。但执行侧：

- `qmt_strategy/qmt_strategy/watchlist/remote_watchlist.py:166-171` 在 `rows` 为空时直接返回 0。
- `qmt_strategy/qmt_strategy/storage/watchlist_source.py:92-113` 的 `save_watchlist([])` 明确不删除任何日期旧名单。

影响：如果某个 `target_trade_date` 本地曾经落过名单，后来信号侧重算为空，或执行侧在同一日期重启后再次拉到 200 空集，本地旧名单仍可被 `WatchlistLoader` 读出，导致交易已被信号侧撤回的标的。

建议：

- 区分“HTTP 失败/超时”和“HTTP 成功但空名单”：
  - HTTP 失败：保留旧名单或降级只守仓。
  - HTTP 200 空名单：清空该 `target_trade_date` 的本地名单，并记录同步 marker。
- 增加 `clear_watchlist(target_trade_date)`，并补测试覆盖“旧名单 + 成功空响应 = 当日不可开仓”。

### P1：QMT 错误回报与信号侧表约束不兼容

执行侧在 `qmt_strategy/qmt_strategy/data_writer/callbacks.py:151-185` 中为 `on_order_error` 构造 `OrderRecord`。注释也说明多数 XtOrderError 只有 `order_id/error_id/error_msg`，缺 `stock_code` 时 `ts_code` 与 `qmt_stock_code` 会落 `None`。

但信号侧远端表：

- `stock-ah-premium-ai/backend/app/db/models/qmt.py:133-135` 要求 `qmt_order.ts_code` 和 `qmt_stock_code` 非空。
- `stock-ah-premium-ai/backend/alembic/versions/20260613_0053_create_qmt_tables.py:98-100` 迁移同样是 NOT NULL。
- `stock-ah-premium-ai/backend/app/services/qmt_ingest_service.py:157-162` 只提前校验唯一键，其他 NOT NULL 由数据库报错，接口会在 `routes_qmt_ingest.py:70-74` 返回 500。

影响：真实下单失败这类最需要留痕的记录，可能无法从执行侧同步到信号侧，并会以 `synced=0` 无限重试。更严重的是，失败原因不会进入复盘闭环。

建议：

- 二选一：
  - 远端 `qmt_order.ts_code/qmt_stock_code` 允许 nullable，错误行以 `order_id` 为最小事实先入库。
  - 执行侧在 `on_order_error` 中通过本地台账按 `order_id` 反查 `ts_code/qmt_stock_code/order_remark` 后再回流。
- `qmt_ingest_service` 增加全量必填列校验，数据契约错误返回 422，并带 `table/index/missing_columns`，避免伪装成 500 重试。
- 增加跨项目集成测试：最小 `XtOrderError(order_id, error_id, error_msg)` 经过 HTTP ingest 后能被远端留痕。

### P1：未知成交方向默认 BUY，会污染交易事实

`qmt_strategy/qmt_strategy/data_writer/normalize.py:123-146` 的 `default_side_resolver` 在无法识别 `order_type` 时默认返回 `TradeSide.BUY`。注释里也写明 “无法判定时默认 BUY”。

影响：真实 xtquant 版本字段若变化，卖出成交或未知成交会被记成买入，直接污染持仓、盈亏、成交率、买卖归因和信号回测对账。

建议：

- 增加 `TradeSide.UNKNOWN`，或在成交/委托事实表中保留未知方向并进入 quarantine。
- 对 `order_type is None` 或未知数值的记录，不参与持仓和收益计算，只进入数据质量告警。
- 目标机实测后固化 `_ORDER_TYPE_BUY/_ORDER_TYPE_SELL`，并把未知方向数量纳入每日健康检查。

### P1：成交 ID 缺失会被字符串化为 `"None"`

`qmt_strategy/qmt_strategy/data_writer/normalize.py:210-216` 将 `traded_id` 直接 `str(getattr(..., None))`。如果回报缺少成交编号，会变成字符串 `"None"`。本地和远端唯一键都依赖 `(account_id, trade_date, traded_id)`，多个缺失 ID 的成交会互相覆盖。

影响：成交明细丢失或串号，盘后对账可能错误地认为成交完整。

建议：

- `traded_id` 缺失时不要写入主成交表，应进入 quarantine 表或本地错误队列。
- 如必须落主表，应使用强 provenance 的合成键，例如 `MISSING:{order_id}:{traded_time}:{price}:{volume}:{seq}`，并明确标记 `data_quality=MISSING_TRADED_ID`。

### P1：买入前风控配置没有全部生效

配置中声明了多项开仓风控：

- `qmt_strategy/qmt_strategy/config/settings.py:94-105`：`max_position_per_stock`、`max_total_exposure`、`max_orders_per_day`、`per_order_max_amount`、`price_deviation_guard_pct` 等。

但实际链路中：

- `qmt_strategy/qmt_strategy/app/main.py:204-238` 的 `_router_sink` 没有调用 `Risk.gate` 或订单数/总敞口/价差偏离检查。
- `qmt_strategy/qmt_strategy/order/order_executor.py:264-287` 只按现金、单笔金额和单票金额计算下单量。
- `qmt_strategy/qmt_strategy/risk/risk.py:44-134` 主要覆盖行情/交易通道、账户回撤、亏损和空仓闸门，并且目前主要在卖出路径 `app/main.py:263-281` 使用。

影响：配置看起来是硬约束，实盘却可能没有生效，容易形成虚假的安全感。

建议：

- 在 `Engine._router_sink` 或 `OrderExecutor.place` 前增加统一 `PreOrderRiskGate`。
- 硬性校验：
  - 当日订单数不超过 `max_orders_per_day`。
  - 当前总敞口 + 本单预算不超过 `max_total_exposure`。
  - 本单价格相对涨停价/盘口/昨收的偏离不超过 `price_deviation_guard_pct`。
  - 市场状态落入 `market_state_block` 时禁开仓，而不只依赖 watchlist 上下文。
- 风控拒单要写本地决策日志，供盘后解释“为什么没买”。

### P1：生产日历仍可能错判节假日

`qmt_strategy/qmt_strategy/common/trade_calendar.py:45-50` 明确说明 `WeekdayTradeCalendar` 生产禁用。但真实入口 `qmt_strategy/qmt_strategy/app/run.py:113-114` 仍使用它。

影响：春节、国庆、临时休市等都会被当作交易日，导致：

- 盘前用错误的 `prev_open(today)` 拉错信号日。
- T+1 可卖日提前。
- 对账反推 `signal_trade_date` 失败。

建议：

- 执行侧启动时必须加载信号侧 `a_trade_calendar` 的只读快照。
- 如果日历覆盖不到当前日期前后至少 N 个交易日，直接 fail-fast 或只采集不下单。
- 日历版本和最近开市日写入运行态健康检查。

### P2：TTL 巡检在 SUBMITTED 未确认状态下会移除后续跟踪

`qmt_strategy/qmt_strategy/order/order_executor.py:306-328` 在 TTL 到期后调用 `on_ttl_expired`，随后无条件从 `_ttl_deadline` 删除。`on_ttl_expired` 在 `qmt_strategy/qmt_strategy/order/order_executor.py:379-386` 对 `SUBMITTED` 状态只记录 `order_ttl_state_not_cancelable`，不会撤单。

影响：如果迟迟没有 `on_stock_order` 回报，该订单会失去 TTL 跟踪，无法再次尝试取消或升级告警，只能等盘后对账发现异常。

建议：

- `on_ttl_expired` 返回处理结果：`CANCEL_SENT`、`TERMINAL`、`KEEP_TRACKING`。
- 对 `SUBMITTED` 保留 `_ttl_deadline`，或转入单独的 `pending_ack_deadline` 并触发连接/回报链路告警。
- 增加测试：SUBMITTED 到期后下一轮仍会被巡检。

### P2：watchlist 导出对数据质量问题过于静默

`stock-ah-premium-ai/backend/app/api/routes_watchlist_export.py:104-121` 对坏行只记日志跳过，并返回剩余 items 的 count。若最新 READY 报告存在，但 `limit_up_selected_stock` 因落表失败或坏行全部被跳过，执行侧看到的是合法空名单。

影响：执行侧无法区分“策略主动空仓”和“信号侧数据断裂”。量化系统中这两类含义完全不同。

建议：

- 响应增加 `data_quality`、`skipped_count`、`source_analysis_id`、`prompt_version`、`degraded_reason`。
- 如果 READY 报告存在但选股行缺失或坏行过多，应返回明确 degraded 状态，执行侧默认只守仓并报警。
- 导出接口可复用 `_backfill_selected_stocks_if_missing`，或在只读接口中做“检测但不修复”的健康提示。

### P2：投资建议状态可能与选股行 `advice_degraded` 脱节

`stock-ah-premium-ai/backend/app/services/limit_up_push_service.py:417-420` 在报告 READY 同事务内落 `limit_up_selected_stock`，而 `stock-ah-premium-ai/backend/app/services/limit_up_push_service.py:807-888` 的投资建议是后续附加产物。落表时 `stock-ah-premium-ai/backend/app/services/limit_up_push_service.py:597` 用 `analysis.advice_status != READY` 写入 `advice_degraded`。

影响：如果选股行先落表，建议随后生成 READY，已有 `limit_up_selected_stock.advice_degraded` 仍可能保持 True。后续基于该列的回测、导出或运营判断会读到陈旧状态。

建议：

- `ensure_advice_for_analysis` 成功后，同步更新同 `source_analysis_id` 的 selected rows。
- 或者导出/看板实时 join `LimitUpAnalysisCache.advice_status`，不把可变建议状态复制到选股事实行。

### P2：打板回测服务尚未产品化，也缺少与实盘回流的 gap 看板

`stock-ah-premium-ai/backend/app/services/limit_up_backtest_service.py` 口径清晰，但当前检索未发现对应 API 路由或前端页面。`frontend/src/pages/LimitUpPushPage.tsx` 主要覆盖报告生成、推送、分享和建议，没有 QMT 回流健康、watchlist 导出质量、实盘成交率、回测实盘 gap 看板。

影响：回测结果、信号质量、执行质量、买不进/撤单/成交/卖出归因没有在同一操作台闭环。实盘运行后，人工很难判断问题出在信号、数据、行情、执行、券商还是风控。

建议：

- 新增“QMT 交易复盘 / 执行健康”页面：
  - 今日 watchlist 导出状态、条数、版本、degraded reason。
  - 执行侧最近拉取时间、拉取到的 `target_trade_date`。
  - 当日 qmt_trade/qmt_order/qmt_position_snapshot/qmt_account_daily 回流完整性。
  - 买入信号数、下单数、成交数、撤单数、废单数、买不进数。
  - 回测预期收益 vs 实盘实际收益 gap。
  - 未同步行、ingest 失败行、缺失字段 quarantine。
- 把 `LimitUpBacktestService` 接成只读 API 和后台任务，支持指定区间、prompt_version、撮合版本重跑。

### P2：模块体积和前端包体需要拆分

当前存在明显超大文件：

- `stock-ah-premium-ai/backend/app/services/limit_up_push_service.py`：4149 行。
- `stock-ah-premium-ai/frontend/src/pages/LimitUpPushPage.tsx`：1340 行。
- `stock-ah-premium-ai/frontend/src/pages/OverviewPage.tsx`：1632 行。

前端 `npm run build` 通过，但 Vite 提示多个 chunk 过大：`index` 约 806 KB，`charts` 约 1052 KB，`antd` 约 1135 KB。

影响：后续接入实盘看板、回测看板、QMT 归因时，维护成本和首屏加载成本都会继续上升。

建议：

- 后端拆分 `limit_up_push_service.py`：
  - context builder
  - stage runner
  - selected stock persistence
  - advice generation
  - push delivery
  - report query/read model
- 前端拆分 `LimitUpPushPage`：
  - report list
  - generate controls
  - advice panel
  - share/publish panel
  - backtest/QMT health 独立页或 tab
- 用 `React.lazy`/动态 import 拆分图表页、Ant Design 重页面和报告分享页。

## 五、推荐落地路线

### 阶段 1：禁止实盘、只采集联调

目标：证明 QMT 适配、回调、盘后回流、watchlist 拉取能稳定跑完，但不允许下单。

必须完成：

- `kill_switch` 默认 True。
- 增加 live trading 双开关和启动自检。
- 用真实 `a_trade_calendar` 替换 `WeekdayTradeCalendar`。
- watchlist 200 空名单清理本地旧名单。
- QMT ingest 支持最小错误回报入库或执行侧补齐错误回报字段。
- 每日收盘后校验四张 qmt 表是否完整回流。

验收标准：

- 连续 5 个交易日只采集运行无未处理异常。
- watchlist 拉取、空名单、网络失败三种路径均有明确日志和状态。
- 所有 QMT 回调样例字段已归档到文档。

### 阶段 2：小资金沙盒

目标：打开真实下单，但资金、标的、次数、时间窗全部收紧。

必须完成：

- `max_total_exposure`、`max_orders_per_day`、`price_deviation_guard_pct` 进入下单前硬闸。
- `OrderExecutor` TTL SUBMITTED 继续跟踪。
- 未知方向、缺成交 ID、坏 ingest 记录进入 quarantine。
- 操作台能看到当日信号、订单、成交、未成交、回流状态。

验收标准：

- 任一新开仓都能追溯到 `limit_up_selected_stock`、本地 ledger、qmt_order、qmt_trade。
- 任一未成交/撤单/废单都有明确原因。
- 实盘成交与本地台账、远端 qmt 表能在 T 日收盘自动对平。

### 阶段 3：策略校准和规模化

目标：让回测、实盘、归因形成持续优化闭环。

必须完成：

- 打板回测 API/UI 产品化。
- 对照组、信号组、实盘组同口径展示。
- 龙头强度、空仓闸门、买不进模型按实盘数据校准并版本化。
- 每次策略参数变更都记录 prompt_version、threshold_version、scoring_version、exec_version。

验收标准：

- 可以按交易日、板块、tier、strategy_family、leader_strength_score 分组看命中率、成交率、收益和滑点。
- 策略收益拆解为：信号 alpha、可成交性损耗、执行滑点、风控空仓成本、卖出规则贡献。

## 六、建议优先拆出的任务

1. `astock-quant-ai`：新增真实交易启动自检和 live gate，默认禁止下单。
2. `astock-quant-ai`：实现 `DbTradeCalendar` 或本地日历快照，替换真实入口的 `WeekdayTradeCalendar`。
3. `astock-quant-ai`：watchlist 成功空响应清理当日旧名单。
4. 跨项目：修复 `on_order_error` 最小错误记录无法同步到 `qmt_order` 的契约冲突。
5. `astock-quant-ai`：成交方向 UNKNOWN/quarantine，禁止未知方向默认 BUY。
6. `astock-quant-ai`：缺失 `traded_id` 的成交进入 quarantine 或合成强 provenance key。
7. `astock-quant-ai`：买入前统一风控闸，覆盖总敞口、订单数、价格偏离。
8. `stock-ah-premium-ai`：watchlist 导出增加数据质量字段和版本字段。
9. `stock-ah-premium-ai`：新增 QMT 回流/执行复盘看板。
10. `stock-ah-premium-ai`：打板回测接入 API/UI，并展示实盘 gap。

## 七、上线判断

当前判断：不建议直接实盘下单。  

可以进入的状态：只采集、不下单、盘后回流联调。  

允许小资金实盘前，至少应关闭本评审的 P0 和 P1 项，并完成真实 Windows + miniQMT 目标机验收。
