"""Phase G1：WhatsApp Cloud API 官方适配器单测。

不联网：验签 / 入站解析 / Kill-Switch 冻结 / send 的 payload 与错误处理（mock aiohttp）。
"""
import hashlib
import hmac
import json

import pytest

from src.integrations import whatsapp_cloud as wac
from src.integrations.whatsapp_cloud import (
    extract_inbound_messages, send_url, verify_wa_signature, wa_send_text,
)


# ── 验签 ─────────────────────────────────────────────────────────────────────

def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

def test_verify_signature_ok():
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_wa_signature(body, _sign(body, "s3cr3t"), "s3cr3t") is True

def test_verify_signature_bad():
    body = b'{"x":1}'
    assert verify_wa_signature(body, _sign(body, "wrong"), "s3cr3t") is False
    assert verify_wa_signature(body, "", "s3cr3t") is False
    assert verify_wa_signature(body, _sign(body, "s"), "") is False  # 空 secret 硬拒


# ── 入站解析 ─────────────────────────────────────────────────────────────────

def test_extract_inbound_text_message():
    body = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "PNID1"},
            "messages": [{"from": "8613800138000", "id": "wamid.X", "timestamp": "100",
                          "type": "text", "text": {"body": "你好"}}],
        }}]}],
    }
    msgs = extract_inbound_messages(body)
    assert len(msgs) == 1
    assert msgs[0]["text"]["body"] == "你好"
    assert msgs[0]["_phone_number_id"] == "PNID1"

def test_extract_ignores_non_wa_object():
    assert extract_inbound_messages({"object": "page", "entry": []}) == []

def test_extract_ignores_status_only_change():
    body = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "P"}, "statuses": [{"status": "delivered"}]}}]}]}
    assert extract_inbound_messages(body) == []


# ── send：mock aiohttp ───────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status, text):
        self.status = status
        self._text = text
    async def text(self):
        return self._text
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False

class _FakeSession:
    def __init__(self, resp, captured):
        self._resp = resp
        self._captured = captured
    def post(self, url, headers=None, json=None):
        self._captured["url"] = url
        self._captured["headers"] = headers
        self._captured["json"] = json
        return self._resp
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


def _patch_session(monkeypatch, resp, captured):
    monkeypatch.setattr(wac.aiohttp, "ClientSession",
                        lambda *a, **k: _FakeSession(resp, captured))
    monkeypatch.setattr(wac.aiohttp, "ClientTimeout", lambda **k: None)


async def test_send_text_ok_payload(monkeypatch):
    captured = {}
    _patch_session(monkeypatch, _FakeResp(200, '{"messages":[{"id":"wamid.OUT"}]}'), captured)
    out = await wa_send_text("8613800138000", "hi there", "PNID1", "TOKEN",
                             check_kill_switch=False)
    assert out["ok"] is True
    assert captured["url"] == send_url("PNID1")
    assert captured["headers"]["Authorization"] == "Bearer TOKEN"
    body = captured["json"]
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == "8613800138000"
    assert body["text"]["body"] == "hi there"


async def test_send_text_http_error(monkeypatch):
    _patch_session(monkeypatch, _FakeResp(401, "Unauthorized"), {})
    out = await wa_send_text("x", "hi", "P", "BADTOKEN", check_kill_switch=False)
    assert out["ok"] is False and "401" in out["error"]


async def test_send_empty_text_skipped(monkeypatch):
    # 空文本不应发起请求
    called = {"n": 0}
    monkeypatch.setattr(wac.aiohttp, "ClientSession",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call")))
    out = await wa_send_text("x", "   ", "P", "T", check_kill_switch=False)
    assert out["ok"] is True and out["data"].get("skipped") == "empty"


# ── Kill-Switch 冻结：官方通道也受全局急停约束 ──────────────────────────────

async def test_send_blocked_by_kill_switch(monkeypatch, tmp_path):
    from src.ops.kill_switch import KillSwitch
    from src.integrations.shared import rpa_send_guard

    ks = KillSwitch(tmp_path / "rf.db")
    ks.set("global", reason="emergency")
    # 让守卫用我们的 ks（不碰真单例）
    monkeypatch.setattr(rpa_send_guard, "get_kill_switch", lambda *a, **k: ks, raising=False)
    import src.ops.kill_switch as ksmod
    monkeypatch.setattr(ksmod, "_singleton", ks, raising=False)

    # 冻结时不应发起 HTTP
    monkeypatch.setattr(wac.aiohttp, "ClientSession",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not send")))
    out = await wa_send_text("x", "hi", "PNID1", "TOKEN", check_kill_switch=True)
    assert out["ok"] is False and out["error"].startswith("kill_switch:")
