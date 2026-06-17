# 18 · watchlist 买入前置过滤层抽象 + 禁买四板及以上/ST — 设计与台账

> 处理文档（CLAUDE.md 文档分类 ④）。处理中放 `doc/` 根；全部落地后高层结论并入
> [`评审与修复状态概要.md`](评审与修复状态概要.md) 并整体归档。
> 本文不含工期/工时预估（CLAUDE.md 硬规则），按阶段 + 验收 + 依赖顺序组织。

## 1. 背景与问题

执行侧（`qmt_strategy`）此前**没有**统一的「watchlist 交易处理前过滤层」抽象：

- 「绝不买入 ST」是硬规则，但实现是**散落**在三层的内联闸门，各自调用
  `common/universe_filter.py` 的 `is_st_stock` 谓词：
  - `watchlist_loader._process_row`（盘前装载 → 转观察名单）；
  - `entry_router._should_skip`（闸门 0 → SKIP 决策）；
  - `order_executor.place`（`decision.is_st` → 唯一下单点拒单）。
- `universe_filter.py` 只是**一组松散谓词**（`is_st_stock` / `is_allowed_prefix` /
  `is_tradable_universe`），不是一个「有序规则集 + 结构化裁决」的过滤**层**抽象——
  新增一条禁买规则时没有「单一落点」，只能再到三处各抄一遍内联判断。
- **完全没有**「连板高度（四板及以上）」相关的禁买过滤。

### 1.1 数据现实（关键）

信号侧（`stock-ah-premium-ai`）的 watchlist 对外契约 `LimitUpWatchlistItem` **已经下发**两个连板维度字段：

- `board_level: int | None` — 连板高度（KPL「N 天 M 板 / X 连 / 首板」解析得来）；
- `tier: str`（**非空**）— 入选分层 `FIRST_BOARD` / `CHAIN` / `HIGH_BOARD`。

信号侧分桶口径（`limit_up_push_service._stocks_by_board_level`）：
`FIRST_BOARD = board_level==1`、`CHAIN = board_level∈{2,3}`、`HIGH_BOARD = board_level>=4`。
**故「四板及以上」⟺ `board_level >= 4` ⟺ `tier == "HIGH_BOARD"`。**

但执行侧 `remote_watchlist.watchlist_item_to_selected`（两侧唯一字段映射点）**没有消费**这两个字段——
它们在执行边界被丢弃，从未进入下单链路。

## 2. 目标

1. 新增**买入前置过滤层抽象** `common/buy_prefilter.py`：一个**有序的「禁买」硬规则集**，
   对每只候选产出结构化裁决 `PrefilterVerdict(allowed, rule_code, reason)`，成为所有买入禁止规则的**单一来源**。
2. 在该层内置两条规则：
   - **RULE_ST**：绝不买入 ST/退市整理（复用 `universe_filter.is_st_stock`，口径不变）；
   - **RULE_HIGH_BOARD**：禁买四板及以上 —— `board_level >= forbid_board_level_min`（默认 4），
     或 `tier == HIGH_BOARD` 兜底（`tier` 恒有值、`board_level` 可空，双源更稳）。
3. 把 `board_level` / `tier` 沿现有 `is_st`/`name` 同一条管道补齐透传：
   `SelectedStockRow → HTTP 映射 → 本机 SQLite（表/迁移/编解码）→ TradableEntry → PlanRow → EntryDecision`。
4. 三层（loader / entry_router / order_executor）**统一委托**给该过滤层，既消除散落的 ST 内联判断，
   又叠加四板禁买，形成「绝不买入」的三层冗余防线（与既有禁买 ST 硬规则同构）。

### 2.1 非目标 / 边界口径

- **只在有正向证据时拦截四板**：`board_level` 与 `tier` 都无法判定高板时**放行**（无法证明它是 4+ 板，
  绝不无证据地全拦）。生产中信号侧恒下发 `tier`，故 4+ 板恒被 `tier==HIGH_BOARD` 命中。
- 本次**不改信号侧**：字段已在 HTTP 契约里，执行侧消费即可。
- 板块前缀白名单（`is_allowed_prefix`，科创/北交/B 股剔除）是「目标市场段」口径、与「单票禁买」正交，
  仍保留为独立的 universe 步骤，不并入本过滤层。
- ST 判定口径与阈值**完全不变**（最严格：显式 `is_st=True` 或当日证券名含 `ST/退`）。

## 3. 过滤层抽象设计（`common/buy_prefilter.py`）

```
CandidateView(ts_code, name, is_st, board_level, tier)   # 过滤层只依赖的最小视图
PrefilterVerdict(allowed: bool, rule_code: str, reason: str)

is_high_board(board_level, tier, *, min_level=4) -> bool
evaluate(view, *, high_board_min_level=4) -> PrefilterVerdict
```

- `evaluate` 按**优先级顺序**逐条跑规则，命中第一条「禁买」即返回该裁决（含 `rule_code` 与中文 `reason`）；
  全部通过返回 `allowed=True`。规则顺序：先 ST（与既有「闸门 0：ST 优先级最高」一致），再四板。
- 规则以「有序函数列表」组织，未来加规则 = 追加一个纯函数，落点唯一。
- 纯函数、无 I/O、确定性，便于单测；三层各自把自己的行类型适配成 `CandidateView` 后调用。

## 4. 数据透传补齐（与 `is_st`/`name` 同路径）

| 落点 | 改动 |
|---|---|
| `contracts/models.py` | `SelectedStockRow` / `TradableEntry` / `PlanRow` / `EntryDecision` 末尾各加 `board_level: Optional[int]`、`tier: Optional[str]`（带默认 None，向后兼容）；`TradableEntry.to_plan_row` 透传 |
| `watchlist/remote_watchlist.py` | `watchlist_item_to_selected` 映射 `board_level`（`_to_int`）、`tier` |
| `storage/schema.py` | `TABLE_META["watchlist"].columns` 加两列；DDL 加 `board_level INTEGER, tier TEXT`；`_COLUMN_MIGRATIONS["watchlist"]` 加 `("board_level","INTEGER"),("tier","TEXT")`（旧库幂等补列） |
| `storage/mappers.py` | `selected_to_row` / `row_to_selected` 加 `board_level`（int 透传）、`tier`（text 透传），保证 SQLite 无损 round-trip |

## 5. 三层接线

| 层 | 改动 | 命中行为 |
|---|---|---|
| `watchlist_loader._process_row` | 把内联 ST 判定替换为 `buy_prefilter.evaluate(view, high_board_min_level=settings.forbid_board_level_min)`；前置的 universe 前缀步骤不动 | 转观察名单（不下单），日志带 `rule_code`/`reason` |
| `entry_router._should_skip` | 把闸门 0（ST）替换为过滤层调用；market_state / tradable_flag 闸门不动 | 产 `SKIP` 决策并留痕 |
| `entry_router._build_decision` | 把 `plan.board_level`/`plan.tier` 锚到 `EntryDecision`（与既有 `is_st` 同样锚定） | —— |
| `order_executor.place` | 把 `if decision.is_st` 终闸替换为按 decision 构造 `CandidateView` → `evaluate`（含四板）；阈值取 `settings.forbid_board_level_min` | 拒发买单、留痕 + 决策采集 |

## 6. 配置项

`config/settings.py` 新增 `forbid_board_level_min: int = 4`（`QMT_FORBID_BOARD_LEVEL_MIN`）。
- 默认 4 = 四板及以上禁买（硬规则）；以 is-not-None 守卫解析，显式配置优先。
- 调大（如 999）可事实上放宽该护栏（仅离线/特殊场景）。README §8.1 同步登记。

## 7. 阶段划分与依赖顺序

- **阶段 A（契约与数据管道）**：models 加字段 → HTTP 映射 → SQLite schema/迁移/mappers。
  - 验收：`SelectedStockRow` 经 `selected_to_row → row_to_selected` 与
    `watchlist_item_to_selected` 两条路径，`board_level`/`tier` 无损 round-trip；既有 645 用例不回归。
- **阶段 B（过滤层抽象）**：实现 `common/buy_prefilter.py` + 单测（ST / 四板 / 兜底 tier / 放行）。
  - 依赖 A 的 `CandidateView` 字段口径。验收：规则优先级、阈值边界（3 板放行 / 4 板拦）、双源（board_level 缺失走 tier）全覆盖。
- **阶段 C（三层接线 + 配置）**：settings 加项 → loader / entry_router / order_executor 委托过滤层。
  - 依赖 A、B。验收：三层各自对「四板票」与「ST 票」均拦截；既有 ST 用例不回归；新增四板用例三层各一。
- **阶段 D（文档）**：README §3「绝不买入 ST」补「禁买四板及以上」；§8.1 加 `QMT_FORBID_BOARD_LEVEL_MIN`；本台账记录评审与修复。
  - 验收：README 现状口径与代码一致；本文随评审轮次续写。

## 8. 评审与修复台账

### 8.1 落地结论（第一轮）

阶段 A/B/C/D 全部落地，独立自评审一轮，全量测试 **667 passed**（基线 645 + 过滤层单测 14 + 三层接线/round-trip 8）。

实际改动文件：
- 新增 `common/buy_prefilter.py`（过滤层抽象）、`tests/test_buy_prefilter.py`（14 用例）；
- `contracts/models.py`（4 个 dataclass 加 board_level/tier + to_plan_row 透传）；
- `watchlist/remote_watchlist.py`（HTTP 映射）；`storage/schema.py`+`storage/mappers.py`（SQLite 表/迁移/编解码）；
- `config/settings.py`（`forbid_board_level_min` + from_env）；
- `watchlist/watchlist_loader.py` / `entry/entry_router.py` / `order/order_executor.py`（三层委托过滤层）；
- `tests/conftest.py`（构造器加 board_level/tier 参数）、`tests/test_review_fixes_2026_06.py`（四板三层 + round-trip 8 用例）；
- `doc/README.md`（§3/§6/§8.1 现状口径）。

### 8.2 自评审发现与修复

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| R1 | P1 | `mappers.row_to_selected` 注释称「旧库无该列时 _g 返回 None」——失实：`sqlite3.Row` 对不存在列**抛 IndexError**（非返 None）。安全实际来自 `init_db→_apply_column_migrations` 对旧库强制补列 + 消费行均来自 `SELECT *`（迁移后）。 | 已改注释，点明保护来自强制迁移、并警示绝不可在未迁移库上 SELECT 老列集再喂本方法。 |
| R2 | P2 | `is_high_board` 原在 `min_level>4` 时禁用 tier 兜底 → 「board_level 缺失 + tier=HIGH_BOARD（确为 4+ 板）」会**静默放行**，与「绝不买入四板及以上」取向相悖。 | 改为 **fail-closed**：tier=HIGH_BOARD 在规则启用（min_level>0）时一律拦；放宽（min_level>4）只作用于 board_level **已知**的精确口径，板高缺失时按 4+ 保守拦。新增专项回归用例 `test_high_board_tier_fallback_fails_closed_above_threshold`。 |
| R3 | P2 | `forbid_board_level_min` 无校验/钳制，误配负数会静默关闭硬规则、无显式提示。 | 维持「无钳制」（与项目既有 int 配置一致），但该值为非敏感字段、已纳入 `Settings.redacted()` 启动快照，运维可在启动日志核对生效值；README §8.1 标注「置 0/负=关闭」。 |

### 8.3 数据路径确认（生产）

- 生产 watchlist 源 = `SqliteSelectedStockSource`（`local_stack`），盘前由 `WatchlistPrefetcher.watchlist_item_to_selected`→`save_watchlist`(`selected_to_row`) 落本机 SQLite，盘中 `fetch`(`row_to_selected`) 读出——**board_level/tier 全程透传，生产路径完整覆盖**。
- `DbSelectedStockSource`（路径 A 直读 MySQL）为死代码（两侧已改 HTTP），未参与生产装配。
- `order_executor.try_next_best` 转次优仍走完整 `place`，过滤层对次优同样生效。

