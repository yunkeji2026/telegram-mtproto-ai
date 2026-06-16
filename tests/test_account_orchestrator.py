"""M5：账号池编排器 单测（假 worker + 假时钟，确定性驱动监督）。"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.integrations import account_orchestrator as orch
from src.integrations.account_orchestrator import AccountOrchestrator, account_key
from src.integrations.account_registry import AccountRegistry


class FakeWorker:
    last = None

    def __init__(self, account, config):
        self.account = account
        self.started = 0
        self.stopped = 0
        self.fail_start = False
        self._healthy = True
        FakeWorker.last = self

    async def start(self):
        self.started += 1
        if self.fail_start:
            raise RuntimeError("boom")

    async def stop(self):
        self.stopped += 1

    async def healthy(self):
        return self._healthy

    def status(self):
        return {"type": "fake", "healthy": self._healthy}


@pytest.fixture()
def registry():
    return AccountRegistry(os.path.join(tempfile.mkdtemp(), "acc.db"))


@pytest.fixture(autouse=True)
def _fake_worker_registered():
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol", lambda a, c: FakeWorker(a, c))
    FakeWorker.last = None
    yield
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


def _clock():
    state = {"t": 0.0}
    return state, (lambda: state["t"])


def test_worker_supported_gating():
    assert orch.worker_supported("telegram", "protocol") is True
    assert orch.worker_supported("telegram", "device") is False   # device 不编排
    assert orch.worker_supported("line", "protocol") is False     # 无 factory


async def test_sync_starts_protocol_ignores_device(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    registry.upsert("line", "2", mode="device", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    st = o.status()
    assert st["total"] == 1                         # 仅 protocol 被接管
    assert st["by_state"].get("running") == 1
    assert account_key("telegram", "1") in {a["key"] for a in st["accounts"]}


async def test_remove_account_stops_worker(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    assert o.status()["by_state"].get("running") == 1
    registry.remove("telegram", "1")
    await o.sync()
    assert o.status()["by_state"].get("stopped") == 1


async def test_unhealthy_triggers_backoff_then_restart(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    state, now = _clock()
    o = AccountOrchestrator(registry=registry, now=now)
    await o.sync()
    w = FakeWorker.last
    m = o._managed[account_key("telegram", "1")]
    assert m.state == "running"

    # 变不健康 → tick 标 error + 安排退避
    w._healthy = False
    await o.tick()
    assert m.state == "error"
    assert m.restarts == 1
    assert m.backoff_until > 0

    # 未到退避时间 → 不重启
    await o.tick()
    assert m.state == "error"

    # 恢复健康 + 推进时钟越过退避 → tick 重启成功
    w._healthy = True
    state["t"] = m.backoff_until + 1
    await o.tick()
    assert m.state == "running"
    assert m.restarts == 0


async def test_start_failure_and_circuit_breaker(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    state, now = _clock()
    o = AccountOrchestrator(registry=registry, now=now)
    await o.sync()                       # 首次启动成功
    w = FakeWorker.last
    m = o._managed[account_key("telegram", "1")]
    # 之后变不健康且重启必失败 → 进入退避重试直至熔断
    w.fail_start = True
    w._healthy = False
    await o.tick()                       # running → unhealthy → error（仅标记，下一 tick 重试）
    for _ in range(40):
        state["t"] = m.backoff_until + 1
        await o.tick()
    assert m.restarts >= orch.MAX_RESTARTS
    assert m.state == "error"
    # 熔断后不再增加启动次数
    started_at_break = w.started
    state["t"] = m.backoff_until + 1000
    await o.tick()
    assert w.started == started_at_break


async def test_manual_start_stop_restart(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    acc = registry.get("telegram", "1")
    assert await o.start_account(acc) is True
    key = account_key("telegram", "1")
    assert o._managed[key].state == "running"
    await o.stop_account(key)
    assert o._managed[key].state == "stopped"
    assert await o.restart_account(key) is True
    assert o._managed[key].state == "running"
