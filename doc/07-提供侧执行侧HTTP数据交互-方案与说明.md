# 提供侧 / 执行侧 HTTP 数据交互 · 方案与说明

> 文档日期：2026-06-13。本文承接 `05-执行侧本地化改造-方案与改动说明.md`，把其中跨侧数据交互
> （§4.1 watchlist 交付、§4.2 回流）从「执行侧直连远端 MySQL」**统一收敛为信号侧托管的两个 HTTP 内网接口**，
> 执行侧作为纯客户端调用。本文同时说明两侧的代码落地、交互契约与执行逻辑。
> 按项目约束：不含工期 / 工时预估，只按阶段与任务给出验收标准与依赖顺序。

---

## 一、为什么改：从「直连库」到「HTTP 双接口」

- **信号侧（提供侧）**：`stock-ah-premium-ai`（Linux，FastAPI + MySQL `stock_ah_ai`），网络可达、已常驻 Web 服务，**产 watchlist 信号、消费回流数据做复盘**。
- **执行侧**：`astock-quant-ai/qmt_strategy`（Windows VPS，单进程 + miniQMT/xtquant + 本机 SQLite），**独立做买卖执行**。

原 `doc/03/05` 中执行侧需要直连信号侧 MySQL（读 watchlist、写 `qmt_*`）。本次变更：两侧交互**全部走 HTTP**，
信号侧托管两个内网接口、执行侧只做客户端调用。收益：

1. **Windows 交易机零入站端口**：执行侧只发起出站 HTTP，不对外暴露任何监听端口，攻击面最小。
2. **不连库更安全**：信号侧 MySQL 不再对 Windows 开放写端口，凭据只在信号侧服务内部使用。
3. **契约稳定**：两侧通过 JSON 契约解耦，互不感知对方表结构 / ORM。

> 方向决策（已确认）：**watchlist 走「执行侧拉取」**（执行侧 GET 信号侧接口），不采用「信号侧推送到执行侧」。
> 收盘交易数据走**「执行侧推送」**（执行侧 POST 信号侧接口）。即——**两个接口都由信号侧托管，执行侧是两个流向的唯一发起方**。

---

## 二、总体架构

```
   信号侧（提供侧）stock-ah-premium-ai (Linux, FastAPI)        执行侧 qmt_strategy (Windows, 单进程)
   ┌──────────────────────────────────────────────┐         ┌────────────────────────────────────────┐
   │ ① GET  /api/internal/watchlist?date=T          │◀─盘前拉取─│ WatchlistPrefetcher                      │
   │       （已存在，本次复用）                       │         │   prev_open(today)=T → GET → 映射 → 落本机 │
   │       → limit_up_selected_stock 结构化导出       │         │   SQLite watchlist 表                     │
   │                                                │         │                                          │
   │ ② POST /api/internal/qmt/ingest                │◀─盘后回流─│ RemoteSyncJob → HttpIngestQmtRepository  │
   │       （本次新增，幂等 upsert→MySQL qmt_*）       │         │   逐行 POST；失败保 synced=0 下轮重试      │
   └──────────────────────────────────────────────┘         └────────────────────────────────────────┘
            两接口同一 X-Internal-Token 鉴权；执行侧用 stdlib urllib（不引三方依赖）
```

- **盘中完全不依赖跨网络**：盘前一次性把名单拉到本机 SQLite，盘中下单只读本机库；回流改为盘后批量。
  网络问题只影响「事后同步」，而同步可重试、幂等。
- **信号侧是唯一服务端**，执行侧是两个数据流向的唯一客户端，`互相` 体现在「一拉一推」两条 HTTP 链路。

---

## 三、接口契约

两接口同属信号侧 `/api/internal/*`，机器对机器，**统一 `X-Internal-Token` 头鉴权**（常量时间比较；token 未配置→503、缺失/不符→401）。
ingest token 缺省回落到 watchlist 导出 token，可一套凭据服务两个接口，也可分别配置隔离。

### 3.1 ① watchlist 拉取（已存在，复用）

| 项 | 说明 |
| --- | --- |
| 方法/路径 | `GET /api/internal/watchlist` |
| 查询参数 | `date`=**信号日 T**（东八区交易日 `YYYY-MM-DD`，必填）；`prompt_version`（可选，缺省取该日最新 READY 报告版本） |
| 鉴权 | `X-Internal-Token` |
| 响应 | `LimitUpWatchlistResponse`：`{schema_version, trade_date, target_trade_date, market_state, count, items[]}`，`items` 为一股一行的结构化契约（不含内部审计 blob） |
| 空数据 | 当日无 READY 报告 → 200 空集（`count=0`），便于执行侧轮询，不 404 |

> 注意「以信号日 T 为键」：执行侧要的是「**今天买入**」的清单，需先用交易日历反推
> `signal_T = prev_open(today)`，再按 `date=signal_T` 拉取（响应里每条 `target_trade_date = T+1 = today`）。

### 3.2 ② 回流 ingest（本次新增）

| 项 | 说明 |
| --- | --- |
| 方法/路径 | `POST /api/internal/qmt/ingest` |
| 鉴权 | `X-Internal-Token` |
| 请求体 | `{ "account_id"?, "trade_date"?, "records": [ { "table", "data" }, ... ] }` |
| `table` | 目标表，白名单四选一：`qmt_trade` / `qmt_order` / `qmt_position_snapshot` / `qmt_account_daily` |
| `data` | 列名→值字典，即执行侧 `storage/mappers.*_to_row` 产出（**JSON 友好**：Decimal→str、date/datetime→ISO、枚举→值、bool→0/1） |
| 响应 | `{ "ok": true, "total": N, "by_table": { 表名: 行数 } }` |
| 错误码 | 401 鉴权失败 / 503 接口未开 / 422 来料校验失败（未知表、缺唯一键列）/ 500 落库异常 |

**幂等口径（关键）**：信号侧按各表**加固唯一键**（含 `trade_date`）做
`INSERT ... ON DUPLICATE KEY UPDATE`（MySQL）/`ON CONFLICT DO UPDATE`（SQLite 单测）：

| 表 | 加固唯一键 | COALESCE（不被后到空值覆盖） |
| --- | --- | --- |
| `qmt_trade` | `(account_id, trade_date, traded_id)` | `signal_trade_date`, `traded_time_east8` |
| `qmt_order` | `(account_id, trade_date, order_id)` | `signal_trade_date`, `order_time_east8` |
| `qmt_position_snapshot` | `(account_id, trade_date, ts_code, snapshot_type)` | — |
| `qmt_account_daily` | `(account_id, trade_date, snapshot_type)` | — |

> 与设计文档原始 DDL（`(account_id, traded_id)` 不含 trade_date）相比，本次**纳入 trade_date** 为加固——
> 防 QMT 订单号 / 成交号跨日复用串号；与执行侧 `repository_unique_with_trade_date=True` 默认口径一致。
> 同一行重传只更新不新增，故网络抖动下「下轮重跑」绝对安全。

**事务**：整批 records 在一个事务内 upsert，全部成功才 commit 返回 200；任一记录失败 → 整批 rollback + 非 2xx。
执行侧逐行 POST（records 长度=1），据非 2xx 保该行 `synced=0` 下轮重试。

---

## 四、字段映射（watchlist item → 执行侧 SelectedStockRow）

信号侧对外契约 `LimitUpWatchlistItem` 与执行侧盘中契约 `SelectedStockRow` 命名/类型有差异，
**唯一映射点**在执行侧 `qmt_strategy/watchlist/remote_watchlist.py:watchlist_item_to_selected`：

| 执行侧 SelectedStockRow | ← 信号侧 item 字段 | 转换 |
| --- | --- | --- |
| `ts_code` | `ts_code` | 原样 |
| `trade_date` | `trade_date` | ISO→date（信号日 T） |
| `target_trade_date` | （对齐 today） | 落库统一用今天买入日，loader 按 `target=today` 读得到 |
| `tradable_flag` | `tradable_flag`（字符串） | `== "TRADABLE"` → bool |
| `role` | `role_tags`（列表） | 取首个标签 |
| `signal_close` | `close` | 数值/字符串→Decimal（兼容两种 JSON 形态） |
| `leader_strength_score` / `continuation_prob` / `next_day_premium_prob` | 同名 | →Decimal |
| `boost` / `fail_conditions` | `boost_conditions` / `fail_conditions` | 原样列表 |
| `strategy_family` / `setup` / `market_state` | 同名 | 原样 |
| `limit_up_price` / `reasonable_open_*` / `first_board_vol` / `float_mktcap` | 信号侧契约不含 | 留空 → 执行侧 `board_rules` 按 `signal_close + 板块` 兜底现算（`price_source=LOCAL_CALC`） |

---

## 五、执行逻辑（盘前 / 盘后时序）

执行侧 `DailyScheduler`（东八区钟点驱动）各阶段：

1. **盘前 PREWARM（≥08:55，<收盘）**：
   `connect_and_subscribe` →（新增）`WatchlistPrefetcher.prefetch(today)`：`prev_open(today)=T` → `GET /internal/watchlist?date=T`
   → 映射为 `SelectedStockRow` → 落本机 SQLite `watchlist` 表（latest-wins）→ `engine.prewarm` 从本机库装载当日名单。
   报告约 08:31 就绪，早于 08:55，拉取时点充裕。
2. **竞价 / 盘中**：只读本机内存名单与本机 SQLite，**不发任何跨网络请求**。
3. **收盘 CLOSE_BATCH（默认 15:05）**：收盘快照 + 对账，`flush` 持久化队列。
4. **盘后 SYNC（默认 15:35）**：`RemoteSyncJob` 取本机 `qmt_*` 当日 `synced=0` 行，**逐行经
   `HttpIngestQmtRepository` POST 到 `/qmt/ingest`**；成功标 `synced=1`，失败保 `synced=0` 下轮重试，
   完成后做条数对账（本地剩余 `synced=0` 是否归零）。

> 复盘触发时点：信号侧若按「当日」消费 `qmt_*`，应在执行侧盘后同步完成后再跑（可由
> `qmt_account_daily` 当日 `CLOSE` 行是否到齐判定）。本变更不引入新标志表。

---

## 六、失败与降级口径

| 场景 | 口径 |
| --- | --- |
| 盘前 watchlist 拉取失败（网络/非2xx/解析） | **不抛、不崩溃**：记 warn 返回 0，本机库保持原状；loader 读不到当日名单 → 自动降级「只守仓、不开新仓」 |
| watchlist 返回空集 | 合法（当日无 READY 报告/无候选）：不删本机旧名单，按空名单处理 |
| 回流单行 POST 失败 | 该行保 `synced=0`，不中断整表，下轮 SYNC 重试；幂等保证重传不产生重复行 |
| 回流来料非法（未知表/缺唯一键） | 信号侧 422；执行侧数据由 mappers 生成正常不触发，触发即记日志定位 |

**关键不变量**：盘中交易**完全不依赖跨服务器网络**；HTTP 仅服务「盘前一次拉取」与「盘后批量回流」，二者皆可重试、幂等。

---

## 七、安全面

- **鉴权**：`X-Internal-Token`（机器对机器，与登录 JWT 无关）；token 经文件优先解析（不落 .env 明文、不进日志）。
- **Windows 交易机零入站端口**：执行侧只出站，不监听任何端口。
- **信号侧入站口**：`/api/internal/*` 建议加来源 IP 白名单 / 反向代理限流；MySQL 不对 Windows 开放。
- **执行侧零三方依赖**：HTTP 客户端用 stdlib `urllib`，交易机依赖面不扩大（项目硬依赖仅 `tzdata`）。

---

## 八、配置项清单

### 信号侧（`backend/.env`）

| 变量 | 说明 |
| --- | --- |
| `WATCHLIST_EXPORT_INTERNAL_TOKEN` / `_FILE` | watchlist 导出接口 token（已存在） |
| `QMT_INGEST_INTERNAL_TOKEN` / `_FILE` | 回流接口 token（缺省回落 watchlist token） |

### 执行侧（Windows `.env` / 系统环境，`QMT_*`）

| 变量 | 说明 |
| --- | --- |
| `QMT_SIGNAL_BASE_URL` | 信号侧服务根，如 `http://<信号侧IP>:8000` |
| `QMT_SIGNAL_INTERNAL_TOKEN` / `_FILE` | `X-Internal-Token` 值（文件优先，建议落盘不进 .env） |
| `QMT_HTTP_TIMEOUT_SECONDS` | 单次接口超时秒数（默认 10） |

> 选路：配了 `QMT_SIGNAL_BASE_URL` → 回流走 HTTP（`HttpIngestQmtRepository`）+ 盘前启用 watchlist 拉取；
> 仅配 `QMT_MYSQL_DSN`（未配 base_url）→ 回落旧直连 MySQL 通道；都没配 → 仅本地、不同步。

---

## 九、两侧代码落地索引

### 信号侧（`stock-ah-premium-ai/backend`）

| 文件 | 内容 |
| --- | --- |
| `app/db/models/qmt.py` | 新增 `QmtTrade/QmtOrder/QmtPositionSnapshot/QmtAccountDaily` ORM（加固唯一键） |
| `alembic/versions/20260613_0053_create_qmt_tables.py` | 建四表迁移（down_revision=0052） |
| `app/schemas/qmt_ingest.py` | ingest 请求/响应契约 |
| `app/services/qmt_ingest_service.py` | 逐记录反序列化 + 方言自适应幂等 upsert + COALESCE + 白名单校验 |
| `app/api/routes_qmt_ingest.py` | `POST /internal/qmt/ingest` + `X-Internal-Token` 鉴权 |
| `app/core/config.py` | `qmt_ingest_internal_token(_file)` + `resolve_qmt_ingest_internal_token()`（回落 watchlist token） |
| `app/main.py` | 注册 `qmt_ingest_router` |
| `app/api/routes_watchlist_export.py` | watchlist 拉取接口（已存在，复用） |
| `tests/test_qmt_ingest.py` | 鉴权 / 幂等 / COALESCE / 类型 / 校验 / 批量 |

### 执行侧（`astock-quant-ai/qmt_strategy`）

| 文件 | 内容 |
| --- | --- |
| `qmt_strategy/common/http_client.py` | stdlib urllib JSON GET/POST 客户端（token 头、超时、非2xx/网络异常→`SignalHttpError`） |
| `qmt_strategy/storage/http_ingest_repository.py` | `HttpIngestQmtRepository`（实现 `QmtRepository`，逐行 POST `/qmt/ingest`，失败上抛） |
| `qmt_strategy/watchlist/remote_watchlist.py` | item→SelectedStockRow 映射 + `WatchlistPrefetcher`（盘前拉取落本机库） |
| `qmt_strategy/config/settings.py` | `signal_base_url` / `signal_internal_token(_file)` / `http_timeout_seconds` + `resolve_signal_token()` |
| `qmt_strategy/app/run.py` | `_build_signal_client` / `_build_remote_repo`（HTTP 优先）+ 装配 watchlist prefetch 钩子 |
| `qmt_strategy/app/scheduler.py` | PREWARM 先 prefetch 后 `engine.prewarm`（`watchlist_prefetch` 钩子，缺省跳过向后兼容） |
| `tests/test_http_client.py` `test_http_ingest_repository.py` `test_watchlist_prefetch.py` `test_scheduler_prefetch.py` | 对应单测 |

---

## 十、阶段 · 任务 · 验收 · 依赖

### 阶段 1 · 信号侧落地（接口托管）
- 任务：qmt_* 四表 ORM + 迁移 0053；ingest schema/service/route/鉴权/配置；注册路由。
- **验收**：迁移 `alembic upgrade` 在 MySQL 生成四表（BIGINT 自增、加固唯一键、InnoDB/utf8mb4）；
  `tests/test_qmt_ingest.py` 全绿（鉴权 503/401、幂等不增行、COALESCE 不被空覆盖、未知表 422、批量计数）。

### 阶段 2 · 执行侧落地（客户端）（依赖阶段 1 的契约）
- 任务：HTTP 客户端；`HttpIngestQmtRepository`；watchlist 映射 + `WatchlistPrefetcher`；settings；run/scheduler 接线。
- **验收**：`HttpIngestQmtRepository` 满足 `QmtRepository` 协议、逐表 POST 载荷正确、失败上抛；
  prefetch 按 `prev_open(today)` 拉取、映射正确、失败降级不落库；PREWARM 先 prefetch 后 prewarm；对应单测全绿。

### 阶段 3 · 联调与上线（依赖阶段 1、2）
- 任务：信号侧配 token + 跑迁移；执行侧配 `QMT_SIGNAL_BASE_URL` + token；连通性验证（盘前能拉到名单、盘后回流落库）。
- **验收**：执行侧盘前 GET 拉到当日 watchlist 并落本机库；盘后 POST 回流后信号侧 `qmt_*` 出现当日行、
  重跑不增行（幂等）；执行侧 `synced` 全部归零（对账通过）。

### 依赖顺序
```
阶段1 信号侧接口（表/迁移/ingest/鉴权）
  → 阶段2 执行侧客户端（http_client/ingest_repo/prefetch/接线）
  → 阶段3 联调上线（配置/连通性/幂等对账）
```

---

## 十一、与 doc/05 的关系

- 本文是 `doc/05 §4`（信号侧配合项）的 **HTTP 落地**：§4.1 watchlist 交付 = 接口 ①（执行侧拉取）；
  §4.2 回流 = 接口 ②（执行侧推送，替换原「盘后直连 MySQL 同步」）。
- doc/05 的本机 SQLite 数据栈、异步写队列、盘后 `RemoteSyncJob`、关键不变量（不阻塞交易 / 重启幂等 / 同步幂等）**全部不变**；
  本次只把 `RemoteSyncJob` 的「远端写端」从 `MySqlQmtRepository` 换为 `HttpIngestQmtRepository`（协议接缝替换，业务模块零改动），
  并新增盘前 watchlist 拉取钩子。
