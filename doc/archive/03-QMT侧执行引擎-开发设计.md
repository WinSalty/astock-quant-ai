# 打板量化 · QMT 侧执行引擎开发设计

> 文档日期：2026-06-13。

## 定位与读者说明

本文档面向开发者（含**不熟悉 QMT / xtquant 的读者**），描述部署在 **Windows VPS 上的量化策略执行引擎**的设计与落地。读者无需先精通 QMT 也应能读懂全文逻辑：凡涉及 xtquant 的概念，本文均给出「它是什么、为什么这么用」的解释。

一句话定位：

> **本引擎消费信号侧的 watchlist 契约（「今天关注哪些票」）+ 自采实时行情，自行决定择时、价位与仓位，独立完成买入 / 卖出，并承担全部交易风险。**

本文档与既有三篇 `qmt-trade-review-*` 是**互补关系**：那三篇定稿了「数据回流落表口径 + 复盘看板 + 闭环归因」（信号侧消费端），本文档负责「**交易执行决策 + 下单执行 + 回流采集端**」（执行侧写端）。两侧表结构、回流口径不在此重复展开，只做衔接说明并注明引用位置。

凡涉及 `qmt_*` 四表 DDL / 唯一键 / 时间口径 / 复盘看板 / 闭环归因细节，本文一律「引用不重写」，权威出处为：

- `resources/doc/qmt-trade-review-design.md`（四表 DDL、双通道采集、断线补采、时间口径、写入路径、对账、FIFO）。
- `resources/doc/qmt-trade-review-board-design.md`（复盘看板：净值曲线 TWR / Modified Dietz、MDD、夏普、FIFO、滑点双基准、API、前端两屏）。
- `resources/doc/qmt-trade-review-closed-loop-attribution-design.md`（闭环归因：信号-执行-结果 join、漏斗、逆向选择、先验校准、滑点、空仓反事实、`qmt_signal_attribution_daily`）。
- `resources/doc/limit-up-multi-stage-analysis-refactor-plan.md`、`resources/doc/limit-up-push-investment-advice-refactor-plan.md`（信号侧多阶段 pipeline、候选分层与竞价观察清单口径）。

## 章节目录

- [一、概述、架构边界、运行环境与项目结构](#一概述架构边界运行环境与项目结构)
- [二、连接管理与 watchlist 消费](#二连接管理与-watchlist-消费)
- [三、集合竞价轮询 auction_poller](#三集合竞价轮询-auction_poller)
- [四、建仓 entry_router 与下单 order_executor](#四建仓-entry_router-与下单-order_executor)
- [五、持仓管理 position_manager 与卖出/连板决策、风控 risk](#五持仓管理-position_manager-与卖出连板决策风控-risk)
- [六、交易数据回流采集 data_writer、对账 reconcile 与日志 logger](#六交易数据回流采集-data_writer对账-reconcile-与日志-logger)
- [七、配置、模拟盘测试、上线 checklist 与实施次序](#七配置模拟盘测试上线-checklist-与实施次序)
- [与信号侧的依赖与上线次序](#与信号侧的依赖与上线次序)
- [待确认项](#待确认项)

---

## 一、概述、架构边界、运行环境与项目结构

### 1.1 模块定位

本引擎是一个部署在 **Windows VPS 上的独立模块**，与信号侧（Linux `stock-ah-premium-ai`：FastAPI + React + MySQL `stock_ah_ai`）**进程级解耦、物理隔离**。职责边界用一句话概括：

> **消费信号侧的 watchlist 契约（「今天关注哪些票」）+ 自采实时行情，自行决定择时、价位与仓位，独立完成买入 / 卖出，并承担全部交易风险。**

定位要点逐条展开：

- **只消费契约，不消费决策。** 信号侧给的是「候选标的清单 + 该标的的强度 / 角色 / 战法 / 情绪周期 / 先验等只读属性」，本引擎据此**自主**判断：今天到底打不打、几点打、什么价位打、打多少、什么时候卖。信号侧的任何属性都是「参考输入」而非「执行指令」。
- **自采实时行情。** 盘中竞价、分时、盘口等实时行情由本引擎在 Windows 侧用 `xtdata` 自行订阅采集（详见 §1.4），**不依赖**信号侧推送实时数据；信号侧的 `realtime_quote_snapshot` 仅供其自身盯市 / 复盘使用，不构成本引擎的盘中数据源。
- **承担全部交易风险。** 实盘下单、撤单、止盈止损、风控熔断全部由本引擎在隔离账户内自行执行；一切实盘盈亏归用户。信号侧只读 QMT 回流数据做复盘，**不下单、不发起任何写交易动作**（此为既定解耦架构，详见 `resources/doc/qmt-trade-review-board-design.md` 第 0 节、`resources/doc/qmt-trade-review-design.md` 第 0 节）。
- **仓位由用户在隔离账户内自决，本引擎不做「仓位说教」。** 引擎提供仓位计算与风控护栏（单票上限、总仓上限、可用资金校验、T+1 可卖校验等，详见 position_manager / risk 模块），但**具体仓位比例、是否满仓、是否分批**等策略参数由用户在配置中自行设定。本文档与代码不对「应该用多大仓位」做价值判断或强制约束，只保证「按用户设定的仓位口径正确、安全地执行，并在越界时拦截」。

### 1.2 与信号侧的唯一契约边界

信号侧与执行侧之间**只有一条数据契约通道**：watchlist（关注清单）。除此之外两侧不共享任何运行时状态。

#### 1.2.1 契约读取方式（二选一，落地时择定其一并固化）

| 方式 | 读取目标 | 说明 |
| --- | --- | --- |
| A. 直读表 | MySQL 表 `limit_up_selected_stock`（信号侧产出） | 执行侧持有一个**只读**该表的 MySQL 账号，按 `target_trade_date = 当日交易日` 拉取当日候选清单。与既定「两侧只通过 MySQL 通信」架构一致。 |
| B. 内网只读接口 | `GET /api/internal/watchlist?trade_date=YYYY-MM-DD` | 信号侧暴露一个内网只读端点返回当日候选清单（鉴权 / 字段裁剪在接口层做），执行侧 HTTP 拉取。DB 不对执行侧开放。 |

> **落地决策（须在评审固化）**：优先 A（直读表，与回流写入用同一条 MySQL 通道，最少依赖面）；若 MySQL 端口对 Windows 侧暴露不可接受，则退化为 B。两种方式产出的**契约字段集合必须完全一致**（见 §1.2.2），watchlist_loader 模块对上层屏蔽来源差异（详见第二节 watchlist_loader 设计）。
>
> 注意：`limit_up_selected_stock` 表当前**尚无 DDL / Alembic 迁移 / ORM 模型**（已核验：backend 下 grep 该表名零命中），仅在 `qmt-trade-review-*` 三篇里给出「字段列表」。该表的建表与字段口径由 **信号侧 watchlist 契约产出设计** 负责落地；本文档（执行侧）只**消费**其字段，不负责建表。该表的 Alembic 迁移须接在当前唯一 head `20260612_0049` 之后（`down_revision="20260612_0049"`），多个新迁移须 `20260613_0050 → _0051 → …` 顺次串联，**不得并列同一 down_revision**（否则产生 alembic 多头，`upgrade head` 报错）；`revision` / `down_revision` 取值为完整 `YYYYMMDD_XXXX` 字符串，不可写裸 `0049`；MySQL 大文本沿用 `sa.Text().with_variant(mysql.LONGTEXT(), "mysql")`，`upgrade` / `downgrade` 须补中文注释。

#### 1.2.2 契约「含什么」

watchlist 契约是一只票一行的**只读候选清单**，字段口径以 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 1.1 节为准（信号侧主表字段族），关键字段：

- **身份与日期**：`ts_code`（标准代码 `600000.SH` / `000001.SZ`）、`trade_date`（= T 信号日）、`target_trade_date`（= T+1 计划买入日）。
- **强度与画像**：`leader_strength_score`（龙头强度分）、`role`（角色：龙头 / 中军 / 补涨 / 分歧转一致等）、`strategy`（战法）、`market_state`（情绪周期：启动 / 高潮 / 震荡 / 退潮 / 冰点 / 空仓）、`tradable_flag`（可成交性 / 是否重点可参与）。
- **先验概率**：`continuation_prob`（次日续板概率先验）、`next_day_premium_prob`（隔日溢价为正概率先验）。
- **参考价位**：`signal_close`（T 日收盘价）、`limit_up_price`（T 日理论涨停价）、`reasonable_open_high_low` / `reasonable_open_high_high`（合理高开买点区间）。

#### 1.2.3 契约「绝不含什么」（关键边界）

契约是「**关注什么**」，不是「**怎么交易**」。以下内容**绝不进入契约**，全部由执行侧自决：

- **不含仓位**：不给「买几股 / 几成仓 / 多少金额」。仓位 100% 由执行侧 position_manager 按用户配置计算。
- **不含买卖指令**：不给「现在买 / 现在卖 / 挂单价」。是否下单、下单时机与挂单价由执行侧 entry_router + order_executor 决定。
- **不含止盈止损价**：不给「涨到 X 卖、跌到 Y 止损」。止盈止损规则由执行侧 risk / position_manager 持有。
- **不含实时竞价 / 盘口数据**：契约是**盘前静态**的候选属性（基于 T 日收盘后生成），不携带 T+1 盘中的竞价、分时、盘口。实时行情由执行侧 auction_poller / data 模块用 `xtdata` 自采。

> 一句话边界：**`reasonable_open_high_*` 与 `*_prob` 是「信号侧的参考判断」，不是「执行侧必须照做的价位或概率指令」**。执行侧可以参考、可以否决、可以叠加自己的竞价规则。契约越薄，两侧耦合越低，执行侧的策略自由度越高，风险归属也越清晰。

### 1.3 与既有三篇 `qmt-trade-review-*` 的关系（衔接说明，不重复展开）

本引擎承担「**交易执行 + 回流采集端**」，三篇 review 文档承担「**回流落表口径 + 复盘 + 归因**」。分工与引用：

| 关注点 | 归属文档 | 本文档处理方式 |
| --- | --- | --- |
| `qmt_*` 四表（成交 / 委托 / 持仓快照 / 账户日快照）DDL、唯一键、时间口径 | `qmt-trade-review-design.md` 第 2 节 | 本引擎 data_writer 模块**按该 DDL 写入**，不重新定义表；引用注明「详见 `resources/doc/qmt-trade-review-design.md` 第 2 节」。唯一键的跨日 ID 复用加固见第六节 §6.5。 |
| 回流采集职责：实时回调落明细 + 定时快照兜底、断线重连补采、对账 | `qmt-trade-review-design.md` 第 1、3 节 | 本引擎 data_writer / reconcile / order_executor 回调即是该职责的**实现端**；采集口径引用该文，本文只描述模块如何落地。 |
| 复盘看板（净值曲线 / TWR / MDD / 夏普 / FIFO / API / 前端分区） | `qmt-trade-review-board-design.md` 全文 | 属信号侧**消费端**，本引擎不实现；只保证按其表口径正确回流，使看板有数据可读。 |
| 闭环归因（信号 × 执行 × 次日结果、逆向选择、滑点、空仓反事实、`qmt_signal_attribution_daily`） | `qmt-trade-review-closed-loop-attribution-design.md` 全文 | 同属信号侧消费端；本引擎通过 order_executor 在 `order_remark` 透传信号来源标识、回流 `signal_trade_date`，**为归因提供可关联的事实数据**。 |
| 时间口径（QMT 时间戳 → UTC naive 入库 + east8 原值留痕，禁手工 ±8h） | `qmt-trade-review-design.md` 第 4 节 | 本引擎 data_writer 严格遵循该口径，引用注明，不重述换算细节。 |

> **跨文档命名待对齐项（须在评审统一）**：`qmt-trade-review-design.md` 用 `qmt_account_daily` / `qmt_position_snapshot`，`qmt-trade-review-board-design.md` 用 `qmt_asset_daily_snapshot` / `qmt_position_daily_snapshot` 并另含 `qmt_account` / `qmt_cash_flow`。本引擎 data_writer 的写入目标表名以**信号侧最终落地的 Alembic 迁移为准**，落地时统一采用 design 文档命名（`qmt_position_snapshot` / `qmt_account_daily`），并在 `03_full_schema_with_comments.sql` / `database-schema.md` 同步；本文档凡引用快照表，均以「账户资产日快照表 / 持仓快照表」指代并标注候选表名，待信号侧定稿后回填统一名称。

### 1.4 运行环境（Windows-only）

本引擎对运行环境有**强绑定**，与信号侧（Linux）完全不同，落地前须逐项核对：

- **操作系统：Windows-only。** xtquant / miniQMT 仅提供 Windows 运行时，本引擎无法在 Linux / macOS 运行。信号侧仍在 Linux，两侧物理隔离。
- **券商终端：miniQMT（国金证券）。** 需在该 Windows VPS 上安装并登录国金证券 miniQMT 客户端，且账户已开通**程序化交易（量化）权限**。miniQMT 进程须常驻并保持登录，本引擎通过本地 xtquant 库与其通信。
- **依赖库：`xtquant`（含 `xttrader` / `xtdata`）。** 三者职责：
  - `xttrader`：交易接口——下单 `order_stock`、撤单、`query_stock_asset / positions / orders / trades`、回调订阅（`on_stock_trade` 等）。**交易与回流的事实源。**
  - `xtdata`：行情接口——订阅 / 拉取实时竞价、分时、盘口、日线。**本引擎自采实时行情的来源**（auction_poller / data 模块用它）。
  - `xttrader` 关键约束（落地强约束，详见 `resources/doc/qmt-trade-review-design.md` 第 0 节，本文不重述）：`query_*` 只返回当日、隔日清空；无 `on_connected`，连接成功靠 `connect()` 返回 0 判断；断开不自动重连，需主动 `connect` + `subscribe`；进程须 `run_forever()` 常驻；字段集存在 xtquant 版本差异，落地前须在目标机用 `vars(obj)` / `dir(obj)` 实测确认。
- **Python 版本：与目标机 miniQMT 自带 / 兼容的 `xtquant` 版本对齐。** xtquant 对 Python 版本与位数（通常 64 位）有兼容性要求；落地前须在**目标 Windows 机**实测 `import xtquant` 通过的 Python 版本并固化到 config 与部署文档，**不得**沿用信号侧 Linux 的 Python 版本假设。
- **账户：国金证券资金账户 + 程序化权限 + 隔离账户。** 该账户为用户**专用隔离账户**，仓位由用户自决。账户号、登录凭据等敏感信息**不硬编码、不入库、不写日志**（与项目 AGENTS.md 一致），按本机环境口径从外部配置注入。
- **常驻方式：Windows 任务计划程序（Task Scheduler）保活。** 引擎主进程 `run_forever()` 常驻；用 Windows 任务计划在**每日开盘前**拉起 / 重连一次（规避隔夜连接失效），并在进程异常退出后自动重启。盘后兜底批次（收盘快照、当日 `query_*` 全量对齐）亦由任务计划触发。进程退出即丢失当日推送与订阅，故保活是**回流不漏数据**的前提。

### 1.5 项目结构 `qmt_strategy/`

本引擎为 Windows 侧独立 Python 项目，建议根目录 `qmt_strategy/`，与信号侧仓库分离部署（信号侧根目录为 `/Users/salty/codeProject/ai/coding/stock-ah-premium-ai`）。模块划分按「读契约 → 采行情 → 决策择时 / 价位 → 执行下单 → 管仓位 → 风控 → 回流写库 → 对账」的交易闭环组织：

```text
qmt_strategy/
├── watchlist_loader/   # 读取 watchlist 契约（直读 limit_up_selected_stock 表 或 GET /api/internal/watchlist），
│                       #   屏蔽来源差异，向上提供当日候选清单（含强度/角色/战法/情绪周期/先验/参考价位）。
├── auction_poller/     # 用 xtdata 自采 T+1 盘中实时行情（集合竞价/分时/盘口），驱动择时与价位判断。
├── entry_router/       # 入场决策路由：综合契约属性 + 自采竞价行情 + 用户策略参数，
│                       #   自主判定"是否打、何时打、以什么价位打"（信号侧不给买卖指令，此处自决）。
├── order_executor/     # 调 xttrader.order_stock 下单/撤单，维护本地下单台账，
│                       #   order_remark 透传信号来源标识（供闭环归因 signal_trade_date 关联）。
├── position_manager/   # 仓位计算与持仓管理：按用户配置的仓位口径算下单量，
│                       #   T+1 可卖校验、隔日卖出执行；不做"仓位说教"，只按用户设定执行。
├── risk/               # 风控护栏：单票上限/总仓上限/可用资金校验/熔断/止盈止损规则执行，越界即拦截。
├── data_writer/        # 回流写库：xttrader 回调落明细 + 定时 query_* 落快照，
│                       #   写入信号侧 qmt_* 表（DDL/口径详见 qmt-trade-review-design.md 第 2、4 节），
│                       #   时间戳东八区→UTC naive 入库 + east8 原值留痕，禁手工 ±8h。
├── reconcile/          # 对账：本地下单台账 vs xttrader 回报（委托/成交/资产/滑点对账），
│                       #   断线重连后当日 query_* 全量补采（详见 qmt-trade-review-design.md 第 3 节）。
├── logger/             # 执行侧本地日志：下单/回调/断线/对账/风控拦截留痕（不写敏感信息）。
└── config/             # 配置：账户/权限/MySQL 写账号/契约来源(A 表 或 B 接口)/仓位与风控参数/
                        #   xtquant 版本与路径；敏感项从外部注入，不硬编码、不入库、不入日志。
```

模块协作主链路（一个交易日的执行闭环）：

```text
盘前  watchlist_loader 拉当日候选清单（契约只读）
  │
T+1 盘中  auction_poller(xtdata 自采竞价/盘口)
  │            │
  ▼            ▼
entry_router 自决择时/价位  ──→  position_manager 按用户仓位口径算量 + risk 护栏校验
  │
  ▼
order_executor 调 xttrader 下单（order_remark 透传信号标识）
  │
  ▼  xttrader 回调
data_writer 落 qmt_trade/qmt_order 明细  ←┐
  │                                        │ 断线/漏采
收盘  data_writer 定时 query_* 落快照      │
  │                                        │
  ▼                                        │
reconcile 本地台账 vs 回报对账 + 当日补采 ─┘
  │
信号侧（Linux）只读 qmt_* → 复盘看板 / 闭环归因（本引擎不参与，仅提供事实数据）
```

> 各模块的「文件路径与新增 / 改动点、关键方法签名与伪逻辑、数据结构 / 契约、边界与异常 / 幂等、单测要点、验收标准、依赖顺序」在后续各章逐一展开；本章只确立**定位、边界、运行环境与结构骨架**。

---

## 二、连接管理与 watchlist 消费

> 本节属于【执行侧 / Windows VPS / `xttrader`】策略进程的设计。复盘看板、QMT 数据回流（`qmt_trade` / `qmt_order` / `qmt_position_snapshot` / `qmt_account_daily` 四表 DDL、双通道采集、断线补采、时间口径、闭环归因）已在三篇 QMT 复盘文档定稿，本节只做「衔接说明 + 引用」，不重复展开：
> - 采集双通道与回调清单：详见 `resources/doc/qmt-trade-review-design.md` 第 1 节。
> - 断线重连后补采机制：详见 `resources/doc/qmt-trade-review-design.md` 第 3.2 节。
> - 东八区 / UTC naive 时间口径：详见 `resources/doc/qmt-trade-review-design.md` 第 4 节。
> - `limit_up_selected_stock` 与 `qmt_*` 关联键（`norm_code + target_trade_date`）：详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 1 节、`resources/doc/qmt-trade-review-design.md` 第 2.5 节。
> - `limit_up_selected_stock` 字段全集（`market_state` / `tradable_flag` / `continuation_prob` / `next_day_premium_prob` / `signal_close` / `limit_up_price` / `reasonable_open_high_low` / `reasonable_open_high_high` 等）以上述文档为权威，本节直接消费，不重定义。

### 2.1 模块定位与边界

执行侧策略进程职责拆成两块，**职责互不耦合**：

1. **连接守护（connection guard）**：维护 `xttrader` 长连接的生命周期（建会话 → 注册回调 → 启动 → 连接 → 订阅 → 常驻），断线后重建。它只管「连接活着且推送 / 订阅有效」，不读 watchlist、不做选股。回报落库（`on_stock_trade` / `on_stock_order` 等）属于回流职责，**详见 `resources/doc/qmt-trade-review-design.md` 第 1 节**，本节不重述回调写表逻辑。
2. **盘前 watchlist 装载（watchlist_loader）**：每个交易日开盘前一次性把「今日可交易 / 观察名单 + 每票今日涨停价与合理高开区间 + 当日是否空仓」装入内存，供盘中下单决策（第三、四节）按 `ts_code` O(1) 查询。它只读、不写库。

两者的连接点：连接守护保证 `xttrader` 可用是「能不能下单」的前提；watchlist_loader 产出的内存契约是「该不该对某票下单、用什么价」的依据。任一不就绪，盘中一律退化为「只守仓、不开新仓」（见 2.6 异常兜底）。

> 不可推翻的 QMT API 强约束（落地依据，已据官方文档 + GitHub 镜像核实，**详见 `resources/doc/qmt-trade-review-design.md` 第 0 节**）：`on_connected` 回调不存在，连接成功只能靠 `connect()` 返回 `0` 判断；连接是一次性的，断开后**不会自动重连**；进程必须 `run_forever()` 常驻，进程退出即丢失当日推送与订阅；字段集存在版本差异，落地前须在目标机用 `vars(obj)` / `dir(obj)` 实测字段存在性，避免 `AttributeError`。

### 2.2 连接生命周期（时序）

标准启动顺序固定为：**构造 → 注册回调 → start → connect（判返 0）→ subscribe → run_forever 常驻**。中间任一步失败即视为连接未就绪。

```
启动（每日开盘前由任务计划拉起，或进程内每日重建）：
  session_id = 生成唯一会话号（时间戳/自增；每次重连必须换新 session_id，旧 session 不复用）
  trader = XtQuantTrader(qmt_userdata_path, session_id)   # path=QMT 客户端 userdata 目录
  callback = ExecCallback(...)                            # 实现 on_stock_trade / on_stock_order /
                                                          #   on_order_error / on_cancel_error /
                                                          #   on_stock_asset / on_stock_position /
                                                          #   on_disconnected（注意：无 on_connected）
  trader.register_callback(callback)
  trader.start()                                          # 启动交易线程
  rc = trader.connect()                                   # rc == 0 视为连接成功；非 0 即失败
  if rc != 0:
      log("connect 失败 rc=%s" % rc); 退避后重试 / 上报告警; 本轮不进入下单态
  acc = StockAccount(account_id)
  sub_rc = trader.subscribe(acc)                          # 订阅账户推送，sub_rc == 0 成功
  if sub_rc != 0:
      log("subscribe 失败 sub_rc=%s"); 视为未就绪
  # 至此连接就绪：盘中推送（成交/委托/资产/持仓）开始进回调
  ready = True
  trader.run_forever()                                    # 阻塞常驻，进程不退出

断线（运行期，xttrader 主动回调）：
  on_disconnected():                                      # 注意：QMT 不自动重连
      ready = False
      log("disconnected at %s" % now_east8)               # 写执行侧本地日志
      重建：换新 session_id → 新建 XtQuantTrader → register_callback → start
            → connect()（判返 0）→ subscribe()（判返 0）→ ready=True
      重连成功后立即用 query_* 全量补采当日缺口         # 补采细节详见 qmt-trade-review-design.md 第 3.2 节
      # 补采不在本节展开，只在此标注「断线 → 重建 session → 重连 → 重订阅 → 补采」串联点
```

关键口径：

- **每次重连必须使用新 `session_id`**，不得复用已断开的旧 session，否则可能订阅失效却无回报。
- **不实现自动重连轮询替代任务计划**：QMT 连接隔夜易失效，依赖「Windows 任务计划 / 守护进程每日开盘前主动拉起或重建一次」作为主线（**详见 `resources/doc/qmt-trade-review-design.md` 第 3.2 节第 3 点**），`on_disconnected` 重建仅作盘中兜底，二者叠加而非互斥。
- **建议每日开盘前（如 9:00 前）主动重建一次 session 重连重订阅**，规避隔夜连接失效；重建与 watchlist_loader 装载同窗口完成，盘前一次性把「连接 + 名单 + 价位 + 空仓判定」全部就绪。

### 2.3 watchlist_loader：装载流程

watchlist_loader 在**开盘前（约 9:00 前）**由调度触发一次，整批装载当日名单，产出常驻内存的 `WatchlistContext`，供盘中只读。流程：

1. **定交易日**：`target_trade_date = 今日`（东八区自然交易日，DATE）。先用 `a_trade_calendar` 校验今日 `is_open=1`；非交易日直接空载并标记 `tradable=False`（盘中不开新仓）。
2. **整批取信号**（两路二选一，落地前定一种，另一路作降级）：
   - **路径 A：直读 MySQL**——执行侧用只读账号直接 `SELECT * FROM limit_up_selected_stock WHERE target_trade_date = :today`。与既定解耦架构（两侧只通过 MySQL / 只读接口通信）一致，延迟最低。
   - **路径 B：只读接口**——调用信号侧 FastAPI 只读端点（如 `GET /api/selected-stocks?target_trade_date=today`）。当执行侧不便直连 MySQL 时使用。
   - 两路返回同一契约（一股一行，字段以 `limit_up_selected_stock` 全集为准，**详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 1.1 节**）。
3. **universe_filter 兜底**：对取回的每一行复跑**信号侧同一套 universe 规则**。规则采用「闭式 allow-list」前缀白名单（仅放行下列 10 个前缀，其余一律剔除）：沪市主板 `600/601/603/605`、深市主板 `000/001/002/003`、创业板 `300/301`；隐式排除科创 `688/689`、北交 `8xx/920`、老三板 / 北交 `4xx`、B 股 `2xx/9xx` 等非目标段，并剔除 ST / 停牌 / 退市（T+1 口径）。这是**冗余防线**——信号侧理应已过滤，但执行侧再过一遍，防止信号侧规则漂移或脏数据导致对禁交易标的下单。规则来源复用信号侧，不在执行侧另立一套阈值（避免两侧不一致）。
   - **ST 判定口径**：本节属 T+1 实盘 live 路径，ST 过滤沿用「按当日实时行名称含 `ST`（及退市整理 `退` / `*退`）判定」即可——当日名称即 point-in-time 正确，无需 `a_stock_st` 历史表。`a_stock_st`（按 `trade_date` 取每日 ST 名单）仅在**回测历史 universe** 路径才需要（避免用最新名称引入幸存者偏差 / 未来函数），与本 live 路径不同（见「待确认项」）。
4. **按 `tradable_flag` 拆名单**：
   - `tradable_flag` 表「可成交性」（如非一字板、有合理买点）→ 进入「**可交易名单**」（盘中允许开新仓的候选）。
   - 其余（一字 / 秒封先验买不进、或被 universe_filter 拦下）→ 进入「**观察名单**」（盘中只跟踪、不下单，留作复盘对照与「买不进机会成本」统计）。
5. **预算今日价位（按 board）**：对每票用信号侧给的 `signal_close`（T 日收盘价）按所属板块涨跌幅规则现算：主板 ±10%、创业板 ±20%（不复权，按 A 股涨跌停四舍五入到 0.01 规则）。
   - 优先直接采用信号侧已算好的 `limit_up_price`（T 日理论涨停价）与 `reasonable_open_high_low` / `reasonable_open_high_high`（合理高开区间）字段；
   - 信号侧未给或字段缺失时，执行侧用 `signal_close` + board 规则**兜底现算**涨停价与合理高开区间，并标记 `price_source=LOCAL_CALC` 以便复盘区分。
6. **读 market_state 判空仓闸门**：取当日 `market_state`（情绪周期，信号侧统一口径）。判定「是否空仓日」：
   - 空仓信号（如情绪冰点 / 系统性风险）→ `open_new_position_allowed = False`：盘中**只守仓（管理已有持仓的卖出 / 止盈止损），不开任何新仓**。
   - 非空仓 → `open_new_position_allowed = True`：按可交易名单 + 价位区间正常开新仓。
   - 空仓判定**只关「开新仓」闸门，不影响卖出 / 守仓**（T+1 已买入持仓仍按既定纪律处置）。
7. **装入内存**：产出 `WatchlistContext`（见 2.5 数据结构），盘中下单决策只读它，不再回库查询（降低盘中延迟、避免盘中 DB 抖动影响下单）。

### 2.4 loader 方法签名（伪逻辑，非可运行代码）

```python
class WatchlistLoader:
    """盘前一次性装载当日可交易/观察名单与价位契约。

    口径：target_trade_date=今日；两路取数二选一（A 直读 MySQL / B 只读接口）；
    复跑信号侧 universe_filter 兜底；按 tradable_flag 拆名单；按 board 预算今日涨停价与
    合理高开区间；读 market_state 决定是否空仓（空仓只守仓不开新仓）。只读、不写库。
    """

    def load(self, today: date) -> "WatchlistContext":
        """主入口：装载当日全量契约。异常路径见 2.6（取契约失败=禁开新仓只守仓）。"""

    def _fetch_selected_stocks(self, today: date) -> list[SelectedStockRow]:
        """整批取 target_trade_date=today 的信号行。
        路径 A：只读账号 SELECT FROM limit_up_selected_stock WHERE target_trade_date=today；
        路径 B：调信号侧只读端点。两路返回同一契约。空结果 → 当日无候选。"""

    def _apply_universe_filter(self, rows: list[SelectedStockRow]) -> list[SelectedStockRow]:
        """复用信号侧 universe 规则兜底：闭式 allow-list 前缀白名单（仅放行
        600/601/603/605/000/001/002/003/300/301，其余一律剔除）、排除 ST/停牌/退市。
        冗余防线，防信号侧规则漂移；被拦下的转入观察名单。"""

    def _split_by_tradable(self, rows) -> tuple[list, list]:
        """按 tradable_flag 拆 (可交易名单, 观察名单)。一字/秒封先验买不进 → 观察名单。"""

    def _budget_prices(self, row: SelectedStockRow) -> PriceBudget:
        """按 board 预算今日价位：优先用信号侧 limit_up_price / reasonable_open_high_*；
        缺失则以 signal_close + board 规则（主板±10% / 创业板±20%，不复权）兜底现算，
        标 price_source=LOCAL_CALC。"""

    def _resolve_open_gate(self, market_state: str) -> bool:
        """读 market_state 判空仓闸门：空仓 → 返回 False（open_new_position_allowed），
        盘中只守仓不开新仓；非空仓 → True。该闸门只关开新仓，不影响卖出。"""
```

### 2.5 内存数据结构（盘中只读契约）

```python
@dataclass
class PriceBudget:
    limit_up_price: Decimal            # 今日理论涨停价（信号侧给或 board 现算）
    reasonable_open_low: Decimal       # 合理高开区间下沿
    reasonable_open_high: Decimal      # 合理高开区间上沿
    board: str                         # MAIN(主板±10%) / CHINEXT(创业板±20%)
    price_source: str                  # SIGNAL=采用信号侧 / LOCAL_CALC=执行侧兜底现算

@dataclass
class TradableEntry:
    norm_code: str                     # 归一 ts_code（600000.SH 等），与 qmt_trade 关联键一致
    target_trade_date: date            # =今日（=信号 T 的 T+1 买入日）
    signal_trade_date: date            # =T（透传，便于回流期与 limit_up_selected_stock join）
    market_state: str                  # 情绪周期（透传，盘中分组/记录用）
    tradable_flag: ...                 # 可成交性（信号侧口径）
    role: str; strategy: str           # 角色/战法（透传，供下单决策与复盘）
    leader_strength_score: ...         # 龙头强度分（透传）
    continuation_prob: ...; next_day_premium_prob: ...   # 连板/隔日溢价先验（透传）
    price: PriceBudget

@dataclass
class WatchlistContext:
    trade_date: date                   # target_trade_date=今日
    is_open: bool                      # a_trade_calendar 校验
    open_new_position_allowed: bool    # 空仓闸门：False=只守仓不开新仓
    tradable: dict[str, TradableEntry] # 可交易名单，key=norm_code，盘中 O(1) 查
    watch_only: list[TradableEntry]    # 观察名单（不下单，留作复盘/买不进对照）
    degraded: bool                     # 是否进入降级态（取契约失败兜底）
    degraded_reason: str | None
```

`norm_code` / `target_trade_date` / `signal_trade_date` 三个透传字段是为了与回流期 `qmt_trade` / `qmt_order` 的关联键对齐（**关联口径详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 1.2 节**），下单时由执行侧透传到 `order_remark` / `signal_trade_date`，闭环归因据此回挂。

### 2.6 异常兜底与降级（核心安全口径）

**总原则：宁可少做不可错做——任何上游不确定，一律退化为「只守仓、不开新仓」，绝不在契约残缺时盲开新仓。**

| 异常场景 | 判定 | 兜底动作 |
| --- | --- | --- |
| **取契约失败**（路径 A 直读 MySQL 失败 / 路径 B 接口超时或非 200 / 返回空且非预期） | `WatchlistContext.degraded=True` | **禁开新仓、只守仓**：`open_new_position_allowed=False`，`tradable={}`；不阻塞已有持仓的卖出纪律。记日志 + 告警。 |
| 路径 A 故障且配置了路径 B | 主路失败 | 切换降级路（A↔B 互为备份），仍失败再走「取契约失败」兜底。 |
| `a_trade_calendar` 显示今日非交易日 | `is_open=False` | 空载，盘中不开新仓（理论上不应被拉起，作防御）。 |
| `market_state` 取不到 / 不可解析 | 闸门不确定 | **按空仓处理**（保守）：`open_new_position_allowed=False`，只守仓。 |
| 某票 `signal_close` 缺失且 `limit_up_price` / 合理高开区间也缺失 | 无法预算价位 | 该票单独剔出可交易名单转入观察名单（**单票降级，不影响整批**），记 `price_source=MISSING`。 |
| universe_filter 命中（ST / 科创 / 北交 / 停牌 / 退市 / 非白名单前缀等） | 不应交易 | 该票转入观察名单，不下单。 |
| 连接守护未就绪（`connect()` / `subscribe()` 未返 0，或 `on_disconnected` 后未重连成功） | `ready=False` | 盘中不发起任何新开仓委托；卖出 / 撤单等守仓动作在连接恢复后按补采口径处理（**补采详见 `resources/doc/qmt-trade-review-design.md` 第 3.2 节**）。 |

「禁开新仓只守仓」是这套兜底的统一落点：开新仓需要「连接就绪 + 当日契约完整 + 非空仓 + 单票价位可算 + 通过 universe」全部成立；守仓（管理 T+1 已买入持仓的卖出）只要求连接就绪，**不依赖盘前契约**，因此契约失败时守仓仍可进行。

### 2.7 单测 / 模拟要点

连接守护与 loader 都依赖外部（QMT API、MySQL / 接口），单测以**桩件 + 契约断言**为主，不连真实 `xttrader`：

- **连接时序**：mock `XtQuantTrader`，断言调用顺序严格为 `register_callback → start → connect → subscribe → run_forever`；`connect()` 返回非 0 时断言**不进入就绪态、不调 subscribe、不发新开仓**；`subscribe()` 返回非 0 时断言 `ready=False`。
- **无 on_connected**：断言就绪判定只读 `connect()` 返回值，代码中不存在对 `on_connected` 的依赖（防止误加该回调）。
- **断线重连**：触发 mock `on_disconnected()`，断言生成**新 `session_id`**（≠ 旧值）、重新走 `register_callback → start → connect → subscribe` 全序、且重连后触发当日补采入口（补采本身的写表断言归回流测试，本节只断言「触发」）。
- **loader 两路取数**：分别 mock 路径 A（DB session 返回固定行集）与路径 B（HTTP 桩返回同一 JSON），断言两路产出**同一 `WatchlistContext`**；mock A 抛异常时断言切 B 或进降级态。
- **universe_filter 兜底**：构造含 ST / 科创（`688*`）/ 北交（`.BJ` / `8xx` / `920`）/ 非白名单前缀 / 停牌行，断言被剔出可交易名单、进观察名单。
- **tradable_flag 拆名单**：构造 `tradable_flag` 真 / 假混合行，断言可交易 / 观察名单数量与归属正确；一字板先验票进观察名单。
- **board 价位预算**：参数化主板（`signal_close=10.00` → 涨停 `11.00`）与创业板（`signal_close=10.00` → 涨停 `12.00`），断言涨停价与合理高开区间数值及 `price_source`（信号侧给 → `SIGNAL`，缺失 → `LOCAL_CALC`，全缺 → 该票转观察名单 `MISSING`）。
- **空仓闸门**：参数化 `market_state` 为空仓 / 非空仓 / 不可解析，断言 `open_new_position_allowed` 分别为 `False / True / False`；并断言空仓态下守仓动作仍允许、开新仓被拒。
- **取契约失败兜底**：mock 取数全部失败，断言 `degraded=True`、`open_new_position_allowed=False`、`tradable={}`、产生告警，且不抛出致使进程退出的异常（loader 失败不得拖垮常驻连接进程）。
- **时间 / 代码归一**：断言 `norm_code` 经 `stock_identity_resolver` 归一为 `600000.SH` 形态、`target_trade_date` 为东八区交易日 DATE（口径**详见 `resources/doc/qmt-trade-review-design.md` 第 4 节**）。

### 2.8 验收标准

- 进程启动严格按 `构造 → register_callback → start → connect(判返0) → subscribe → run_forever` 时序，无对 `on_connected` 的依赖；`connect()` / `subscribe()` 任一非 0 时进程不进入开新仓态。
- `on_disconnected` 后能自动重建会话（新 `session_id`）并重连重订阅，且触发当日补采；进程在断线后不退出、可恢复。
- Windows 任务计划 / 守护进程配置就位，能每日开盘前主动重建连接一次。
- watchlist_loader 盘前一次装载即产出完整 `WatchlistContext`：`target_trade_date=今日`、两路取数任一可用、universe_filter 兜底生效、按 `tradable_flag` 正确拆名单、每票今日涨停价与合理高开区间已缓存、`open_new_position_allowed` 正确反映 `market_state` 空仓判定。
- 任一上游异常（取契约失败 / 连接未就绪 / `market_state` 缺失 / 非交易日）均退化为「只守仓、不开新仓」，且不影响已有持仓的卖出守仓；单票价位缺失只降级单票不影响整批。
- 上述单测要点全部覆盖并通过；loader 装载失败不导致常驻连接进程崩溃。

### 2.9 依赖顺序

1. **前置（已定稿，引用）**：`qmt_*` 四表 DDL、双通道采集、断线补采、时间口径（`resources/doc/qmt-trade-review-design.md` 第 1、2、3、4 节）；`limit_up_selected_stock` 字段全集与关联键（`resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 1 节）。
2. **本节交付（无先后强约束，可并行）**：连接守护（2.2）与 watchlist_loader（2.3–2.6）。loader 不依赖连接守护，连接守护不依赖 loader。
3. **本节下游**：盘中下单决策（第三、四节）消费 `WatchlistContext` 与就绪的连接；回流期闭环归因消费 loader 透传的 `norm_code` / `signal_trade_date` / `target_trade_date`。

---

## 三、集合竞价轮询 auction_poller

> 本节及第四节属【执行侧（Windows VPS / QMT `xttrader`）建仓链路设计】。`auction_poller`、`entry_router`、`order_executor` 均运行在执行侧 Windows 进程内，不在信号侧 FastAPI 服务里实现；信号侧只通过 MySQL 提供 `limit_up_selected_stock` 计划单、并只读 `qmt_*` 回流表。
>
> **与既有文档的衔接（引用不重写）**：
> - QMT 落表 `qmt_trade` / `qmt_order` / `qmt_position_snapshot` / `qmt_account_daily` 的完整 DDL、字段语义、`order_remark` / `signal_trade_date` 设计、东八区 → UTC naive 时间口径、断线重连补采、本地下单台账对账，全部已在 **详见 `resources/doc/qmt-trade-review-design.md` 第 1 / 2 / 3 / 4 节** 定稿，本两节只做「建仓链路如何写入这些表、如何透传 `order_remark`」的衔接说明，不重复展开 DDL。
> - `limit_up_selected_stock` 的字段族（`role` / `strategy` / `market_state` / `tradable_flag` / `continuation_prob` / `next_day_premium_prob` / `reasonable_open_high_low/high` / `limit_up_price`）**详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 1 节**；本两节作为该表的【消费方】使用这些字段做路由与下单。
> - 复盘看板 / 数据回流职责 / 闭环归因 / `qmt_*` 四表，**详见上述三篇 qmt-trade-review-* 文档**，本文不重复。

### 3.1 模块定位与文件落点

`auction_poller` 是建仓链路的「眼睛」：在 A 股 9:15–9:25 集合竞价窗口内，持续采集计划买入标的的竞价行情，把原始 tick 加工成「竞价高开幅度 / 竞价量能（与首板爆量比例）/ 分时重心 / 虚拟封单」四个决策因子，喂给下游 `entry_router`（第四节）。它**只采集与计算，不做买 / 弃决策、不下单**。

- 运行位置：执行侧 Windows VPS（与 `xtdata` 行情、`xttrader` 下单同进程或同机姊妹进程），不进信号侧 FastAPI。
- 建议文件落点（执行侧仓库，与信号侧 `stock-ah-premium-ai` 物理解耦）：`execution/auction/auction_poller.py`、`execution/auction/auction_factors.py`（因子计算纯函数，便于单测）、`execution/auction/tick_source.py`（封装 `xtdata.get_full_tick` / 订阅）。
- 信号侧改动点：仅「产出 `limit_up_selected_stock` 候选」，无新增代码归本节；本节读该表（经只读 MySQL 连接或执行侧本地复制的当日计划单）。

### 3.2 为什么必须自写定时器（不能依赖 tick 回调）

`xtdata` 的实时 tick 推送（`subscribe_quote` + 回调）在集合竞价阶段**不保证按节奏触发回调**：竞价撮合不连续、9:15–9:25 不产生连续成交，推送回调可能稀疏、延迟、甚至整段不回调。建仓窗口对时延极敏感（9:20 后不可撤单、9:25 定盘），不能赌回调。

**口径：竞价窗口一律自写定时器主动轮询 `get_full_tick(codes)`，不依赖 tick 回调。** 这是与「盘中成交 / 委托用回调」（详见 `resources/doc/qmt-trade-review-design.md` 第 1.1 节）相反的设计选择，原因是竞价阶段回调不可靠、且窗口只有 10 分钟。

- 轮询周期：可配 1–3 秒（`AUCTION_POLL_INTERVAL_SECONDS`，默认 1.5s）。越接近 9:20 / 9:25 关键时点越密（可做「临近时点加密」策略，见 3.6）。
- `codes`：当日 `limit_up_selected_stock` 中 `target_trade_date = 今日` 且参与建仓的标的（口径见 3.5）。批量传入，一次 `get_full_tick` 取全部，避免逐股轮询放大请求数。

### 3.3 竞价窗口三段时序（强约束）

竞价规则决定了轮询行为与下游撤单约束，必须按段处理：

| 时段 | 撤单规则 | auction_poller 行为 | 下游 order_executor 约束 |
| --- | --- | --- | --- |
| 9:15–9:20 | **可撤单**（可挂可撤，虚拟开盘价会跳变） | 轮询采集，标 `phase=AUCTION_CANCELABLE`；此段高开 / 封单为「虚假繁荣」高发区，因子只观测不定盘 | 此段挂的竞价单**可撤**，order_executor 允许撤改 |
| 9:20–9:25 | **不可撤单**（只可挂、不可撤） | 轮询采集，标 `phase=AUCTION_LOCKED`；此段虚拟开盘价更接近真实开盘，是定盘前的关键观测段 | 此段一旦挂单**不可撤**，order_executor 在此段禁止发撤单（撤单必废） |
| 9:25 | 定盘，产生真实开盘价 | 取 9:25 后第一帧 tick 作为「竞价定盘结果」，封板因子 final 化，输出最终决策因子 | 9:25 定盘价即为竞价单成交价 / 开盘集合竞价结果 |
| 9:25–9:30 | 休市（无连续交易） | 轮询暂停或低频心跳 | 9:30 开盘后转连续竞价口径 |

> 关键边界：9:20–9:25 不可撤单意味着「在 9:20 前挂的竞价买单，9:20 后无法撤回」。因此凡「竞价强开追」类挂竞价单的策略，决策必须在 9:20 前完成；9:20 后只能「开盘后再追」，不能再赌竞价单。这条直接约束第四节 `entry_router` 的触发时点。

### 3.4 四个竞价决策因子（计算口径）

`auction_factors.py` 把每帧 `get_full_tick` 的原始字段加工成下列因子。`get_full_tick` 返回的可用字段（**以目标机实际 `xtdata` 版本实测为准**，落地前必须 `print(tick)` 确认键名，避免 `KeyError`）通常含：`lastPrice`（最新价 / 竞价虚拟成交价）、`open`、`lastClose`（昨收）、`volume` / `amount`（竞价累计撮合量 / 额）、`bidPrice` / `bidVol`（买档，竞价阶段一档即虚拟封单）、`askPrice` / `askVol`（卖档）。

1. **竞价高开幅度 `open_pct`**
   `open_pct = (auction_price − pre_close) / pre_close`，其中 `auction_price` 取当前帧虚拟成交价（`lastPrice`），`pre_close` 取 `lastClose`（昨收）。
   - 口径直接对接信号侧「竞价观察清单」阈值（**详见 `resources/doc/limit-up-push-investment-advice-refactor-plan.md` 第 3.2.2 节**：「竞价弱于 X% 放弃 / X%~Y% 低吸观察 / 高开超 Y% 警惕」）。`auction_poller` 只算 `open_pct` 数值，阈值比较在 `entry_router`。
   - 边界：`pre_close` 缺失（停牌复牌首日等）→ `open_pct=None`，标 `data_quality=NO_PRE_CLOSE`，下游降级（见 3.7）。

2. **竞价量能 / 与首板爆量比例 `auction_vol_ratio`**
   竞价撮合量能反映承接强度。口径：
   `auction_vol_ratio = 竞价累计撮合量(volume) / 该股首板当日成交量(first_board_vol)`。
   - `first_board_vol` 来源：信号侧 `limit_up_selected_stock` 应携带该股「首板放量基准」（若信号侧未落该字段，执行侧降级用近 N 日均量或首板日 `tencent_unadjusted_daily_quote.vol`，口径在落地文档固化）。
   - 业务意图：竞价就放出首板爆量的相当比例，说明承接资金活跃（强开）；竞价量能极小则高开多为「虚高」，开盘易回落。
   - 边界：基准量缺失 → 该因子 `None` + `data_quality=NO_BASE_VOL`，下游不据此加分。

3. **分时重心 `auction_centroid`**
   竞价 10 分钟内虚拟成交价的「价格重心」，衡量竞价是「越竞越强」还是「高开走低」：
   `auction_centroid = Σ(price_i × Δvolume_i) / Σ(Δvolume_i)`（成交量加权均价，`Δvolume_i` 为相邻两帧撮合增量），并附 `centroid_trend`（末帧价 vs 重心：末帧 > 重心 → 竞价上行 / 越竞越高；末帧 < 重心 → 高开回落）。
   - 业务意图：单看最后一帧高开会被「瞬时拉竞价」骗；重心 + 趋势能识别「9:15 拉到很高、9:20 后往下砸」的诱多型竞价。

4. **虚拟封单 `virtual_seal`**
   竞价阶段若虚拟成交价 = 涨停价（`limit_up_price`，来自信号侧），买一档挂单即「虚拟封单量」：
   `virtual_seal_amount = bidVol(买一) × bidPrice(买一=涨停价)`，并算 `seal_to_float_ratio = 虚拟封单额 / 流通市值`（流通市值来自信号侧或 `a_daily_basic`）。
   - 业务意图：一字 / 竞价即封涨停时，虚拟封单大小决定「能否排进队」。封单极大 → 大概率买不进（见第四节一字 / 秒封处理）；无虚拟封单（未顶涨停）→ 可正常竞价挂单。
   - 边界：未达涨停价时 `virtual_seal_amount=0`，正常；达涨停价但 `bidVol` 取不到 → 标 `data_quality=NO_SEAL_VOL`。

### 3.5 参与标的口径（codes 的来源）

`codes` = 当日 `limit_up_selected_stock` 满足下列条件的 `ts_code`：
- `target_trade_date = 今日`（T+1 买入日 = 今日，即 T 为上一交易日，经 `a_trade_calendar` 映射；映射口径详见 `resources/doc/qmt-trade-review-design.md` 第 2.5 节）。
- `tradable_flag` 标记为「可参与 / 重点」（口径与闭环归因「重点候选 N」一致，**详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 2.1 节**）。
- 只主板 + 创业板、排除 ST / 科创 / 北交（信号侧已过滤，执行侧再做一次防御性校验，前缀白名单口径见 §2.3 第 3 步）。

执行侧应在 9:14 前完成「读取当日计划单 → 解析 `codes` → 预热 `get_full_tick` 订阅」，避免 9:15 开窗才临时拉计划单。

### 3.6 状态机与伪逻辑

```text
状态枚举 AuctionPhase:
  PRE_AUCTION      # < 9:15，预热（拉计划单、订阅）
  AUCTION_CANCELABLE  # 9:15–9:20 可撤
  AUCTION_LOCKED   # 9:20–9:25 不可撤
  SETTLED          # >= 9:25 定盘
  CLOSED_WINDOW    # >= 9:30 或窗口结束

每只股票的因子聚合对象 AuctionSnapshot:
  ts_code, phase, ts(采集时刻 UTC naive),
  open_pct, auction_vol_ratio, auction_centroid, centroid_trend,
  virtual_seal_amount, seal_to_float_ratio,
  is_limit_up(是否顶涨停), data_quality(列表), tick_seq(已采帧数)

主循环（伪逻辑，非可运行代码）:
  codes = load_today_plan_codes()          # 3.5 口径
  prewarm_subscribe(codes)                 # 9:14 前
  history = {code: 累积帧序列}              # 算重心用增量
  while now < 9:30:
      phase = resolve_phase(now)           # 按 3.3 时段映射
      if phase in (PRE_AUCTION, CLOSED_WINDOW):
          sleep(poll_interval); continue
      ticks = get_full_tick(codes)         # 批量；失败→见 3.7 降级
      for code in codes:
          tick = ticks.get(code)
          snap = compute_factors(code, tick, history[code], phase)  # 3.4 四因子
          history[code].append(tick)
          push_to_router(snap)             # 交给 entry_router 观测，不在此下单
      interval = poll_interval
      if near_keypoint(now, [9:19:30, 9:24:30]):  # 临近 9:20/9:25 加密
          interval = min(interval, 1.0)
      sleep(interval)
  # 9:25 后取定盘帧，final 化封单/高开，输出最终 AuctionSnapshot(phase=SETTLED)
```

> `push_to_router` 是进程内回调 / 队列，把每帧 `AuctionSnapshot` 推给 `entry_router`；`entry_router` 自己持有「是否已对该股决策 / 下单」的状态，`auction_poller` 不关心。

### 3.7 竞价数据实测不可得 → 降级（务必落地实测验证）

集合竞价 tick 字段在不同 `xtdata` 版本、不同行情源、不同标的（如复牌首日、低流动性票）下，可能拿不到完整竞价量 / 买一档，甚至 `get_full_tick` 在竞价段只回最新价不回竞价撮合量。**这是已知现实风险**，必须有降级路径：

- **降级 A（部分因子缺失）**：某因子取不到 → 该因子 `None` + 在 `data_quality` 记原因，下游 `entry_router` 对缺失因子按「中性 / 不加分」处理，不臆造。
- **降级 B（竞价数据整体不可用）**：若整段竞价只能拿到 `last_price`（拿不到竞价量 / 封单 / 逐帧）→ 退化为「**只看 `last_price` 算高开 + 开盘后确认**」：
  - 竞价段只用 `open_pct ≈ (last_price − pre_close) / pre_close` 做粗判（弱开 / 平开 / 强开 / 涨停四档）。
  - 不在竞价段下竞价单，等 9:30 开盘后用开盘后 1–3 分钟的连续行情（实际开盘价、首分钟量能、是否回封）做「开盘后确认」，再交给 `entry_router` 决策。
  - 该降级与第四节「竞价强开追」在竞价不可得时自动退化为「开盘后追」是同一口径。

> **能力须真实交易日实测**：本节所有竞价因子的可得性、`get_full_tick` 在 9:15–9:25 的字段完整度、回调是否真不触发、量能基准是否能对齐「首板爆量」——**都必须在真实交易日的集合竞价窗口实盘实测确认**，不能仅凭文档或历史 tick 回放下结论。落地验收（3.10）以真实交易日盘前实测为准。

### 3.8 方法签名（执行侧，建议）

```python
# tick_source.py
def get_full_tick(codes: list[str]) -> dict[str, dict]:
    """批量取竞价/盘中全推 tick；返回 {ts_code: 原始 tick dict}。
    失败抛 TickSourceError，由 poller 主循环捕获走降级。"""

# auction_factors.py（纯函数，便于单测）
def compute_auction_factors(
    ts_code: str,
    tick: dict | None,
    prev_ticks: list[dict],     # 该股已采帧序列，算重心用
    phase: str,                 # AuctionPhase
    plan: PlanRow,              # 来自 limit_up_selected_stock 的该股计划（limit_up_price/first_board_vol/...）
    now_utc: datetime,
) -> AuctionSnapshot: ...

def open_pct(auction_price: Decimal, pre_close: Decimal | None) -> Decimal | None: ...
def auction_volume_ratio(cum_vol: int, first_board_vol: int | None) -> Decimal | None: ...
def auction_centroid(prev_ticks: list[dict], cur_tick: dict) -> tuple[Decimal | None, str]: ...
def virtual_seal(tick: dict, limit_up_price: Decimal, float_mktcap: Decimal | None) -> SealInfo: ...

# auction_poller.py
class AuctionPoller:
    def __init__(self, tick_source, plan_loader, router, settings): ...
    def resolve_phase(self, now_utc: datetime) -> str: ...        # 3.3 时段映射（按东八区判定）
    def run(self) -> None: ...                                    # 主循环（3.6）
```

> 时间口径：`now_utc` 用 `datetime.now(UTC).replace(tzinfo=None)`；时段判定先转东八区比较（9:15 / 9:20 / 9:25 是东八区钟点），`AuctionSnapshot.ts` 以 UTC naive 留痕，与 `resources/doc/qmt-trade-review-design.md` 第 4 节口径一致，禁止手工 ±8h。

### 3.9 单元测试要点

- **时段映射**：9:14:59 → PRE_AUCTION；9:15:00 → CANCELABLE；9:20:00 → LOCKED；9:25:00 → SETTLED；9:30:00 → CLOSED_WINDOW（边界精确到秒，按东八区）。
- **高开计算**：给定 `last_price / pre_close` 算 `open_pct`；`pre_close=None` 返回 `None` + `NO_PRE_CLOSE`。
- **量能比例**：`first_board_vol=None` → `auction_vol_ratio=None` + `NO_BASE_VOL`，不报错。
- **重心趋势**：构造「先高后低」帧序列 → `centroid_trend=DOWN`；「越竞越高」→ `UP`。
- **虚拟封单**：达涨停价且 `bidVol` 有值 → 计算封单额；未达涨停价 → 封单额 0；达涨停但无 `bidVol` → `NO_SEAL_VOL`。
- **整体降级 B**：`get_full_tick` 只回 `last_price`（无竞价量 / 封单）→ 仅 `open_pct` 有值、其余 `None`，`AuctionSnapshot` 仍可正常产出供「开盘后确认」。
- **回调不触发的兜底**：模拟 tick 回调零触发，验证定时器仍按周期产出帧（证明不依赖回调）。

### 3.10 验收标准

- 9:15–9:25 自写定时器按 1–3s 周期持续产出 `AuctionSnapshot`，**不依赖 tick 回调**；真实交易日实测确认回调确实不可靠而轮询稳定。
- 四因子在竞价数据可得时数值正确、可与信号侧阈值口径对接；任一因子不可得时按降级 A 标 `data_quality` 而非崩溃。
- 竞价数据整体不可得时按降级 B 退化为「只看 `last_price` + 开盘后确认」，不在竞价段乱下单。
- 9:20 / 9:25 关键时点的时段标记准确，供下游严格遵守「9:20 后不可撤」。
- 时间字段无 ±8h；所有竞价能力结论以真实交易日盘前实测为准并写入落地文档。

### 3.11 依赖顺序

依赖：信号侧 `limit_up_selected_stock` 当日计划单可读（详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 1 节字段） → 执行侧 `xtdata` 可用且 `get_full_tick` 实测通过 → `auction_poller` → 产出供第四节 `entry_router`。

---

## 四、建仓 entry_router 与下单 order_executor

### 4.1 两模块定位与边界

| 模块 | 职责 | 边界 |
| --- | --- | --- |
| `entry_router` | 按 `strategy_family + setup + action` 把每只计划股路由到一种建仓策略，结合 `auction_poller` 因子判定「如果…就买 / 弃」，产出**建仓决策（EntryDecision）** | **只出决策，不直接下单**；不碰 `xttrader`；决策可被复盘 / 回放 |
| `order_executor` | 把 `EntryDecision(BUY)` 翻译成 `xttrader` 下单调用，负责唯一业务单号、幂等防重、限价排队 / 撤单、成交回报确认、`order_remark` 透传、账户隔离、一字 / 秒封「买不进」处理 | **唯一**触碰 `xttrader.order_stock` / `cancel_order` 的地方；只认 `xttrader` 回报才算建仓成功 |

文件落点（执行侧）：`execution/entry/entry_router.py`、`execution/entry/strategies/*.py`（各策略一文件，便于单测与扩展）、`execution/order/order_executor.py`、`execution/order/local_ledger.py`（本地下单台账）。

> 数据流：`auction_poller` → `entry_router`（决策）→ `order_executor`（下单）→ `xttrader` 回报 → 写 `qmt_order` / `qmt_trade`（落表口径与回流职责详见 `resources/doc/qmt-trade-review-design.md` 第 1 / 2 / 3 节，本节只产生写入这两表的源事件，不重定义表）。

### 4.2 entry_router：路由维度与五类策略

路由键来自信号侧计划行（`limit_up_selected_stock`，字段族详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 1 节）：
- `strategy_family`：战法大类（如 打板 / 低吸 / 龙回头），对应 `limit_up_selected_stock.strategy` 的归类。
- `setup`：技术形态 / 位置（如 首板 / 连板接力 / 均线粘合 / 高位回踩），可由 `role` + 连板高度 + 信号侧形态字段推导。
- `action`：建仓动作类型，路由产出，取值见下。

按 `(strategy_family, setup)` 路由到五种 `action`，每种给「如果…就买 / 弃」触发条件。竞价阈值口径统一对接 `resources/doc/limit-up-push-investment-advice-refactor-plan.md` 第 3.2.2 节「竞价弱于 X% 放弃 / X%~Y% 低吸观察 / 高开超 Y% 警惕」，X / Y 由信号侧 `reasonable_open_high_low/high` 或配置给出，不在执行侧臆造：

1. **打板跟买 `CHASE_LIMIT_UP`**（强势连板 / 龙头，setup=连板接力）
   - **买**：盘中（或竞价即封）顶板且封单稳、`market_state` 非退潮 / 冰点、未触一字（一字另走 4.7）→ 挂涨停价排队跟买。
   - **弃**：开板回落 / 封单快速减小（炸板风险）/ `market_state ∈ {退潮, 冰点}` → 放弃。

2. **竞价强开追 `CHASE_AUCTION_STRONG`**（强预期票，setup=首板或连板，竞价定方向）
   - **买**：竞价 `open_pct` 落在「强开档」（≥ Y，对接 3.2.2 高开阈值）、`auction_vol_ratio` 达标（竞价放出首板爆量相当比例）、`centroid_trend=UP`（越竞越强）→ **9:20 前**挂竞价买单（9:20 后不可撤，故必须 9:20 前定）。
   - **弃**：竞价 `open_pct` 弱于 X%（弱开）或 `centroid_trend=DOWN`（高开走低 / 诱多）→ 放弃；竞价数据不可得 → 退化为「开盘后追」（4.6 降级）。

3. **均线低吸 `DIP_BUY_MA`**（低吸战法，setup=均线粘合 / 回踩支撑）
   - **买**：竞价 / 开盘 `open_pct` 落在「低吸档」（X%~Y%，对接 3.2.2 区间）、回踩不破关键均线、`market_state` 允许 → 挂限价低吸。
   - **弃**：高开超 Y%（追高风险，3.2.2「高开超 Y% 警惕」）或跌破支撑 → 放弃。

4. **龙回头 `LEADER_PULLBACK`**（前期龙头回调后再起，setup=高位回踩 / 分歧转一致）
   - **买**：龙头分歧日回踩后出现承接（量价配合）、`leader_strength_score` 高、`continuation_prob` 不弱 → 限价分批吸。
   - **弃**：回踩无承接、放量下跌、龙头地位被新龙头取代 → 放弃。

5. **放弃 `SKIP`**（任何 setup 命中放弃条件）
   - 触发：`market_state ∈ {退潮, 冰点}` 且策略要求收手（对接信号侧「退潮 / 冰点不得激进接力」口径，详见 `resources/doc/limit-up-push-investment-advice-refactor-plan.md` 第 3.2.2 节）；或竞价 / 开盘因子全面走弱；或 `tradable_flag` 标记不可参与。
   - 产出 `EntryDecision(action=SKIP, reason=...)`，**不下单**，但**仍落决策留痕**（供闭环归因「未下单 N−M」口径，详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 2.1 节）。

> `entry_router` 对每只股票在建仓窗口内**只产出一次有效 BUY 决策**（幂等由其内部「已决策」状态保证）；因子持续更新时，若已 BUY 则不再重复路由（撤改归 order_executor）。

### 4.3 EntryDecision 数据结构（契约）

```python
@dataclass(frozen=True)
class EntryDecision:
    ts_code: str
    signal_trade_date: date     # T，来自计划行（透传给 order_remark / qmt_*.signal_trade_date）
    target_trade_date: date     # T+1 买入日 = 今日
    strategy_family: str        # 打板/低吸/龙回头...
    setup: str                  # 连板接力/首板/均线粘合...
    action: str                 # CHASE_LIMIT_UP / CHASE_AUCTION_STRONG / DIP_BUY_MA / LEADER_PULLBACK / SKIP
    side: str = "BUY"           # 建仓只有 BUY；卖出不在本链路
    limit_price: Decimal | None # 计划限价（涨停价/低吸价/竞价价）；SKIP 为 None
    plan_volume: int | None     # 计划买入股数（按仓位/资金口径，见 4.4 账户隔离）
    order_phase: str            # AUCTION(竞价单) / OPENING(开盘后) —— 决定可撤性
    decided_at: datetime        # UTC naive
    reason: str                 # 买/弃理由（含命中的因子值，供复盘）
    factors_snapshot: dict      # 决策时的 AuctionSnapshot 关键因子留痕
```

`reason` / `factors_snapshot` 是闭环归因「为什么买 / 弃」的事实源，建议 `entry_router` 把 SKIP 与 BUY 决策都落一行执行侧本地决策台账（与本地下单台账可同表或姊妹表），供事后对照 `qmt_order` / `qmt_trade`。

### 4.4 order_executor：下单封装与七项职责

`order_executor` 是唯一调用 `xttrader.order_stock` / `cancel_order` 的地方，承接 `EntryDecision(BUY)`：

#### (1) 唯一业务单号 `biz_order_no`
格式：`日期 + ts_code + 战法 + 序号`，例：`20260613_300750.SZ_CHASE_LIMIT_UP_001`。
- 生成时机：决定下单前生成，写本地下单台账。
- 作用：①幂等防重的去重键；②写入 `order_remark` 透传到 `qmt_order` / `qmt_trade`，供回流对账（`order_remark` 字段语义详见 `resources/doc/qmt-trade-review-design.md` 第 2.1 / 2.2 节）。

#### (2) 幂等防重（下单前查本地台账）
**下单前先查本地下单台账**：同一 `(target_trade_date, ts_code, strategy_family)` 已有「未终结（已报 / 部成 / 已成）」单 → **不重复下单**，直接返回既有 `biz_order_no`。
- 防重场景：`entry_router` 重复推 BUY、进程重启后重放、断线重连后补发。
- 口径与 `qmt_order` 的唯一键互补：DB 级唯一键防「QMT 侧同一委托重复落表」，`biz_order_no` 防「业务侧同一计划重复下单」。两层都要有。`qmt_order` / `qmt_trade` 唯一键的跨日 ID 复用加固（纳入 `trade_date`）见第六节 §6.5。

#### (3) 限价排队 / 撤单（最长存活时限）
- **限价排队**：建仓一律限价单（`price_type=限价`），打板挂涨停价排队、低吸挂目标价；不挂市价（避免滑点失控）。
- **最长存活时限 `order_ttl`**：每单设存活上限（如竞价单到 9:25 定盘后 / 开盘单 N 分钟）；超时未成 → 撤单（受 4.5 撤单时段约束）→ 按 4.7 转次优或放弃。
- **撤单约束**：9:20–9:25 不可撤（竞价定盘前），`order_executor` 在该段禁发 `cancel_order`（详见第三节 3.3）；其余时段可撤。

#### (4) 成交回报确认（只认 xttrader 回报才算建仓）
- **建仓成立的唯一标准 = 收到 `xttrader` 的 `on_stock_trade`（XtTrade）回报且 `traded_volume>0`**；本地「已发出 `order_stock`」不等于建仓成功。
- 下单后状态机：`SUBMITTED`（已发 order_stock）→ `REPORTED`（on_stock_order 已报）→ `PART_TRADED` / `TRADED`（on_stock_trade 回报累计）→ 终态 `TRADED` / `CANCELLED` / `REJECTED` / `ERROR`。
- 成交回报落 `qmt_trade`、委托状态落 `qmt_order`（写入路径与幂等口径详见 `resources/doc/qmt-trade-review-design.md` 第 1.1 / 2 节）；`order_executor` 不自己算持仓盈亏（归复盘侧）。
- `on_order_error`（下单失败）→ 该 `biz_order_no` 标 ERROR，进 4.7 处理；绝不「静默当成功」。

#### (5) order_remark 携带「信号日 T + 短选股 id」
- `order_remark` 写入：`{signal_trade_date(T)}|{selected_stock_id 或 biz_order_no}`，例 `20260612|lus_88231`（落地推荐统一格式 `LUP|<signal_trade_date T>|<ts_code>`，与第六节 §6.8 回填解析口径一致）。
- 作用：QMT 回报落 `qmt_order.order_remark` / `qmt_trade.order_remark` 后，回流侧据此回填 `signal_trade_date`，无需再经 `a_trade_calendar` 反推（口径详见 `resources/doc/qmt-trade-review-design.md` 第 2.5 节）。
- 边界：`order_remark` 长度受 QMT 限制（≤255，对齐 `qmt_order.order_remark` VARCHAR(255)），超长截断且优先保 `signal_trade_date` 段。

#### (6) 账户隔离
- 多账户场景：`order_executor` 持有 `account_id`，每单绑定账户；`plan_volume` 按该账户可用资金 / 仓位上限算（资金读 `xttrader.query_stock_asset` 当日 `cash`，口径详见 `resources/doc/qmt-trade-review-board-design.md` 第 1 节）。
- 不同账户的下单 / 台账 / `qmt_*` 写入按 `account_id` 物理隔离，幂等键与唯一键均含 `account_id`（`qmt_*` 唯一键含 `account_id` 详见 `resources/doc/qmt-trade-review-design.md` 第 2 节）。

#### (7) 一字 / 秒封「买不进」现实处理
A 股一字板 / 秒封涨停时，散户挂单大概率排不到，「下了单 ≠ 买得到」。处理口径（对接闭环归因「买不进」分类，详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 2.1 节）：
- **一字板**（开盘 `low==high==涨停价`，竞价即虚拟封死）：仍**挂涨停价排队**（不放弃排队，万一开板能成），但预期成交率低；设较短 `order_ttl`，超时未成 → **撤单** → 标 `miss_reason=一字未成` → 转次优标的或放弃（不追高破板买）。
- **秒封**（开盘瞬间封涨停、封单巨大）：挂涨停价排队，`virtual_seal / seal_to_float_ratio`（第三节因子）过大 → 标「大概率买不进」，超时未成同样撤单 → 转次优 / 放弃。
- **转次优**：`entry_router` 决策时可携带「次优候选序列」，主标的买不进则按序列尝试次优（仍走完整幂等 / 限价 / 确认流程）。
- **不计收益口径**：买不进的标的不产生成交，复盘按「未成交机会成本」单列、不计真实收益（与回测「一字 / 秒封买不进不计收益」一致，详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 2.1 节）。

### 4.5 order_executor 下单状态机（伪逻辑）

```text
状态 OrderState:
  PLANNED        # 已生成 biz_order_no、写本地台账，未发 order_stock
  SUBMITTED      # 已调 order_stock，待回报
  REPORTED       # on_stock_order=已报
  PART_TRADED    # on_stock_trade 累计 0<traded<plan
  TRADED         # 全部成交（建仓成功）
  CANCELLING     # 已发 cancel_order（仅可撤时段）
  CANCELLED      # 已撤（含超时撤、买不进撤）
  REJECTED       # 废单
  ERROR          # on_order_error / 下单异常

place(decision: EntryDecision):
  if decision.action == SKIP: 落决策台账; return        # 不下单
  biz_no = build_biz_order_no(decision)                 # (1)
  if ledger.has_active(decision.target_trade_date, decision.ts_code, decision.strategy_family):
      return existing_biz_no                            # (2) 幂等防重
  ledger.insert(biz_no, state=PLANNED, account_id, plan, remark=build_remark(decision))  # (5)(6)
  if not within_cancelable_or_allowed_phase(now):       # (3) 9:20–9:25 不可撤段的下单按"不可撤"标记
      mark_non_cancelable(biz_no)
  order_id = xttrader.order_stock(account, ts_code, BUY, plan_volume, 限价, limit_price,
                                  strategy_name, order_remark=remark)   # 唯一下单点
  ledger.update(biz_no, order_id, state=SUBMITTED)
  start_ttl_timer(biz_no, order_ttl)                    # (3) 超时撤

回调侧（与回流共用，详见 qmt-trade-review-design.md 第1节）:
  on_stock_order(o):   ledger.sync_status(o.order_id -> REPORTED/CANCELLED/REJECTED); upsert qmt_order
  on_stock_trade(t):   ledger.add_fill(t); 若累计=plan -> TRADED 否则 PART_TRADED; upsert qmt_trade  # (4) 只认回报
  on_order_error(e):   ledger.mark(order_id, ERROR, e.msg); -> 进 4.7 转次优/放弃
  on_cancel_error(e):  ledger.mark_cancel_failed(order_id)

ttl 到期(biz_no):
  if state in (REPORTED, PART_TRADED) and 可撤时段:
      xttrader.cancel_order(order_id) -> CANCELLING
      # 撤成后若一字/秒封未成 -> miss_reason 标记 -> 尝试次优(4.7) 或放弃
  elif 9:20–9:25 不可撤段:
      不撤，等定盘后处理
```

### 4.6 竞价不可得时的降级（与第三节联动）
- `CHASE_AUCTION_STRONG` 依赖竞价因子；当 `auction_poller` 走降级 B（竞价数据整体不可得，第三节 3.7）→ `entry_router` 把该 action 自动改判为「开盘后追」：`order_phase=OPENING`，等 9:30 后用开盘行情确认再 `place`，**不在竞价段下竞价单**。
- 这样竞价能力实测不可得时链路仍可运行（退化为开盘后建仓），不至于整条链路阻塞。

### 4.7 边界与异常 / 幂等口径汇总
- **幂等**：`biz_order_no` 业务级去重 + `qmt_*` 唯一键 DB 级去重；下单前查台账、回调 upsert，重启 / 重连 / 重放均不重复下单、不重复落表。
- **断线重连**：进程断线后按 `resources/doc/qmt-trade-review-design.md` 第 3.2 节补采当日 `query_*` 对齐 `qmt_order` / `qmt_trade`；本地台账与回报对账（第 3.3 节）定位漏单。
- **下单失败**（on_order_error）：必落 ERROR、不静默；可转次优。
- **部分成交**：`PART_TRADED` 是合法终态之一（TTL 到撤掉未成部分）；已成部分按真实成交计，未成部分计买不进。
- **不可撤段撤单**：9:20–9:25 禁发 `cancel_order`，误发会 `on_cancel_error`，代码层先校验时段再撤。
- **账户隔离**：所有键含 `account_id`，资金 / 仓位按账户独立。

### 4.8 方法签名（建议）
```python
# entry_router.py
class EntryRouter:
    def on_auction_snapshot(self, snap: AuctionSnapshot, plan: PlanRow) -> EntryDecision | None: ...
    def route(self, plan: PlanRow, snap: AuctionSnapshot) -> EntryDecision: ...   # (strategy_family,setup)->action + 买/弃
    def _should_skip(self, plan: PlanRow, snap: AuctionSnapshot) -> tuple[bool, str]: ...  # market_state/tradable_flag 闸门

# order_executor.py
class OrderExecutor:
    def __init__(self, xttrader, account_id, ledger: LocalLedger, settings): ...
    def place(self, decision: EntryDecision) -> str | None: ...      # 返回 biz_order_no；SKIP 返回 None
    def build_biz_order_no(self, decision: EntryDecision) -> str: ...
    def build_order_remark(self, decision: EntryDecision) -> str: ... # signal_trade_date|selected_id
    def on_ttl_expired(self, biz_no: str) -> None: ...               # 超时撤+转次优/放弃
    def try_next_best(self, decision: EntryDecision) -> str | None: ...

# local_ledger.py
class LocalLedger:
    def has_active(self, target_trade_date, ts_code, strategy_family) -> bool: ...  # 幂等
    def insert(self, ...): ...
    def sync_status(self, order_id, state, msg=None): ...
    def add_fill(self, xt_trade): ...
```

### 4.9 单元测试要点（幂等 / 撤单 / 部分成交）
- **幂等**：同一 `EntryDecision` 连续 `place` 两次 → 只调一次 `xttrader.order_stock`，第二次返回既有 `biz_order_no`；进程重启后台账已有 active 单 → 不重下。
- **路由**：`market_state=退潮` → 任意 setup 路由出 `SKIP`、不下单但落决策台账；竞价强开命中 → `CHASE_AUCTION_STRONG` 且 `order_phase=AUCTION`、9:20 前下单；竞价不可得 → 自动改 `OPENING`。
- **撤单（含不可撤段）**：可撤段 TTL 到期 → 发 `cancel_order`；9:20–9:25 段 TTL 到期 → **不发**撤单（断言未调用 `cancel_order`）；`on_cancel_error` → 标 `cancel_failed`。
- **部分成交**：`plan_volume=1000`，回报累计 600 后 TTL 到 → 撤未成 400 → 终态 `PART_TRADED`，已成 600 计成交、400 计买不进。
- **成交确认口径**：只发了 `order_stock` 但无 `on_stock_trade` 回报 → 状态停在 `SUBMITTED / REPORTED`，**不计建仓成功**。
- **一字 / 秒封**：模拟一字（封单巨大、超时未成）→ 挂涨停价排队 → TTL 到撤 → `miss_reason=一字未成` → 尝试次优或放弃。
- **order_remark / signal_trade_date**：透传字符串含正确 T 日；超长截断保 `signal_trade_date` 段。
- **账户隔离**：两账户同股同战法各自独立下单、幂等键互不串。
- **下单失败**：`on_order_error` → ERROR 落账，不静默、可转次优。

### 4.10 验收标准
- `entry_router` 按 `(strategy_family, setup)` 正确路由到五类 action，每类买 / 弃触发条件与信号侧竞价口径（3.2.2）一致；**只出决策、不下单**；SKIP 也留痕。
- `order_executor` 是唯一下单点；`biz_order_no = 日期 + ts_code + 战法 + 序号` 唯一且写入 `order_remark`；下单前查台账幂等、重启 / 重连不重下。
- 限价排队 + TTL 撤单生效，且 9:20–9:25 不发撤单；只认 `xttrader` 回报才算建仓成功；`on_order_error` 不静默。
- `order_remark` 携带 `signal_trade_date(T) + 短选股 id`，回流可据此回填（口径对齐 `resources/doc/qmt-trade-review-design.md` 第 2.5 节）；账户隔离生效。
- 一字 / 秒封「买不进」按「挂涨停价排队 + 超时撤单 + 转次优 / 放弃 + 不计收益」处理，与回测口径一致。
- 时间字段无 ±8h；成交 / 委托落 `qmt_trade` / `qmt_order` 走既定幂等 upsert（不重定义表）。

### 4.11 依赖顺序
1. `qmt_*` 四表与回流落表口径就绪（**详见 `resources/doc/qmt-trade-review-design.md` 第 1 / 2 节**，前置）。
2. 信号侧 `limit_up_selected_stock` 当日计划单可读（含 `strategy / role / market_state / tradable_flag / limit_up_price / reasonable_open_high_low/high`，**详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 1 节**）。
3. 第三节 `auction_poller` 产出 `AuctionSnapshot`（竞价因子）。
4. `entry_router`（决策）→ `order_executor`（下单 + 本地台账）→ `xttrader` 回报 → 落 `qmt_*` → 供复盘看板 / 闭环归因消费（**详见三篇 qmt-trade-review-* 文档**）。

> 全链路能力（竞价因子可得性、回调可靠性、一字 / 秒封实际成交率、`order_remark` 长度限制）**均须在真实交易日实盘实测确认**后方可投产，落地验收以真实交易日盘前 / 盘中实测为准。

---

## 五、持仓管理 position_manager 与卖出/连板决策、风控 risk

> 本节定义执行侧（Windows VPS / QMT `xttrader`）持仓管理与卖出决策的状态机与口径。它消费**信号侧每日刷新的 `limit_up_selected_stock`**（先验：`continuation_prob` / `boost` / `fail_conditions` 等，DDL / ORM / 迁移见信号侧 watchlist 契约产出设计），结合 **可卖日实时盘口**做秒级卖出决策。
>
> **注（口径核验）**：本节 `position_manager` / `sell_decider` / `risk` 三件套为本文档新增的执行侧设计模块——它们在当前信号侧代码库中尚不存在（grep `position_manager` / `PositionManager` 零命中），属待落地的执行侧 Windows 进程模块，不在信号侧 `backend/` 内实现。
>
> 与 QMT 回流 / 复盘 / 闭环归因的衔接：本节只负责**做出并执行卖出动作**；卖出后的成交回报、持仓快照、收益归因如何回流、如何复盘、如何归因，**已在三篇 QMT 文档定稿，本节只引用不重述**——成交 / 委托 / 持仓 / 资产四表 DDL 与回流双通道详见 `resources/doc/qmt-trade-review-design.md` 第 1 节、第 2 节；FIFO 已实现盈亏、持有天数、滑点口径详见 `resources/doc/qmt-trade-review-board-design.md` 第 4.5 节、第 4.6 节；信号 → 执行 → 结果闭环与先验校准详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 1 节、第 3 节。

### 5.1 范围、不可推翻的前提与名词澄清

**模块定位**：`position_manager`（持仓状态机）+ `sell_decider`（卖出 / 连板决策）+ `risk`（风控闸门）三件套，运行在执行侧 QMT 进程内，常驻 `run_forever()`。它**只做卖出方向的决策与下单触发**；买入由信号侧 `limit_up_selected_stock` + 执行侧建仓逻辑（第三、四节）负责，不在本节。

**不可推翻的既定前提（与项目约束一致）**：

- A 股 T+1：当日买入当日不可卖，最早次一交易日卖。
- 只做主板 + 创业板、排除 ST / 科创 / 北交；主板 ±10%、创业板 ±20%；不复权。
- 解耦架构：信号侧 Linux（只读 QMT 落库数据），执行侧 Windows QMT（只写 `qmt_*` 表 + 持仓 / 下单）。本节属执行侧，**实时盘口在执行侧 `xtdata` 手里**——信号侧 `realtime_quote_snapshot` 表只回流 `last_price` 一个价格字段（`DbRealtimeQuoteProvider.get_a_quote(ts_code)` 仅返回 `RealtimeQuote.last_price`，**无买一卖一 / 封单量 / 分时量能**，见 `backend/app/services/realtime_market_service.py:35`、`:70`）。**故所有依赖盘口深度（封单额、量价匹配、炸板瞬时挂单）的判断必须在执行侧用 `xtdata` 原生盘口取数，不得依赖信号侧快照表。**

**关键名词澄清（务必统一，杜绝 T 偏移歧义）**：

| 表述 | 含义 | 等价关系 |
| --- | --- | --- |
| 涨停日 / 信号日 | 标的涨停、信号侧生成 `limit_up_selected_stock` 的那一天 | = `trade_date` = **T** |
| 买入日 B | 实际下单买入的那一天 | = `target_trade_date` = **T+1**（信号日的下一交易日，经 `a_trade_calendar` 映射） |
| 「第二天卖」 | 用户口语「买完第二天卖」 | = **买入日 B 的次一交易日** = `trade_cal_next(B)` |

> **澄清结论（口径以买入日 B 为锚，经核验修正）**：打板隔日卖的系统口径是「**卖出日 = 买入日 B（= `target_trade_date` = T+1）经 `a_trade_calendar` 取的 `trade_cal_next(B)` 次一交易日**」，**以买入日为锚、按实际持有交易日数计**，而非「从涨停日 T 机械数到 T+2」。当典型持有为 1 个交易日时，`trade_cal_next(T+1)` 数值上恰为 T+2，这只是「典型持有 1 个交易日」的副产物；若实盘实际持有 >1 个交易日（连板续持），卖出日就不再是 T+2。与 `qmt-trade-review-board-design.md` 第 4.5 节「持有天数按 `a_trade_calendar` 交易日计，打板隔日卖典型为 1」、`qmt-trade-review-closed-loop-attribution-design.md`「B → `trade_cal_next(B)` 隔日卖」一致。
>
> **守 T+1 的准确表述**：买入当日（即 B = T+1 本身）就是「当日不可卖」那天，最早 `trade_cal_next(B)` 才可卖（`can_use_volume` 不含当日买入）。即不可卖的是「买入当日」本身，而非在 B 之外再守一道闸。下文「次日」一律指**买入日 B 的次一交易日 `trade_cal_next(B)`**，即首个可卖日；**所有日推算一律经 `a_trade_calendar`，禁止自然日 +1**。
>
> **价位口径澄清**：本节决策只产出「现在该不该卖、卖多少」的**决策动作**与触发条件（如「竞价弱于阈值 → 减 / 清」）；**具体止盈价、止损价、挂单价由 QMT 执行侧自定**（结合实时盘口、滑点控制），**不来自信号契约**。`limit_up_selected_stock` 的 `continuation_prob` / `boost` / `fail_conditions` 是**方向性先验**（续板概率、加分项、失败条件），不是价位指令。信号侧给的 `reasonable_open_high_*`（合理高开区间）是买入参考，**不作为卖出价位来源**。

### 5.2 持仓状态机 position_manager

#### 5.2.1 状态定义

每笔持仓（按 `account_id + ts_code` 聚合为一个持仓单元，多笔买入按 FIFO 合并成本，FIFO 口径见 `qmt-trade-review-board-design.md` 第 4.5 节）维护一个状态：

| 状态 | 含义 | 进入条件 | 可卖性 |
| --- | --- | --- | --- |
| `LOCKED_T1` | 当日买入锁定（守 T+1） | 买入成交回报落地、且 `当前交易日 == 买入日 B` | **不可卖**（`can_use_volume` 不含当日买入） |
| `HOLDING` | 正常持有、等待卖出决策 | 跨过买入日（`当前交易日 >= trade_cal_next(B)`），即进入首个可卖日及以后 | 可卖（仅昨日及更早持仓） |
| `SELLING` | 已发出卖出委托、等待回报 | 卖出决策触发并 `order_stock` 报单成功 | 委托在途 |
| `SOLD` | 全部卖出成交 | `qmt_trade` 卖出累计成交量 == 持仓量 | 仓位归零、单元关闭 |
| `PART_SOLD` | 部分卖出（减仓） | 卖出成交量 < 持仓量 | 剩余部分回 `HOLDING` |
| `FROZEN` | 风控冻结态 | 行情中断 / 下单通道中断 / 风控阈值击穿（见 5.4） | 暂停一切新卖出决策，只允许人工 / 恢复后处理 |

#### 5.2.2 「最早可卖日」标记（守 T+1 的硬口径）

每笔买入成交落地时，position_manager 给该持仓单元打 **`earliest_sellable_date`（最早可卖日）= `trade_cal_next(买入日 B)`**，经 `a_trade_calendar` 映射（`is_open=1` 的下一日，**禁止 `买入日 + 1` 自然日加减**，与全项目交易日历口径一致）。

- 买入当日（B 当日）：`当前交易日 < earliest_sellable_date` → 状态 `LOCKED_T1`，**卖出决策直接短路返回「不可卖」**，连决策都不进入。这是守 T+1 的第一道闸，与 QMT 原生 `can_use_volume`（T+1 可卖部分不含当日买入，见 `qmt-trade-review-design.md` 第 1 节）**双重保险**：以 `earliest_sellable_date` 为业务闸、以 `can_use_volume` 为下单量上限。
- **卖出只对「昨日及更早持仓」生效**：决策与下单的标的集合恒为 `{持仓单元 | 当前交易日 >= earliest_sellable_date}`，可卖数量上限恒取 `min(决策卖出量, can_use_volume)`。当日买入部分即使风控想清仓也卖不掉（T+1 物理约束），只能次日处理。

#### 5.2.3 状态机图

```
            买入成交回报落地
                  │
                  ▼
          ┌───────────────┐   当前交易日 < earliest_sellable_date
          │  LOCKED_T1     │◀── （守 T+1：买入当日 B 不可卖，
          └───────┬───────┘       can_use_volume 不含当日买入）
                  │ 跨过买入日（当前交易日 >= earliest_sellable_date = trade_cal_next(B)）
                  ▼
          ┌───────────────┐
   ┌─────▶│   HOLDING      │── 风控阈值击穿 / 行情中断 / 下单中断 ──▶ ┌──────────┐
   │      └───────┬───────┘                                          │  FROZEN  │
   │              │ sell_decider 触发卖出（5.3）                      └────┬─────┘
   │              ▼                                                       │ 恢复+人工确认
   │      ┌───────────────┐                                              │
   │      │   SELLING      │◀─────────────────────────────────────────────┘
   │      └───┬───────┬───┘
   │  部成    │       │ 全成
   │  ┌───────▼──┐   ▼
   └──┤PART_SOLD │  ┌──────┐
      └──────────┘  │ SOLD │ （仓位归零，单元关闭）
       剩余回 HOLDING └──────┘

   并行刷新通道（不改可卖性，只刷先验）：
   每日 limit_up_selected_stock 刷新后：
     · 持仓股次日"又涨停"→ 在新报告中重新刷新该股先验（continuation_prob/boost/fail_conditions），续持决策继续吃信号侧先验
     · 持仓股次日"没涨停"→ 该股退出信号池，position_manager 转"纯技术退出"模式（不再等先验，按实时盘口走技术止盈/止损）
```

#### 5.2.4 持仓股「次日是否又涨停」的先验衔接（关键分叉）

打板隔日卖不是「机械到点必卖」，而是**根据次日表现决定续持还是退出**：

- **次日又涨停（续板成功）**：该股会重新出现在**当日刷新的 `limit_up_selected_stock`** 里（信号侧每日重算涨停池与连板天梯，见 `limit-up-multi-stage-analysis-refactor-plan.md` 多阶段 pipeline）。position_manager 据 `ts_code` 重新挂接当日先验行，`continuation_prob` / `boost` / `fail_conditions` 全部刷新为最新一档，续持决策**继续吃信号侧先验**（即「连板续持」走信号驱动）。
- **次日没涨停（断板 / 弱转）**：该股不再进入当日 `limit_up_selected_stock` 信号池。position_manager 把该单元切到 **`纯技术退出`** 模式——**不再等待先验**，卖出决策完全由实时盘口技术形态驱动（走弱破位止损、冲高不及预期了结，见 5.3）。
- 先验挂接由代码归一键 `norm_code`（`stock_identity_resolver.resolve_by_code` 归一 `ts_code`）+ 当前交易日完成；信号侧无该股时返回空先验，自动落入纯技术退出分支。

### 5.3 卖出/连板决策 sell_decider

#### 5.3.1 决策输入：信号侧先验 + 可卖日实时盘口（两路融合）

| 输入路 | 来源 | 字段（举例） | 角色 |
| --- | --- | --- | --- |
| 信号侧先验 | 当日刷新的 `limit_up_selected_stock`（DDL 见信号侧 watchlist 契约产出设计） | `continuation_prob`（续板概率先验）、`boost`（加分项：龙头 / 题材卡位 / 封板质量等正向因子）、`fail_conditions`（结构化失败条件，如「竞价弱于 X% / 炸板未回封 / 题材退潮」）、`market_state`（情绪周期）、`role`、`strategy` | **方向性先验**：决定「倾向续持还是倾向了结」的基调与触发阈值 |
| 可卖日实时盘口 | 执行侧 `xtdata` 原生盘口（**不是信号侧 `last_price` 快照**） | 竞价高开幅度、封单额 / 封流比、开板次数、分时量价、炸板瞬时挂单 | **实时定夺**：在先验基调上做秒级触发 |

> 融合原则：**先验定基调、盘口定扳机**。先验说「`continuation_prob` 高、`boost` 多」→ 倾向续持，但若盘口出现 `fail_conditions` 命中（背离 / 炸板 / 走弱）→ 实时盘口一票否决，转减 / 清。先验说「弱、`fail_conditions` 易触发」→ 倾向了结，盘口只要不超预期就按计划出。

#### 5.3.2 次日竞价定夺（开盘集合竞价阶段，可卖日 9:15–9:25）

竞价是隔日卖的第一决策点。口径与 `limit-up-push-investment-advice-refactor-plan.md` 第 3.2.2 节「竞价观察清单」对齐（竞价弱于 X% 放弃 / X%~Y% 低吸观察 / 高开超 Y% 警惕），**此处用于卖出侧**：

| 竞价情形（盘口） | 先验校验 | 决策动作 |
| --- | --- | --- |
| 高开强 **且** 量价匹配（高开幅度落在合理区间、竞价量能放大但不背离） | `continuation_prob` 高、未命中 `fail_conditions` | **续持**，进入分时定夺（5.3.3） |
| 高开但量价背离（高开幅度大、竞价量能虚高或骤缩，封板预期弱） | 命中 `fail_conditions`（背离类） | **竞价或开盘减 / 清**（在竞价阶段挂单或开盘后立即出） |
| 平开 / 低开走弱 | 命中 `fail_conditions`（弱开类） | **开盘减 / 清**，不恋战 |

#### 5.3.3 分时定夺（盘中连续竞价，可卖日 9:30 起）

竞价判为「续持」或竞价未触发清仓的标的，进入分时实时盯盘决策：

| 分时盘口形态 | 决策动作 | 价位归属 |
| --- | --- | --- |
| 秒板 / 快速封板（盘口封单稳、封流比高） | **续持**（连板续持，吃 `continuation_prob` 先验） | 不卖 |
| 冲高（涨幅显著、量能透支或上方筹码压力） | **冲高止盈**（减 / 清） | 止盈价由 QMT 自定 |
| 走弱破位（跌破分时关键支撑 / 均价线、量能放大下杀） | **走弱破位止损** | 止损价由 QMT 自定 |
| 炸板（已封涨停后开板、封单瓦解） | **炸板出**（开板即出，不赌回封） | 出价由 QMT 自定 |
| 烂板（反复开板封板、封不实、换手剧增） | **烂板出**（质量差，宁可出） | 出价由 QMT 自定 |
| 冲高不及预期（高开后冲高乏力、全天弱于先验设定的续板预期） | **尾盘了结**（不强行隔夜） | 出价由 QMT 自定 |

> 所有止盈 / 止损 / 挂单**价位由 QMT 执行侧结合实时盘口与滑点控制自定**，本节只产出「该动作 + 触发条件」；`fail_conditions` 提供「什么情况作废续持」的结构化判据，具体阈值（如「跌破均价线 X%」「炸板封单减少 Y%」）在执行侧按战法定参并固化到执行侧文档，与 `qmt-trade-review-closed-loop-attribution-design.md` 第 9 节「买不进 / 炸板判定阈值需按战法定义并写入文档」口径一致。

#### 5.3.4 决策表（如果…就…，汇总）

```
IF 当前交易日 < earliest_sellable_date            THEN 不可卖（守 T+1，短路返回）           [LOCKED_T1]
IF risk 处于 FROZEN（行情/下单中断/阈值击穿）       THEN 暂停新卖出决策，等恢复 + 人工确认       [安全默认：不确定宁可不交易]
IF market_state == 空仓                            THEN 只守仓不新开，存量持仓仍按下列规则卖出   [空仓闸门联动 5.4]

# —— 持仓股次日先验分叉 ——
IF 次日又涨停 且 命中当日 limit_up_selected_stock   THEN 刷新先验，按"续持基调"进入竞价/分时决策
IF 次日没涨停（退出信号池）                          THEN 转纯技术退出（无先验，仅盘口驱动）

# —— 次日竞价定夺（可卖日 9:15–9:25） ——
IF 高开强 且 量价匹配 且 continuation_prob 高 且 未命中 fail_conditions  THEN 续持 → 进分时
IF 高开 且 量价背离（命中 fail_conditions 背离类）                       THEN 竞价/开盘 减或清
IF 平开/低开走弱（命中 fail_conditions 弱开类）                          THEN 开盘 减或清

# —— 分时定夺（可卖日 9:30 起） ——
IF 秒板/稳封（封流比高）                            THEN 续持（连板续持）
IF 冲高 且 量能透支/上方压力                         THEN 冲高止盈（减/清）
IF 走弱破位（破支撑+放量下杀）                       THEN 走弱破位止损
IF 炸板（封单瓦解、开板）                            THEN 炸板出
IF 烂板（反复开板、封不实）                          THEN 烂板出
IF 冲高不及预期（全天弱于续板预期）                   THEN 尾盘了结
```

### 5.4 风控 risk

#### 5.4.1 风控层级与冻结态

| 层级 | 阈值 / 触发 | 动作 |
| --- | --- | --- |
| **账户级** | 当日账户回撤超阈值、当日已实现亏损超阈值、可用资金异常 | 全账户进入 `FROZEN`，暂停所有新卖出 / 买入决策，告警 |
| **单票级** | 单票浮亏超阈值、单票连续触发异常下单 | 该票进入 `FROZEN`，仅人工恢复后处理 |
| **行情中断** | `xtdata` 行情断流 / 盘口取数失败超时 | 进入 `FROZEN`：**没有可信盘口就不做实时卖出决策**（安全默认） |
| **下单中断** | `xttrader` 断线（`on_disconnected`，无 `on_connected`，断开不自动重连，见 `qmt-trade-review-design.md` 第 0 节、第 3.2 节）/ `order_stock` 失败 | 进入 `FROZEN`：暂停下单，触发重连 + 当日 `query_*` 补采（补采机制详见 `qmt-trade-review-design.md` 第 3.2 节，本节只联动不重述） |

> 浮动盈亏口径：原生 `XtPosition` 不给浮动盈亏，需用「盯市现价 − 成本 × volume」自算（盯市价来源与计算口径见 `qmt-trade-review-design.md` 第 1 节、`qmt-trade-review-board-design.md` 第 4.1 节）。**风控用的盯市价必须取执行侧 `xtdata` 实时价，不取信号侧 `last_price` 快照**（快照有延迟且仅单字段）。

#### 5.4.2 空仓闸门联动（market_state = 空仓）

- 信号侧情绪周期 `market_state`（启动 / 高潮 / 震荡 / 退潮 / 冰点 / **空仓**，口径见 `qmt-trade-review-closed-loop-attribution-design.md` 第 3.3 节）每日刷新。
- **`market_state == 空仓` 时：只守仓不新开**——执行侧停止一切买入；**存量持仓仍按 5.3 决策表正常卖出**（空仓闸门只关买入闸，不锁卖出闸，避免该出的票出不掉）。
- 退潮期 / 分歧期默认下调续持评级（对齐建议侧「退潮 / 冰点不得激进接力」口径，`limit-up-push-investment-advice-refactor-plan.md` 第 3.2.2 节）：`continuation_prob` 同档下，退潮期更倾向了结。
- 空仓闸门是否「救了我们」由复盘侧行情反事实回看验证（详见 `qmt-trade-review-closed-loop-attribution-design.md` 第 5 节，本节不重述）。

#### 5.4.3 安全默认（不确定宁可不交易）

- 先验缺失（信号侧无该股先验）+ 盘口取数失败 → **不做主动续持**，按纯技术退出的保守分支（破位即出）。
- 任一关键输入不可信（行情断流、盘口异常、状态机状态不明）→ **进 `FROZEN`，宁可不卖也不误卖**；恢复后人工确认再解冻。
- 卖出量恒取 `min(决策量, can_use_volume)`，越界一律以可卖量为准，**绝不下超量卖单**（防废单与对账失真）。

### 5.5 文件路径与新增/改动点

> 执行侧代码运行在 Windows VPS QMT 进程，**不在本仓库 `backend/` 内**（解耦架构，本仓库为信号侧）。本节给出执行侧建议模块组织 + 信号侧需为本节配套提供的契约面。

**执行侧（Windows，建议模块，仓库外）**：

- `position_manager.py`：持仓状态机（状态定义、`earliest_sellable_date` 标记、状态流转、先验挂接 / 纯技术退出分叉）。
- `sell_decider.py`：竞价定夺 + 分时定夺 + 决策表（消费先验 + `xtdata` 实时盘口）。
- `risk.py`：账户级 / 单票级阈值、`FROZEN` 冻结态、空仓闸门联动、安全默认。
- 复用既有执行侧基建：`xttrader` 回调注册、`order_stock` 下单、`query_*` 补采（与 `qmt-trade-review-design.md` 第 1–3 节回流模块同进程）。

**信号侧（本仓库，配套改动点）**：

- `limit_up_selected_stock` 表：必须含 `continuation_prob` / `boost` / `fail_conditions` / `market_state` / `role` / `strategy` / `tradable_flag` / `trade_date` / `target_trade_date` 等列（**DDL / ORM / Alembic 迁移 / `03_full_schema_with_comments.sql` / `database-schema.md` 四处同步在信号侧 watchlist 契约产出设计交付**，本节只声明本模块对这些列的消费契约）。
- 落表切入点：`backend/app/services/limit_up_push_service.py` 的 `ensure_analysis_for_trade_date` READY 收口段（`status=READY` 之后、单一 `commit`（`:402`）之前，即 `:398`～`:399` 之间）落 `limit_up_selected_stock`，与 `status=READY` 同事务原子提交。先验数据来源为多阶段 pipeline 写入的 `context["pipeline"]` 的 `selected_*_stocks` 与 `_emotion_cycle_metrics`（`_generate_multi_stage_llm_report` `:2172-2183`，已经 `:397` `context_json=json_dumps(context)` 持久化）。整组按 `trade_date` 做 **delete-then-insert** 实现 latest-wins（防止本次落选、上次入选的旧股票残留；`limit_up_selected_stock` 设计为一股一行、按 `ts_code + trade_date`）。
  - **READY 早退守卫（须覆盖两条早退路径）**：①`existing.status == READY` 直接 `return existing`（`:345-346`）；②并发 `IntegrityError` 回查后 `status == READY` 直接 `return`（`:376-377`）。这两条都不经收口段，首次上线（历史 READY 行先于本特性存在）或并发命中缓存时会「该日不落表」。守卫判据：该 `trade_date` 在 `limit_up_selected_stock` 无行则**从 `existing.context_json["pipeline"]` 做 delete-then-insert 回填**（早退分支未跑 `_generate_llm_report`，内存 `context["pipeline"]` 为空，数据源必须取已持久化的 `context_json.pipeline`）。GENERATING-not-stale 两条早退（`:347-351`、`:378-382`）无需守卫（另一并发 worker 仍在生成、终将走收口段）。
- 代码归一：`stock_identity_resolver.resolve_by_code` 统一 `ts_code`，供执行侧先验挂接与回流回挂复用。
- `a_trade_calendar`：`earliest_sellable_date`（B → `trade_cal_next(B)`）与「次日」映射均经此表，**禁止自然日加减**（已存在物理表）。

### 5.6 关键方法签名与伪逻辑（非可运行整段）

```python
# position_manager.py
def mark_position_on_fill(self, fill: XtTrade) -> PositionUnit:
    """买入成交回报落地时建立/更新持仓单元，打最早可卖日标记。
    业务意图：守 T+1——买入当日 B 不可卖。
    边界：earliest_sellable_date = a_trade_calendar 的 trade_cal_next(B)（禁自然日 +1）。
    幂等：按 (account_id, ts_code) 合并，按 traded_id 去重（重复回报不重复加仓）。
    """
    # 合并 FIFO 成本 → 设 earliest_sellable_date → 状态置 LOCKED_T1（若当日买入）

def refresh_state(self, today: date) -> None:
    """每交易日开盘前推进状态：跨过买入日的 LOCKED_T1 → HOLDING；并刷新先验挂接。"""
    # for unit in 持仓: if today >= unit.earliest_sellable_date: unit.state = HOLDING
    # 先验挂接：prior = signal_prior_for(unit.ts_code, today)
    #   prior 存在（次日又涨停）→ unit.mode = SIGNAL_DRIVEN
    #   prior 为空（次日没涨停）→ unit.mode = TECH_EXIT

def sellable_units(self, today: date) -> list[PositionUnit]:
    """卖出只对昨日及更早持仓生效：返回 today >= earliest_sellable_date 的单元。"""

# sell_decider.py
def decide_auction(self, unit, prior: SignalPrior | None, book: OrderBook) -> SellAction:
    """次日竞价定夺（可卖日 9:15–9:25）。
    输入：信号先验（continuation_prob/boost/fail_conditions）+ xtdata 实时竞价盘口。
    边界：盘口必须来自执行侧 xtdata（非信号侧 last_price 快照）。
    返回：HOLD / REDUCE / CLEAR（价位不在此处，由 QMT 下单层自定）。
    """
    # 高开强且量价匹配且 prior 强 → HOLD
    # 命中 fail_conditions（背离/弱开）→ REDUCE/CLEAR

def decide_intraday(self, unit, prior, book) -> SellAction:
    """分时定夺（可卖日 9:30 起）：秒板续持/冲高止盈/破位止损/炸板出/烂板出/尾盘了结。"""

# risk.py
def gate(self, account, unit, today: date) -> RiskDecision:
    """风控闸门：返回 ALLOW / FREEZE / SELL_ONLY_HOLD（空仓只守仓）。
    边界：行情/下单中断 → FREEZE；market_state==空仓 → 只守仓不新开；
    安全默认：关键输入不可信 → FREEZE，宁可不交易。
    """

def clamp_sell_volume(self, decision_vol: int, can_use_volume: int) -> int:
    """卖出量上限 = min(决策量, can_use_volume)，绝不下超量卖单（守 T+1 + 防废单）。"""
    return min(decision_vol, can_use_volume)
```

### 5.7 数据结构 / 契约

- **`SignalPrior`（消费 `limit_up_selected_stock` 的视图对象）**：`ts_code` / `trade_date(T)` / `target_trade_date(T+1)` / `continuation_prob` / `boost` / `fail_conditions`（结构化条件列表）/ `market_state` / `role` / `strategy`。**只读**，执行侧不回写。
- **`PositionUnit`**：`account_id` / `ts_code` / `volume` / `can_use_volume` / `avg_cost` / `earliest_sellable_date` / `state` / `mode`（SIGNAL_DRIVEN | TECH_EXIT）。
- **`OrderBook`**：执行侧 `xtdata` 实时盘口快照（竞价高开幅度、封单额、封流比、开板次数、分时量价），**仅执行侧可得**。
- **`SellAction`**：`HOLD` / `REDUCE`（部分）/ `CLEAR`（清仓）；附 `reason`（秒板续持 / 竞价背离 / 冲高止盈 / 走弱破位 / 炸板 / 烂板 / 尾盘了结）。`reason` 透传到 `qmt_order.order_remark`，供闭环归因回挂卖出意图。

### 5.8 边界与异常 / 幂等

- **守 T+1 双保险**：`earliest_sellable_date` 业务闸 + `can_use_volume` 量闸，二者任一不满足都不下卖单。
- **盘口来源**：实时决策盘口一律取执行侧 `xtdata`；信号侧 `last_price` 快照仅供信号侧展示 / 弱校验，**不进卖出决策路径**。
- **断线幂等**：`on_disconnected` → `FROZEN` + 重连 + 当日 `query_*` 补采（机制详见 `qmt-trade-review-design.md` 第 3.2 节）；补采按唯一键 upsert（含跨日 ID 复用加固，见 §6.5），重连补采不产生重复成交 / 委托。
- **卖单幂等**：同一持仓单元在 `SELLING` 态不重复发同向卖单；部成回 `PART_SOLD` 后按剩余量重评，不叠加。
- **时间口径**：成交 / 委托时间戳东八区 → UTC naive 入库、`created_at / updated_at` 东八区理解（详见 `qmt-trade-review-design.md` 第 4 节），本节决策日志同口径，禁手工 ±8h。
- **安全默认**：任何不确定（先验缺失 + 盘口失败、状态不明）→ 保守不卖或 `FROZEN`，绝不误卖、绝不超量卖。

### 5.9 单测要点

| 用例 | 构造 | 断言 |
| --- | --- | --- |
| **T+1 锁定** | 当日买入成交、`今日 == 买入日 B` | 状态 `LOCKED_T1`；`sellable_units` 不含该单元；`decide_*` 短路返回不可卖；`clamp_sell_volume` 受 `can_use_volume=0` 钳为 0 |
| **可卖日推进** | 买入次日（`trade_cal_next(B)`）`refresh_state` | `LOCKED_T1 → HOLDING`；`earliest_sellable_date` 经 `a_trade_calendar` 跳过周末 / 节假日（非自然日 +1） |
| **连板续持** | 次日又涨停、命中当日 `limit_up_selected_stock`、`continuation_prob` 高、盘口秒板稳封、未命中 `fail_conditions` | `mode=SIGNAL_DRIVEN`；`decide_intraday` 返回 `HOLD`（续持） |
| **破位止损** | `HOLDING`、盘口跌破支撑 + 放量下杀（命中 `fail_conditions`） | `decide_intraday` 返回 `CLEAR` / `REDUCE`，`reason=走弱破位` |
| **次日没涨停转纯技术退出** | 次日未涨停、信号池无该股 | `mode=TECH_EXIT`；先验为空，决策只吃盘口；弱势直接出 |
| **竞价背离减仓** | 高开但量价背离（命中 `fail_conditions` 背离类） | `decide_auction` 返回 `REDUCE` / `CLEAR` |
| **炸板出** | 已封涨停后开板、封单瓦解 | `decide_intraday` 返回 `CLEAR`，`reason=炸板出` |
| **空仓闸门** | `market_state==空仓`、持有存量仓 | `risk.gate` 返回 `SELL_ONLY_HOLD`：不新开、存量仍按规则可卖 |
| **冻结态** | 行情断流 / `on_disconnected` | `risk.gate` 返回 `FREEZE`；暂停新卖出决策；恢复需人工确认 |
| **卖量钳制** | 决策清仓但 `can_use_volume < volume`（含当日补买部分） | 实际卖单量 == `can_use_volume`，不超量 |
| **盘口来源** | 注入信号侧 `last_price` 与执行侧 `xtdata` 盘口不一致 | 决策取 `xtdata`，不取 `last_price` 快照 |

### 5.10 验收标准

1. 买入当日绝不卖出；`earliest_sellable_date = trade_cal_next(B)` 经 `a_trade_calendar` 映射，跨周末 / 节假日正确（守 T+1）。
2. 卖出决策与下单标的恒为「昨日及更早持仓」，卖量恒 ≤ `can_use_volume`，无超量卖单 / 废单。
3. 「第二天卖 = 买入日 B 的次一交易日 `trade_cal_next(B)`（以买入日为锚、按实际持有交易日数计）」在代码注释、决策日志、状态机标记中口径统一，无 T 偏移歧义。
4. 持仓股次日又涨停 → 重新刷新先验、按信号驱动续持；次日没涨停 → 转纯技术退出，二者分支可验证、互不串。
5. 竞价定夺与分时定夺覆盖决策表全部分支（高开强续持 / 背离减清 / 秒板续持 / 冲高止盈 / 破位止损 / 炸板出 / 烂板出 / 尾盘了结），且止盈止损**价位由 QMT 自定、不来自信号契约**。
6. 风控：账户级 / 单票级阈值击穿、行情 / 下单中断进 `FROZEN` 并触发重连补采；`market_state==空仓` 只守仓不新开、存量仍可卖；关键输入不可信时安全默认（宁可不交易）。
7. 状态机所有流转、卖出动作、风控冻结 / 恢复均落决策日志（时间口径合规），卖出 `reason` 透传 `qmt_order.order_remark` 供闭环归因回挂（归因消费见 `qmt-trade-review-closed-loop-attribution-design.md` 第 1 节，本节只产出不复述）。
8. 单测覆盖 5.9 全部用例并通过；与三篇 QMT 文档的回流 / 复盘 / 归因衔接点（四表写入、补采、回挂键）口径一致、只引用不重复。

### 5.11 依赖顺序

- **D0（前置）**：信号侧 watchlist 契约产出设计中 `limit_up_selected_stock` 表 + 先验列（`continuation_prob` / `boost` / `fail_conditions` / `market_state`）落地并在 `ensure_analysis_for_trade_date` READY 收口段写入（含 §5.5 两条 READY 早退守卫）。无先验表则本节先验驱动分支无数据。
- **D1**：执行侧 `qmt_*` 四表回流就绪（详见 `qmt-trade-review-design.md` 第 1–2 节），position_manager 才能基于成交回报建持仓单元、卖出回报才能落账。
- **D2（本节主体）**：`position_manager`（状态机 + T+1 标记）→ `risk`（闸门 / 冻结 / 空仓联动）→ `sell_decider`（竞价 / 分时决策表）。`risk` 先于 `sell_decider`（决策前必过闸门）。
- **D3**：决策日志 + `order_remark` 卖出意图透传，供复盘看板与闭环归因消费（消费侧详见 `qmt-trade-review-board-design.md` 第 4–6 节、`qmt-trade-review-closed-loop-attribution-design.md` 第 2–5 节，本节不重复展开）。

---

## 六、交易数据回流采集 data_writer、对账 reconcile 与日志 logger

> 定位：本节是 QMT 执行侧（Windows VPS）把账户 / 委托 / 成交 / 持仓数据**写回** Linux MySQL（`stock_ah_ai`）的「写端」实现规范。`qmt_*` 四表的完整 DDL、唯一键、时间字段口径、复盘看板与闭环归因的「读端」消费逻辑，已在三篇 QMT 设计文档定稿，本节不重复展开，只做采集 / 写入逻辑与衔接说明并显式引用：
> - 四表 DDL（`qmt_trade` / `qmt_order` / `qmt_position_snapshot` / `qmt_account_daily`）、唯一键、时间口径、写入路径选型、断线补采、对账思路：**详见 `resources/doc/qmt-trade-review-design.md` 第 2、3、4 节**。
> - 复盘看板（净值曲线 TWR / Modified Dietz、MDD、夏普、FIFO、滑点双基准、API 端点 `/api/review/*`、前端两屏）：**详见 `resources/doc/qmt-trade-review-board-design.md` 第 4、5、6 节**。
> - 闭环归因（信号-执行-结果 join、漏斗、逆向选择、先验校准、滑点归因、空仓反事实、`qmt_signal_attribution_daily`）：**详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 全文**。
> - 多阶段 pipeline 与投资建议产出的信号侧上游：**详见 `resources/doc/limit-up-multi-stage-analysis-refactor-plan.md`**、**`resources/doc/limit-up-push-investment-advice-refactor-plan.md`**；`limit_up_selected_stock` 的字段口径见 `qmt-trade-review-design.md` 第 2.5 节与闭环归因第 1.1 节。
>
> 注意四表命名以 `qmt-trade-review-design.md` 第 2 节为准（`qmt_position_snapshot` / `qmt_account_daily`）；board 文档第 3 节中 `qmt_asset_daily_snapshot` / `qmt_position_daily_snapshot` 为同义表的早期命名，落地时统一采用 design 文档命名，并在 `03_full_schema_with_comments.sql` / `database-schema.md` 同步。

### 6.0 运行位置与读者提示（给不熟悉 QMT 的开发者）

- 本节描述的 `data_writer` / `reconcile` / `logger` 三个模块**运行在 Windows VPS 上的独立 Python 进程内**（与 `xttrader` 同进程或同机守护），不属于 Linux 侧 FastAPI 后端代码；它们只对 Linux MySQL 的 `qmt_*` 库做写入。Linux 后端对这四张表只读（复盘看板 / 归因消费）。
- QMT（`xttrader`）的硬约束（落地强约束，已核实，**详见 `qmt-trade-review-design.md` 第 0 节**）：`query_stock_asset / positions / orders / trades` 只返回「当日」数据、隔日清空不可补；`on_connected` 不存在、断开不自动重连、进程退出即丢推送；`XtAsset` 只有 6 字段不给净值、`XtPosition` 不给浮动盈亏，第三方封装字段（`float_profit` / `last_price`）原生不返回，禁止假定其存在；字段集存在版本差异，落地前必须在目标机用 `vars(obj)` / `dir(obj)` 实测。
- 因此回流采用「实时回调落明细 + 收盘定时快照兜底」两条腿（**详见 `qmt-trade-review-design.md` 第 1 节**），本节给出这两条腿的采集流程伪逻辑、写入接口契约、幂等 / 补采规则、单测与验收。

### 6.1 模块划分与文件路径（执行侧新增）

执行侧新增一个独立采集工程（建议目录 `qmt-collector/`，与 Linux 后端 `backend/` 解耦，可单独部署到 Windows）。本节定义其模块；若后续选择「经 Linux 内网写接口 /ingest」路径，则在 Linux 后端 `backend/app/` 内新增对应 `routes_ingest.py` + service（见 6.4）。

| 模块 | 建议文件路径（执行侧） | 职责 |
| --- | --- | --- |
| `data_writer` | `qmt-collector/qmt_collector/data_writer.py` | 回调与定时快照统一的「落库写端」：字段规整（代码 / 方向 / 状态 / 时间）→ 幂等 upsert 到 `qmt_*`，封装两种写入路径（优先直连 MySQL，次选 /ingest） |
| `callbacks` | `qmt-collector/qmt_collector/callbacks.py` | `xttrader` 回调注册：`on_stock_trade / on_stock_order / on_order_error / on_cancel_error / on_stock_asset / on_stock_position / on_disconnected`，转交 `data_writer` |
| `snapshot_job` | `qmt-collector/qmt_collector/snapshot_job.py` | 收盘 / 盘中 / 开盘前定时调 `query_*`，全量快照 + 当日明细兜底补采 |
| `reconcile` | `qmt-collector/qmt_collector/reconcile.py` | 本地下单台账 vs `xttrader` 回报对账（委托 / 成交 / 资产 / 滑点四类勾稽 + 偏差告警） |
| `logger` | `qmt-collector/qmt_collector/logger.py` | 结构化本地日志（回调流水、补采、对账偏差、断线重连、写失败重试），券商对账单兜底归档 |
| `time_utils` | `qmt-collector/qmt_collector/time_utils.py` | QMT 时间戳 → UTC naive + east8 双写的统一转换（见 6.6，杜绝盲目 ±8h） |
| `local_order_ledger` | `qmt-collector/qmt_collector/local_order_ledger.py` | 本地下单台账（每次 `order_stock` 计划单落盘），对账事实源之一 |

> 若 Linux 后端需新增 `/ingest` 写端点（6.4 方案 B），涉及 Linux 侧改动点：`backend/app/api/routes_ingest.py`（新路由，挂在 `app.include_router(..., prefix="/api")`，参照 `backend/app/main.py` 的注册风格）、`backend/app/api/deps_auth.py`（新增固定 token 校验依赖，不复用用户登录态）、`backend/app/core/config.py`（新增 `QMT_INGEST_TOKEN` 等配置，参照其 `Field(default=..., alias=...)` 风格）。

### 6.2 采集流程伪逻辑（两条腿）

#### 6.2.1 实时回调落明细（事件驱动增量）

废单 / 拒单 / 撤单失败回调是**回调独有、`query_*` 兜底无法还原**的事实（隔日 API 不返回失败态历史），必须落库；这是回调路径不可替代的核心价值。映射关系**详见 `qmt-trade-review-design.md` 第 1.1 节表格**。

```text
# 进程启动：建 session → connect()（返回 0 视为成功，on_connected 不存在）→ subscribe(acc) → 注册回调 → run_forever()
启动():
    session = XtQuantTrader(path, session_id)
    register_callback(MyCallback)        # 见下
    若 session.connect() != 0: 日志告警并退避重试
    若 session.subscribe(account) != 0: 日志告警
    session.run_forever()                # 进程常驻，退出即丢推送

on_stock_trade(t: XtTrade):              # 成交回报——唯一事实源，绝不可丢
    rec = 规整成交(t)                    # 代码归一/方向映射/时间双写/trade_date
    rec.data_source = "CALLBACK"
    data_writer.upsert_trade(rec)        # 幂等键见 6.5（建议纳入 trade_date 防跨日 ID 复用）

on_stock_order(o: XtOrder):              # 委托状态变化（已报/部成/已成/已撤/废单）
    rec = 规整委托(o)
    rec.order_status = 映射状态枚举(o.order_status)   # REPORTED/PART_TRADED/TRADED/CANCELLED/REJECTED
    rec.data_source = "CALLBACK"
    data_writer.upsert_order(rec)        # 幂等键见 6.5

on_order_error(e: XtOrderError):         # 【回调独有】下单失败——废单/拒单须落，避免计划单凭空消失
    rec = 由台账或回报补全委托骨架(e.order_id)
    rec.order_status = "ERROR"
    rec.error_id, rec.error_msg = e.error_id, e.error_msg
    data_writer.upsert_order(rec)        # 同一 order_id upsert，不新增重复行

on_cancel_error(e: XtCancelError):       # 【回调独有】撤单失败——留痕
    data_writer.mark_cancel_failed(account_id, e.order_id, e.error_id, e.error_msg)
    # 仅在既有委托行追加 cancel_failed=1 + error_*，不改 order_status 终态

on_stock_asset(a: XtAsset):              # 盘中资产——仅刷新当日 INTRADAY 行，不写历史净值
    data_writer.upsert_account_daily(规整资产(a), snapshot_type="INTRADAY")

on_stock_position(p: XtPosition):        # 盘中持仓——仅刷新当日 INTRADAY 行
    data_writer.upsert_position(规整持仓(p), snapshot_type="INTRADAY")

on_disconnected():                       # 断线检测——驱动补采（见 6.2.3）
    logger.warn("disconnected")
    触发 重连并补采()
```

#### 6.2.2 收盘定时全量快照兜底

收盘批次是历史还原的**唯一机会**（隔日 API 清空），必须保证当日至少成功落一次 `CLOSE`。时机 / 调用 / 目标表**详见 `qmt-trade-review-design.md` 第 1.2 节表格**。

```text
收盘批次(trade_date=今日东八区交易日):      # 调度如 15:05 / 15:30
    # 1) 明细全量兜底补全（把盘中回调可能漏的补齐 → 当日权威全集）
    for t in query_stock_trades(account):
        rec = 规整成交(t); rec.data_source = "QUERY_BACKFILL"
        data_writer.upsert_trade(rec)        # 唯一键 upsert，已有则覆盖为终态
    for o in query_stock_orders(account):
        rec = 规整委托(o); rec.data_source = "QUERY_BACKFILL"
        data_writer.upsert_order(rec)
    # 2) 快照（资产/持仓）以定时拉取为权威 → CLOSE
    asset = query_stock_asset(account)
    data_writer.upsert_account_daily(规整资产(asset), snapshot_type="CLOSE")  # 净值曲线唯一来源
    for p in query_stock_positions(account):
        data_writer.upsert_position(规整持仓(p), snapshot_type="CLOSE")
    # 3) 收盘后触发对账（见 6.7）
    reconcile.run(trade_date)
    # 4) 失败重试：CLOSE 当日未成功落，按退避重试并强告警（隔日不可补）
```

开盘前（如 9:10，可选）`query_stock_positions` 落 `OPEN` 作昨夜拥股基线；盘中分钟级（可选）落 `INTRADAY` 覆盖给当日页用。

#### 6.2.3 断线补采

```text
重连并补采():
    重建 session（新 session_id）→ connect()（返回 0）→ subscribe(account)   # 断开不自动重连，须主动重建
    # 重连成功后立即全量补采当日缺失（断线期间漏掉的成交/委托被补回）
    for t in query_stock_trades(account):
        rec = 规整成交(t); rec.data_source = "QUERY_BACKFILL"   # 明确标记补采来源
        data_writer.upsert_trade(rec)
    for o in query_stock_orders(account):
        rec = 规整委托(o); rec.data_source = "QUERY_BACKFILL"
        data_writer.upsert_order(rec)
    query_stock_asset/positions → 刷新当日 INTRADAY 快照
    logger.info("backfill_done", missing_recovered=N)
```

补采前提：`query_*` 返回当日全量，故重连后拉一次即可补齐当日缺失，但**必须当日内完成，隔日不可补**。配合 Windows 任务计划 / 守护进程常驻、每日开盘前主动重连一次。**详见 `qmt-trade-review-design.md` 第 3.2 节**。

### 6.3 字段规整（落库前在执行侧统一）

落库前 `data_writer` 对所有记录做四类规整，规整后再走幂等 upsert：

- **代码归一 `ts_code`**：QMT `stock_code`（如 `600036.SH` / `SH600036` / `600036` / `.BJ` 脏数据）经 `stock_identity_resolver` 归一为带交易所后缀的标准 `ts_code`（如 `600036.SH`）。Linux 侧解析器为 `backend/app/services/stock_identity_resolver.py` 的 `StockIdentityResolver.resolve_code(ts_code) -> StockResolveResult`；执行侧无 DB 连接时，可在 `/ingest` 服务端做归一（方案 B），或在执行侧内置等价的「交易所后缀补全」轻量逻辑（方案 A，规则与解析器一致：6 位 `60/68 → .SH`、`00/30 → .SZ`、`8/4 → .BJ`，主创板范围只取主板 + 创业板）。同时保留 `qmt_stock_code` 原值便于排查。
- **方向 `trade_side`**：由 `XtTrade / XtOrder` 的 `order_type` / `offset_flag` 统一映射为 `BUY` / `SELL`，`offset_flag` 原值同时落库。
- **状态 `order_status`**：QMT 状态枚举映射为 `REPORTED / PART_TRADED / TRADED / CANCELLED / REJECTED / ERROR`（`REJECTED`=废单来自 `on_stock_order`、`ERROR`=下单失败来自 `on_order_error`）。
- **时间**：见 6.6。
- **版本兼容**：`avg_price / frozen_volume / on_road_volume / yesterday_volume` 等版本可能缺失字段，用 `getattr(obj, name, None)` 取，缺失落 NULL（DDL 已设可空），不得因 `AttributeError` 崩溃。

`signal_trade_date`（=信号日 T）与 `resolved_ts_code`（归一代码）回填见 6.8。

### 6.4 写入路径与 /ingest 接口契约

两路径对比与「推荐直连 MySQL」的理由**详见 `qmt-trade-review-design.md` 第 3.1 节**。`data_writer` 抽象出统一写接口，二选一可配置：

- **方案 A（design 文档推荐，与既定解耦架构一致）：Windows 直连 MySQL**。执行侧持有**独立写账号**，权限仅限 4 张 `qmt_*` 表的 `INSERT / UPDATE / SELECT`，不授予其他业务表写权限；网络层用内网 / VPN / IP 白名单限制 MySQL 端口暴露面，启用 TLS 或 SSH 隧道；连接信息按 `/Users/salty/codeProject/ai/doc/mysqluse.md` 口径，不硬编码、不入库敏感信息、不写日志。`data_writer.upsert_*` 直接执行带 `ON DUPLICATE KEY UPDATE` 的 SQL（见 6.5）。
- **方案 B（备选，若 MySQL 端口暴露不可接受）：经 Linux 内网写接口 `/ingest`**。在 Linux 后端新增鉴权写端点，执行侧只放通 HTTPS，DB 不对外；接口层集中做归一 / 校验 / 幂等 upsert。

`/ingest` 接口契约（方案 B）：

- **端点**：`POST /api/ingest/qmt`（新建 `backend/app/api/routes_ingest.py`，挂 `prefix="/api"`）。
- **鉴权**：请求头 `X-QMT-Ingest-Token: <token>`，服务端与 `config.py` 新增配置 `qmt_ingest_token = Field(default=None, alias="QMT_INGEST_TOKEN")` 常量时间比较；**不复用用户登录态**（执行侧是机器，不是用户）。校验失败返回 401。建议追加来源 IP 白名单。
- **请求体**（JSON，单次可批量，按表分组以便服务端按唯一键 upsert）：

```json
{
  "account_id": "1000000365",
  "trade_date": "2026-06-12",
  "source": "CALLBACK",            // CALLBACK | QUERY_BACKFILL
  "trades":   [ { "traded_id": "...", "ts_code": "600036.SH", "qmt_stock_code": "600036.SH",
                  "order_id": 123, "trade_side": "BUY", "offset_flag": 48,
                  "traded_price": 35.12, "traded_volume": 200, "traded_amount": 7024.0,
                  "traded_time": "2026-06-12T05:31:02",        // UTC naive（已转换，见 6.6）
                  "traded_time_east8": "2026-06-12T13:31:02",  // 东八区 naive 原值
                  "strategy_name": "limit_up", "order_remark": "LUP|2026-06-11|600036.SH" } ],
  "orders":   [ { "order_id": 123, "ts_code": "600036.SH", "trade_side": "BUY",
                  "order_status": "TRADED", "order_volume": 200, "traded_volume": 200,
                  "error_id": null, "error_msg": null, "cancel_failed": false,
                  "order_time": "...", "order_time_east8": "...", "order_remark": "LUP|..." } ],
  "positions":[ { "ts_code": "600036.SH", "snapshot_type": "CLOSE", "volume": 200,
                  "can_use_volume": 0, "open_price": 35.0, "market_value": 7080.0 } ],
  "account":  { "snapshot_type": "CLOSE", "total_asset": 100000.0, "cash": 92000.0,
                "frozen_cash": 0.0, "market_value": 8000.0 }
}
```

- **响应**：`200 { "ok": true, "upserted": {"trades": N, "orders": M, "positions": P, "account": 1}, "rejected": [] }`；校验失败的单条记录进 `rejected`（带原因），不整批回滚（部分成功 + 逐条结果），保证幂等重传安全。
- **幂等**：服务端按 6.5 唯一键 upsert，执行侧重传同一批不会产生重复行；执行侧可对未确认批次安全重发。
- **服务端落库**：复用现有 SQLAlchemy 会话与模型；归一 / 时间口径与 6.3 / 6.6 一致。

### 6.5 幂等口径（含唯一键跨日 ID 复用加固）

唯一键与 `ON DUPLICATE KEY UPDATE` 口径以四表 DDL 为准（**详见 `qmt-trade-review-design.md` 第 2.1–2.4 节**）。

> **现行设计 DDL 与本节加固建议的关系（经核验澄清）**：`qmt-trade-review-design.md` 现行 DDL 的明细表唯一键**不含 `trade_date`**——`qmt_trade` 为 `uk_qmt_trade_account_traded (account_id, traded_id)`（design 第 111 行）、`qmt_order` 为 `uk_qmt_order_account_order (account_id, order_id)`（design 第 154 行），即把 `traded_id` / `order_id` 当作账户内全局去重最小单位。本节的「纳入 `trade_date`」是**对现行设计的加固建议，而非现行设计本身**。

加固背景与落地口径：

- QMT / xtquant 的 `order_id` 通常按交易日 / 会话重置，`traded_id` 多为较长计数串、跨日相对更安全，但官方均未契约式保证跨日唯一，单凭 `(account_id, order_id)` / `(account_id, traded_id)` 存在**跨日 ID 复用串号风险**，`account_id` 不能消除该风险。
- **落地决策（须在评审固化）**：先按 design 第 354 行要求在目标机用 `vars(obj)` / `dir(obj)` **实测 xtquant 字段的跨日唯一性**：
  - 若实测确认 `order_id` / `traded_id` 在账户内跨日唯一 → 可沿用现行 `(account_id, traded_id)` / `(account_id, order_id)`。
  - 若实测无法确认跨日唯一（更稳妥的默认假设）→ 把 `trade_date` 纳入明细唯一键以彻底规避跨日序号复用：
    - `qmt_trade` 唯一键改为 `uk_qmt_trade_account_date_traded (account_id, trade_date, traded_id)`；
    - `qmt_order` 唯一键改为 `uk_qmt_order_account_date_order (account_id, trade_date, order_id)`。
  - 采用加固方案时须同步 Alembic 迁移（接在当前 head 之后，串联不并列，见 §1.2.1）、`03_full_schema_with_comments.sql`、`database-schema.md`，并在迁移注释中写明「纳入 trade_date 防跨日 ID 复用」的业务意图。
- **纳入 `trade_date` 的前提保障**：`trade_date` 必须由权威来源稳定派生（东八区自然交易日，与 `a_trade_calendar.cal_date` 对齐，见 6.6），保证同一委托多次状态推送的 `trade_date` 一致；否则会破坏「同委托 upsert 覆盖为终态」的幂等收口（同 `order_id` 不同 `trade_date` 会被当成两行）。
- 快照表唯一键不变：`qmt_position_snapshot (account_id, trade_date, ts_code, snapshot_type)`、`qmt_account_daily (account_id, trade_date, snapshot_type)`。
- **写入语义**：所有写入走 `INSERT ... ON DUPLICATE KEY UPDATE`，UPDATE 列覆盖为「后到终态」（成交价量、`order_status`、`traded_volume`、`error_*`、快照值等），但**不回退已有非空字段为空**（如 `signal_trade_date`、`*_time_east8` 一旦回填不被空覆盖，用 `COALESCE(VALUES(col), col)` 口径）。回调与 `QUERY_BACKFILL` 谁先到谁先写、后到覆盖为终态；`data_source` 列保留最后写入来源以便审计。

```sql
-- 示例：成交明细幂等 upsert（执行侧直连或 /ingest 服务端）。
-- 业务意图：回调与收盘兜底/断线补采多次写同一 traded_id 不产生重复行；后到覆盖为终态，
-- 已回填的 signal_trade_date / *_east8 不被空值覆盖（COALESCE 口径）。
-- 注：唯一键是否含 trade_date 取决于 6.5 的 xtquant 字段实测结论；加固方案下唯一键含 trade_date。
INSERT INTO qmt_trade (account_id, trade_date, traded_id, ts_code, qmt_stock_code,
                       order_id, trade_side, traded_price, traded_volume, traded_amount,
                       traded_time, traded_time_east8, signal_trade_date, data_source)
VALUES (...)
ON DUPLICATE KEY UPDATE
  traded_price      = VALUES(traded_price),
  traded_volume     = VALUES(traded_volume),
  traded_amount     = VALUES(traded_amount),
  traded_time       = VALUES(traded_time),
  traded_time_east8 = COALESCE(VALUES(traded_time_east8), traded_time_east8),
  signal_trade_date = COALESCE(VALUES(signal_trade_date), signal_trade_date),
  data_source       = VALUES(data_source);
```

### 6.6 时间口径（杜绝 ±8h）

完整口径表与转换约定**详见 `qmt-trade-review-design.md` 第 4 节**。`time_utils` 固化为唯一转换入口，任何写端不得在 SQL / 前端手工 ±8h：

- QMT `traded_time` / `order_time` 是 **Unix 时间戳（秒，东八区运行）**。先按 `Asia/Shanghai` 解释，再转 UTC naive 入 `traded_time` / `order_time`；原值的东八区 naive 落 `traded_time_east8` / `order_time_east8` 供人工核对对账。

```python
# time_utils.py：QMT 时间戳 → UTC naive + 东八区 naive 双写。
# 业务意图：traded_time/order_time 与后端 UTC naive 字段同口径，前端 formatEast8DateTime(value) 展示；
#           *_east8 原样落东八区供对账，前端 formatEast8DateTime(value, {naiveAsEast8: true})。
# 边界：ts 为 None/0/异常时返回 (None, None) 并告警，不写错误时间。
def qmt_ts_to_db(ts: int | None) -> tuple[datetime | None, datetime | None]:
    if not ts:
        return None, None
    east8 = datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Shanghai"))
    utc_naive   = east8.astimezone(timezone.utc).replace(tzinfo=None)  # → traded_time/order_time
    east8_naive = east8.replace(tzinfo=None)                           # → *_east8
    return utc_naive, east8_naive
```

- `trade_date`（DATE）直接落**东八区自然交易日**（无时区问题，**不随入库机 UTC 当天漂移**），与 `a_trade_calendar.cal_date` 对齐。
- `created_at` / `updated_at` 由 DB `CURRENT_TIMESTAMP` 生成、按东八区理解（与 Linux 侧 `app/db/base.py` `TimestampMixin` 一致），前端 `formatEast8DateTime(value, { naiveAsEast8: true })`。

### 6.7 对账 reconcile（委托 / 成交 / 资产 / 滑点四类勾稽）

收盘批次后触发，事实源为 `local_order_ledger`（本地下单台账：每次 `order_stock` 落 `ts_code / 方向 / 计划量 / 计划价 / 信号来源 order_remark / 下单时刻`）vs `xttrader` 回报（`qmt_order` / `qmt_trade`）。四类勾稽口径**详见 `qmt-trade-review-design.md` 第 3.3 节**：

- **委托对账**：台账每条计划单应在 `qmt_order` 找到对应回报（经 `order_remark` 透传的本地单号或 `order_id` 关联）。台账有、回报无 → 漏单 / 下单失败（查 `on_order_error`，应已落 `order_status=ERROR`）；回报有、台账无 → 非本系统下单（手工单）单独标记。
- **成交对账**：`qmt_order.traded_volume` 应等于该委托下 `qmt_trade` 成交量之和；不一致 → 有成交回报漏采，触发 `query_stock_trades` 补采（`QUERY_BACKFILL`）。
- **资产对账**：当日 `Σ 成交净额 ± 费用` 与 `qmt_account_daily` 资产变动方向应一致（粗校验），偏差超阈值告警。
- **滑点对账**：台账「下单时刻价 / 信号决策价」对比 `qmt_trade.traded_price`，落为执行质量指标（滑点双基准与归因**详见 `qmt-trade-review-closed-loop-attribution-design.md` 第 4 节**、`qmt-trade-review-board-design.md` 第 4.6 节）。
- **偏差处理**：偏差落执行侧本地日志（`logger`），可选写一张对账记录表（design 文档先不强制建表，纳入后续阶段）；**券商对账单兜底**：每日导出券商成交 / 资产对账单归档，作为 `qmt_*` 与台账双向校验之外的第三方权威，月末做一次全量勾稽。
- **成本法**：已实现盈亏固定 **FIFO**，与 QMT / 券商对账单口径一致（**详见 `qmt-trade-review-design.md` 第 7 节、board 第 4.5 节**）。

### 6.8 signal_trade_date 回填与 resolved_ts_code 归一

衔接信号侧 `limit_up_selected_stock`（关联口径**详见 `qmt-trade-review-design.md` 第 2.5 节**、闭环归因第 1 节）：

- **`signal_trade_date` 由 `order_remark` 解析回填**：下单时执行侧把信号来源标识写入 `order_remark`（建议格式 `LUP|<signal_trade_date T>|<ts_code>`，如 `LUP|2026-06-11|600036.SH`）。落库 / 对账阶段解析 `order_remark` 取出 T 回填 `signal_trade_date`；若 `order_remark` 缺失，则用 `a_trade_calendar` 由 `trade_date`（=买入日 T+1）反推 `pretrade_date` 作为 T 兜底回填。回填后 `qmt_trade.signal_trade_date = limit_up_selected_stock.trade_date AND ts_code 相同` 即可直接关联，免去 join 时再算 T+1 映射。
- **`resolved_ts_code` 经身份解析归一**：三方代码（信号侧 / 行情侧 / QMT 侧）格式不一，统一经 `stock_identity_resolver` 归一为带交易所后缀的 `norm_code`（处理 `SH600000` / `600000` / `.BJ` 脏数据），作为信号-执行-结果三方 join 的代码键（**详见闭环归因第 1.2 节代码归一键**）。执行侧落库的 `ts_code` 即归一后值，`qmt_stock_code` 保留原值。

### 6.9 单测要点

执行侧采集逻辑应可在无真实 QMT 环境下用 fake `XtTrade / XtOrder / XtAsset / XtPosition` 对象单测（构造带 / 不带版本可选字段两组对象）：

- **回调落库**：mock 各回调对象 → 断言 `data_writer.upsert_*` 收到规整后记录（代码归一、方向 / 状态映射、时间双写正确）。
- **废单 / 拒单 / 撤单失败独有落库**：`on_order_error` → `order_status=ERROR` + `error_*` 落库；`on_cancel_error` → 既有委托行 `cancel_failed=1`，不误改终态、不新增重复行。
- **幂等**：同一明细（同唯一键）回调 + `QUERY_BACKFILL` 各写一次 → 表中仅 1 行，终态为后到值；`signal_trade_date` / `*_east8` 不被空覆盖（COALESCE 口径）。
- **跨日 ID 复用（仅加固方案下）**：两个不同 `trade_date` 但相同 `traded_id` → 落为 2 行（验证 `trade_date` 纳入唯一键生效）；若沿用现行不含 trade_date 唯一键，则此用例不适用，改为以 xtquant 实测结论为准。
- **时间转换**：给定东八区时间戳，`qmt_ts_to_db` 返回的 `traded_time`（UTC naive）= east8 − 8h、`traded_time_east8` = 原值；`ts=None/0` 返回 `(None, None)` 不抛错。
- **断线补采**：mock `on_disconnected` → 重连 + `query_*` 全量 → 断线期间缺失明细被补回、`data_source=QUERY_BACKFILL`。
- **收盘兜底**：mock `query_stock_trades / orders` 返回当日全集 → 漏采明细被补齐为当日权威全集；`CLOSE` 资产 / 持仓快照各落一行。
- **版本兼容**：缺 `avg_price / frozen_volume` 等字段的 fake 对象 → 落 NULL 不抛 `AttributeError`。
- **对账**：构造「台账有回报无」「成交量不勾稽」样例 → `reconcile` 命中漏单 / 触发补采、偏差告警。
- **/ingest（方案 B）**：无 / 错 token → 401；正确 token 批量 → 逐条 upsert，重传同批不产生重复行，部分非法记录进 `rejected` 不整批回滚。
- **`order_remark` 解析回填**：标准 / 缺失两种 `order_remark` → 分别解析得 T、经 `a_trade_calendar` 反推得 T；`resolved_ts_code` 对脏代码归一命中。

### 6.10 验收标准

- 盘中回调实时落明细（`qmt_trade` / `qmt_order`），废单 / 拒单 / 撤单失败均有记录、可定位失败原因，不出现计划单凭空消失。
- 收盘后四表均有当日权威数据，`CLOSE` 资产 / 持仓快照各至少一份（净值曲线 / 持仓复盘可还原）。
- 断线重连后当日缺失被补齐（标 `QUERY_BACKFILL`），且补采当日内完成。
- 唯一键幂等：重跑 / 重传 / 回调与兜底并发均不产生重复行；跨日相同 ID 不串号（按 §6.5 实测结论选用「现行唯一键」或「纳入 trade_date 的加固唯一键」）。
- 时间字段无 ±8h：`traded_time` / `order_time` 为 UTC naive、`*_east8` 为东八区原值，`trade_date` 取东八区交易日不随入库机 UTC 漂移，前端按来源选 `formatEast8DateTime` 参数展示正确。
- 写入路径满足安全约束：直连方案为仅 `qmt_*` 库 `INSERT / UPDATE / SELECT` 的独立账号 + IP 白名单 + TLS / 隧道；/ingest 方案带 token 鉴权、DB 不对外；敏感连接信息不硬编码 / 不入库 / 不写日志。
- 对账可用：每条本地计划单可对到回报或定位为漏单 / 失败；委托 / 成交 / 资产 / 滑点四类勾稽产出，偏差超阈值告警，本地日志 + 券商对账单可兜底。
- 衔接可用：`signal_trade_date` 由 `order_remark` 解析（缺失则 `a_trade_calendar` 反推）回填、`resolved_ts_code` 归一，能直接与 `limit_up_selected_stock` 按 `signal_trade_date + ts_code` 关联，供复盘看板 / 闭环归因消费。

### 6.11 依赖顺序

1. **P0**：四表 DDL / 唯一键 / 时间口径 / 写入路径选型固化（**依赖 `qmt-trade-review-design.md` 第 2、3、4 节**，含本节 6.5 唯一键是否纳入 `trade_date` 的 xtquant 字段实测结论与加固迁移）；执行侧独立写账号或 `/ingest` token 配置就绪。
2. **P1（依赖 P0）**：`data_writer` + `callbacks` 落明细 + 字段规整 + `time_utils` 时间转换；连接 / 订阅 / `run_forever` 常驻。
3. **P2（依赖 P1）**：`snapshot_job` 收盘全量快照 + 明细兜底补采 + 断线补采 + 开盘前重连。
4. **P3（依赖 P1）**：`reconcile` 四类勾稽 + `local_order_ledger` 台账 + 偏差告警 + 券商对账单归档；`signal_trade_date` / `resolved_ts_code` 回填。
5. **P4（依赖 P1–P3 数据就绪）**：Linux 侧复盘看板 / 闭环归因「读端」消费（**详见 board 与闭环归因文档**，不在本节展开）。

---

## 七、配置、模拟盘测试、上线 checklist 与实施次序

> 本节是执行侧（QMT / Windows VPS）从「代码就绪」到「接实盘小仓」之间的最后一道工程闸门。复盘看板、QMT 数据回流四表 DDL、闭环归因口径等已在三篇 QMT 文档定稿，本节只做「衔接说明 + 引用」，不重复展开：
> - QMT 四表（`qmt_trade` / `qmt_order` / `qmt_position_snapshot` / `qmt_account_daily`）完整 DDL、采集双通道（回调 + 定时快照）、断线重连补采、写入路径（Windows 直连 MySQL + 独立写账号）、时间口径，详见 `resources/doc/qmt-trade-review-design.md` 第 1~4、6~7 节。
> - 复盘看板指标口径（TWR / MDD / 夏普 / FIFO），详见 `resources/doc/qmt-trade-review-board-design.md`。
> - 信号-执行-结果闭环归因（计划 vs 实际漏斗、逆向选择量化、先验校准、滑点、空仓反事实），详见 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 2~6、9 节。

本节读者为开发者（含不熟悉 QMT 者）。所有工期 / 工时一律不写，只给配置项、清单、阶段、依赖顺序与验收标准。

### 7.1 配置项清单（执行侧 QMT 配置 + 接入信号侧的开关）

执行侧配置不入库、不进日志、不硬编码敏感信息（账户、token、数据库口令）；账户 / 口令按 `/Users/salty/codeProject/ai/doc/mysqluse.md` 与执行侧本地 `.env` / 配置文件管理。信号侧新增的开关（如「是否启用竞价择时」）按 `app/core/config.py` 既有风格落 `Field(default=..., alias="UPPER_SNAKE_CASE")` 并补中文注释（业务意图 / 边界 / 重跑口径）。

#### 7.1.1 账户与连接（执行侧，敏感，不入库）

| 配置项 | 含义 | 来源 / 约束 |
| --- | --- | --- |
| `QMT_ACCOUNT_ID` | QMT 资金账号（落 `qmt_*.account_id`） | 国金账户开通后填入；不进日志 |
| `QMT_ACCOUNT_TYPE` | 账号类型枚举（落 `qmt_*.account_type`） | 以实际 `xtquant` 版本 `vars(obj)` 实测为准 |
| `QMT_MINI_PATH` | QMT 客户端 `userdata_mini` 路径（`xttrader` 连接所需） | Windows VPS 上 QMT 安装目录 |
| `QMT_SESSION_ID` | 交易 session id（`xttrader` 一次性连接标识） | 断线重连须换新 session id（详见 qmt-trade-review-design.md 第 3.2 节） |
| `QMT_MYSQL_DSN` | 执行侧写库连接串（仅 `qmt_*` 四表写权限的独立账号） | 独立最小权限账号；内网 / VPN / IP 白名单；不硬编码（详见 qmt-trade-review-design.md 第 3.1 节） |

#### 7.1.2 信号侧写回接口地址与 token（仅当采用「经接口写」退化方案时）

> 既定写入路径为 **Windows 直连 MySQL + 独立写账号**（qmt-trade-review-design.md 第 3.1 节推荐 A 方案）。以下两项**默认不启用**，仅当后续 MySQL 端口暴露不可接受、退化为 B 方案（信号侧鉴权写接口）时才需要：

| 配置项 | 含义 | 默认 |
| --- | --- | --- |
| `QMT_WRITEBACK_BASE_URL` | 信号侧内网写接口地址（B 方案专用） | 空（A 方案不需要） |
| `QMT_WRITEBACK_TOKEN` | 写接口鉴权 token（不进日志、不入库） | 空 |

#### 7.1.3 竞价轮询与采集节奏（执行侧）

| 配置项 | 含义 | 默认 / 口径 |
| --- | --- | --- |
| `QMT_AUCTION_POLL_INTERVAL_SEC` | 9:15–9:25 竞价阶段 `get_full_tick` 轮询间隔（秒） | 默认 3s；实测前用于采集竞价撮合价 / 竞买竞卖量（见 7.2 实测清单） |
| `QMT_AUCTION_WINDOW` | 竞价采集窗口 | `09:15-09:25`（9:25 末轮须抓到最终竞价） |
| `QMT_INTRADAY_SNAPSHOT_MINUTES` | 盘中 `query_*` 快照间隔（写 `INTRADAY`，不进历史净值） | 默认 5min（详见 qmt-trade-review-design.md 第 1.2 节） |
| `QMT_CLOSE_SNAPSHOT_TIME` | 收盘 `CLOSE` 快照触发（净值 / 持仓唯一权威来源，当日必须成功一次） | 如 `15:05` 成交 / 委托兜底、`15:30` 资产快照 |

#### 7.1.4 各战法阈值与开关（信号侧，对齐既有竞价观察清单口径）

> 战法分层与「次日竞价观察清单」口径（竞价弱于 X% 放弃 / X%~Y% 低吸观察 / 高开超 Y% 警惕核按钮）已在 `resources/doc/limit-up-push-investment-advice-refactor-plan.md` 第 144、268 节固化。执行侧「竞价择时」是否真正按这套阈值动作，受 `QMT_AUCTION_TIMING_ENABLED` 总开关控制（见 7.1.6）。下列阈值是把信号侧文本口径**参数化**为执行侧可读的开关，便于实盘前调参，不改变信号侧既有语义：

| 配置项 | 含义 | 口径 |
| --- | --- | --- |
| `QMT_AUCTION_ABANDON_PCT` | 竞价高开弱于该幅度 → 放弃 | 对齐「竞价弱于 X% 放弃」 |
| `QMT_AUCTION_LOWBUY_PCT_LOW` / `_HIGH` | 低吸观察的竞价高开区间 [X%, Y%] | 对齐「X%~Y% 低吸观察」 |
| `QMT_AUCTION_OVERHEAT_PCT` | 竞价高开超该幅度 → 警惕核按钮 | 对齐「高开超 Y% 警惕」 |
| `QMT_STRATEGY_<NAME>_ENABLED` | 各战法（首板精选 / 两三连接力 / 高连龙头）参与开关 | 对应 `limit_up_selected_stock.strategy`；首板精选默认更克制（advice-plan 第 144 节口径） |
| `QMT_LEADER_STRENGTH_MIN` | 龙头强度分下限（低于不参与） | 对应 `leader_strength_score`（闭环归因文档 §1.1） |

#### 7.1.5 风控阈值（执行侧硬约束，下单前生效）

| 配置项 | 含义 | 默认 / 口径 |
| --- | --- | --- |
| `QMT_MAX_POSITION_PER_STOCK` | 单票最大仓位（金额或占比） | 小仓试错期取低值 |
| `QMT_MAX_TOTAL_EXPOSURE` | 总持仓暴露上限 | 限制最大回撤面 |
| `QMT_MAX_ORDERS_PER_DAY` | 单日最大下单笔数（防程序异常连发） | 防失控刹车 |
| `QMT_PER_ORDER_MAX_AMOUNT` | 单笔委托金额上限 | 防 fat-finger / 程序错单 |
| `QMT_PRICE_DEVIATION_GUARD_PCT` | 委托价相对现价偏离超阈值则拒单 | 防错价 |
| `QMT_MARKET_STATE_BLOCK` | 哪些情绪周期禁止开仓（如 `退潮,冰点,空仓`） | 对应 `market_state`；空仓闸门（闭环归因文档 §5） |
| `QMT_KILL_SWITCH` | 全局熔断开关：置 true 则只采集、不下单 | 实盘小仓最高优先级安全阀 |

> A 股交易制度硬约束（只做主板 + 创业板、排除 ST / 科创 / 北交、T+1、主板 ±10% / 创业板 ±20%）在信号侧 `limit_up_selected_stock` 选股阶段已过滤；执行侧下单前**再做一次本地复核**（前缀白名单代码段见 §2.3、ST 名单、是否当日可买），不依赖单侧。

#### 7.1.6 竞价择时总开关（实测前必须关）

| 配置项 | 含义 | 默认 |
| --- | --- | --- |
| `QMT_AUCTION_TIMING_ENABLED` | 是否启用「竞价择时」（按 7.1.4 阈值在 9:15–9:25 据竞价撮合价 / 竞买竞卖量动态决定挂单价与是否参与） | **默认 false** |

> **强约束**：在 7.2「竞价数据能力真实交易日实测」通过之前，`QMT_AUCTION_TIMING_ENABLED` 必须为 false。原因：竞价择时强依赖 `get_full_tick` 在 9:15–9:25 是否真的返回竞价撮合价与竞买 / 竞卖量、是否随撮合刷新、9:25 能否拿到最终竞价——这是**未经实测的能力假设**。未实测即开启，等于把不确定的数据能力直接放进实盘下单决策。实测通过前，执行侧只用「信号侧已给定的竞价观察清单文本口径 + 人工 / 固定价挂单」，竞价数据仅**采集留痕**、不参与下单决策。

### 7.2 竞价数据能力【真实交易日实测】清单

目的：在接实盘前，于真实交易日盘前竞价时段实测 QMT `xtdata.get_full_tick`（及必要的订阅接口）到底能拿到什么，逐条出「能 / 不能 + 字段名 + 刷新行为」的结论。**只在真实交易日 9:15–9:25 实测，仿真 / 历史回放不算数**（竞价撮合是实时行为，回放不还原撮合刷新）。实测仅采集、绝不下单（`QMT_KILL_SWITCH=true`）。

| # | 实测项 | 通过判据（须逐条记录实测结果，非假设） |
| --- | --- | --- |
| A1 | 9:15–9:25 调 `get_full_tick` 是否返回**竞价撮合价**（虚拟开盘参考价 / 集合竞价匹配价） | 返回非空且数值合理（落在前收 ±涨跌停内）；记录实际字段名（如 `lastPrice` / `askPrice[0]` / 厂商自定义字段，以 `vars` / `dir` 实测为准） |
| A2 | 是否返回**竞买量 / 竞卖量**（未匹配买 / 卖挂单量、匹配量） | 字段存在且非零；记录字段名与含义（匹配量 vs 未匹配量须分清） |
| A3 | 竞价数据是否**随撮合刷新**（每轮轮询值有变化） | 在 `QMT_AUCTION_POLL_INTERVAL_SEC` 轮询下，连续多轮取值随竞价进程变化（9:20 后未匹配量缩减、参考价收敛），而非冻结同一值 |
| A4 | **9:25 能否拿到最终竞价**（确定的集合竞价成交价 = 当日开盘价） | 9:25:00 之后某轮能取到稳定的最终竞价价与最终匹配量，且与盘后开盘价一致 |
| A5 | 9:15–9:20 撤单期 vs 9:20–9:25 不可撤期数据是否可区分 | 能从竞买 / 竞卖量变化合理反映两阶段差异（用于判断「虚假竞价」） |
| A6 | 字段版本差异核验 | 在目标 Windows 机用 `vars(tick)` / `dir(tick)` 实测当前安装版本字段，落 README / 实测记录，避免 `AttributeError`（与 qmt-trade-review-design.md 第 0、7 节「字段版本差异必须实测」口径一致） |

实测产出：一份「竞价数据能力实测记录」（字段名映射表 + 各项通过 / 不通过结论 + 截图 / 原始 tick 样本）。

实测结论分支：
- **全部通过** → 竞价择时具备数据基础，方可考虑在小仓阶段开启 `QMT_AUCTION_TIMING_ENABLED`（仍建议先灰度）。
- **A1 / A2 / A4 任一不通过** → 竞价择时数据基础不成立，`QMT_AUCTION_TIMING_ENABLED` 保持 false，执行侧改用「信号侧竞价观察清单文本 + 固定挂单价」策略；竞价数据继续采集留痕供后续复盘验证。

### 7.3 模拟盘 / 纸上交易测试

在接实盘小仓前，用**信号侧已验证规则**（已通过回测 go/no-go 的 `limit_up_selected_stock` 候选与分层口径）跑模拟盘 / 纸上交易，核心是量化「计划 vs 实际可成交」差异，而非验证策略本身（策略对错由信号侧回测负责）。

#### 7.3.1 测试方式

- **优先纸上交易（paper trading）**：执行侧每个交易日按信号侧候选生成「本地计划单台账」（`ts_code` / 方向 / 计划量 / 计划价 / 信号来源 / 下单时刻，复用 qmt-trade-review-design.md 第 3.3 节「本地下单台账」结构），但**不真实下单**；用真实行情（竞价数据 + 盘中 `realtime_quote_snapshot` / `get_full_tick`）判定「若按计划挂这个价，是否能成交、成交在什么价」。
- **可选 QMT 仿真账户**：若国金提供仿真交易账户，用仿真账户真实跑 `order_stock`，验证回调 / 委托 / 成交链路与 `qmt_*` 落表全流程（此时仿真数据须用独立 `account_id` 隔离，绝不混入实盘账户复盘，见 7.4 账户隔离）。

#### 7.3.2 要量化的核心差异（复用闭环归因口径，引用不重写）

> 以下指标与 `resources/doc/qmt-trade-review-closed-loop-attribution-design.md` 第 2 节（计划 vs 实际漏斗）、第 2.2 节（逆向选择量化）完全同口径，模拟盘阶段提前跑，目的是在真金白银前就看清「计划与可成交的差距有多大、漏掉的是不是最强的票」：

- **计划 vs 实际可成交漏斗**：N 重点候选 → M 计划下单 → K 模拟可成交（纸上判定能买进），算下单率 M/N、成交率 K/M、整体兑现率 K/N、买不进比例 (N−K)/N（详见闭环归因文档 §2.1）。
- **买不进原因分布**：未下单 / 一字未成 / 秒封未成 / 排队未成（用竞价数据 + 买入日行情形态判定，口径见闭环归因文档 §2.1、§9）。
- **量化逆向选择**：买进组 vs 买不进组的 `leader_strength_score` 分位差、Top 强度命中率、错失最强单、两组次日盯市收益对比（详见闭环归因文档 §2.2）——若模拟盘就显示「最强的票系统性买不进」，说明实盘兑现会显著低于回测，须在开实盘前先解决（调挂单价策略 / 接受兑现率打折）。
- **滑点预估**：纸上判定的可成交价 vs 信号侧理论涨停价 / 合理高开区间（口径见闭环归因文档 §4），提前看实盘追高代价量级。

#### 7.3.3 模拟盘验收

- 漏斗 N/M/K 可逐日产出且与当日候选 / 行情手工核对一致。
- 逆向选择卡片在「构造的最强票一字买不进」样例上给出正向信号（与闭环归因文档 §8 P1 验收一致）。
- 模拟盘整体兑现率与逆向选择程度达到「可接受接实盘小仓」的门槛（门槛值由人工评审固化，不在本文给死值）。

### 7.4 上线 checklist（接实盘小仓前逐项打勾，全绿才上）

> 多数项的实现细节已在 qmt-trade-review-design.md 定稿，此处只做「上线前必须确认」的清单化收口，括号内标注引用出处。

- [ ] **账户隔离**：实盘账户与仿真 / 模拟账户用不同 `account_id`，`qmt_*` 表与复盘看板按 `account_id` 物理区分，仿真数据绝不混入实盘净值 / 收益统计；执行侧 MySQL 账号仅对 `qmt_*` 四表有写权限、不碰其他业务表（详见 qmt-trade-review-design.md 第 3.1 节）。
- [ ] **幂等**：所有写入走唯一键 upsert（明细表唯一键按 §6.5 xtquant 实测结论选用现行 `(account_id, traded_id)` / `(account_id, order_id)` 或纳入 `trade_date` 的加固键；快照按 `(account_id, trade_date, ts_code/—, snapshot_type)`），重跑 / 补采 / 重连补采均不产生重复行（详见 qmt-trade-review-design.md 第 2 节 DDL 唯一键、第 3.2 节补采）。
- [ ] **撤单**：撤单路径可用且 `on_cancel_error` 留痕（`qmt_order.cancel_failed` / `error_msg`）；竞价 / 盘中超时未成交的计划单有明确撤单或转限价策略，不留悬挂未知态（详见 qmt-trade-review-design.md 第 1.1 节回调表）。
- [ ] **断线重连**：`on_disconnected` 触发后重建 session（换 `session_id`）→ `connect`（返回 0 判成功，注意 `on_connected` 不存在）→ `subscribe`，并立即 `query_*` 全量补采当日（`data_source=QUERY_BACKFILL`）；进程 `run_forever()` 常驻 + Windows 守护 / 任务计划 + 每日开盘前主动重连一次（详见 qmt-trade-review-design.md 第 0、3.2 节）。
- [ ] **安全默认**：首次上线 `QMT_KILL_SWITCH` 可一键熔断；`QMT_AUCTION_TIMING_ENABLED` 默认 false（7.2 未通过不开）；风控阈值（单票 / 总仓 / 单笔 / 单日笔数 / 价偏离 / 情绪周期禁开仓）全部生效且取保守值；执行侧下单前本地复核 A 股交易制度硬约束（主板 + 创业板、排除 ST / 科创 / 北交、当日可买性）。
- [ ] **对账**：本地计划单台账 vs `qmt_order` / `qmt_trade` 委托 / 成交 / 资产对账可跑、偏差告警；`qmt_order.traded_volume` 与该委托下 `qmt_trade` 成交量之和勾稽一致（详见 qmt-trade-review-design.md 第 3.3 节）。
- [ ] **收盘快照**：当日至少成功落一次 `CLOSE` 资产 / 持仓快照（净值 / 持仓历史唯一来源，隔日不可补，详见 qmt-trade-review-design.md 第 1.2 节）。
- [ ] **时间口径**：`traded_time` / `order_time` 按「东八区 → UTC naive」入库 + `_east8` 原值留底，`trade_date` 取东八区交易日，前端 `formatEast8DateTime` 展示，无任何手工 ±8h（详见 qmt-trade-review-design.md 第 4 节）。
- [ ] **回滚**：可一键停下单（`QMT_KILL_SWITCH`）退回「只采集不交易」；信号侧推送侧若涉及可经 `LIMIT_UP_PUSH_CONTENT_MODE=REPORT` 回滚（见信号侧文档）；执行侧出问题时回流采集与实盘下单解耦、互不阻塞（采集继续、下单暂停）。

### 7.5 本节实施次序（强约束，详见末章「与信号侧的依赖与上线次序」）

执行侧接实盘必须是「信号侧已被验证 + QMT 能力已被实测」之后的事，次序不可颠倒。完整次序图与依赖说明见文末「与信号侧的依赖与上线次序」一章，本节不重复展开。核心硬约束：**先 go/no-go + 竞价实测，后实盘**；**先小仓 + 熔断 + 保守风控，后放量**；竞价择时**先关，实测通过再灰度开**。

### 7.6 本节验收标准

- **配置**：7.1 各类配置项在执行侧配置文件 / 信号侧 `config.py`（按 `Field(alias=...)` 风格 + 中文注释）落地齐全；敏感项（账户 / token / DSN）不入库、不进日志、不硬编码；`QMT_AUCTION_TIMING_ENABLED` 默认 false 且实测前不可被误开（有保护 / 校验）。
- **竞价实测**：7.2 A1–A6 逐条产出「能 / 不能 + 字段名 + 刷新行为」实测记录（真实交易日 9:15–9:25），并据此给出竞价择时 go/no-go 结论；字段以目标机实测版本为准、无 `AttributeError` 风险。
- **模拟盘**：7.3 计划 vs 实际可成交漏斗、买不进原因分布、逆向选择量化、滑点预估均可逐日产出且与行情手工核对一致；逆向选择在构造样例上给出正向信号。
- **上线 checklist**：7.4 九项全部可逐条验证通过（账户隔离 / 幂等 / 撤单 / 断线重连 / 安全默认 / 对账 / 收盘快照 / 时间口径 / 回滚）。
- **实施次序**：文末「与信号侧的依赖与上线次序」的「前置就位 + 信号侧 go/no-go 通过 + QMT 竞价实测通过 → 实盘小仓 → 放量」次序被流程 / 评审强制执行，存在可追溯的 go/no-go 决策记录，无跳步上线。

> 衔接说明：本节不重复 QMT 四表 DDL、复盘看板指标、闭环归因细节，相关内容分别见 `resources/doc/qmt-trade-review-design.md`、`resources/doc/qmt-trade-review-board-design.md`、`resources/doc/qmt-trade-review-closed-loop-attribution-design.md`；信号侧候选 / 分层 / 竞价观察清单口径见 `resources/doc/limit-up-multi-stage-analysis-refactor-plan.md` 与 `resources/doc/limit-up-push-investment-advice-refactor-plan.md`。

---

## 与信号侧的依赖与上线次序

本章统一收口执行侧与信号侧的依赖关系与上线先后，作为评审与上线流程的强约束基线。

### 跨模块依赖总览（信号侧 → 执行侧）

| 信号侧前置（本仓库 / Linux） | 执行侧消费方 | 关系 |
| --- | --- | --- |
| `limit_up_selected_stock` 表（DDL / ORM / Alembic 迁移 / `03_full_schema_with_comments.sql` / `database-schema.md` 四处同步，含 `continuation_prob` / `boost` / `fail_conditions` / `market_state` / `role` / `strategy` / `tradable_flag` / `signal_close` / `limit_up_price` / `reasonable_open_high_*`） | watchlist_loader（第二节）、auction_poller（第三节）、entry_router（第四节）、sell_decider（第五节） | 唯一契约通道，执行侧只读消费不建表 |
| `ensure_analysis_for_trade_date` READY 收口段落 `limit_up_selected_stock`（与 `status=READY` 同事务、delete-then-insert latest-wins、两条 READY 早退守卫，见 §5.5） | 同上 | 决定执行侧当日能否拿到完整且最新的候选 |
| `a_trade_calendar`（已存在物理表） | watchlist_loader / position_manager 的 T → T+1 → 可卖日映射、data_writer 的 `signal_trade_date` 反推 | 所有日推算经此表，禁自然日 ±1 |
| `stock_identity_resolver.resolve_code` | 全链路代码归一 `norm_code` | 信号-执行-结果三方 join 代码键 |
| `qmt_*` 四表 DDL / 唯一键 / 时间口径（`qmt-trade-review-design.md` 第 2/3/4 节，唯一键跨日加固见 §6.5） | data_writer / reconcile（第六节） | 执行侧按既定 DDL 写入，不重定义 |
| 信号侧回测 go/no-go 结论 | 执行侧实盘开仓 | 无 go 不接实盘 |

### Alembic 迁移落地次序（信号侧）

`limit_up_selected_stock` 及相关 `qmt_*` 加固迁移须接在当前唯一 head `20260612_0049` 之后：`down_revision="20260612_0049"`，多个新迁移 `20260613_0050 → _0051 → …` 顺次串联，**不得并列同一 down_revision**（否则 alembic 多头、`upgrade head` 报错）；`revision` / `down_revision` 用完整 `YYYYMMDD_XXXX` 串，MySQL 大文本沿用 `sa.Text().with_variant(mysql.LONGTEXT(), "mysql")`，`upgrade` / `downgrade` 补中文注释。

### 上线次序（不可颠倒的硬约束）

```
前置就位（外部依赖，不在本系统代码内）
  └─ 国金账户权限开通 + 分钟级行情权限 + Windows VPS 到位
        │
        ▼
信号侧回测 go/no-go 通过 ──┐
                          ├──（两个 AND 条件，缺一不可）──▶ 接实盘【小仓】试错
QMT 竞价能力实测通过(7.2) ──┘                                 │
                                                            ▼
                                                  小仓灰度 → 逐步放量（按复盘/归因结论）
```

实施次序与依赖逐条：

1. **前置（外部到位）**：国金账户权限 / 分钟级行情权限 / Windows VPS 三者到位。这是一切的硬前置——无账户无法下单、无分钟权限竞价数据可能受限、无 VPS 无 `xttrader` 运行环境。此前置未就位时，执行侧只能做「纸上交易 + 竞价采集留痕」。
2. **依赖 A — 信号侧回测 go/no-go 通过**：`limit_up_selected_stock` 候选与分层规则经回测验证、给出明确 go 结论（回测对账口径见闭环归因文档 §9：不复权、一字 / 秒封不计、T → T+1 交易日历映射）。**no-go 则不接实盘**。
3. **依赖 B — QMT 竞价能力实测通过（7.2）**：竞价数据能力实测全绿，否则 `QMT_AUCTION_TIMING_ENABLED` 保持 false（但若信号侧 go 且采用「固定价 / 人工挂单 + 不依赖竞价择时」，仍可接实盘小仓，只是不开竞价择时）。
4. **A AND B 满足 → 接实盘小仓**：先小仓试错，开 `QMT_KILL_SWITCH` 可熔断、风控阈值保守、上线 checklist（7.4）全绿。
5. **小仓阶段持续跑闭环归因（引用三篇 QMT 文档）**：用实盘数据验证「计划 vs 实际、逆向选择、先验校准、滑点、空仓闸门」，据归因结论决定是否放量、是否开竞价择时。

> 顺序硬约束：**先 go/no-go + 竞价实测，后实盘**；**先小仓 + 熔断 + 保守风控，后放量**；竞价择时**先关，实测通过再灰度开**。任何一步跳过都视为违反上线流程，须留可追溯的 go/no-go 决策记录。

### 模块内部依赖顺序（执行侧，可并行项已标注）

1. P0：`qmt_*` 四表回流口径 + `limit_up_selected_stock` 契约就绪。
2. P1（可并行）：连接守护（§2.2）、watchlist_loader（§2.3–2.6）、data_writer + callbacks + time_utils（§6.1–6.6）。
3. P2：auction_poller（第三节）依赖 `xtdata` 与契约；snapshot_job + 断线补采（§6.2.2–6.2.3）依赖 P1。
4. P3：entry_router → order_executor（第四节）依赖 P1 + auction_poller；reconcile + local_order_ledger（§6.7–6.8）依赖 P1。
5. P4：position_manager → risk → sell_decider（第五节）依赖 `qmt_*` 回流 + 先验表；`risk` 先于 `sell_decider`（决策前必过闸门）。
6. P5：配置、模拟盘、上线 checklist（第七节）依赖上述全部就绪后才进入实盘灰度。

## 待确认项

以下为评审 / 落地前须显式拍板或实测确认的开放项，每条标注归属与影响面：

1. **watchlist 契约读取方式（A 直读表 / B 只读接口）二选一固化**（§1.2.1）：优先 A；若 MySQL 端口对 Windows 暴露不可接受退 B。影响 watchlist_loader 取数路径与运维安全面。

2. **`qmt_*` 明细表唯一键是否纳入 `trade_date`**（§6.5）：现行 design DDL 唯一键为 `(account_id, traded_id)` / `(account_id, order_id)`，**不含** `trade_date`。须先在目标机用 `vars(obj)` / `dir(obj)` 实测 xtquant 的 `order_id` / `traded_id` 跨日唯一性：实测确认跨日唯一则沿用现行键；无法确认（更稳妥假设）则改为 `(account_id, trade_date, traded_id)` / `(account_id, trade_date, order_id)` 加固，并保证 `trade_date` 由权威来源稳定派生（否则破坏「同委托 upsert 覆盖终态」幂等）。须同步 Alembic 迁移 / `03_full_schema_with_comments.sql` / `database-schema.md`。

3. **快照表统一命名**（§1.3）：`qmt_position_snapshot` / `qmt_account_daily`（design 命名）vs `qmt_asset_daily_snapshot` / `qmt_position_daily_snapshot`（board 早期命名）。落地以信号侧最终 Alembic 迁移为准，统一采用 design 命名并同步两份 schema 文档。

4. **竞价数据能力真实交易日实测结论**（§3.7、§7.2）：A1–A6 逐条「能 / 不能 + 字段名 + 刷新行为」须在真实交易日 9:15–9:25 实测（仿真 / 回放不算）。A1 / A2 / A4 任一不通过则 `QMT_AUCTION_TIMING_ENABLED` 保持 false，竞价择时降级为固定价 / 人工挂单。

5. **`first_board_vol`（首板放量基准）字段来源**（§3.4 因子 2）：信号侧 `limit_up_selected_stock` 是否落该字段；未落则执行侧降级用近 N 日均量或首板日 `tencent_unadjusted_daily_quote.vol`，口径须在落地文档固化。

6. **xtquant 字段集与版本兼容**（§1.4、§6.3）：`XtTrade` / `XtOrder` / `XtAsset` / `XtPosition` / tick 的实际字段名与可空性须在目标 Windows 机实测固化（账号类型枚举、`avg_price` / `frozen_volume` 等可选字段、竞价 tick 键名），并写入部署 README，避免 `AttributeError` / `KeyError`。

7. **回测「全市场涨停池对照组」数据来源**（信号侧前置，影响 go/no-go）：当前涨停 / 连板数据每日直连第三方接口、仅以 JSON 快照存于 `limit_up_analysis_cache.context_json`，无物理表。对照组来源须二选一并处理一致性：①新增持久化同步——**主源是 `kpl_list`（`KPL_REQUIRED_API`）而非 `limit_list_d`**（后者仅 `OPTIONAL_APIS`、可能无权限），若采用 `limit_list_d` 须明确与 kpl 涨停名单的口径差异（数量 / 字段 / 过滤 / 连板统计字段）并固化对齐规则；②从 `context_json.pipeline / limit_up_stocks` 抽取——须处理字段版本漂移（compact_row schema 2026-06-10 才定型，更早快照可能缺字段）、同一 `trade_date` 多行须按唯一键（`trade_date + model + prompt_version + data_snapshot_hash`）选 `status=READY` 且最新 `generated_at` 的权威行，且只有曾生成报告的交易日才有快照（历史缺口需用交易日历比对回到第三方接口补采）。涨停池上限为 `LIMIT_UP_CONTEXT_STOCK_LIMIT=360`（非「约 88 只」）。

8. **回测历史 universe 的 ST 判定**（§2.3 第 3 步）：本节 live 路径沿用「按当日实时行名称含 ST / 退市标识」判定即可（point-in-time 正确，无需 `a_stock_st`）；但**回测历史 universe** 须按信号日 T 的当日 ST 状态判定（避免用最新名称引入幸存者偏差 / 未来函数）。当前项目无 `a_stock_st` 本地表，若做回测对照需新增 DatasetSpec（如 `stock_st` 按 `trade_date` 取每日 ST 名单或 `namechange`），并补「退市整理（退 / *退）」覆盖。此项归信号侧回测路径，不阻塞执行侧 live 上线。

9. **`order_remark` 长度上限与格式**（§4.4(5)、§6.8）：QMT `order_remark` 长度限制须实测确认（≤255 对齐 `qmt_order.order_remark VARCHAR(255)`），固定格式 `LUP|<T>|<ts_code>`，超长截断须优先保 `signal_trade_date` 段。
