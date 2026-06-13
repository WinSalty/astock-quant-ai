"""执行侧结构化本地日志（§6.1 logger）。

业务意图：下单 / 回调 / 断线 / 对账 / 风控拦截留痕，便于排查与对账兜底。
安全口径：绝不写敏感信息（账户号、口令、token、DSN）——调用方传字段时已脱敏；
本模块对常见敏感键名再做一道防御性打码。
"""

from __future__ import annotations

import json
import logging
from typing import Any

# 防御性脱敏键名：即便上游误传，也不落明文。
_SENSITIVE_KEYS = {"token", "password", "passwd", "secret", "dsn", "mysql_dsn", "account_pwd"}


def _scrub(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k.lower() in _SENSITIVE_KEYS:
            out[k] = "***REDACTED***"
        else:
            out[k] = v
    return out


class StructLoggerImpl:
    """基于标准 logging 的结构化日志实现。实现 contracts.StructLogger 协议。

    每条日志输出 `event` 名 + JSON 字段，便于后续采集/检索。时间由 logging 自带（按机器本地时区，
    与业务时间口径无关：业务时间一律走 time_utils）。
    """

    def __init__(self, name: str = "qmt_strategy", level: int = logging.INFO):
        self._log = logging.getLogger(name)
        if not self._log.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._log.addHandler(handler)
        self._log.setLevel(level)

    def _emit(self, fn, event: str, fields: dict) -> None:
        payload = _scrub(fields)
        try:
            body = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            body = str(payload)
        fn("%s %s", event, body)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(self._log.info, event, fields)

    def warn(self, event: str, **fields: Any) -> None:
        self._emit(self._log.warning, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(self._log.error, event, fields)


class RecordingLogger:
    """记录型日志（单测断言用）：把每条 (level, event, fields) 存入 records 列表。"""

    def __init__(self):
        self.records = []  # list[tuple[str, str, dict]]

    def info(self, event: str, **fields: Any) -> None:
        self.records.append(("info", event, _scrub(fields)))

    def warn(self, event: str, **fields: Any) -> None:
        self.records.append(("warn", event, _scrub(fields)))

    def error(self, event: str, **fields: Any) -> None:
        self.records.append(("error", event, _scrub(fields)))

    def events(self) -> list:
        """已记录的 event 名列表，便于断言「是否产生了某条告警」。"""
        return [e for _, e, _ in self.records]
