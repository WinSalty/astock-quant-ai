# 打板量化项目 · 总览

> A 股打板/涨停量化交易闭环。**信号侧负责「看得准」，执行侧负责「买得到、守得住、可复盘」**，
> 两侧物理隔离、通过 HTTP 解耦，回流数据闭合成可归因的环。
> 本文是整个项目（信号侧 + 执行侧）的单一入口；各模块详细设计见 [`archive/`](archive/)，实战战法见 `06-A股龙头战法与追板操作指导手册.md`。

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
- **下单时段**：竞价段（强开追/竞价卖）+ 开盘后连续交易（打板跟买/低吸/龙回头/止盈止损/炸板/尾盘）；竞价择时实测前默认**关**，实际下单全在开盘后。

**贯穿全局的硬口径**：守 T+1 双保险 · 三层幂等（业务单号+DB唯一键+traded_id）· 时间口径无 ±8h（东八区↔UTC naive）· 安全默认「契约残缺/中断→只守仓不开新仓」· `KILL_SWITCH` 一键熔断 · 竞价择时实测前关。

---

## 4. 两侧数据交互（HTTP · 详见 [archive/07-提供侧执行侧HTTP数据交互](archive/07-提供侧执行侧HTTP数据交互-方案与说明.md)）

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
  - 完整清单见 [archive/07-QMT执行侧服务器部署运维清单](archive/07-QMT执行侧服务器部署运维清单.md)。

**上线硬次序**（不可颠倒）：信号侧回测 go/no-go ✅ + QMT 竞价能力实测 ✅ →（两 AND）接实盘**小仓**（熔断+保守风控+checklist 全绿）→ 小仓灰度 → 逐步放量；竞价择时**先关，实测通过再灰度开**。

---

## 6. 当前状态

| 部分 | 状态 |
|---|---|
| 执行引擎（连接/竞价/建仓/持仓/风控/回流/对账） | ✅ 已落地、单测通过 |
| 执行侧本地化（单进程+SQLite 异步持久化） | ✅ 已落地、对抗式评审修复、测试通过 |
| 调度器 + xtquant 适配器模板 + 真实入口骨架 | ✅ 已落地（待目标机填 `TODO(实测)`） |
| 信号侧 HTTP 双接口 + `qmt_*` 表/迁移 + 客户端 | ✅ 已落地 |
| 实盘对接（miniQMT/xtquant + 调度任务计划 + 实测） | ⬜ 待 QMT 客户端到位 |

代码仓库：执行侧 [github.com/WinSalty/astock-quant-ai](https://github.com/WinSalty/astock-quant-ai)。

---

## 7. 文档导航

| 文档 | 内容 |
|---|---|
| `06-A股龙头战法与追板操作指导手册.md`（根目录） | 实战战法与操作准则（人读手册） |
| [`archive/01-打板竞价择时策略-可行性分析与优化方向`](archive/01-打板竞价择时策略-可行性分析与优化方向.md) | 策略可行性分析与优化方向 |
| [`archive/02-信号侧改造-开发设计`](archive/02-信号侧改造-开发设计.md) | 信号侧六模块完整开发设计 |
| [`archive/03-QMT侧执行引擎-开发设计`](archive/03-QMT侧执行引擎-开发设计.md) | 执行侧引擎完整开发设计 |
| [`archive/04-QMT侧执行引擎-开发计划`](archive/04-QMT侧执行引擎-开发计划.md) | 执行侧开发计划与验收 |
| [`archive/05-执行侧本地化改造-方案与改动说明`](archive/05-执行侧本地化改造-方案与改动说明.md) | 单进程+SQLite 本地化方案 |
| [`archive/07-提供侧执行侧HTTP数据交互-方案与说明`](archive/07-提供侧执行侧HTTP数据交互-方案与说明.md) | 两侧 HTTP 双接口契约与交互 |
| [`archive/07-QMT执行侧服务器部署运维清单`](archive/07-QMT执行侧服务器部署运维清单.md) | Windows 部署/保活/安全/磁盘运维清单 |
| [`archive/08-信号侧与执行侧量化交易代码评审报告`](archive/08-信号侧与执行侧量化交易代码评审报告.md) | 两侧量化/供数代码正式评审（P0/P1/P2 + 优化空间 + 上线硬门槛）；明细见 `archive/08-评审发现明细.json` |
