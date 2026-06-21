# 打板量化项目 · 总览

> A 股打板/涨停量化交易闭环。**信号侧负责「看得准」，执行侧负责「买得到、守得住、可复盘」**，
> 两侧物理隔离、通过 HTTP 解耦，回流数据闭合成可归因的环。
> 本文是整个项目（信号侧 + 执行侧）的单一入口，只记**现状口径**（交易逻辑 / 架构 / 部署设计 / 资金口径 / 环境变量与配置）；
> 还要做什么见 [`待办与上线验证清单.md`](待办与上线验证清单.md)，评审与修复状态见 [`评审与修复状态概要.md`](评审与修复状态概要.md)，
> 生产部署/运维见本机运维手册 `/Users/salty/codeProject/ai/doc/Qmt生产服务器.txt`；历史开发设计/计划/评审已移入工作区级归档 `/Users/salty/codeProject/ai/doc/已归档-完成不再维护/`（不再看）。

---

## 1. 一句话架构

```
 信号侧 stock-ah-premium-ai (Linux/FastAPI/MySQL)        执行侧 astock-quant-ai/qmt_strategy (Windows/单进程)
 ┌───────────────────────────────────────────┐        ┌──────────────────────────────────────────┐
 │ LLM 多阶段选股 → 龙头增强 → 回测            │        │ 盘前 GET watchlist → 本机 SQLite           │
 │  → limit_up_selected_stock(当日 watchlist) │──盘前拉──▶│ 盘中：自采行情 + 自主择时/价位/仓位 → 下单 │
 │                                            │  GET ① │      持仓/卖出/风控（只读本机，不跨网络）   │
 │ qmt_* 四表 → 复盘看板 / 闭环归因 / 先验校准 │◀─盘后推──│ 盘后 POST 回流(qmt_*) ← 本机 SQLite        │
 └───────────────────────────────────────────┘ POST ② └──────────────────────────────────────────┘
        两接口同属信号侧 /api/internal/*，X-Internal-Token 鉴权；执行侧是「一拉一推」的唯一发起方
```

**核心边界**：信号侧给的 watchlist 是「关注什么」的只读契约（强度/角色/战法/情绪周期/先验/参考价位），
**绝不含**仓位、买卖指令、止盈止损价、实时盘口——这些 100% 由执行侧自决，风险也归执行侧。

---

## 2. 信号侧 `stock-ah-premium-ai`（Linux）

**做什么**：每个信号日 T 产出「今天该关注哪些票」的结构化候选清单，并消费执行侧回流做复盘。

- **多阶段选股 pipeline**：首板精选 → 两/三连接力筛选 → 高连板筛选 →（各阶段 FOCUS 重点分析）→ 投资建议 → 合成报告 → **READY 收口**。
- **龙头增强（M2）**：六维打分卡（封板/题材/高度/资金/位置筹码/辨识度）→ `leader_strength_score` + `role`；READY 同事务落 `limit_up_selected_stock`。
- **先验结构化**：FOCUS 改 JSON-mode 产出 `continuation_prob`（续板）、`next_day_premium_prob`（隔日溢价）。
- **回测**：历史样本 T→T+1 映射回测，给闭环归因对账（go/no-go）。
- **复盘/归因**：只读执行侧回流的 `qmt_*` 四表 → 净值看板 + 信号×执行×结果闭环归因 + 先验校准。
- **技术栈**：FastAPI + React + MySQL `stock_ah_ai`（SQLAlchemy/Alembic）+ APScheduler（东八区定时）+ DeepSeek LLM + Tushare 行情。

## 3. 执行侧 `astock-quant-ai/qmt_strategy`（Windows + miniQMT）

**做什么**：消费 watchlist + 自采实时行情，自主决定打不打/几点打/什么价/多少仓/何时卖，独立下单并承担全部盈亏。

- **交易闭环模块**：连接守护（xttrader 生命周期）· 集合竞价四因子（高开/量能/重心/虚拟封单）· 建仓五策略（打板跟买/竞价强开追/低吸/龙回头/放弃）· 下单执行（唯一下单点+本地台账）· 持仓状态机+卖出/连板决策 · 风控护栏 · 回流落库 · 对账。
- **本地化（doc/05）**：**单进程 + 本机 SQLite，异步持久化（write-behind 绝不阻塞交易）**；盘前名单入库、盘中内存权威、盘后幂等同步回远端。
- **调度（`DailyScheduler`）**：东八区钟点触发 盘前装载 → 竞价 → 盘中 sweep/卖出 → 收盘对账 → 盘后同步。
- **适配器（`adapters/xt_real.py`）**：把 xtquant 翻译成 `Protocol`（**唯一 import xtquant 处**，仅 Windows 运行）；非 Windows 用 fake 跑全部单测。
- **建仓时点口径**：建仓决策由**集合竞价 + 定盘窗（9:15–9:30）的因子轮询**驱动（`AuctionPoller`，9:30 退出）；竞价段（强开追/竞价卖）默认只采集（竞价择时实测前关），**定盘窗（9:25–9:30）对竞价封板/强开票产出 `OPENING` 限价单挂出、待开盘成交**（TTL 顺延至开盘起算）。**当前不做开盘后连续交易段（9:30+）的盘中买入轮询**——即只打「竞价/一字封板」一类，盘中(如 10:30)才封板的票不追（有意设计：竞价定夺 + 挂 OPENING 单待成交）；卖出（止盈止损/炸板/尾盘）则覆盖盘中（由 `QMT_SELL_PASS_LIVE` 门控）。
- **买入前置过滤层（绝不买入 ST / 四板及以上）**：执行侧把所有「禁买」硬规则收敛为统一的**买入前置过滤层**（`common/buy_prefilter.py`：有序规则集 + 结构化裁决，单一来源），三层闸叠加贯彻——盘前 watchlist 装载剔出可交易名单 + 建仓路由 SKIP + 唯一下单点最终拒单：
  - **绝不买入 ST**：ST/*ST/退市整理标的一律不买入；判定 = 信号侧显式 `is_st` 为真 **或** 当日证券名含 `ST/退`（`is_st` 经 HTTP→SQLite→PlanRow 可靠透传，不再单点押在 name 上）。
  - **禁买四板及以上**：连板高度 `board_level >= 4`（阈值 `QMT_FORBID_BOARD_LEVEL_MIN`，默认 4），或信号侧分层 `tier==HIGH_BOARD` 兜底（`board_level` 缺失时按 4+ 板 fail-closed 保守拦）；`board_level`/`tier` 经 HTTP→SQLite→PlanRow→EntryDecision 与 `is_st` 同路透传（设计见工作区归档 doc/18）。

**贯穿全局的硬口径**：守 T+1 双保险 · 三层幂等（业务单号+DB唯一键+traded_id）· 时间口径无 ±8h（东八区↔UTC naive）· 安全默认「契约残缺/中断→只守仓不开新仓」· **买入前置过滤层：绝不买入 ST + 禁买四板及以上（三层闸）** · `KILL_SWITCH` 一键熔断 · 竞价择时实测前关。

---

## 4. 两侧数据交互（HTTP · 历史方案详见工作区归档 `07-提供侧执行侧HTTP数据交互-方案与说明.md`）

执行侧是两条链路的唯一发起方，信号侧托管两接口，统一 `X-Internal-Token` 鉴权：

| # | 接口 | 时点 | 口径 |
|---|---|---|---|
| ① | `GET /api/internal/watchlist?date=T` | 盘前 | 执行侧按 `prev_open(today)=T` 拉当日名单 → 落本机 SQLite；空集返 200（不 404）；失败降级「只守仓」 |
| ② | `POST /api/internal/qmt/ingest` | 盘后 | 逐行幂等回流 `qmt_*` 四表（加固唯一键含 `trade_date`，`signal_trade_date`/`*_east8` 走 COALESCE）；失败保 `synced=0` 下轮重试 |

**收益**：Windows 交易机**零入站端口**（只出站）；**盘中完全不依赖跨服务器网络**；两侧 JSON 契约解耦、互不感知表结构。

---

## 5. 部署（两台服务器）

- **信号侧（Linux）**：FastAPI + MySQL + LLM 常驻；配 `X-Internal-Token` + 跑 Alembic 迁移；`/api/internal/*` 加来源 IP 白名单。
- **执行侧（Windows Server 2022）**：4 核 / 8GB / **≥60GB SSD**（≥100GB 省心）、国内低延迟到券商；miniQMT 常驻登录 + 程序化交易/分钟级行情权限。
  - **已就位**：Windows Server 2022(64位/C盘~160G空闲) · Python 3.11.9(+`tzdata`) · Git · SQLite(Python 自带) · OpenSSH(免密钥) · 项目代码 + venv，全量测试通过。
  - **待办**：miniQMT/xtquant 到位 → 核对 xtquant 的 Python 版本 → 填 `xt_real.py` 的 `TODO(实测)` → 配 `QMT_*` + 任务计划保活 → `python -m qmt_strategy.app.run`（先开 `KILL_SWITCH`、竞价择时保持关）。
  - 上线步骤/验证见 [`待办与上线验证清单.md`](待办与上线验证清单.md)；**完整生产部署/重启/token/计划任务/连通性自检见本机运维手册** `/Users/salty/codeProject/ai/doc/Qmt生产服务器.txt`；历史详细运维清单见工作区归档 `07-QMT执行侧服务器部署运维清单.md`。

**上线硬次序**（不可颠倒）：信号侧回测 go/no-go ✅ + QMT 竞价能力实测 ✅ →（两 AND）接实盘**小仓**（熔断+保守风控+checklist 全绿）→ 小仓灰度 → 逐步放量；竞价择时**先关，实测通过再灰度开**。

---

## 6. 当前状态

| 部分 | 状态 |
|---|---|
| 执行引擎（连接/竞价/建仓/持仓/风控/回流/对账） | ✅ 已落地；**2026-06 正式评审发现并修复多处致命断裂**（路由全 SKIP 不开仓 / 持仓不回写不卖出 / 买入绕过风控 / go-no-go 闸门缺失等），见 [评审与修复状态概要](评审与修复状态概要.md) |
| 盘中卖出（止损/炸板/破位/连板了结/T+1 常规卖出） | 🟡 **接线代码已落地（2026-06 阶段0-C）+ 生产门控默认关**：`sell_book_builder` + `Engine.build_sell_books`（实时盘口→`OrderBook`）+ `prior_provider` 续板先验 + `DailyScheduler` 注入均已落地并有 fake 单测；**评审 doc/19 已补 C-2（`decide_auction` 竞价段读 `is_sealed`、正封涨停弱先验票不再被误判弱开清仓）+ C-3（`build_order_book` 跨帧字段 `cross_frame_builder` 接线扩展点 + `set_sell_cross_frame_builder` 注入点）**。但**生产卖出仍由 `QMT_SELL_PASS_LIVE` 默认关门控**（=provider=None、盘中不自动卖，回退接线前安全行为）——剩余=跨帧字段**数值保真**未做、不在今日 watchlist 的真封板隔夜票仍会被误判（B1）。**待阶段1 T1.2 跨帧数值保真 + 隔夜持仓补数 + 真机实测后才开 `QMT_SELL_PASS_LIVE`**（开时强制配单票浮亏止损）。详见 [待办 §0/§A](待办与上线验证清单.md)、[状态概要](评审与修复状态概要.md)（详细开发计划/台账见工作区归档 doc/16、doc/20） |
| 执行侧本地化（单进程+SQLite 异步持久化） | ✅ 已落地；评审修复发单前同步落盘等幂等缺口（见状态概要） |
| 调度器 + xtquant 适配器模板 + 真实入口骨架 | ✅ 已落地（待目标机填 `TODO(实测)`）；交易日历已改 fail-closed（部署须知见 [待办 §B](待办与上线验证清单.md)） |
| 信号侧 HTTP 双接口 + `qmt_*` 表/迁移 + 客户端 | ✅ 已落地；评审补回流信封一致校验、watchlist 竞价两因子供数（见状态概要） |
| 信号侧生产部署 + 内网 token + 复盘看板 | ✅ 已上线（2026-06-14 首部署；2026-06-18 增量同步：信号侧代码/迁移=最新(0057)、**ingest 写 token 已补配（两接口全开）**；执行侧 Windows 已 ff 到 main 最新 `21efdcb`+**风控两闸已配**，仍仅差 miniQMT，见 [待办 §G](待办与上线验证清单.md)） |
| 实盘对接（miniQMT/xtquant + 调度任务计划 + 实测） | 🟡 **2026-06-18 晚 模拟上线配置完成（交易通道保持关）**：miniQMT 已装+登录（`userdata_mini=C:\qmtrun\userdata_mini` 纯英文）、`xtquant-250516.1.1` 接入 venv、`run_qmt.bat` 已填 `QMT_ACCOUNT_ID`/`QMT_MINI_PATH`；实测（只读零下单）xtdata 取真实快照 + xttrader connect rc=0/账户确认 + `build_real_engine` collect-only 装配 OK。**剩**：交易日手动 collect-only 实地观察 → 计划任务注册 → §A 真机固化 → go/no-go 后才开 `KILL_SWITCH`。详见 [待办 §G](待办与上线验证清单.md) + 运维手册 §F |

代码仓库：执行侧 [github.com/WinSalty/astock-quant-ai](https://github.com/WinSalty/astock-quant-ai)。

> ⚠️ **上线前红线（已从「代码缺失」收口为「生产门控」）：盘中卖出链接线代码已落地（阶段0-C，见上表「盘中卖出」行），但生产门控 `QMT_SELL_PASS_LIVE` 默认关、盘中不自动卖；开门控前（须 T1.2 跨帧数值保真 + 真机实测 + 配单票浮亏止损 `QMT_STOCK_FLOAT_LOSS_LIMIT`）买入即裸奔无止损出口，严禁放量实盘。** 部署侧务必先看 [`待办 §B 上线部署步骤`](待办与上线验证清单.md)——含信号侧跑迁移、执行侧提供交易日清单文件（J-3 起执行侧盘前会自动经 `/api/internal/trade_calendar` 校验/补取本地日历，不足且补取失败则 fail-closed 只守仓不开新仓）、以及内网 token：**doc/29 J-8 起通用 token 打通所有接口**——只配一套 `WATCHLIST_EXPORT_INTERNAL_TOKEN(_FILE)` 即打通名单/日历/回流三接口（`QMT_INGEST_INTERNAL_TOKEN` 未单独配时回落该通用 token，消除漏配致 `/ingest` 恒 503 断流的陷阱；需读写隔离再单配）；评审与修复全貌见 [评审与修复状态概要](评审与修复状态概要.md)。

---

## 7. 资金分配口径（强度加权 + 单日建仓只数上限）

有限资金 + 多只候选时，**先按强度优选 top-N 只进入买入名额，再在这 N 只内按 `leader_strength_score` 强度加权分配**（强的分得多、不被弱的抢光、名额内满额部署不闲置）：

- **单日建仓只数上限**（`QMT_MAX_POSITIONS_PER_DAY`，**默认 5**）：当日最多买入 5 只不同标的。盘前按强度降序取 **top-5** 进入买入 universe，权重 `w_i = 强度_i / Σ(top-5 强度)` 在这 5 只内归一（**Σw=1 → 总预算上限满额部署到 top-5，不摊到买不到的弱票上而闲置现金**）；其余候选预算为 0、不开仓。下单层再叠加硬闸（占名额口径：在途/部成/已成占名额，终态零成交=买不进不占、释放名额）作双保险。设极大值可放宽不限只数。
- 每只票下单时计划金额 = `min( 可用现金, 总预算上限 − 已承诺, 总预算上限×w, 单笔上限, 单票上限 )`，再 ÷ 限价向下取整到 100 股；算出不足 1 手则不下单。权重盘前一次性算定、**与触发时序无关**——弱票即使买点先达标，也只吃自己的小份额，强票份额被保留。
- **总预算上限**（ceiling）= 日初权益 × `QMT_TARGET_POSITION_RATIO`（默认 `1.0`=用全部日初权益；调小可留现金，如 `0.8`），并与 `QMT_MAX_TOTAL_EXPOSURE`（绝对总敞口，可选）取小。
- **已承诺** = 当日活跃买单（在途+已成）金额之和，下单前从预算扣减 → 避免券商 `frozen_cash` 未及时刷新时多单各按全额现金测算而**超额废单**。
- 持仓继续持有（昨买涨停今不卖）的资金已是 `market_value` 占用、不在可用现金里；不卖不回笼，新票只用剩余现金。

> 例（10 只入选、某瞬间 8 只达标、cap=5）：盘前已选定强度 top-5 为买入名额，权重在这 5 只内归一；8 只达标里**只有属于 top-5 的那几只**会下单，按各自强度权重分 ceiling，其余（含达标但非 top-5 的、及未达标的）预算 0 不买。若 top-5 全部达标 → ceiling 基本满额部署到这 5 只。
>
> 关键开关：`QMT_MAX_POSITIONS_PER_DAY`（单日建仓只数上限，默认 5）、`QMT_TARGET_POSITION_RATIO`（目标总仓位比）、`QMT_PER_ORDER_MAX_AMOUNT`（单笔金额上限）、`QMT_MAX_POSITION_PER_STOCK`（单票金额上限）、`QMT_MAX_TOTAL_EXPOSURE`（总敞口上限）、`QMT_MAX_ORDERS_PER_DAY`（下单**次数**上限，区别于只数）。

---

## 8. 环境变量与配置（运行前必须确认）

> **安全口径（两侧一致，沿用 `AGENTS.md`）**：账号 / token / key / DSN 等敏感项**不硬编码、不入库、不进日志**，一律从外部环境变量或本机 `_FILE` 落盘文件注入；凡有 `_FILE` 变体的，**生产优先用 `_FILE`**（指向本机文件），避免明文落 `.env`。两侧均提供脱敏快照（执行侧 `Settings.redacted()`、信号侧同口径），敏感字段打印即打码。生产实际值（服务器/账号/token）见本机运维手册 `/Users/salty/codeProject/ai/doc/Qmt生产服务器.txt`（**严禁 commit 进任一仓库**）。

### 8.1 执行侧 `qmt_strategy`（Windows，全部 `QMT_*`）

唯一解析点：`qmt_strategy/config/settings.py` 的 `Settings.from_env(os.environ)`（`app/run.py` 启动时调用）——下表即权威清单，未列出的 `QMT_*` 不被消费。**装配期 `assert_safe_to_trade()` 会对安全门控做 fail-closed 校验**（见 8.1.F：竞价择时未实测放行 / 卖出链开启未配单票浮亏止损；**评审 doc/19 H-3 新增**：账户回撤闸 `QMT_ACCOUNT_DRAWDOWN_LIMIT` + 限价偏离闸 `QMT_PRICE_DEVIATION_GUARD_PCT` 缺配即拒启，见 8.1.D）。

**A. 账户与连接（敏感，必配才能真实交易）**

| 变量 | 用途 | 默认 / 口径 |
|---|---|---|
| `QMT_ACCOUNT_ID` | QMT 资金账号（落 `qmt_*.account_id`） | 无（真实交易必配） |
| `QMT_MINI_PATH` | miniQMT `userdata_mini` 路径 | 无（真实交易必配） |
| `QMT_SESSION_ID` | 交易 session id（重连须换新） | 无 |
| `QMT_ACCOUNT_TYPE` | 账号类型（**保留·当前未接入** `StockAccount` 构造）；现货单参即可，两融启用前须先改字符串映射（见 doc/16 T0.5） | 无 |

**B. 交易日历（评审 P0-E1，生产 fail-closed）**

| 变量 | 用途 | 默认 / 口径 |
|---|---|---|
| `QMT_TRADE_CALENDAR_FILE` | 真实交易日清单文件路径（每行一个 `YYYY-MM-DD`，从信号侧 `a_trade_calendar` 导出）；T+1/名单键/对账都依赖它 | 无（**生产必配**） |
| `QMT_ALLOW_WEEKDAY_CALENDAR` | 未提供日历文件时是否退化为「仅排周末」 | `false`（**fail-closed，拒绝启动**；仅离线/测试可显式置 `true`） |

**C. 信号侧 HTTP 对接（盘前拉 watchlist + 盘后推 `qmt_*` 回流，见 §4）**

| 变量 | 用途 | 默认 / 口径 |
|---|---|---|
| `QMT_SIGNAL_BASE_URL` | 信号侧服务根，如 `http://1.2.3.4:8000` | 无（不配则盘前无名单、盘后不回流） |
| `QMT_SIGNAL_INTERNAL_TOKEN` / `…_FILE` | `X-Internal-Token` 统一回落值 | 无 |
| `QMT_SIGNAL_WATCHLIST_TOKEN` / `…_FILE` | 盘前 `GET /internal/watchlist` 专用 token | 缺省回落统一 token |
| `QMT_SIGNAL_INGEST_TOKEN` / `…_FILE` | 盘后 `POST /internal/qmt/ingest` 专用 token | 缺省回落统一 token |
| `QMT_WATCHLIST_SOURCE` | 名单来源 `DB`（直读表）/ `HTTP` | `DB` |
| `QMT_WATCHLIST_API_URL` | `HTTP` 方案的只读接口地址 | 无 |
| `QMT_HTTP_TIMEOUT_SECONDS` | 单次接口超时秒数 | `10.0` |
| `QMT_MYSQL_DSN` | 旧方案 A 直连 MySQL 回流（**通道未落地**） | 无；**配了即装配期拒启**，生产应改用 HTTP 回流 |
| `QMT_WRITEBACK_BASE_URL` / `QMT_WRITEBACK_TOKEN` / `QMT_INGEST_TOKEN` | 旧 B 方案写回接口（保留） | 默认空 |

**D. 资金与风控（下单前硬约束；分配语义详见 §7）**

| 变量 | 用途 | 默认 / 口径 |
|---|---|---|
| `QMT_TARGET_POSITION_RATIO` | 总预算上限 = 日初权益 × 本比例 | `1.0`（满仓上限；调小留现金，`0`=空跑不开新仓） |
| `QMT_MAX_POSITIONS_PER_DAY` | 单日建仓**只数**上限（不同标的数） | `5`（设 0/负/极大值放宽不限） |
| `QMT_FORBID_BOARD_LEVEL_MIN` | 禁买连板高度下限（`board_level >= 本值` 即禁买；买入前置过滤层硬规则，三层闸 + `tier==HIGH_BOARD` 兜底） | `4`（=禁买四板及以上）；调低（如 `3`）拦更多板，置 `0`/负=关闭高板口径（值进 `Settings.redacted()` 启动快照可核对） |
| `QMT_MAX_ORDERS_PER_DAY` | 单日下单**次数**上限（含转次优/卖出，区别于只数） | 无 |
| `QMT_PER_ORDER_MAX_AMOUNT` | 单笔金额上限 | 无 |
| `QMT_MAX_POSITION_PER_STOCK` | 单票持仓金额上限 | 无 |
| `QMT_MAX_TOTAL_EXPOSURE` | 绝对总敞口上限（与 ratio 取小） | 无 |
| `QMT_PRICE_DEVIATION_GUARD_PCT` | 限价相对参考价偏离护栏（限价偏离盘口现价超此比例即拒发；缺盘口现价时 fail-closed 拒发） | 无；**装配期 fail-closed 必配（评审 doc/19 H-3）**，缺配拒启动；显式关停配大值如 `0.99`（勿用 0=零容忍） |
| `QMT_ACCOUNT_DRAWDOWN_LIMIT` | 账户级当日回撤阈值（§5.4.1）；配了即生效：盘前日初权益基线抓取失败时 fail-closed 禁开新仓（不冻结卖出） | 无；**装配期 fail-closed 必配（评审 doc/19 H-3）**，缺配拒启动；显式关停配大值如 `0.99`（勿用 0=任何亏损即熔断） |
| `QMT_ACCOUNT_LOSS_LIMIT` | 账户级当日已实现亏损阈值 | 无；⚠️**当前未独立接线**——已由 `QMT_ACCOUNT_DRAWDOWN_LIMIT` 的 total_asset 回撤综合承载，单配本项不会独立触发熔断（启动期会强告警提示） |
| `QMT_STOCK_FLOAT_LOSS_LIMIT` | 单票浮亏止损阈值（**比例**口径，如 `0.05`） | 无；**开 `QMT_SELL_PASS_LIVE` 时强制必配** |
| `QMT_MARKET_STATE_BLOCK` | 禁开仓的 `market_state` 集合（逗号分隔） | `空仓,谨慎参与,退潮,冰点`（仅「参与」开新仓） |
| `QMT_ALLOW_PLAN_VOLUME_FALLBACK` | 缺 `plan_volume` 时是否回退现算（绕过强度/名额约束） | `false`（**对真实 BUY fail-closed**；仅离线置 `true`） |

**E. 战法阈值与采集节奏（对齐竞价观察清单口径）**

| 变量 | 用途 | 默认 / 口径 |
|---|---|---|
| `QMT_AUCTION_ABANDON_PCT` / `QMT_AUCTION_OVERHEAT_PCT` | 竞价弱于该幅度放弃 / 超该幅度警惕 | 无 |
| `QMT_AUCTION_LOWBUY_PCT_LOW` / `…_HIGH` | 低吸触发区间（**平开/微跌回踩**区间，如 `-0.02` / `0.01`） | 无；⚠️**未配则低吸族 fail-closed 不开仓**——绝不再回退用「合理高开区间」臆造低吸档（否则低吸买卖方向相反），低吸战法须显式配此区间 |
| `QMT_LEADER_STRENGTH_MIN` | 龙头强度分下限 | 无 |
| `QMT_SEAL_RATIO_MIN` | 打板跟买封流比下限（低于视为封单不稳→弃） | `0`（**关闭**；目标机实测 `bidVol` 量纲后再配正阈值如 `0.005`） |
| `QMT_STRATEGY_<NAME>_ENABLED` | 各战法独立开关（`<NAME>` 小写汇总进 `strategy_enabled`） | 未配即不在集合内 |
| `QMT_AUCTION_POLL_INTERVAL_SEC` | 竞价轮询间隔秒 | `3.0` |
| `QMT_CLOSE_SNAPSHOT_TIME` | 收盘快照时点 | `15:05` |

**F. 安全门控开关（核心红线，默认全部保守；装配期强制校验）**

| 变量 | 用途 | 默认 / 口径 |
|---|---|---|
| `QMT_KILL_SWITCH` | 全局熔断 | `false`；置 `true` 则**只采集不下单**（一键熔断） |
| `QMT_AUCTION_TIMING_ENABLED` | 竞价择时总开关 | `false`（**实测前必须关**） |
| `QMT_AUCTION_TIMING_VERIFIED` | 竞价数据能力实测放行标记 | `false`；`ENABLED=true` 但本项非真 → **启动期 `RuntimeError` 拒启** |
| `QMT_SELL_PASS_LIVE` | 盘中卖出链生产放行门控 | `false`（**当前必须保持关**，见 §6「盘中卖出」行）；开启时**强制要求**已配 `QMT_STOCK_FLOAT_LOSS_LIMIT` 否则拒启 |

**G. 下单/台账与本地化数据栈（doc/05 单进程 + SQLite）**

| 变量 | 用途 | 默认 / 口径 |
|---|---|---|
| `QMT_ORDER_TTL_SECONDS` | 开盘单最长存活秒（竞价单到 9:25 定盘） | `60` |
| `QMT_TRADE_CONN_HEARTBEAT_FAIL_THRESHOLD` | 下单通道心跳连续失败几次才置 FREEZE | `3` |
| `QMT_CANCEL_GRACE_SECONDS` | `CANCELLING` 单撤单回执宽限秒 | `30` |
| `QMT_UNIQUE_WITH_TRADE_DATE` | 明细唯一键是否纳入 `trade_date`（§6.5 加固） | `true` |
| `QMT_LOCAL_DB_PATH` | 本机 SQLite 库路径（回流/台账/名单） | `qmt_local.db` |
| `QMT_DECISION_LOG_ENABLED` | 决策链路采集（复盘用，与交易热路径物理隔离） | `true`；置 `false` 一键降级 no-op |
| `QMT_DECISION_LOG_QUEUE_SIZE` / `QMT_DECISION_LOG_BATCH_SIZE` | 采集有界队列容量 / 回流攒批大小 | `2000` / `50` |
| `QMT_WRITE_QUEUE_MAX` | 写队列长度硬上限（>0 启用溢出熔断，防写线程挂死无界堆积 OOM；评审 F07） | `50000`（已默认武装；配 `0` 关闭上限） |
| `QMT_WRITE_QUEUE_STUCK_SECONDS` | 写线程「卡死」看门狗阈值秒：有积压却连续该秒数无任务推进 → `is_healthy` 转 False → fail-closed 停开新仓（评审 F09） | `30`（配 `0` 关看门狗） |
| `QMT_RECONCILE_ASSET_ABS_FLOOR` | 收盘资产对账偏差绝对容差下限（元；评审 F01） | `1000` |
| `QMT_RECONCILE_ASSET_REL_RATE` | 资产对账偏差相对容差（× 当日成交额，覆盖佣金/印花税费用噪声；评审 F01） | `0.003` |

### 8.2 信号侧 `stock-ah-premium-ai`（Linux）

权威清单：`backend/app/core/config.py`（Pydantic Settings）+ 模板 `backend/.env.example`；从 `backend/.env` 或系统环境注入。下表只列**打板交易闭环相关**的必配/核心项；信号侧还有问答引擎、配图、雪球发布、PushPlus 等平台能力的大量调参（`AGENT_*` / `IMAGE_GEN_*` / `QWEN_*` / `XUEQIU_*` / `PUSHPLUS_*` / `PY_SANDBOX_*` 等），**与本交易闭环无关、均有默认或缺省降级，完整清单以 `backend/.env.example` 为准**，按需启用。

**核心必配 / 强约束**

| 变量 | 用途 | 默认 / 口径 |
|---|---|---|
| `STOCK_AH_DB_URL` | MySQL `stock_ah_ai` 连接串（SQLAlchemy/Alembic） | `mysql+pymysql://root@127.0.0.1:3306/stock_ah_ai?charset=utf8mb4`（**生产必配真实库**） |
| `WATCHLIST_EXPORT_INTERNAL_TOKEN` / `…_FILE`（**兼作通用内网 token**） | `GET /api/internal/watchlist`（盘前名单）+ `GET /api/internal/trade_calendar`（J-3 日历）的 `X-Internal-Token`；并作 ingest 写接口的回落 token | 无；**未配即接口恒 503 关闭** |
| `QMT_INGEST_INTERNAL_TOKEN` / `…_FILE` | 盘后 `POST /api/internal/qmt/ingest` 的 `X-Internal-Token` | 无；**未单独配则回落 watchlist 通用 token（doc/29 J-8，通用 token 打通所有接口）**——只配一套 watchlist token 即打通名单/日历/回流三接口；需读写权限隔离的部署再显式单配本项覆盖回落 |
| `WATCHLIST_EXPORT_IP_WHITELIST` / `QMT_INGEST_IP_WHITELIST` | 两接口来源 IP 白名单（逗号分隔，与 token 叠加） | 空=不启用（**生产建议配置或反代层加白名单**） |

> ✅ **token 口径变更（doc/29 J-8，通用 token 打通所有接口；有意回退 SIG-QMT-06 读写分离）**：`resolve_qmt_ingest_internal_token` 在未单独配回流写 token 时**回落 watchlist 通用 token**，消除「漏配回传 token → `/ingest` 恒 503 静默断流」陷阱。本机内网部署、读写两接口同源可信，安全边界改由**内网 + IP 白名单/反代来源绑定**承担（与 watchlist 导出同口径）。`/api/internal/trade_calendar`（J-3）亦复用同一通用 token。需读写权限隔离时仍可显式配独立 `QMT_INGEST_INTERNAL_TOKEN(_FILE)` 覆盖回落。

**行情 / LLM（选股 pipeline 依赖）**

| 变量 | 用途 | 默认 / 口径 |
|---|---|---|
| `TUSHARE_TOKEN` / `…_FILE` | Tushare 行情 token | `_FILE` 默认指向 `…/doc/tushare-token.txt` |
| `TUSHARE_API_URL` | Tushare 代理地址 | `https://tt.xiaodefa.cn` |
| `LLM_API_KEY` / `…_FILE` | DeepSeek LLM key | `_FILE` 默认指向 `…/doc/deepseek-apikey.txt` |
| `LLM_BASE_URL` / `LLM_MODEL` | LLM 端点 / 默认模型 | `https://api.deepseek.com` / `deepseek-v4-flash` |

**调度与时区（东八区定时口径）**

| 变量 | 用途 | 默认 / 口径 |
|---|---|---|
| `SYNC_SCHEDULER_TIMEZONE` | APScheduler 时区（全局定时口径） | `Asia/Shanghai` |
| `SYNC_SCHEDULER_ENABLED` / `ALERT_SCHEDULER_ENABLED` / `LIMIT_UP_PUSH_SCHEDULER_ENABLED` | 增量同步 / 预警扫描 / 打板推送调度总开关 | 各 `true` |

**打板 pipeline / 闸门 / 龙头 / 回测调参（影响 watchlist 产出口径）**

> 这些是选股链路的可调阈值，均有默认、不配也能跑；需要调口径时按 `backend/.env.example` 配置。代表项：`LIMIT_UP_PUSH_MODEL`（默认 `deepseek-v4-pro`）、`LIMIT_UP_LEADER_SCORING_ENABLED`（默认 `true`，龙头六维打分卡，对应 §2「龙头增强 M2」）、`LIMIT_UP_GATE_*`（空仓闸门否决阈值，对应 go/no-go）、`LIMIT_UP_BACKTEST_DEFAULT_LOOKBACK_DAYS`/`LIMIT_UP_BACKTEST_CONTROL_SOURCE`/`LIMIT_UP_BACKTEST_BUY_AT`（回测口径，默认 `60` / `CACHE_POOL` / `T1_OPEN`）。完整项与默认值见 `backend/.env.example`。

**前端（React/Vite）**：`VITE_API_BASE_URL`（默认空=走 `/api` 代理）、`FRONTEND_PORT`（`5173`）、`BACKEND_PORT`（`8000`）；见 `frontend/vite.config.ts` 与 `scripts/start-frontend.sh`。

---

## 9. 文档导航

文档分五类（规则见仓库 `CLAUDE.md`）：① 本 README（现状口径）；② 待办清单（还要做什么）；③ 评审与修复状态概要（持久状态记录）；④ 评审/修复处理文档（处理中，处理完归档）；⑤ 已归档（完成存档、不再看）。

**① 现状 / 参考（README 同级，常看）**

| 文档 | 内容 |
|---|---|
| `README.md`（本文） | 项目现状口径：交易逻辑 + 架构 + 部署设计 + 资金分配口径 + 环境变量与配置 |
| [`核心逻辑详解-交易策略-兜底-配置.md`](核心逻辑详解-交易策略-兜底-配置.md) | 执行侧逐模块深入：交易逻辑/五策略 + 竞价四因子 + 下单幂等/TTL + 持仓卖出/T+1 + 风控 + **意外情况兜底全景** + **配置项全表**（带 `file.py:行号`） |
| [`待办与上线验证清单.md`](待办与上线验证清单.md) | 还需处理 + 只能上生产/目标机实测验证的事项（活文档） |
| [`评审与修复状态概要.md`](评审与修复状态概要.md) | 评审做了什么、修了什么、当前状态（持久概要，指向已归档的 08/09/11–14 详情） |
| 运维手册（本机 workspace）`/Users/salty/codeProject/ai/doc/Qmt生产服务器.txt` | 两台生产机部署/重启/token/计划任务/连通性自检 + 执行侧 Windows bring-up（非本仓库，含密码勿外泄） |

**② 处理文档（处理中，在 `doc/` 根；处理完移入工作区归档）**

| 文档 | 内容 |
|---|---|
| [`21-全链路代码评审-交易严重bug独立评审报告`](21-全链路代码评审-交易严重bug独立评审报告.md) + [`22-全链路代码评审修复台账`](22-全链路代码评审修复台账.md) | 第六轮全链路独立评审（P1–P4 封板质量/重连漏卖 + R1–R3 对账关联 + S1）与逐条修复台账。高层状态见 ③ |
| [`23-打板因子补充-watchlist契约1.2.0-设计与台账`](23-打板因子补充-watchlist契约1.2.0-设计与台账.md) | 信号侧 watchlist 契约补打板因子（封板时序 + 封单额口径统一 + 位置/强度，1.1.0→1.2.0）；执行侧消费接线列为待办（[`§D`](待办与上线验证清单.md)） |
| [`24-执行侧qmt_strategy交易闭环独立评审报告`](24-执行侧qmt_strategy交易闭环独立评审报告.md) + [`25-信号侧打板推送链独立评审报告`](25-信号侧打板推送链独立评审报告.md) + [`26-两侧联合交易整体评审报告`](26-两侧联合交易整体评审报告.md) | 第七轮：执行侧 / 信号侧打板推送链分别独立评审 + 两侧 HTTP 契约联合整体评审（确认去重 34 条，含 P0×2：E-1 重复下单窗口、J-4/5/6 回流四表 NOT NULL 毒丸）。高层状态见 ③ |
| [`27-修复顺序指南`](27-修复顺序指南.md) + [`28-回测方向问题记录`](28-回测方向问题记录.md) | 第七轮评审的修复排期：数据缺测统一约定（null→放弃买入/持仓卖出）、契约类待决策（J-1/J-2/J-4-6/J-8 + E-6 + S-10 主源 + 日历同步），含数据可得性盘查结论；回测口径问题剥离至 28（低优先级、可暂不处理） |
| [`29-缺测改造-设计与实现规格`](29-缺测改造-设计与实现规格.md) + [`30-缺测改造遗留与待确认项`](30-缺测改造遗留与待确认项.md) | 缺测改造（S-2/S-3/S-9/S-10/1.2.0 取数 + 缺测 sentinel 契约 + 执行侧买入拦截 + E-6 + J-3 日历接口 + J-1/J-8/S-12）的**自包含实现规格** + 遗留三项台账。**口径变更（2026-06-21）：缺测「持仓卖出」(B3) 已下线，卖出交由实时盘口；缺测仅保留买入侧拦截**；doc/30 项 1/2/3 已处理（B3 下线消解项 1、`fd_amount` 死代码修复、生产缺测率核实）。详见 ③ 状态概要 |

> **注**：盘中卖出链真机固化（C-1/C-3）、moneyflow 资金主线等**仍未完成的活项**，其详细历史设计/台账已随归档移走，但活项跟踪保留在 [`待办`](待办与上线验证清单.md) §0/§A/§D 与 [`评审与修复状态概要.md`](评审与修复状态概要.md)（README §6 表与上线红线即以此为准）。各轮「全清零」指**当轮登记的 P0/P1/P2 条目**已修，**不含**「盘中卖出」接线缺口（历轮当待落地 TODO 延后、未计入清零口径）。

**③ 已归档（完成存档·不再看）** → 已移入**工作区级**归档 `/Users/salty/codeProject/ai/doc/已归档-完成不再维护/`（本仓库 `doc/` 下不再保留）。含：01–05 开发设计/计划、07 HTTP 交互 + 部署运维清单、08–09 第一轮评审/台账、10 信号侧打板分析建议、11–12 第二轮、13–14 第三轮、15 推送供数评审、16 卖出链开发计划、17 禁买 ST 硬化、18 买入前置过滤层、19–20 预上线评审/台账、旧 codex 评审稿。前几轮评审高层状态见 [`评审与修复状态概要.md`](评审与修复状态概要.md)。
