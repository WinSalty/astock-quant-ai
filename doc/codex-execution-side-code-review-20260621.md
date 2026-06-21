# astock-quant-ai 执行侧代码评审

评审范围：`qmt_strategy` 执行侧的盘前名单预取、本机 SQLite 覆盖、买入下单、持仓卖出、调度与交易安全门控。

## 问题

### P0-EXEC-1：`QMT_SELL_PASS_LIVE=false` 时仍可真实开新仓，执行侧会形成只买不自动卖的运行态

代码位置：
- `qmt_strategy/qmt_strategy/app/run.py:446`-`457`
- `qmt_strategy/qmt_strategy/app/scheduler.py:147`-`153`
- `qmt_strategy/qmt_strategy/config/settings.py:431`-`454`
- `qmt_strategy/qmt_strategy/order/order_executor.py:356`-`370`

启动装配在 `sell_pass_live=false` 时把 `sell_books_provider` 置为 `None`，调度器盘中遇到 `provider=None` 直接返回，不调用 `engine.run_sell_pass`。`Settings.assert_safe_to_trade()` 只校验“卖出链已开但没配单票浮亏止损”这一种组合，没有禁止“卖出链关闭、买入仍可下单”。买入唯一下单点只看 `kill_switch` 等买入闸门，不检查卖出链是否接入。

触发场景：生产默认配置 `QMT_SELL_PASS_LIVE=false`，同时 `QMT_KILL_SWITCH=false`，盘前清单里有可交易标的，竞价/定盘窗产出 BUY 决策。

错误结果：执行侧可以开出新仓，但盘中不会自动跑止损、破位、炸板、尾盘了结等卖出决策。仓位只能依赖人工、券商侧手动处理或后续重新配置，代码层面没有把“不能自动卖”拦在开仓之前。

### P0-EXEC-2：合法空名单不会覆盖本机旧名单，旧候选可能在同一天继续被加载

代码位置：
- `qmt_strategy/qmt_strategy/watchlist/remote_watchlist.py:189`-`199`
- `qmt_strategy/qmt_strategy/watchlist/remote_watchlist.py:244`-`261`
- `qmt_strategy/qmt_strategy/storage/watchlist_source.py:92`-`113`
- `qmt_strategy/tests/test_watchlist_prefetch.py:188`-`199`
- `qmt_strategy/tests/test_watchlist_source.py:336`-`345`

盘前预取把信号侧 2xx 且 `items=[]` 解释成合法空名单，并直接返回 `0`，不会调用 `save_watchlist`。本机 SQLite 的 `save_watchlist([])` 也明确早返回，不删除任何日期的旧名单；测试用例还固定了“空清单不删本机旧名单”的行为。

触发场景：同一个 `target_trade_date` 本机库里已经有旧名单，后续盘前重拉时信号侧最新 `READY` 版本为空清单，例如新一轮报告无候选、空仓短路、选股行被清空、或导出行全被校验跳过。

错误结果：本机 `watchlist` 表仍保留旧行。`Engine.prewarm(today)` 后续从本机库按 `target_trade_date=today` 加载，可能把已经被信号侧移除的股票重新放进当天交易计划，继续参与权重分配和买入决策。

### P1-EXEC-3：同票多张卖单对账时，旧撤单会覆盖新在途单，持仓可能被错误复位

代码位置：
- `qmt_strategy/qmt_strategy/app/main.py:489`-`503`
- `qmt_strategy/qmt_strategy/app/main.py:507`-`510`
- `qmt_strategy/qmt_strategy/position/position_manager.py:585`-`590`
- `qmt_strategy/qmt_strategy/app/main.py:1153`-`1158`

盘前/重连对账把券商所有卖单压成 `state_by_code[ts_code]`。同一股票多张卖单并存时，代码注释说“撤/废优先于在途”，实现也是只要当前 `term` 是 `CANCELLED`、`REJECTED`、`ERROR` 就覆盖前值。`PositionManager.reconcile_stuck_selling` 收到这些终态后会把 `SELLING` 单元复位为可卖态；而真正仍在途的 `ACTIVE/REPORTED/PARTIAL` 卖单被同票旧撤单掩盖。

触发场景：某股票先挂出卖单后撤掉或被拒，随后同日又挂出新的卖单且仍在途；或者券商查询返回同票历史撤单和当前活跃卖单混在一起。

错误结果：执行侧会把仍有活跃卖单的持仓单元从 `SELLING` 复位回 `HOLDING`，下一轮卖出巡检不再被 `unit.state == SELLING` 拦住，可能再次发出卖单，造成重复卖、超卖废单或收盘对账异常。

### P1-EXEC-4：`DATA_MISSING` 在执行侧内部契约和卖出决策之间已经分裂

代码位置：
- `qmt_strategy/qmt_strategy/contracts/models.py:94`-`100`
- `qmt_strategy/qmt_strategy/watchlist/remote_watchlist.py:115`-`122`
- `qmt_strategy/qmt_strategy/watchlist/remote_watchlist.py:166`-`168`
- `qmt_strategy/qmt_strategy/watchlist/watchlist_loader.py:268`-`294`
- `qmt_strategy/qmt_strategy/position/sell_decider.py:120`-`122`
- `qmt_strategy/qmt_strategy/position/sell_decider.py:209`-`210`

执行侧模型注释仍写着：`data_missing=True` 表示核心交易指标缺测，买入侧放弃买入，已过 T+1 的持仓单元强制清仓。实际映射层把 `DATA_MISSING` 转成 `data_missing=True` 后，只在买入前置过滤层剔出可交易名单；卖出决策里明确写了缺测强制清仓分支已下线，后续完全依赖 xtdata 实时盘口扳机。

触发场景：信号侧对某只已持仓股票下发 `tradable_flag=DATA_MISSING`，而执行侧当日该持仓已过 T+1。

错误结果：执行侧不会因为缺测标记主动清仓；只要实时盘口没有触发破位、炸板、浮亏止损等卖出条件，持仓会继续保留。执行侧同一份代码里“契约注释、测试语义、真实卖出行为”不一致，复盘时会误以为缺测风险已经被卖出链处理。

