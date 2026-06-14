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
| 执行引擎（连接/竞价/建仓/持仓/风控/回流/对账） | ✅ 已落地；**2026-06 正式评审发现并修复多处致命断裂**（路由全 SKIP 不开仓 / 持仓不回写不卖出 / 买入绕过风控 / go-no-go 闸门缺失等），见 §8 |
| 执行侧本地化（单进程+SQLite 异步持久化） | ✅ 已落地；评审修复发单前同步落盘等幂等缺口（§8） |
| 调度器 + xtquant 适配器模板 + 真实入口骨架 | ✅ 已落地（待目标机填 `TODO(实测)`）；交易日历已改 fail-closed（§8 上线须知） |
| 信号侧 HTTP 双接口 + `qmt_*` 表/迁移 + 客户端 | ✅ 已落地；评审补回流信封一致校验、watchlist 竞价两因子供数（§8） |
| 实盘对接（miniQMT/xtquant + 调度任务计划 + 实测） | ⬜ 待 QMT 客户端到位 |

代码仓库：执行侧 [github.com/WinSalty/astock-quant-ai](https://github.com/WinSalty/astock-quant-ai)。

> ⚠️ 上线前务必先看 §8「上线须知」——本轮修复引入了两个必须的部署动作（信号侧跑迁移 0054、执行侧提供交易日清单文件）。

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
| [`archive/09-两侧代码修复计划与执行清单`](archive/09-两侧代码修复计划与执行清单.md) | 评审后修复的分批计划与逐项进度台账（含 commit 索引、待确认口径、部署须知） |

---

## 8. 近期评审与修复（2026-06）

针对本项目「真实投入资金」的属性，做了一次正式代码评审（[`archive/08`](archive/08-信号侧与执行侧量化交易代码评审报告.md)，166 条发现，确认 12 critical + 24 high + 63 medium），并据此完成修复（计划与逐项 commit 台账见 [`archive/09`](archive/09-两侧代码修复计划与执行清单.md)）。

### 8.1 修复概要（已全部测试 + 推送）

- **建仓闭环可用**：修 `strategy_family` 英文枚举与执行侧中文路由失配（原全部计划票落 SKIP、永不开仓）；修买入成交从未回写持仓状态机（原 `_units` 恒空、永不卖出、裸奔扛单）；统一 `market_state` 三档/六档口径（谨慎参与=禁开仓）。
- **风控护栏**：买入接入 `risk.gate` + 喂入账户日内回撤阈值（原买入完全绕过风控、账户熔断对开仓零作用）；情绪闸门 `highest_chain_change`（最高板升降）由 dict 误当标量恒为 0 修复；单日下单次数上限落地。
- **下单/幂等/唯一下单点**：卖出改走唯一下单点 `OrderExecutor.place_sell` 并落卖单台账（原直连 trader 绕过台账/无单号/不可对账）；在途 SELLING 单元不再重复下卖单；发单前同步落盘堵崩溃窗口重复下单；重启重建 biz 序号防同号覆盖；成交去重键跨重启类型归一 + 缺主键合成键。
- **价位/时段口径**：board 判不出段（科创/北交/未知）不再按主板 10% 兜底现算涨停价（降级 MISSING）；卖出限价改用盘口现价（原用成本价炸板卖不出）；竞价限价封顶至涨停价；`order_phase` 按时段判定；午休（11:30–13:00）隔离不发撤/卖单。
- **供数口径**：封流比 `circ_mv` 万元/元单位错配（放大约 1 万倍）修复；竞价虚拟封单 tick 五档归一（原封板场景封单恒 0）；辨识度子分按四维强弱均值分档（原「强恒胜」）；**F3 竞价两因子供数补齐**——契约/ORM/迁移补 `float_mktcap`/`first_board_vol`，执行侧透传 + 量能比阈值重标定。
- **回测/回流**：回测新增 `go/no-go` 结构化裁决出口（原只产指标、无判定；阈值保守占位待校准）；回流 ingest 校验 `record.data` 的 `account_id/trade_date` 与请求信封一致（防串账户/串日）。
- **交易日历 fail-closed**：执行侧不再硬编码「仅排周末」的近似日历（节假日会污染 T+1/名单键/对账），改为必须提供真实交易日清单、否则启动期拒绝（见 §8.3）。

### 8.2 上线须知 ①：信号侧跑数据库迁移

F3 给 `limit_up_selected_stock` 新增了 `float_mktcap`、`first_board_vol` 两列（迁移 `0054`）。**信号侧 MySQL 必须执行迁移**，否则选股落表会因缺列报错：

```bash
cd /path/to/stock-ah-premium-ai/backend && . .venv/bin/activate
alembic upgrade head
```

### 8.3 上线须知 ②：执行侧提供交易日清单文件

执行侧交易日历改为 **fail-closed**：必须用与信号侧 `a_trade_calendar` 同源的真实交易日（含法定节假日），否则进程启动期直接拒绝（避免「把节假日当交易日」污染 T+1/名单/对账）。

**导出（信号侧，用本仓库脚本 `backend/scripts/export_trade_calendar.py`）：**
```bash
cd /path/to/stock-ah-premium-ai/backend && . .venv/bin/activate
# 导出 SSE 的 is_open=1 交易日为「每行一个 YYYY-MM-DD」的文件；自动校验是否覆盖到足够未来
python scripts/export_trade_calendar.py --output /opt/qmt/trade_days.txt --min-future-days 90
```
- 脚本复用项目 DB 入口（`app.db.session.SessionLocal`，不硬编码 DSN）；输出不含时间戳、可复现；末日距今不足 `--min-future-days` 会强 WARNING（提示先同步 `a_trade_calendar` 未来日期再导出）。
- 退出码：0=成功；2=`--start` 非法；3=零交易日（exchange 写错/库未同步）。

**部署（执行侧 Windows 交易机）：** scp 上述文件过去，配环境变量：
```
QMT_TRADE_CALENDAR_FILE=C:\qmt\trade_days.txt
```
**维护：** 每年新一年节假日安排公布后（Tushare `trade_cal` 更新）重新同步 `a_trade_calendar` → 重新导出 → scp 覆盖；否则跨年后 `next_open` 会越界。**仅离线/联调**可临时 `QMT_ALLOW_WEEKDAY_CALENDAR=true` 退化为周末近似（强告警，**严禁用于实盘**）。

### 8.4 仍待办（非阻塞，多需回测校准 / 目标机实测）

- 上线流程须强制读取回测 `summary['go_no_go']` 裁决（GO/NO_GO/INSUFFICIENT）为放行闸门；其阈值为保守占位，待回测/实盘校准后随版本固化。
- 目标机 `xtdata.get_full_tick` 五档结构 / `xtconstant` 常量须 `vars()` 实测核对，固化 `auction_factors`/`order_executor`/`normalize` 的 `TODO(实测)` 占位。
- 回测显著性检验、默认含费净口径、LLM 概率校准、龙头打分全涨停池基准、role 分级消费等优化项见 [`archive/09`](archive/09-两侧代码修复计划与执行清单.md) 剩余项。
