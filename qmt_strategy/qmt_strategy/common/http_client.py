"""信号侧内网接口 HTTP 客户端（stdlib urllib，零三方依赖）。

业务意图：执行侧（Windows/单进程）作为纯 HTTP 客户端，盘前 GET 信号侧 watchlist、盘后 POST 回流
    qmt_* 数据。两侧只通过信号侧托管的两个内网接口通信（详见 doc/07）。

为何用 stdlib urllib 而非 requests/httpx：执行侧目标机是交易机，依赖越少越稳；本项目硬依赖仅 tzdata，
    不为一个 JSON 调用引入三方库。urllib 足以覆盖「带 token 头的 JSON GET/POST + 超时 + 非 2xx 抛错」。

安全口径（§7.1 / AGENTS.md）：token 经 X-Internal-Token 头传递，不拼进 URL、不写日志；
    本模块异常信息只含状态码与响应体摘要，不回显 token。

异常口径：网络错误 / 非 2xx / JSON 解析失败一律抛 ``SignalHttpError``，由调用方（回流仓储 / watchlist
    拉取）按各自降级口径处理——回流保 synced=0 下轮重试；watchlist 拉取失败转「当日无名单 → 只守仓」。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional


class SignalHttpError(RuntimeError):
    """信号侧接口调用失败（网络 / 非 2xx / 解析）。带 status 与 body 摘要便于排查。"""

    def __init__(self, message: str, *, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        # 只保留响应体前 500 字符，避免超长报错刷屏 / 误带敏感信息。
        self.body = (body or "")[:500]


class SignalHttpClient:
    """信号侧内网接口客户端：统一 base_url + token 头 + 超时 + JSON 编解码。

    依赖注入：
    - base_url：信号侧服务根（如 http://127.0.0.1:8000），与 path 拼成完整 URL；
    - token：X-Internal-Token 值（机器对机器鉴权）；为空则不带该头（由服务端 401/503 兜底）；
    - timeout：单次请求超时秒数；logger：结构化日志（不记 token / 不记完整 URL 查询串）。
    """

    def __init__(self, base_url: str, token: Optional[str], timeout: float, logger=None):
        # 去掉尾部斜杠，避免与 path 的前导斜杠拼出双斜杠。
        self._base_url = (base_url or "").rstrip("/")
        self._token = token
        self._timeout = timeout
        self._logger = logger

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        return headers

    def _full_url(self, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
        url = self._base_url + (path if path.startswith("/") else "/" + path)
        if params:
            # 过滤 None 值；其余转字符串后 urlencode（防注入/防空参数）。
            query = {k: str(v) for k, v in params.items() if v is not None}
            if query:
                url = url + "?" + urllib.parse.urlencode(query)
        return url

    def get_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """GET 并返回解析后的 JSON。非 2xx / 网络 / 解析失败抛 SignalHttpError。"""
        url = self._full_url(path, params)
        req = urllib.request.Request(url, method="GET", headers=self._headers())
        return self._send(req, path)

    def post_json(self, path: str, payload: Any) -> Any:
        """POST JSON 体并返回解析后的 JSON。非 2xx / 网络 / 解析失败抛 SignalHttpError。"""
        url = self._full_url(path)
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers=self._headers())
        return self._send(req, path)

    def _send(self, req: urllib.request.Request, path: str) -> Any:
        """执行请求：2xx 解析 JSON 返回；HTTPError(非2xx) / URLError(网络) / 解析失败统一抛 SignalHttpError。"""
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.getcode()
        except urllib.error.HTTPError as exc:
            # 非 2xx：读响应体摘要（含服务端 detail），抛带 status 的错误供上层判定/重试。
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 读响应体失败无害，按空体处理
                body = ""
            raise SignalHttpError(
                f"signal http {path} 非2xx: {exc.code}", status=exc.code, body=body
            ) from exc
        except urllib.error.URLError as exc:
            # 网络层失败（连接拒绝 / 超时 / DNS 等）：无 status，抛网络错误。
            raise SignalHttpError(f"signal http {path} 网络失败: {exc.reason}") from exc

        # 2xx 但空体：返回 None（调用方按需处理）。
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise SignalHttpError(
                f"signal http {path} 响应非 JSON", status=status, body=raw
            ) from exc
