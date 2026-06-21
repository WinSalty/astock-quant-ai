# 两系统联合交易代码评审

评审范围：`stock-ah-premium-ai` 信号侧 watchlist 供数与 `astock-quant-ai/qmt_strategy` 执行侧预取、落库、买入、持仓卖出之间的联合交易语义。

## 问题

### P0-XCUT-1：信号侧最新空名单与执行侧本机旧名单叠加，会把已撤销的旧信号继续拿去交易

代码位置：
- 信号侧 `backend/app/api/routes_watchlist_export.py:93`-`119`
- 信号侧 `backend/app/services/limit_up_push_service.py:675`-`678`
- 执行侧 `qmt_strategy/qmt_strategy/watchlist/remote_watchlist.py:244`-`261`
- 执行侧 `qmt_strategy/qmt_strategy/storage/watchlist_source.py:92`-`113`
- 执行侧 `qmt_strategy/qmt_strategy/app/scheduler.py:210`-`235`

信号侧最新 `READY` 版本可能合法返回空清单，例如空仓日、无候选、当前 `prompt_version` 清空旧行、或落表失败后没有选股行。执行侧收到 `items=[]` 后返回 `0`，不写库、不删除本机同日旧行；调度器把 `saved==0` 当成合法空名单，不重试，随后照常 `engine.prewarm(today)` 读取本机库。

触发场景：同一个买入日早些时候本机已经落过非空名单；之后信号侧重生成同一信号日，最新版本为空，或导出结果变空。执行侧盘前重跑预热，HTTP 成功但 `items=[]`。

错误结果：执行侧不会得到“当日应为空”的状态，而是继续使用本机旧名单。被信号侧撤销、降级或替换掉的股票仍可能参与当天买入，联合链路的 latest-wins 语义在空集场景失效。

### P0-XCUT-2：`DATA_MISSING` 强制清仓契约两端不一致，已持仓缺测票不会按信号侧口径退出

代码位置：
- 信号侧 `backend/app/schemas/limit_up_watchlist.py:26`-`39`
- 信号侧 `backend/app/services/limit_up_push_service.py:805`-`839`
- 执行侧 `qmt_strategy/qmt_strategy/contracts/models.py:94`-`100`
- 执行侧 `qmt_strategy/qmt_strategy/watchlist/remote_watchlist.py:166`-`168`
- 执行侧 `qmt_strategy/qmt_strategy/position/sell_decider.py:120`-`122`
- 执行侧 `qmt_strategy/qmt_strategy/position/sell_decider.py:209`-`210`

信号侧契约和落表注释都把 `DATA_MISSING` 定义为“放弃买入 + 已过 T+1 持仓强卖”。执行侧映射后保留 `data_missing=True`，但真实卖出决策已经下线缺测强卖分支，只在买入前置过滤层使用该标记。

触发场景：某只股票昨日买入、今日已可卖；信号侧今日对同一股票输出 `DATA_MISSING`，原因可能是信号日核心行情、连板高度、先验或封板时序缺测。

错误结果：联合交易口径显示该票应按缺测最保守处理，但执行侧不会因该标记卖出。只要 xtdata 盘口没有触发其它卖出条件，系统会继续持有一只信号侧明确标为核心数据缺测的股票。

### P1-XCUT-3：封板时序数据源整体异常时，信号侧不标缺测，执行侧也无法识别这个缺口

代码位置：
- 信号侧 `backend/app/schemas/limit_up_watchlist.py:22`-`30`
- 信号侧 `backend/app/services/limit_up_push_service.py:731`-`741`
- 信号侧 `backend/app/services/limit_up_push_service.py:820`-`829`
- 执行侧 `qmt_strategy/qmt_strategy/watchlist/watchlist_loader.py:268`-`294`
- 执行侧 `qmt_strategy/qmt_strategy/order/order_executor.py:323`-`354`

`first_limit_time`、`last_limit_time`、`open_times` 是 watchlist 1.2.0 明确新增的封板时序字段，1.3.0 又把“封板时序无源”列入缺测口径。但信号侧在 `limit_list_d` 整体非 `OK` 时不把三项加入缺测集合，导出的行不会带 `DATA_MISSING`。执行侧买入三层硬拦只消费 `data_missing=True`；字段本身为 `None` 时不会等价触发缺测硬拦。

触发场景：`limit_list_d` 整体失败或空返回，当日候选其它核心字段齐全，并被信号侧落为 `TRADABLE` 或普通 `BLOCKED/WATCH`。

错误结果：联合链路把“封板时序源整体不可用”当成普通可消费清单传递。执行侧可能在不知道开板次数、首封/末封时刻的情况下继续对候选做买入或观察，封板质量相关的硬风险没有进入跨系统契约。

### P1-XCUT-4：信号侧部分坏行被跳过后，执行侧仍用残缺清单做资金分配和交易

代码位置：
- 信号侧 `backend/app/api/routes_watchlist_export.py:133`-`160`
- 执行侧 `qmt_strategy/qmt_strategy/watchlist/remote_watchlist.py:226`-`254`
- 执行侧 `qmt_strategy/qmt_strategy/app/main.py:686`-`755`

信号侧单行校验失败时返回 200，并通过 `skipped_count` 表示有行被跳过。执行侧看到 `skipped_count>0` 只打告警，随后继续映射剩余 `items` 并落本机库；盘前强度权重只基于剩余 `tradable` 集合计算。

触发场景：同一批 watchlist 中一只或多只候选行字段不合法，尤其是强度高、排序靠前、或原本应占 top-N 名额的行校验失败。

错误结果：执行侧会基于残缺候选集重新计算 top-N 和资金权重。原本不该拿到名额的弱票可能进入 top-N，或者单票资金权重被放大，联合交易结果偏离信号侧完整清单的排序和资金分配语义。

### P2-XCUT-5：目标买入日只用响应顶层和执行侧 `today` 对齐，单条 item 的 `target_trade_date` 异常会被覆盖

代码位置：
- 信号侧 `backend/app/api/routes_watchlist_export.py:154`-`160`
- 执行侧 `qmt_strategy/qmt_strategy/watchlist/remote_watchlist.py:234`-`248`

信号侧响应顶层 `target_trade_date` 取第一条 item；执行侧只校验顶层 `target_trade_date` 是否等于 `today`，随后把每个 item 都映射为入参 `today`，不使用 item 自带的 `target_trade_date`。

触发场景：信号侧同一响应里混入个别目标买入日异常的 item，但第一条 item 的目标日正常；或者顶层字段正常、单条 item 异常。

错误结果：异常 item 会被执行侧强行改写成今天买入并进入本机库。该票原本属于其它买入日或脏数据行，却可能被当天交易逻辑消费，跨系统交易日一致性只在整批第一条层面生效。
