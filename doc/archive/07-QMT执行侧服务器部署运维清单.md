# QMT 执行侧服务器部署运维清单

> 文档日期：2026-06-13。本清单覆盖执行侧（Windows VPS / miniQMT）从空白服务器到接实盘小仓的部署与运维。
> 配套：`03-QMT侧执行引擎-开发设计.md`（设计）、`05-执行侧本地化改造-方案与改动说明.md`（单进程+SQLite）。
> 敏感信息（IP/账号/口令）见 `doc/Qmt生产服务器.txt`，**不写入本清单、不入库、不进日志**。
> 按项目约束：不含工期 / 工时预估，只给配置、步骤、清单与口径。

## 一、当前服务器状态（已就位）

生产服务器（Windows Server 2022 / 64 位 / C 盘空闲 ~160GB）已完成：

- ✅ OpenSSH Server（22 端口，免密钥登录已配，管理员 `administrators_authorized_keys` + 严格 ACL）
- ✅ Python 3.11.9（64 位，系统 PATH）+ pip；**SQLite 为 Python 自带 `sqlite3`（3.45.1），无需单独安装**
- ✅ Git 2.47.1（for Windows）
- ✅ 项目代码 + venv：`C:\qmt\qmt_strategy\`（venv 已装 `pytest`、`tzdata`），**379 个单测全部通过**
- ⬜ miniQMT + xtquant：待提供 QMT 客户端后安装（见 §五）

## 二、服务器配置要求

| 项 | 推荐 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows Server 2022 / Win10-11（64 位） | xtquant/miniQMT 仅 Windows；GUI 客户端，须带桌面 |
| CPU | 4 核 | 负载轻（低频交易 + 竞价 10 分钟轮询） |
| 内存 | 8 GB | Windows GUI + miniQMT + Python |
| 磁盘 | ≥60 GB SSD（100GB 省心） | 大头是 Windows 本体 + 页面文件 + 更新缓存；项目本身极小 |
| 网络 | 国内、低延迟、稳定到券商 | 打板对 9:20 前定盘敏感；带宽需求低，要稳 |

## 三、外部前置（账户/权限，非硬件）

- 国金证券资金账户（隔离账户）+ **程序化交易（量化）权限** + **分钟级行情权限**；
- miniQMT 客户端安装包（券商提供）；
- 信号侧回测 **go/no-go 通过**（无 go 不接实盘，设计文末上线次序）。

## 四、基础环境安装（复现步骤）

> 已在生产机执行；此处留作复现 / 重建参考。RDP 登录后管理员 PowerShell 执行。

```powershell
# 1) OpenSSH Server（远程命令行运维）
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service -Name sshd -StartupType Automatic
# 2) Python 3.11.9 64 位（国内镜像，静默装、加 PATH）
Invoke-WebRequest "https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-amd64.exe" -OutFile C:\qmt\py.exe
Start-Process C:\qmt\py.exe -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_test=0' -Wait
# 3) Git（国内镜像，静默）
Invoke-WebRequest "https://mirrors.huaweicloud.com/git-for-windows/v2.47.1.windows.1/Git-2.47.1-64-bit.exe" -OutFile C:\qmt\git.exe
Start-Process C:\qmt\git.exe -ArgumentList '/VERYSILENT','/NORESTART','/SP-' -Wait
# 4) 拉代码（公开仓库）+ venv + 依赖
cd C:\qmt; git clone https://github.com/WinSalty/astock-quant-ai
& "C:\Program Files\Python311\python.exe" -m venv C:\qmt\astock-quant-ai\qmt_strategy\.venv
$vpy="C:\qmt\astock-quant-ai\qmt_strategy\.venv\Scripts\python.exe"
& $vpy -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e C:\qmt\astock-quant-ai\qmt_strategy  # 含 tzdata
& $vpy -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pytest
# 5) 验证
cd C:\qmt\astock-quant-ai\qmt_strategy; & $vpy -m pytest -q   # 期望 379 passed
```

> **`tzdata` 必装**：时间口径用 `zoneinfo.ZoneInfo("Asia/Shanghai")`，Windows 无系统 IANA 时区库需 tzdata 兜底
> （已列入 pyproject `dependencies`，`pip install -e .` 自动带上）；否则 `import time_utils` 即报错。
> GitHub 在国内可能慢；如 `git clone` 不畅，可改用本机 `scp` 传项目压缩包，或配置代理。

## 五、接 xtquant 真实环境（待 miniQMT 提供后）

1. **安装并登录 miniQMT**（国金），账户须已开通程序化交易 + 分钟级行情权限；miniQMT 进程常驻、保持登录。
2. **确认 Python 版本与 xtquant 兼容**：在目标机 `import xtquant` 实测通过的版本为准（设计 §1.4）。
   当前装的是 3.11.9；**若 xtquant 要求其它版本，按需加装匹配版本**（单测是 3.9+ 通用，不受影响）。
   xtquant 来源：用 miniQMT 自带 Python，或 `pip install` 对应 wheel——以券商/实测为准。
3. **填 `adapters/xt_real.py` 与 `normalize.py` 的 `TODO(实测)`**：用 `vars(obj)`/`dir(obj)`/`print(tick)` 核对
   `xtconstant`（STOCK_BUY/FIX_PRICE/order_status 数值）、`order_stock` 签名、`StockAccount` 构造、
   回报对象字段名、`get_full_tick` 键名（设计 §6.0/§6.3/§7.2、待确认项 6）。
4. **配置（环境变量 / `.env`，敏感项不入库不入日志，§7.1）**：
   `QMT_ACCOUNT_ID` / `QMT_MINI_PATH`（userdata_mini 路径）/ `QMT_LOCAL_DB_PATH`（本机 SQLite）/
   `QMT_MYSQL_DSN`（仅盘后同步用，独立写账号）/ 风控阈值（单票/总仓/单笔/单日笔数/价偏离）/
   `QMT_MARKET_STATE_BLOCK`；**`QMT_KILL_SWITCH` 与 `QMT_AUCTION_TIMING_ENABLED` 首次上线保持默认（熔断可用、竞价择时关）**。
5. **竞价数据能力实测（§7.2 A1–A6）**：真实交易日 9:15–9:25 实测 `get_full_tick` 字段/刷新；通过前竞价择时不开。

## 六、进程保活与调度

- **入口**：`python -m qmt_strategy.app.run`（`app/run.py` 装配本地 SQLite 栈 + xtquant 适配器 + Engine + 连接守护 + 调度线程）。
- **调度线程**（`app/scheduler.py` `DailyScheduler`，按东八区钟点自动触发，无需多个任务计划项）：
  盘前(≥08:55) 装载名单 → 竞价(09:15–09:30) 轮询 → 盘中 周期巡检超时撤单(+注入盘口源后跑卖出) → 收盘(默认 15:05) 快照+对账 → 盘后(默认 15:35) 同步回远端。
- **Windows 任务计划**：每日开盘前（如 08:50）**自动登录的桌面会话**里拉起 miniQMT + `app.run`（GUI 程序须在交互桌面会话，不能用 session 0）；进程异常退出自动重启。
- **桌面会话**：RDP **只「断开」不「注销」**（注销会杀掉 miniQMT）；配**自动登录 + 禁屏保/锁屏**。
- **禁 Windows 自动更新重启**（别盘中重启断连）；**OS 时区设 Asia/Shanghai**。

## 七、安全

- **22 / 3389 不对公网裸奔**：安全组只放行白名单 IP 或走 VPN / 堡垒机；NLA、强口令、账户锁定；可改默认端口。
- 敏感信息（账号/口令/token/DSN）**不硬编码、不入库、不写日志**（`Settings.redacted()` 脱敏后才打印）。
- `QMT_KILL_SWITCH=true` 为一键熔断（只采集不下单）；执行侧 MySQL 账号仅对 `qmt_*` 四表有写权限。

## 八、磁盘卫生（长期）

- 限制页面文件大小；定期 `Dism /Online /Cleanup-Image /StartComponentCleanup`；
- 控制 xtdata 行情缓存（只订阅 watchlist 范围、不囤历史 tick）；
- 本机 SQLite 同步回远端成功后定期归档清理（doc/05 待确认项 4）；日志轮转；加磁盘占用告警。

## 九、上线 checklist（接实盘小仓前逐项打勾，全绿才上，引用设计 §7.4）

- [ ] 账户隔离：实盘/仿真用不同 `account_id`，`qmt_*` 按 account_id 物理区分；MySQL 账号仅 `qmt_*` 写权限
- [ ] 幂等：所有写走唯一键 upsert，重跑/补采/重连补采不产生重复行；台账重启可重建（不重复下单）
- [ ] 撤单：9:20–9:25 不发撤单；超时撤单路径可用；`on_cancel_error` 留痕
- [ ] 断线重连：`on_disconnected` → 换新 session 重连重订阅 + 当日 `query_*` 补采；任务计划每日开盘前拉起
- [ ] 安全默认：`QMT_KILL_SWITCH` 可熔断；`QMT_AUCTION_TIMING_ENABLED` 默认 false（§7.2 未通过不开）；风控阈值保守
- [ ] 对账：本地台账 vs `qmt_order`/`qmt_trade` 委托/成交/资产/滑点四类勾稽可跑、偏差告警
- [ ] 收盘快照：当日至少成功落一次 `CLOSE` 资产/持仓快照（隔日不可补）
- [ ] 时间口径：`*_time` UTC naive + `*_east8` 原值；`trade_date` 取东八区；无手工 ±8h
- [ ] 回滚：`QMT_KILL_SWITCH` 一键退回「只采集不交易」；本机同步与盘中下单解耦
- [ ] 次序：**先 go/no-go + 竞价实测，后实盘**；**先小仓 + 熔断 + 保守风控，后放量**；竞价择时先关、实测通过再灰度

## 十、常用运维命令（从开发机经 SSH）

```bash
# 连接（密钥已配；密码见 doc/Qmt生产服务器.txt）
ssh -i ~/.ssh/id_ed25519 Administrator@<IP>
# 跑测试（服务器）
cd C:\qmt\astock-quant-ai\qmt_strategy && .\.venv\Scripts\python.exe -m pytest -q
# 拉取最新代码
cd C:\qmt\astock-quant-ai && git pull
# 启动引擎（真实环境，需 miniQMT 已登录 + 配置就位）
cd C:\qmt\astock-quant-ai\qmt_strategy && .\.venv\Scripts\python.exe -m qmt_strategy.app.run
```
