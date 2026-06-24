"""Stage M 发送入口审计：编排器中心护栏 + A 线 send_message 纳入统一发送栈。

覆盖此前的旁路风控缺口——主动问候/唤醒/关怀经 ``orchestrator.send`` 或
``CompanionWorker.send→send_message`` 直发裸 client，绕过 Kill-Switch/反封号。
"""

from __future__ import annotations

import logging
import os
import tempfile
import time

import pytest

import src.ops.kill_switch as ks_mod
from src.integrations import account_orchestrator as orch
from src.integrations.account_orchestrator import AccountOrchestrator, account_key
from src.integrations.account_registry import AccountRegistry
from src.integrations.shared.send_guard import send_blocked
from src.ops.kill_switch import KillSwitch


@pytest.fixture
def fresh_ks(tmp_path, monkeypatch):
    """干净的 Kill-Switch 单例（隔离全局状态，测后还原）。"""
    ks = KillSwitch(tmp_path / "runtime_flags.db")
    monkeypatch.setattr(ks_mod, "_singleton", ks)
    return ks


# ── send_blocked 纯逻辑 ──────────────────────────────────────────────────────

def test_send_blocked_kill_switch_account(fresh_ks):
    fresh_ks.set("account:telegram:1", reason="手动急停")
    blocked, reason = send_blocked("telegram", "1")
    assert blocked is True
    assert reason.startswith("kill_switch:")


def test_send_blocked_kill_switch_global(fresh_ks):
    fresh_ks.set("global", reason="全局冻结")
    blocked, reason = send_blocked("whatsapp", "x")
    assert blocked is True
    assert reason == "kill_switch:global"


def test_send_blocked_clear_when_no_flag(fresh_ks):
    blocked, reason = send_blocked("telegram", "1")
    assert blocked is False and reason == ""


def test_send_blocked_gate_disabled_passes(fresh_ks):
    # gate 默认关 → 即便信号差也不拦（零破坏）
    blocked, _ = send_blocked("telegram", "1", config={"companion_send_gate": {"enabled": False}})
    assert blocked is False


def test_send_blocked_gate_enabled_blocks_banned(fresh_ks):
    class _Reg:
        def get(self, p, a):
            return {"meta": {"banned": True}, "status": "removed"}

    blocked, reason = send_blocked(
        "telegram", "1",
        config={"companion_send_gate": {"enabled": True}},
        registry=_Reg())
    assert blocked is True
    assert reason.startswith("send_gate:")


def test_send_blocked_failopen_on_error(fresh_ks, monkeypatch):
    # 守卫自身异常 → 放行（broken guard 不得卡死所有发送）
    def _boom(*a, **k):
        raise RuntimeError("ks down")
    monkeypatch.setattr(ks_mod, "is_blocked", _boom)
    blocked, reason = send_blocked("telegram", "1")
    assert blocked is False and reason == ""


# ── 编排器中心护栏 ───────────────────────────────────────────────────────────

class _SendWorker:
    def __init__(self, account, config):
        self.sent = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def healthy(self):
        return True

    def status(self):
        return {"type": "fake_send"}

    async def send(self, chat_key, text):
        self.sent.append(("text", chat_key, text))
        return {"delivered": True, "message_id": "m1"}

    async def send_media(self, chat_key, *, media_path, media_type, caption=""):
        self.sent.append(("media", chat_key, media_type))
        return {"delivered": True}


@pytest.fixture
def registry():
    return AccountRegistry(os.path.join(tempfile.mkdtemp(), "acc.db"))


@pytest.fixture(autouse=True)
def _send_worker_registered():
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol", lambda a, c: _SendWorker(a, c))
    yield
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


async def _started_orch(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    return o, o._managed[account_key("telegram", "1")].worker


@pytest.mark.asyncio
async def test_orchestrator_send_blocked_by_kill_switch(registry, fresh_ks, monkeypatch):
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda *a, **k: None)
    o, worker = await _started_orch(registry)
    fresh_ks.set("account:telegram:1", reason="freeze")
    res = await o.send("telegram", "1", "chat1", "hi")
    assert res.get("delivered") is False
    assert str(res.get("blocked", "")).startswith("kill_switch:")
    assert worker.sent == []  # 护栏拦下，worker 未真发


@pytest.mark.asyncio
async def test_orchestrator_send_allowed_when_clear(registry, fresh_ks, monkeypatch):
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda *a, **k: None)
    o, worker = await _started_orch(registry)
    res = await o.send("telegram", "1", "chat1", "hi")
    assert res.get("delivered") is True
    assert worker.sent == [("text", "chat1", "hi")]


@pytest.mark.asyncio
async def test_orchestrator_send_media_blocked_by_kill_switch(registry, fresh_ks, monkeypatch):
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda *a, **k: None)
    o, worker = await _started_orch(registry)
    fresh_ks.set("platform:telegram", reason="平台冻结")
    res = await o.send_media(
        "telegram", "1", "chat1",
        media_path="/x.png", media_url="/static/x.png", media_type="image", caption="c")
    assert res.get("delivered") is False
    assert str(res.get("blocked", "")).startswith("kill_switch:")
    assert worker.sent == []


# ── A 线 send_message 纳入统一发送栈 ─────────────────────────────────────────

class _TextCli:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("rpc")
        self.calls.append((chat_id, text))


def _text_sender(cli, *, min_interval=0, last_send=0.0):
    from src.client.sender import TelegramSenderMixin

    class _Cfg:
        def get(self, k, d=None):
            if k == "reply":
                return {"split_send": {"min_interval_seconds": min_interval}}
            return d if d is not None else {}

    class _S(TelegramSenderMixin):
        def __init__(self):
            self.client = cli
            self.logger = logging.getLogger("test_send_msg")
            self.account_id = "a"
            self.config = _Cfg()
            self._last_send_wallclock = last_send

    s = _S()
    s._shared_send_limiter = lambda cfg: None
    return s


@pytest.mark.asyncio
async def test_send_message_blocked_by_presend_guard(monkeypatch):
    s = _text_sender(_TextCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda: True)
    assert await s.send_message(7, "hi") is False
    assert s.client.calls == []  # 冻结/被闸门拦 → 不真发（不绕过风控）


@pytest.mark.asyncio
async def test_send_message_success_records_count(monkeypatch):
    s = _text_sender(_TextCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    assert await s.send_message(7, "hi") is True
    assert s.client.calls == [(7, "hi")]
    assert s._last_send_wallclock > 0  # 记账刷新墙钟（喂下次节流 + 共用计数器）


@pytest.mark.asyncio
async def test_send_message_paces_against_wallclock(monkeypatch):
    slept = {}

    async def _fake_sleep(sec):
        slept["sec"] = sec

    monkeypatch.setattr("src.client.sender.asyncio.sleep", _fake_sleep)
    s = _text_sender(_TextCli(), min_interval=5, last_send=time.time())
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    assert await s.send_message(7, "hi") is True
    assert slept.get("sec") is not None and slept["sec"] > 0


@pytest.mark.asyncio
async def test_send_message_no_client_returns_false(monkeypatch):
    s = _text_sender(None)
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    assert await s.send_message(7, "hi") is False


@pytest.mark.asyncio
async def test_send_message_failure_returns_false(monkeypatch):
    s = _text_sender(_TextCli(fail=True))
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    assert await s.send_message(7, "hi") is False  # RPC 抛 → False、不冒泡
