# 打板量化 · QMT 侧执行引擎开发计划

> 文档日期：2026-06-13。本文是 `03-QMT侧执行引擎-开发设计.md` 的落地开发计划，按
> **规划 → 开发 → 测试 → review → 修改 → commit** 的流程组织，覆盖设计文档全部功能。
> 按项目约束：不含工期 / 工时预估，只按阶段与任务划分，给出验收标准与依赖顺序。

## 一、范围与总体策略

本计划交付设计文档第二~七节的全部执行侧模块代码与单元测试。落地工程位于本项目
`qmt_strategy/`（独立 Python 包，与信号侧 `stock-ah-premium-ai` 物理解耦）。

### 1.1 关键工程决策（落地约束）

1. **xtquant 全抽象，跨平台可测**：`xtquant`（`xttrader` / `xtdata`）仅 Windows + miniQMT 运行期可用，
   无法在开发 / CI 机安装。故本工程把 xtquant、MySQL、信号侧取数、交易日历等所有外部依赖抽象为
   `contracts/protocols.py` 中的 `Protocol`，业务模块只依赖接口；单测一律用 fake 对象（`contracts/xt_objects.py`）
   与内存实现（`InMemoryQmtRepository` / `InMemoryLocalLedger` / `StaticTradeCalendar`），
   在 macOS / Linux / Windows 均可跑通逻辑与单测。真实落地时注入 xtquant / PyMySQL 实现即可。
   —— 这与设计文档每节「单测要点」明确要求的「桩件 + 契约断言，不连真实 xttrader」完全一致。
2. **契约先行、分层并行**：先锁定 `contracts/`（枚举 / 数据结构 / 接口协议）与 `common/`（时间 / 代码归一 /
   交易日历 / 板块规则 / universe 过滤 / 日志）+ 仓储 / 台账基建，使上层模块只依赖契约、互不依赖对方实现，
   从而可按依赖分层并行开发并保持口径一致。
3. **Python 3.9+ 兼容**：全部模块 `from __future__ import annotations`；价位用 `Decimal`，时间用 `zoneinfo`。
4. **时间口径统一**：所有「东八区 ↔ UTC naive」换算只走 `common/time_utils.py`，禁手工 ±8h（对齐设计 §6.6）。
5. **安全口径**：账户 / token / DSN 等敏感项不硬编码、不入库、不写日志；`Settings.redacted()` 脱敏后才可打印。

### 1.2 待确认项的开发期默认取值（设计文档「待确认项」）

落地评审最终拍板前，代码按以下默认实现，且均做成可配置 / 可注入，便于评审结论落地后切换：

| 待确认项 | 开发期默认 | 可切换方式 |
| --- | --- | --- |
| 1. watchlist 取数 A/B | A 直读表为主，B 只读接口为备 | `Settings.watchlist_source=DB/HTTP`，二者经 `SelectedStockSource` 协议统一 |
| 2. 明细唯一键是否含 trade_date | 默认含（加固方案，防跨日 ID 复用） | `Settings.repository_unique_with_trade_date=False` 退回现行键 |
| 3. 快照表命名 | design 命名 `qmt_position_snapshot` / `qmt_account_daily` | 固定采用，不再切换 |
| 4. 竞价数据能力实测 | `auction_timing_enabled` 默认 False；实现降级 B（开盘后追） | 实测通过后置 True |
| 5. first_board_vol 来源 | 计划行带则用，缺则因子降级为 None | `PlanRow.first_board_vol` |
| 6. xtquant 字段集 | 一律 `getattr(obj, name, None)`，缺失落 NULL | fake 对象覆盖「带 / 不带可选字段」两组 |
| 9. order_remark 格式 | `LUP|<T>|<ts_code>`，超长截断保 signal_trade_date 段 | `order_executor.build_order_remark` |

### 1.3 目录与设计文档路径映射

设计文档在不同小节给出 `qmt_strategy/` / `execution/` / `qmt-collector/` 三种建议落点；本工程统一为单一
可安装包 `qmt_strategy`，模块按交易闭环分子包，映射关系如下：

| 设计建议落点 | 本工程落点 |
| --- | --- |
| `watchlist_loader/` | `qmt_strategy/watchlist/` |
| 连接守护（§2.2） | `qmt_strategy/connection/connection_guard.py` |
| `auction_poller/` / `execution/auction/*` | `qmt_strategy/auction/`（`tick_source` / `auction_factors` / `auction_poller`） |
| `entry_router/` / `execution/entry/*` | `qmt_strategy/entry/`（`entry_router` + `strategies/*`） |
| `order_executor/` / `execution/order/*` | `qmt_strategy/order/`（`order_executor` + `local_ledger`） |
| `position_manager/` `risk/`（第五节） | `qmt_strategy/position/`（`position_manager` + `sell_decider`）、`qmt_strategy/risk/` |
| `data_writer/` `reconcile/` `logger/`（`qmt-collector/*`，第六节） | `qmt_strategy/data_writer/`（`data_writer` / `normalize` / `callbacks` / `snapshot_job` / `repository`）、`qmt_strategy/reconcile/`、`qmt_strategy/common/logger.py` |
| `config/` | `qmt_strategy/config/`（`Settings`） |

## 二、阶段与任务划分

### 阶段 0 · 规划（本阶段产出）

- T0.1 工程骨架：`pyproject.toml`（pytest 配置）、各子包 `__init__.py`、`README.md`、本计划文档。
- T0.2 锁定契约层 `contracts/`：`enums` / `models` / `protocols` / `errors` / `xt_objects`。
- T0.3 基础工具 `common/`：`time_utils` / `identity` / `board_rules` / `universe_filter` / `trade_calendar` / `logger`。
- T0.4 共享基建：`data_writer/repository.py`（`InMemoryQmtRepository` + `MySqlQmtRepository` + SQL 构造）、
  `order/local_ledger.py`（`InMemoryLocalLedger`）、`config/settings.py`。
- T0.5 测试脚手架：`tests/conftest.py` + 基础层单测（时间 / 归一 / 板块 / universe / 日历 / 仓储 / 台账 / 配置）。
- **验收**：基础层单测全绿；`import qmt_strategy` 在仓库根目录可解析（无需安装）。

### 阶段 1 · 开发（按依赖分层并行，全部配套单测）

> 每个模块交付：实现文件 + 对应 `tests/test_*.py`，关键类 / 方法 / 分支补中文注释（业务意图 / 边界 / 幂等 / 重跑口径）。

**层 P1（仅依赖契约 + 基础层）**

- T1.1 连接守护 `connection/connection_guard.py`（§2.2）：构造 → register_callback → start → connect(判返0)
  → subscribe → run_forever 时序；无 on_connected；`on_disconnected` 重建新 session_id 并重连重订阅 + 触发当日补采入口。
  *验收*：对齐设计 §2.8 连接守护项；单测覆盖 §2.7 连接时序 / 无 on_connected / 断线重连。
- T1.2 watchlist 装载 `watchlist/watchlist_loader.py` + `watchlist/sources.py`（§2.3–2.6）：
  定交易日 → 两路取数（A DB / B HTTP，经 `SelectedStockSource`）→ universe_filter 兜底 → 按 tradable_flag 拆名单
  → board 预算价位 → market_state 空仓闸门 → 产出 `WatchlistContext`；全异常退化为「只守仓不开新仓」、单票价位缺失只降级单票。
  *验收*：设计 §2.8 loader 项；单测覆盖 §2.7 两路取数 / universe / 拆名单 / 价位预算 / 空仓闸门 / 取契约失败兜底。

**层 P2（依赖 P1 + 基础层）**

- T2.1 竞价因子 `auction/auction_factors.py`（纯函数，§3.4 / §3.8）：`open_pct` / `auction_volume_ratio` /
  `auction_centroid` / `virtual_seal` + `compute_auction_factors`；缺字段标 `data_quality` 不崩溃。
- T2.2 tick 源 `auction/tick_source.py`（§3.8）：封装 `XtDataLike.get_full_tick`，失败抛 `TickSourceError`；提供 fake 源。
- T2.3 竞价轮询 `auction/auction_poller.py`（§3.3 / §3.6）：自写定时器轮询（不依赖回调）、时段映射（按东八区精确到秒）、
  临近 9:20/9:25 加密、降级 A/B、`push_to_router` 推帧。
  *验收*：设计 §3.10；单测覆盖 §3.9 时段映射 / 高开 / 量能 / 重心趋势 / 虚拟封单 / 整体降级 B / 回调不触发兜底。
- T2.4 回流写端 `data_writer/normalize.py` + `data_writer/data_writer.py`（§6.2–6.5）：字段规整（代码归一 / 方向 / 状态 /
  时间双写 / 版本兼容 getattr）→ 经 `QmtRepository` 幂等 upsert；`DataWriter` 协议实现，带 `snapshot_type`。
  *验收*：设计 §6.10（落明细 / 幂等 / 时间口径）；单测覆盖 §6.9 回调落库 / 幂等 COALESCE / 时间转换 / 版本兼容。

**层 P3（依赖 P1/P2 + 基础层）**

- T3.1 建仓路由 `entry/entry_router.py` + `entry/strategies/*`（§4.2 / §4.3 / §4.6 / §4.8）：按 (strategy_family, setup)
  路由五类 action（CHASE_LIMIT_UP / CHASE_AUCTION_STRONG / DIP_BUY_MA / LEADER_PULLBACK / SKIP），
  每只票建仓窗口内只产一次有效 BUY；竞价不可得自动改判「开盘后追」；SKIP 也留痕。
  *验收*：设计 §4.10 entry_router 项；单测覆盖 §4.9 路由 / 竞价不可得改判。
- T3.2 下单执行 `order/order_executor.py`（§4.4 / §4.5 / §4.7 / §4.8）：唯一调 `xttrader.order_stock` / `cancel_order`；
  `biz_order_no` 幂等防重；限价排队 + TTL 撤单 + 9:20–9:25 不可撤；只认回报算建仓；`order_remark` 透传；
  账户隔离；一字 / 秒封挂涨停排队 + 超时撤 + 转次优 / 放弃 + 不计收益。
  *验收*：设计 §4.10 order_executor 项；单测覆盖 §4.9 幂等 / 路由 / 撤单（含不可撤段）/ 部分成交 / 成交确认口径 / 一字秒封 / order_remark / 账户隔离 / 下单失败。
- T3.3 持仓状态机 `position/position_manager.py`（§5.2 / §5.6）：按 (account_id, ts_code) 聚合 FIFO；
  `earliest_sellable_date = trade_cal_next(B)`（经交易日历，禁自然日 +1）；LOCKED_T1 → HOLDING 推进；
  先验挂接 / 纯技术退出分叉；卖出只对昨日及更早持仓生效。
- T3.4 风控 `risk/risk.py`（§5.4 / §5.6）：账户级 / 单票级阈值 + 行情 / 下单中断 → FROZEN；空仓闸门 SELL_ONLY_HOLD；
  安全默认（不确定宁可不交易）；`clamp_sell_volume = min(决策量, can_use_volume)`。
- T3.5 回调注册 `data_writer/callbacks.py`（§6.2.1）：`on_stock_trade / on_stock_order / on_order_error /
  on_cancel_error / on_stock_asset / on_stock_position / on_disconnected` → 转交 `DataWriter`；
  废单 / 拒单 / 撤单失败独有落库；同时回写 `LocalLedger`（成交累计、状态同步）。
  *验收*：单测覆盖 §6.9 废单 / 拒单 / 撤单失败独有落库、回调落库规整。

**层 P4（依赖 P3 + 基础层）**

- T4.1 卖出决策 `position/sell_decider.py`（§5.3 / §5.6）：竞价定夺（9:15–9:25）+ 分时定夺（9:30 起）+ 决策表；
  先验定基调、盘口定扳机；盘口取执行侧（OrderBook，非信号 last_price）；返回 HOLD/REDUCE/CLEAR（价位不在此）。
  *验收*：设计 §5.10；单测覆盖 §5.9 全部用例（T+1 锁定 / 可卖推进 / 连板续持 / 破位止损 / 纯技术退出 / 竞价背离 / 炸板 / 空仓闸门 / 冻结 / 卖量钳制 / 盘口来源）。
- T4.2 收盘 / 补采 `data_writer/snapshot_job.py`（§6.2.2 / §6.2.3）：收盘 `query_*` 全量明细兜底 + CLOSE 资产 / 持仓快照；
  断线重连补采（QUERY_BACKFILL）；开盘前 OPEN（可选）；CLOSE 失败退避重试 + 强告警。
  *验收*：单测覆盖 §6.9 断线补采 / 收盘兜底。
- T4.3 对账 `reconcile/reconcile.py`（§6.7 / §6.8）：委托 / 成交 / 资产 / 滑点四类勾稽（台账 vs 回报）+ 偏差告警；
  `signal_trade_date` 由 order_remark 解析（缺失则交易日历反推）回填、`resolved_ts_code` 归一。
  *验收*：设计 §6.10 对账 / 衔接项；单测覆盖 §6.9 对账 / order_remark 解析回填。

**层 P5（编排）**

- T5.1 进程编排 `app/main.py`（§1.5 主链路 / §7.4）：装配各模块 + 连接守护 + 调度入口（盘前装载、竞价轮询、收盘快照、对账）；
  `kill_switch` 只采集不下单；`auction_timing_enabled` 默认关。仅做装配与依赖注入，不重复业务逻辑。

### 阶段 2 · 测试

- T6.1 全量 `pytest` 通过；覆盖各节「单测要点」。
- T6.2 跨模块集成自检：watchlist → auction → entry → order → ledger → data_writer → reconcile 在内存实现下端到端跑通一条「下单 → 成交回报 → 落库 → 对账」链路。

### 阶段 3 · review

- T7.1 多维代码评审：正确性（守 T+1 / 幂等 / 时段不可撤 / 时间口径无 ±8h / 安全默认）、与设计文档口径一致性、
  接口契约一致性、注释完备性（中文、业务意图 / 边界 / 幂等）。

### 阶段 4 · 修改

- T8.1 按 review 结论修订并回归 `pytest`，直至全绿且评审项闭环。

### 阶段 5 · commit

- T9.1 提交。当前目录非 git 仓库且无远程：按 AGENTS.md，在本项目内初始化本地仓库做初始提交，**不强行配置 / 推送远程**，
  并在交付说明中标注「无远程，未 push」。commit message 末尾追加 model 后缀。

## 三、依赖顺序（强约束）

```
阶段0 契约/基础层  →  P1(连接守护 ∥ watchlist)  →  P2(auction ∥ data_writer)
   →  P3(entry → order ∥ position ∥ risk ∥ callbacks)  →  P4(sell_decider ∥ snapshot_job ∥ reconcile)
   →  P5(app 编排)  →  测试  →  review  →  修改  →  commit
```

- `risk` 先于 `sell_decider`（决策前必过闸门，§5.11 D2）。
- `entry_router` → `order_executor`（决策→下单，§4.11）。
- `callbacks` / `snapshot_job` 依赖 `data_writer` 接口；`reconcile` 依赖 `local_ledger` + `repository`。
- 上线次序（外部硬约束，不在本代码内）：先信号侧 go/no-go + 竞价能力实测，后实盘小仓；竞价择时先关、实测通过再灰度（设计文末「上线次序」）。

## 四、验收总纲

- 设计文档第二~七节各节「验收标准」逐条对应到模块与单测（见阶段 1 各任务 *验收*）。
- 全量 `pytest` 绿；关键安全口径（守 T+1 双保险、幂等 upsert、9:20–9:25 不可撤、时间无 ±8h、降级只守仓、kill_switch）均有单测覆盖。
- 敏感信息不入库 / 不入日志 / 不硬编码；中文注释完备。
