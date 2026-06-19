# 23 · 打板因子补充（watchlist 契约 1.1.0 → 1.2.0）· 设计与台账

> 范围：对照《A股龙头战法与追板操作指导手册》打板判断章节，在**信号侧 `stock-ah-premium-ai`**（Tushare ≥5000 积分、2 万以下）为 watchlist 导出契约补充打板判断因子。本轮只动信号侧（产出 + 导出），执行侧消费接线列为待办。
> 状态：信号侧 P0-1/P1-2/P1-3 **已落地**（feature 分支 `feature/limit-up-watchlist-signal`，未 push/未部署）；P2-4 **搁置**。处理完（合并部署 + 执行侧消费）后本文整体归档，高层结论入《评审与修复状态概要》。

---

## 一、来源：手册对照分析的结论

手册的打板硬指标（流通市值带 / 龙头强度角色 / 情绪周期 / 封流比 / 换手率 / 连板高度 / 龙虎榜 / 筹码 / 涨停类型）**绝大部分信号侧契约已覆盖**（见 watchlist 1.1.0 的 37 字段）。真正缺、且信号侧能干净提供的，集中在三类：

1. **封板时序**（首封/末封时间、开板次数）—— 数据已在内存取用却没落表导出。
2. **封单额口径不自洽** —— 封流比已用 `fd_amount` 兜底，但持久的封单额列没跟上。
3. **位置/强度上下文**（量比、近期涨幅）—— 执行侧只见当天竞价盘口、拿不到历史/位置，而手册把「空间高度/高位风险」列为打板首要风险闸。

> 取舍纪律：手册面向纯手动散户的若干刚性约束（全满足才买/任一即弃的硬清单、固定仓位百分比、硬量能门）**不照搬**——我方有信号侧先验闸 + 强度加权 + 多重风控三层垫，延续「加权降级而非一票否决」。

---

## 二、四点台账

### P0-1 · 封板时序三指标（✅ 已落地）

- **做了什么**：把 `_compact_stock_row` 内存里已取用（仅喂 LLM 判一字/秒板）的 `first_limit_time`（首封时刻）/`last_limit_time`（末封时刻）/`open_times`（开板/炸板次数）落 `limit_up_selected_stock` 表 + 随 watchlist 导出。
- **数据源**：`limit_list_d`（5000 积分，含 first_time/last_time/open_times），`first_limit_time` 缺则 `kpl_list.lu_time` 兜底；`open_times` 无兜底（无 5000 权限则 None）。
- **落点**：迁移 `0059`；ORM `notification.py` ⑧c 三列；写入点 `limit_up_push_service.py`（`row.get(...)`）；schema `limit_up_watchlist.py`。
- **执行侧用途**：秒板最强（9:45 前首封）/ 烂板·反复炸板规避（open_times>0）。

### P1-2 · 封单额口径统一（✅ 已落地）

- **问题**：封流比分子已用 `limit_order or fd_amount`（`:2352`），但持久的 `limit_order` 列只存 raw 实时值——「有 fd_amount 无实时封单额」的票封流比已算出、`limit_order` 却 NULL，两值不自洽。
- **做了什么**：`_compact_stock_row` 的 `limit_order` 改为 `row.get("limit_order") or row.get("fd_amount")`（实时 kpl/ths 优先，回退 `limit_list_d.fd_amount`，单位同为元）。**无新增列/无版本变更**。
- **不做**：不新增独立 `fd_amount` 字段——避免执行侧出现「两个封单字段打架」。

### P1-3 · 位置/强度上下文（✅ 已落地，替代被否的「首板换手率」）

- **否决首板换手率（原计划项）**：对首板股 T=首板日，现有 `turnover_rate` 即首板换手（重复）；对连板股回溯首板日换手会与 `first_board_vol`「最近板量、不回溯」口径冲突（`:2086`）。故**伪缺口，放弃**。
- **改做**：导出 `_calculate_indicator` 已算却没下发的 `volume_ratio`（量比，强度标尺）/`return_5d_pct`/`return_10d_pct`（近 5/10 日涨幅 = 空间高度）。
- **落点**：迁移 `0060`；ORM ⑧d 三列；写入点取自 `tech`；schema。**零新增 Tushare 抓取**。
- **执行侧用途**：补其结构上拿不到的历史/位置盲区——高位连板规避（手册首要风险闸）+ 强度确认。
- **口径**：均为 T 日 daily 衍生值（连板票取最近板日，与 `first_board_vol` 同口径不回溯）；涨幅为百分数可为负。

### P2-4 · 竞价量能比分母 / F3 量纲坑（⏸ 搁置）

- **为何搁置（两条硬事实）**：
  1. 执行侧 `auction_vol_ratio` 已是 `weak_vol` **留痕不弃**（`chase_auction_strong.py:131`、`auction_factors.py:87`「不得单独构成硬弃门槛」）——量纲不对等**不导致错误下单**。
  2. 干净修法需「首板日同时段(9:15–9:25)竞价**成交量**(手)」，但 Tushare/KPL 无此字段（`bid_amount`=成交额元、`bid_turnover`=换手率%、`lu_bid_vol`=竞价封单量，均非成交量手），分钟级集合竞价量需 >2 万积分且 Tushare 不单独提供。硬凑只能用「换手率×流通股本」反推**低保真估算**，反而误导执行侧。
- **结论**：不塞低保真分母。待真机核实 `lu_bid_vol`/分钟数据语义后再定（标 [实测]）。

---

## 三、watchlist 契约演进（1.1.0 → 1.2.0）

`WATCHLIST_SCHEMA_VERSION = "1.2.0"`，新增 6 个**可空只读**列（执行侧按版本兼容旧行，缺则降级）：

| 组 | 字段 | 来源 | 用途 |
|---|---|---|---|
| 封板时序 | first_limit_time / last_limit_time / open_times | limit_list_d（+kpl 兜底） | 秒板/烂板/炸板判断 |
| 位置强度 | volume_ratio / return_5d_pct / return_10d_pct | 已算 daily 衍生 | 高位规避 + 强度确认 |

- **向后兼容**：6 列均 `nullable`，存量行 NULL；执行侧 HTTP 映射 `remote_watchlist.py:106` 用逐字段 `item.get(...)`（非 `**item`），旧版安全忽略新键。
- **版本**：P0-1 已将 1.1.0 bump 至 1.2.0，P1-3 复用同一未发布版本（同批扩展，不发空跑的中间版本）。

---

## 四、执行侧消费接线（待办，见《待办清单 §D》）

信号侧已产出，**执行侧暂未消费**（常规「先下发、后消费」节奏）。后续需：
1. `SelectedStockRow → PlanRow` 增 6 可选字段透传（`remote_watchlist.py` 映射 + 本机 `watchlist` 表 schema）。
2. 打板战法据其降级/规避：`open_times>0` 烂板降级、`first_limit_time` 晚于阈值降级、`return_5d_pct` 过高（高位）降仓/规避。
3. 与「板数×情绪仓位矩阵」（早前讨论的执行侧 P0-2）合并设计。

---

## 五、验收

- 信号侧全量测试 **508 passed, 1 skipped**；新增/改动单测覆盖：落表（有值 + 缺值降级 None）、导出透传（含负值）、封单额回退、量比/涨幅落表。
- 三轮独立评审：P0-1（无阻塞）、P1-3（无阻塞）、三点整体收口（无阻塞，契约四处自洽、迁移链 0058→0059→0060 线性可回滚、向后兼容）。
- 提交：`7e75d41`（P0-1）/ `6dd670a`（P1-2）/ `b1955f4`（P1-3），feature 分支，未 push。

> 遗留小项（不阻塞）：`resources/sql/03_full_schema_with_comments.sql` 人工参考 DDL 早已漂移（缺 lhb/is_st 等多列），非本次回归；生产建表以 alembic 迁移为准。
