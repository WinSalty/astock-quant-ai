"""xtquant 适配器 + 真实入口模板的跨平台安全性测试。

核心：在【无 xtquant 的平台】(本 CI/Mac) 上——
- adapters.xt_real / app.run 必须能 import（xtquant 全惰性，不破坏现有测试）；
- 调用任何 xtquant 工厂应抛【清晰 RuntimeError】(而非深层 ImportError)；
- TraderHolder 在未建连/建连后行为正确（重连换 trader 不影响引擎引用）。
"""

from __future__ import annotations

import pytest

# 顶层 import 必须成功（证明 xtquant 惰性、不在 import 期触发）。
from qmt_strategy.adapters import xt_real
from qmt_strategy.app import run as run_mod
from qmt_strategy.config.settings import Settings
from qmt_strategy.common.logger import RecordingLogger
from qmt_strategy.contracts.errors import ConnectionNotReadyError


def test_modules_import_clean_without_xtquant():
    # 能引用到关键符号即说明 import 链未触发 xtquant。
    assert hasattr(xt_real, "RealXtTrader")
    assert hasattr(run_mod, "build_real_engine")


def test_require_xtquant_raises_clear_error():
    with pytest.raises(RuntimeError):
        xt_real._require_xtquant()


@pytest.mark.parametrize("call", [
    lambda: xt_real.RealXtTrader("path", 1),
    lambda: xt_real.make_stock_account("acc1"),
    lambda: xt_real.make_trader_callback(object()),
    lambda: xt_real.import_xtdata(),
])
def test_xtquant_factories_raise_clear_error_off_target(call):
    """无 xtquant 时各工厂抛 RuntimeError（清晰提示需目标机），不抛深层 ImportError。"""
    with pytest.raises(RuntimeError):
        call()


def test_trader_factory_off_target_does_not_corrupt_holder():
    holder = xt_real.TraderHolder()
    factory = xt_real.make_trader_factory("path", holder)
    with pytest.raises(RuntimeError):
        factory(123)                 # RealXtTrader.__init__ 在 set 之前就因缺 xtquant 抛错
    assert holder.current is None     # holder 未被污染


class _FakeTrader:
    def order_stock(self, *a, **k):
        return 777
    def query_stock_asset(self, account):
        return "asset"
    def cancel_order_stock(self, account, order_id):
        return 0


def test_trader_holder_delegation():
    h = xt_real.TraderHolder()
    # 未建连：调用交易方法抛 ConnectionNotReadyError（不静默发给 None）。
    with pytest.raises(ConnectionNotReadyError):
        h.order_stock(None, "600036.SH", 0, 100, 0, 1.0)
    # set 后委托给当前 trader（模拟重连换实例：再次 set 即切换目标）。
    h.set(_FakeTrader())
    assert h.order_stock(None, "x", 0, 100, 0, 1.0) == 777
    assert h.query_stock_asset(None) == "asset"


def test_build_real_engine_requires_xtquant(tmp_path):
    """无 xtquant 时 build_real_engine 抛清晰 RuntimeError（本地 SQLite 栈会先建好，属预期）。"""
    # 提供完整必配（account_id + mini_path）以越过启动期 fail-closed 校验，使本用例真正测到「xtquant 缺失」
    # 这一目标点（否则会先因 mini_path 缺配拒启，测不到 xtquant 触发）。
    s = Settings.from_env({
        "QMT_LOCAL_DB_PATH": str(tmp_path / "q.db"),
        "QMT_ACCOUNT_ID": "acc1",
        "QMT_MINI_PATH": str(tmp_path / "userdata_mini"),
    })
    with pytest.raises(RuntimeError):
        run_mod.build_real_engine(s, RecordingLogger())
