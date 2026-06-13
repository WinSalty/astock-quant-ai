"""watchlist 契约取数源实现（§2.3 第 2 步 / §2.4 _fetch_selected_stocks）。

业务意图：把「整批取 target_trade_date=today 的信号行」这件事抽象为统一的
``SelectedStockSource`` 协议实现，对上层（WatchlistLoader）屏蔽来源差异：

- 路径 A（DB 直读）：执行侧用**只读账号**直连 MySQL，
  ``SELECT * FROM limit_up_selected_stock WHERE target_trade_date = :today``，延迟最低；
- 路径 B（HTTP 只读接口）：当执行侧不便直连 MySQL 时，调信号侧 FastAPI 只读端点
  （如 ``GET /api/selected-stocks?target_trade_date=today``）。

两路返回**同一契约**（一行一股的 ``SelectedStockRow``），故上层只需面向协议、不感知来源。
真实落地点（SQL 执行 / HTTP 调用 / JSON→SelectedStockRow 反序列化）由注入的 callable 承载，
本层只负责「把 callable 包成 SelectedStockSource 协议、统一异常口径」，便于：
1) 在 macOS/Linux 用内存 fake 跑全部单测（不连真实 MySQL / HTTP）；
2) 真实环境注入 PyMySQL 查询闭包 / requests 调用闭包即可上线。

异常口径：取数 callable 抛出的任何异常一律包成 ``WatchlistLoadError`` 上抛，
由 WatchlistLoader 统一捕获走「切备路 / 降级只守仓」兜底（§2.6），本层不静默吞异常。
"""

from __future__ import annotations

from datetime import date
from typing import Callable, List

from ..contracts.errors import WatchlistLoadError
from ..contracts.models import SelectedStockRow

# 取数闭包签名：入参为 target_trade_date（今日，东八区自然交易日 DATE），出参为当日全量信号行。
# 真实落地时：A 路传「执行 SELECT 并把每行拼成 SelectedStockRow」的闭包；
#             B 路传「调只读端点并把 JSON 反序列化成 SelectedStockRow」的闭包。
FetchFn = Callable[[date], List[SelectedStockRow]]


class CallableSelectedStockSource:
    """以注入 callable 实现的通用取数源（实现 contracts.SelectedStockSource 协议）。

    业务意图：A / B 两路底层取数逻辑各异（SQL vs HTTP），但对上层都是
    ``fetch(today) -> list[SelectedStockRow]``。本类把「真实取数动作」收敛为一个可注入闭包，
    使 DbSelectedStockSource / HttpSelectedStockSource 只是「换一个闭包 + 换一段 docstring」的
    薄派生，避免两路各写一套协议适配。

    异常口径：闭包抛出的任何底层异常（DB 断连 / 接口超时 / 非 200 / 反序列化失败等）
    一律转译为 ``WatchlistLoadError`` 上抛，给上层统一的失败信号，便于 loader 切备路或降级。
    """

    def __init__(self, fetch_fn: FetchFn, *, source_name: str = "callable"):
        # source_name 仅用于失败时拼可读的告警上下文（不含敏感信息：不带 DSN / token / 账户）。
        self._fetch_fn = fetch_fn
        self._source_name = source_name

    def fetch(self, target_trade_date: date) -> List[SelectedStockRow]:
        """整批取 target_trade_date=today 的信号行。

        边界：
        - 闭包返回 None 视为「无返回 = 取数异常」，按失败处理（不当作空名单，避免脏 None 流入下游）；
        - 闭包返回空列表是**合法**结果（当日确实无候选），原样返回，由 loader 当作空名单；
        - 闭包抛任何异常 → 包成 WatchlistLoadError 上抛（保留原异常链便于排查）。
        """
        try:
            rows = self._fetch_fn(target_trade_date)
        except WatchlistLoadError:
            # 已是约定异常类型，直接透传，避免重复包裹丢失上下文。
            raise
        except Exception as exc:  # noqa: BLE001 取数底层异常种类繁多，统一收敛为约定异常
            raise WatchlistLoadError(
                f"selected_stock_source[{self._source_name}] fetch failed: {exc}"
            ) from exc
        if rows is None:
            # None 与空列表语义不同：None=取数链路异常，空列表=当日无候选。
            raise WatchlistLoadError(
                f"selected_stock_source[{self._source_name}] returned None (treated as fetch failure)"
            )
        return list(rows)


class DbSelectedStockSource(CallableSelectedStockSource):
    """路径 A：只读账号直读 MySQL（§2.3 第 2 步）。

    真实落地点：用仅有 ``limit_up_selected_stock`` 只读权限的独立账号执行
    ``SELECT * FROM limit_up_selected_stock WHERE target_trade_date = :today``，
    再把每行映射为 ``SelectedStockRow``（价位列转 Decimal、日期列转 date）。

    本类只接收已封装好该 SELECT + 行映射逻辑的 ``query_fn`` 闭包并包成协议，
    不在此处持有连接 / DSN（敏感信息不入本层、不进日志，§7.1 安全口径）。延迟最低、解耦合规
    （两侧只通过 MySQL 通信）。
    """

    def __init__(self, query_fn: FetchFn):
        # query_fn：注入「执行只读 SELECT 并返回 list[SelectedStockRow]」的闭包。
        super().__init__(query_fn, source_name="db")


class HttpSelectedStockSource(CallableSelectedStockSource):
    """路径 B：调信号侧 FastAPI 只读端点（§2.3 第 2 步）。

    真实落地点：``GET /api/selected-stocks?target_trade_date=today``，
    校验 HTTP 200、把 JSON 数组反序列化为 ``SelectedStockRow`` 列表
    （非 200 / 超时 / JSON 解析失败由 fetch_fn 抛异常，本层转 WatchlistLoadError）。

    用于执行侧不便直连 MySQL 时；与 A 路返回同一契约，对上层等价，可互为主备（§2.6）。
    """

    def __init__(self, fetch_fn: FetchFn):
        # fetch_fn：注入「调只读端点并反序列化为 list[SelectedStockRow]」的闭包。
        super().__init__(fetch_fn, source_name="http")
