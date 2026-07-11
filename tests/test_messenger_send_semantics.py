"""Messenger 网页发送语义契约（P0 稳定性修复三件套）。

背景：messenger-web(Node) 曾在「composer 未清空＝多半没发出去」时仍回 ``ok:true`` →
Python 侧把**没发出去**的草稿标记 sent、审计记 autosend_sent —— 静默丢消息，
运营看指标全绿。三处修复的行为契约在此钉死：

1. **send 误报**（P0-1）：Node 现对未确认送达回 502/``sent:false``；
   ``MessengerWebWorker.send`` 与 ``MessengerInboxAdapter._send_web`` 把
   ``ok/delivered/sent`` 任一显式 False、或 HTTP 异常，一律判**未送达**。
2. **kill-switch 补洞**（P0-3）：编排器不拥有账号时回落适配器直发 :8791，
   此前绕过三级急停；现 ``MessengerInboxAdapter.send`` 入口恒查 ``send_blocked``
   （网页/RPA 两条分流都在其后，全部被护）。
3. **会话健康发送闸**（P0-2）：Node push 的会话状态显示掉线（needs_login/expired）
   → worker 自动路径快速失败（不再对死会话做注定失败的 DOM 尝试）；
   恢复（authorized）后自动放行。人工适配器路径不受此闸限制。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.inbox.channel_adapters import ChannelSendError, MessengerInboxAdapter
from src.integrations.account_orchestrator import MessengerWebWorker


def _req(**state):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def _worker():
    return MessengerWebWorker({"account_id": "100"}, {})


@pytest.fixture(autouse=True)
def _fresh_session_health(monkeypatch):
    """每例独立的会话健康单例（防跨测试污染）。"""
    import src.integrations.platform_session_health as psh
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    yield


def _web_account(monkeypatch):
    """把 account 100 伪装成已登记的网页号，服务基址指向 http://svc。"""
    import src.integrations.account_registry as ar
    import src.integrations.messenger_web_login as mgw
    monkeypatch.setattr(
        ar, "get_account_registry",
        lambda: SimpleNamespace(get=lambda p, a: {"mode": "web"}))
    monkeypatch.setattr(mgw, "web_enabled", lambda cfg: True)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")
    return mgw


# ── 1) send 误报修复：sent/delivered/ok 语义 ─────────────────────────────────

def test_worker_send_sent_false_is_not_delivered(monkeypatch):
    """Node 回 200 但 sent:false（防御路径）→ 未送达，绝不误报成功。"""
    import src.integrations.messenger_web_login as mgw

    async def _fake(url, payload, timeout=20.0):
        return {"ok": True, "sent": False, "error": "composer not cleared"}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    res = asyncio.run(_worker().send("555", "hi"))
    assert res["delivered"] is False


def test_worker_send_http_error_is_not_delivered(monkeypatch):
    """Node 回 502（sent 语义修复后的主路径，_post_json 抛异常）→ 未送达。"""
    import src.integrations.messenger_web_login as mgw

    async def _boom(url, payload, timeout=20.0):
        raise RuntimeError("502 Bad Gateway: composer not cleared")

    monkeypatch.setattr(mgw, "_post_json", _boom)
    res = asyncio.run(_worker().send("555", "hi"))
    assert res["delivered"] is False
    assert "502" in res["error"]


def test_worker_send_ok_true_sent_true_delivered(monkeypatch):
    import src.integrations.messenger_web_login as mgw

    async def _fake(url, payload, timeout=20.0):
        return {"ok": True, "sent": True, "message_id": "m1"}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    res = asyncio.run(_worker().send("555", "hi"))
    assert res["delivered"] is True and res["message_id"] == "m1"


def test_worker_send_media_ok_false_not_delivered(monkeypatch):
    import src.integrations.messenger_web_login as mgw

    async def _fake(url, payload, timeout=120.0):
        return {"ok": False, "error": "file input not found"}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    res = asyncio.run(_worker().send_media(
        "555", media_path="x.jpg", media_type="image"))
    assert res["delivered"] is False


def test_adapter_send_web_sent_false_raises_502(monkeypatch):
    """适配器路径同口径：200+sent:false → ChannelSendError(502)，不再默默当成功。"""
    mgw = _web_account(monkeypatch)

    async def _fake(url, payload, timeout=20.0):
        return {"ok": True, "sent": False, "error": "composer not cleared"}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    req = _req(config_manager=SimpleNamespace(config={}))
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(MessengerInboxAdapter().send(req, "100", "555", "hi"))
    assert ei.value.status_code == 502


def test_adapter_send_web_delivered_false_raises_502(monkeypatch):
    mgw = _web_account(monkeypatch)

    async def _fake(url, payload, timeout=20.0):
        return {"ok": True, "delivered": False}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    req = _req(config_manager=SimpleNamespace(config={}))
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(MessengerInboxAdapter().send(req, "100", "555", "hi"))
    assert ei.value.status_code == 502


# ── 2) kill-switch 补洞：适配器回落路径也被三级急停护住 ──────────────────────

def _install_ks(monkeypatch, tmp_path, scope):
    import src.ops.kill_switch as ksmod
    ks = ksmod.KillSwitch(tmp_path / "rf.db")
    ks.set(scope, reason="emergency")
    monkeypatch.setattr(ksmod, "_singleton", ks, raising=False)
    return ks


async def _explode(url, payload, timeout=20.0):
    raise AssertionError("kill-switch 生效时不应发起 HTTP")


def test_adapter_send_blocked_by_platform_kill_switch(monkeypatch, tmp_path):
    mgw = _web_account(monkeypatch)
    _install_ks(monkeypatch, tmp_path, "platform:messenger")
    monkeypatch.setattr(mgw, "_post_json", _explode)
    req = _req(config_manager=SimpleNamespace(config={}))
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(MessengerInboxAdapter().send(req, "100", "555", "hi"))
    assert ei.value.status_code == 403
    assert "kill_switch" in ei.value.detail


def test_adapter_send_blocked_by_account_kill_switch(monkeypatch, tmp_path):
    mgw = _web_account(monkeypatch)
    _install_ks(monkeypatch, tmp_path, "account:messenger:100")
    monkeypatch.setattr(mgw, "_post_json", _explode)
    req = _req(config_manager=SimpleNamespace(config={}))
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(MessengerInboxAdapter().send(req, "100", "555", "hi"))
    assert ei.value.status_code == 403


def test_adapter_send_other_platform_block_does_not_affect_messenger(
    monkeypatch, tmp_path,
):
    """冻结的是 line → messenger 照常发（验证 platform 作用域精确，不误伤）。"""
    mgw = _web_account(monkeypatch)
    _install_ks(monkeypatch, tmp_path, "platform:line")
    sent = {}

    async def _fake(url, payload, timeout=20.0):
        sent["url"] = url
        return {"ok": True, "sent": True}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    req = _req(config_manager=SimpleNamespace(config={}))
    res = asyncio.run(MessengerInboxAdapter().send(req, "100", "555", "hi"))
    assert res["delivered"] is True
    assert sent["url"] == "http://svc/accounts/100/send"


def test_adapter_rpa_path_also_blocked_by_kill_switch(monkeypatch, tmp_path):
    """护栏在分流之前 → RPA 分支同样被拦（不是只护网页分支）。"""
    import src.integrations.account_registry as ar
    monkeypatch.setattr(
        ar, "get_account_registry",
        lambda: SimpleNamespace(get=lambda p, a: None))  # 注册表沉默 → RPA 优先
    _install_ks(monkeypatch, tmp_path, "platform:messenger")

    class _RpaSvc:
        async def send_to_chat_name_for_account(self, *a, **k):  # pragma: no cover
            raise AssertionError("kill-switch 生效时不应调 RPA 发送")

    req = _req(messenger_rpa_service=_RpaSvc(),
               config_manager=SimpleNamespace(config={}))
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(MessengerInboxAdapter().send(req, "acc", "客户A", "hi"))
    assert ei.value.status_code == 403


# ── 3) 会话健康发送闸：掉线快速失败，恢复自动放行 ────────────────────────────

def test_worker_send_fails_fast_when_session_unhealthy(monkeypatch):
    import src.integrations.messenger_web_login as mgw
    from src.integrations.platform_session_health import (
        get_platform_session_health,
    )
    get_platform_session_health().record(
        "messenger", "100", "expired", detail="crash-loop give-up")
    called = {"n": 0}

    async def _fake(url, payload, timeout=20.0):
        called["n"] += 1
        return {"ok": True, "sent": True}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    res = asyncio.run(_worker().send("555", "hi"))
    assert res["delivered"] is False
    assert res["blocked"] == "session_unhealthy"
    assert called["n"] == 0  # 不做注定失败的 DOM 尝试

    # 会话恢复（Node push authorized）→ 自动放行
    get_platform_session_health().record("messenger", "100", "authorized")
    res2 = asyncio.run(_worker().send("555", "hi"))
    assert res2["delivered"] is True and called["n"] == 1


def test_worker_send_media_also_gated(monkeypatch):
    import src.integrations.messenger_web_login as mgw
    from src.integrations.platform_session_health import (
        get_platform_session_health,
    )
    get_platform_session_health().record("messenger", "100", "needs_login")

    async def _fake(url, payload, timeout=120.0):  # pragma: no cover
        raise AssertionError("掉线会话不应尝试发媒体")

    monkeypatch.setattr(mgw, "_post_json", _fake)
    res = asyncio.run(_worker().send_media(
        "555", media_path="x.jpg", media_type="image"))
    assert res["delivered"] is False and res["blocked"] == "session_unhealthy"


def test_adapter_manual_path_not_gated_by_session_health(monkeypatch):
    """人工适配器路径**不受**健康闸限制（登记陈旧也不锁死人工重试）。"""
    mgw = _web_account(monkeypatch)
    from src.integrations.platform_session_health import (
        get_platform_session_health,
    )
    get_platform_session_health().record("messenger", "100", "expired")
    sent = {}

    async def _fake(url, payload, timeout=20.0):
        sent["url"] = url
        return {"ok": True, "sent": True}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    req = _req(config_manager=SimpleNamespace(config={}))
    res = asyncio.run(MessengerInboxAdapter().send(req, "100", "555", "hi"))
    assert res["delivered"] is True and "url" in sent
