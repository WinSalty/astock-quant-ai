"""universe 兜底过滤（§2.3 第 3 步 / §3.5 / §7.1.5）。

业务意图：信号侧理应已过滤，执行侧再过一遍作冗余防线，防止信号侧规则漂移或脏数据导致对
禁交易标的下单。规则采用「闭式 allow-list」前缀白名单——仅放行 10 个前缀，其余一律剔除：
沪市主板 600/601/603/605、深市主板 000/001/002/003、创业板 300/301；
隐式排除科创 688/689、北交 8xx/920/4xx、B 股 2xx/9xx 等非目标段，并剔除 ST/停牌/退市（T+1 口径）。

ST 判定（§2.3）：本节属 live 路径，按当日实时行情名称含 ST（及退市整理 退/*退）判定即可，
当日名称即 point-in-time 正确，无需 a_stock_st 历史表（回测历史 universe 才需要，见待确认项 8）。
"""

from __future__ import annotations

from typing import Optional

from .identity import board_of

# 闭式前缀白名单（§2.3 第 3 步）：仅这些 3 位前缀放行。
_ALLOW_PREFIXES = frozenset(
    {"600", "601", "603", "605", "000", "001", "002", "003", "300", "301"}
)

# ST / 退市整理名称标识（live 路径按当日名称判定）。
_ST_TOKENS = ("ST", "*ST", "退", "*退")


def is_allowed_prefix(ts_code: Optional[str]) -> bool:
    """前缀白名单判定：归一后取 6 位前 3 位是否在白名单内。

    用 board_of 间接校验（board 判得出即在主板/创业板范围内），再核对 3 位前缀，双保险。
    """
    if board_of(ts_code) is None:
        return False
    code6 = ts_code.strip().upper()
    # board_of 内部已归一，这里再取一次归一后的 6 位
    from .identity import resolve_code

    norm = resolve_code(ts_code)
    if not norm:
        return False
    return norm[:3] in _ALLOW_PREFIXES


def is_st_name(name: Optional[str]) -> bool:
    """按证券名称判定是否 ST / 退市整理（live 口径，§2.3）。名称缺失视为非 ST（不阻塞）。"""
    if not name:
        return False
    upper = str(name).upper()
    # 名称含 ST 或退市标识即判为不可交易
    if "ST" in upper:
        return True
    return any(tok in str(name) for tok in ("退",))


def is_tradable_universe(
    ts_code: Optional[str],
    name: Optional[str] = None,
    is_halted: bool = False,
    is_delisted: bool = False,
) -> bool:
    """综合 universe 校验（§2.3 第 3 步）：在白名单前缀内、且非 ST / 停牌 / 退市才放行。

    返回 False 的票应转入观察名单（不下单，§2.6），不抛异常。
    """
    if is_halted or is_delisted:
        return False
    if not is_allowed_prefix(ts_code):
        return False
    if is_st_name(name):
        return False
    return True
