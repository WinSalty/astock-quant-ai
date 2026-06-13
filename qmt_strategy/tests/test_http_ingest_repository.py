"""HttpIngestQmtRepository 单测：协议一致性、逐表 POST 载荷、失败上抛、只读方法。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from qmt_strategy.common.http_client import SignalHttpError
from qmt_strategy.contracts.enums import OrderStatus, SnapshotType, TradeSide
from qmt_strategy.contracts.models import (
    AccountRecord,
    OrderRecord,
    PositionRecord,
    TradeRecord,
)
from qmt_strategy.contracts.protocols import QmtRepository
from qmt_strategy.storage.http_ingest_repository import INGEST_PATH, HttpIngestQmtRepository


class _FakeLogger:
    def __init__(self):
        self.events = []

    def info(self, e, **f):
        self.events.append((e, f))

    def warn(self, e, **f):
        self.events.append((e, f))

    def error(self, e, **f):
        self.events.append((e, f))


class _CapturingClient:
    """记录 POST 载荷的假客户端；fail=True 时模拟非2xx 抛 SignalHttpError。"""

    def __init__(self, fail=False):
        self.posts = []
        self.fail = fail

    def post_json(self, path, payload):
        if self.fail:
            raise SignalHttpError("boom", status=500, body="err")
        self.posts.append((path, payload))
        return {"ok": True, "total": 1}

    def get_json(self, path, params=None):
        return {"items": []}


def _trade():
    return TradeRecord(
        account_id="A1", trade_date=date(2026, 6, 13), ts_code="600000.SH",
        qmt_stock_code="600000.SH", traded_id="TR1", trade_side=TradeSide.BUY,
        traded_price=Decimal("10.50"), traded_volume=1000,
        traded_time=datetime(2026, 6, 13, 1, 30), signal_trade_date=date(2026, 6, 12),
    )


def _order():
    return OrderRecord(
        account_id="A1", trade_date=date(2026, 6, 13), ts_code="600000.SH",
        qmt_stock_code="600000.SH", order_id=1001, trade_side=TradeSide.BUY,
        order_volume=1000, order_status=OrderStatus.TRADED, traded_volume=1000,
    )


def _position():
    return PositionRecord(
        account_id="A1", trade_date=date(2026, 6, 13), ts_code="600000.SH",
        qmt_stock_code="600000.SH", snapshot_type=SnapshotType.CLOSE, volume=1000,
        can_use_volume=0,
    )


def _account():
    return AccountRecord(
        account_id="A1", trade_date=date(2026, 6, 13),
        total_asset=Decimal("100000.00"), cash=Decimal("50000.00"),
        snapshot_type=SnapshotType.CLOSE,
    )


def test_protocol_conformance():
    repo = HttpIngestQmtRepository(_CapturingClient(), _FakeLogger())
    assert isinstance(repo, QmtRepository)


def test_upsert_trade_posts_record():
    client = _CapturingClient()
    repo = HttpIngestQmtRepository(client, _FakeLogger())
    repo.upsert_trade(_trade())

    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == INGEST_PATH
    assert payload["account_id"] == "A1"
    assert payload["trade_date"] == "2026-06-13"
    rec = payload["records"][0]
    assert rec["table"] == "qmt_trade"
    data = rec["data"]
    # mappers 行：JSON 友好（Decimal→str、datetime→ISO、枚举→值）
    assert data["traded_id"] == "TR1"
    assert data["traded_price"] == "10.50"
    assert data["trade_side"] == "BUY"
    assert data["traded_time"] == "2026-06-13T01:30:00"
    assert data["signal_trade_date"] == "2026-06-12"


def test_upsert_order_position_account_tables():
    client = _CapturingClient()
    repo = HttpIngestQmtRepository(client, _FakeLogger())
    repo.upsert_order(_order())
    repo.upsert_position(_position())
    repo.upsert_account_daily(_account())

    tables = [p[1]["records"][0]["table"] for p in client.posts]
    assert tables == ["qmt_order", "qmt_position_snapshot", "qmt_account_daily"]
    # 抽查账户行序列化
    acc = client.posts[-1][1]["records"][0]["data"]
    assert acc["total_asset"] == "100000.00"
    assert acc["snapshot_type"] == "CLOSE"


def test_failure_propagates_for_retry():
    """非2xx/网络失败由 client 抛 SignalHttpError 上抛——RemoteSyncJob 据此保 synced=0 重试。"""
    repo = HttpIngestQmtRepository(_CapturingClient(fail=True), _FakeLogger())
    with pytest.raises(SignalHttpError):
        repo.upsert_trade(_trade())


def test_read_methods_raise_not_implemented():
    repo = HttpIngestQmtRepository(_CapturingClient(), _FakeLogger())
    with pytest.raises(NotImplementedError):
        repo.get_trades("A1", date(2026, 6, 13))
    with pytest.raises(NotImplementedError):
        repo.get_orders("A1", date(2026, 6, 13))
    with pytest.raises(NotImplementedError):
        repo.get_account_daily("A1", date(2026, 6, 13))


def test_mark_cancel_failed_is_noop_with_log():
    log = _FakeLogger()
    repo = HttpIngestQmtRepository(_CapturingClient(), log)
    repo.mark_cancel_failed("A1", 1001, 7, "撤单失败")
    assert any(e[0] == "http_ingest_mark_cancel_failed_noop" for e in log.events)
