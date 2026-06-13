# qmt_strategy · 打板量化 QMT 侧执行引擎

部署在 **Windows VPS + miniQMT（国金证券）** 上的策略执行引擎。消费信号侧 watchlist 契约
（「今天关注哪些票」）+ 自采实时行情，自主决定择时 / 价位 / 仓位，独立完成买入 / 卖出，
并承担全部交易风险。设计见 `../doc/03-QMT侧执行引擎-开发设计.md`，开发计划见 `../doc/04-QMT侧执行引擎-开发计划.md`。

## 为什么能在非 Windows 上开发与测试

`xtquant`（`xttrader` / `xtdata`）只在 Windows + miniQMT 运行期可用。本工程把它和 MySQL、信号侧取数、
交易日历等所有外部依赖抽象为 `contracts/protocols.py` 的 `Protocol`，业务模块只依赖接口；单测一律用
fake 对象（`contracts/xt_objects.py`）与内存实现，因此在 macOS / Linux / Windows 都能跑通逻辑与全部单测。
真实部署时注入 xtquant / PyMySQL 实现即可。

## 目录结构

```
qmt_strategy/
├── contracts/      # 锁定契约层：枚举/数据结构/接口协议/异常/xtquant fake（单一来源，模块间不互相依赖实现）
├── config/         # 执行侧配置 Settings（§7.1，敏感项不入库/不入日志）
├── common/         # 时间口径/代码归一/交易日历/板块涨跌幅/universe 过滤/结构化日志
├── connection/     # 连接守护（§2.2 xttrader 生命周期，无 on_connected、断线重建新 session）
├── watchlist/      # 盘前装载 WatchlistContext（§2.3–2.6，两路取数+universe兜底+空仓闸门+降级只守仓）
├── auction/        # 集合竞价轮询与四因子（第三节，自写定时器不依赖回调，降级 A/B）
├── entry/          # 建仓决策路由 + 五类策略（第四节 entry_router，只出决策不下单）
├── order/          # 下单执行 + 本地台账（第四节 order_executor，唯一下单点、幂等、TTL撤单、一字秒封处理）
├── position/       # 持仓状态机 + 卖出/连板决策（第五节，守 T+1、先验/纯技术分叉、竞价/分时定夺）
├── risk/           # 风控护栏（§5.4，账户/单票阈值、FROZEN、空仓闸门、卖量钳制）
├── data_writer/    # 回流写库：规整+幂等upsert+仓储（第六节，qmt_* 四表，COALESCE 不空覆盖）
├── reconcile/      # 对账（§6.7 四类勾稽 + signal_trade_date 回填）
└── app/            # 进程编排入口（run_forever 常驻）
```

## 运行单测

```bash
# 在仓库上层创建的 venv（pytest 已装）
../.venv/bin/python -m pytest -q
```

## 安全与口径约束

- 账户 / token / DSN 等敏感项从环境变量注入，不硬编码、不入库、不写日志（`Settings.redacted()` 脱敏）。
- 时间一律经 `common/time_utils.py` 在「东八区 ↔ UTC naive」之间换算，禁手工 ±8h（§6.6）。
- 守 T+1 双保险：`earliest_sellable_date` 业务闸 + `can_use_volume` 量闸（§5.8）。
- 竞价择时总开关 `QMT_AUCTION_TIMING_ENABLED` 默认 **False**，§7.2 真实交易日实测通过前不得开（§7.1.6）。
- 全局熔断 `QMT_KILL_SWITCH=true` 时只采集、不下单（§7.1.5）。
