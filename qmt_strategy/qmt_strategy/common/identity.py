"""证券代码归一（轻量等价于信号侧 stock_identity_resolver，§6.3 / §6.8）。

业务意图：信号侧 / 行情侧 / QMT 侧三方代码格式不一（600036.SH / SH600036 / 600036 / .BJ 脏数据），
统一归一为带交易所后缀的标准 ts_code（如 600036.SH），作为信号-执行-结果三方 join 的代码键。
规则与信号侧解析器一致：6 位 60/68 → .SH、00/30 → .SZ、8/4 → .BJ；本项目只做主板 + 创业板。

执行侧无 DB 连接时用本轻量逻辑（§6.3 方案 A 内置等价后缀补全）；保留原值便于排查。
"""

from __future__ import annotations

import re
from typing import Optional

from ..contracts.enums import Board

# 交易所后缀映射的标准形态
_SH = "SH"
_SZ = "SZ"
_BJ = "BJ"

_DIGITS6 = re.compile(r"(\d{6})")
# 锚点化后缀正则（评审三轮 F1）：只认末尾 .SH/.SZ/.BJ，绝不用裸子串匹配任意位置的字面量。
_SUFFIX_RE = re.compile(r"\.(SH|SZ|BJ)$")


def _suffix_by_prefix(code6: str) -> Optional[str]:
    """按 6 位代码前缀判定交易所后缀（§6.3 规则）。无法判定返回 None。"""
    if code6[:2] in ("60", "68"):
        return _SH          # 沪市（含科创 68，归一保留，由 universe_filter 负责剔除）
    if code6[:2] in ("00", "30"):
        return _SZ          # 深市主板 / 创业板
    if code6[0] in ("8", "4") or code6[:3] == "920":
        return _BJ          # 北交所 / 老三板（由 universe_filter 剔除）
    return None


def resolve_code(raw: Optional[str]) -> Optional[str]:
    """把任意脏代码归一为标准 ts_code（带交易所后缀，大写）。

    支持形态：600036.SH / 600036.sh / SH600036 / sh.600036 / 600036 / 830799.BJ。
    锚点化口径（评审三轮 F1）：显式交易所后缀只认【规范位置】——末尾 .SH/.SZ/.BJ 或开头 SH/SZ/BJ；
    绝不用裸子串 `ex in s`（任意位置出现 SH/SZ/BJ 字面量都被当后缀，脏串如 'SH000001' 会被判到错误交易所
    → 下错单 / norm_code join 错配）。当显式后缀与按数字前缀推断的交易所【矛盾】时判脏数据返回 None（交上游
    降级，绝不让任意位置字面量盖过前缀真值）。
    边界：取不到 6 位数字或无法判定交易所则返回 None。
    """
    if not raw:
        return None
    s = str(raw).strip().upper()
    m = _DIGITS6.search(s)
    if not m:
        return None
    code6 = m.group(1)
    # 只认锚点位置的显式后缀：末尾 .SH/.SZ/.BJ 或开头 SH/SZ/BJ（如 SH600036 / SH.600036）。
    explicit = None
    m_suffix = _SUFFIX_RE.search(s)
    if m_suffix:
        explicit = m_suffix.group(1)
    else:
        for ex in (_SH, _SZ, _BJ):
            if s.startswith(ex):
                explicit = ex
                break
    by_prefix = _suffix_by_prefix(code6)
    # 显式后缀与数字前缀推断矛盾 → 脏数据返回 None（如 'SH000001'：00 前缀属深市，与显式 SH 冲突）。
    if explicit is not None and by_prefix is not None and explicit != by_prefix:
        return None
    suffix = explicit or by_prefix
    if suffix is None:
        return None
    return f"{code6}.{suffix}"


def board_of(ts_code: Optional[str]) -> Optional[Board]:
    """判定板块（仅区分主板 vs 创业板，§2.3 第 5 步涨跌幅规则用）。

    主板：沪 600/601/603/605、深 000/001/002/003 → MAIN（±10%）；
    创业板：300/301 → CHINEXT（±20%）。
    其它（科创 688/689、北交 8xx/920/4xx 等）返回 None（非目标段，应被 universe_filter 剔除）。
    """
    norm = resolve_code(ts_code)
    if not norm:
        return None
    code6 = norm[:6]
    if code6[:3] in ("300", "301"):
        return Board.CHINEXT
    if code6[:3] in ("600", "601", "603", "605", "000", "001", "002", "003"):
        return Board.MAIN
    return None
