"""SignalHttpClient（stdlib urllib）单测：URL/参数拼接、token 头、JSON 编解码、非2xx/网络异常口径。

不打真实网络：monkeypatch urllib.request.urlopen 注入假响应/异常。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from qmt_strategy.common.http_client import SignalHttpClient, SignalHttpError


class _FakeResp:
    """假 HTTP 响应（支持 with 上下文 + read + getcode）。"""

    def __init__(self, body: str, status: int = 200):
        self._b = body.encode("utf-8")
        self._s = status

    def read(self):
        return self._b

    def getcode(self):
        return self._s

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_get_json_builds_url_filters_none_and_sends_token(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.headers)
        captured["timeout"] = timeout
        return _FakeResp(json.dumps({"items": [1, 2]}))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = SignalHttpClient("http://host:8000/", "tok", 5.0)
    out = client.get_json("/api/internal/watchlist", {"date": "2026-06-12", "skip": None})

    assert out == {"items": [1, 2]}
    # base 尾斜杠去重 + 仅保留非 None 参数
    assert captured["url"] == "http://host:8000/api/internal/watchlist?date=2026-06-12"
    assert captured["method"] == "GET"
    assert captured["timeout"] == 5.0
    # token 经 X-Internal-Token 头（urllib 会首字母大写化 header 名，服务端大小写不敏感）
    assert any(k.lower() == "x-internal-token" and v == "tok" for k, v in captured["headers"].items())


def test_post_json_sends_body_and_method(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(json.dumps({"ok": True, "total": 1}))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = SignalHttpClient("http://host:8000", "tok", 5.0)
    out = client.post_json("/api/internal/qmt/ingest", {"records": [{"table": "qmt_trade"}]})

    assert out == {"ok": True, "total": 1}
    assert captured["method"] == "POST"
    assert captured["body"]["records"][0]["table"] == "qmt_trade"


def test_non_2xx_raises_with_status(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "unauthorized", {}, io.BytesIO(b'{"detail":"\\u5185\\u7f51\\u9274\\u6743\\u5931\\u8d25"}')
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = SignalHttpClient("http://host", "tok", 5.0)
    with pytest.raises(SignalHttpError) as ei:
        client.get_json("/x")
    assert ei.value.status == 401
    assert "detail" in ei.value.body


def test_network_error_raises_without_status(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = SignalHttpClient("http://host", "tok", 5.0)
    with pytest.raises(SignalHttpError) as ei:
        client.post_json("/x", {"a": 1})
    assert ei.value.status is None


def test_no_token_omits_header(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower() for k in req.headers}
        return _FakeResp("{}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = SignalHttpClient("http://host", None, 5.0)
    client.get_json("/x")
    assert "x-internal-token" not in captured["headers"]
