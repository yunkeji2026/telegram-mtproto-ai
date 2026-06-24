"""官方通道发送错误统一分类（official_send_error）+ error_kind 贯穿 + IG 窗口回退 单测。

不联网：纯分类函数 + mock aiohttp 验证 error_kind 透出与 IG 24h 窗口降级重发。
"""
import json

import pytest

from src.integrations.shared.official_send_error import (
    TERMINAL_KINDS,
    classify_official_send_error,
    is_terminal,
)


# ── 纯分类：各平台已知 error.code ────────────────────────────────────────────

def test_whatsapp_window_expired():
    body = {"error": {"code": 131047, "message": "Re-engagement message"}}
    out = classify_official_send_error("whatsapp", status=400, body=body)
    assert out["kind"] == "window_expired"
    assert out["retriable"] is False


def test_whatsapp_invalid_token():
    body = {"error": {"code": 190, "message": "access token expired"}}
    assert classify_official_send_error("whatsapp", status=401, body=body)["kind"] == "invalid_token"


def test_whatsapp_rate_limited_is_retriable():
    body = {"error": {"code": 131048, "message": "Spam rate limit hit"}}
    out = classify_official_send_error("whatsapp", status=400, body=body)
    assert out["kind"] == "rate_limited" and out["retriable"] is True


def test_whatsapp_unsupported_type():
    body = {"error": {"code": 131051}}
    assert classify_official_send_error("whatsapp", status=400, body=body)["kind"] == "unsupported"


def test_graph_window_via_subcode():
    body = {"error": {"code": 10, "error_subcode": 2534022,
                      "message": "outside of allowed window"}}
    assert classify_official_send_error("messenger", status=400, body=body)["kind"] == "window_expired"
    assert classify_official_send_error("instagram", status=400, body=body)["kind"] == "window_expired"


def test_graph_rate_limited():
    body = {"error": {"code": 4, "message": "application request limit reached"}}
    assert classify_official_send_error("instagram", status=400, body=body)["kind"] == "rate_limited"


def test_zalo_window_and_token():
    assert classify_official_send_error("zalo", status=200,
                                        body={"error": -213})["kind"] == "window_expired"
    assert classify_official_send_error("zalo", status=200,
                                        body={"error": -201})["kind"] == "invalid_token"


# ── 兜底：body 拿不到 code 时走文本/HTTP 状态 ────────────────────────────────

def test_text_fallback_window_keyword():
    out = classify_official_send_error(
        "whatsapp", status=400,
        error_text='HTTP 400: {"error":{"message":"outside of allowed window"}}')
    assert out["kind"] == "window_expired"


def test_text_fallback_extracts_code_from_raw():
    # body 没解析成 dict，但原始串里有 "code":131047
    out = classify_official_send_error(
        "whatsapp", status=400, error_text='HTTP 400: {"error":{"code":131047}}')
    assert out["kind"] == "window_expired"


def test_http_status_fallback():
    assert classify_official_send_error("whatsapp", status=429)["kind"] == "rate_limited"
    assert classify_official_send_error("whatsapp", status=503)["kind"] == "transient"
    assert classify_official_send_error("whatsapp", status=401)["kind"] == "invalid_token"
    assert classify_official_send_error("whatsapp", status=418)["kind"] == "unknown"


def test_unknown_platform_unknown_kind():
    assert classify_official_send_error("telegram", status=None)["kind"] == "unknown"


def test_is_terminal():
    assert is_terminal("window_expired") and is_terminal("invalid_token")
    assert not is_terminal("rate_limited") and not is_terminal("transient")
    assert "recipient_unavailable" in TERMINAL_KINDS


# ── error_kind 透出：mock aiohttp 让 send 助手返回带 error_kind ─────────────────

class _Resp:
    def __init__(self, status, text):
        self.status = status
        self._text = text
    async def text(self):
        return self._text
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class _Sess:
    def __init__(self, resp):
        self._resp = resp
    def post(self, *a, **k):
        return self._resp
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


async def test_wa_send_surfaces_error_kind(monkeypatch):
    from src.integrations import whatsapp_cloud as wac
    resp = _Resp(400, json.dumps({"error": {"code": 131047}}))
    monkeypatch.setattr(wac.aiohttp, "ClientSession", lambda *a, **k: _Sess(resp))
    monkeypatch.setattr(wac.aiohttp, "ClientTimeout", lambda **k: None)
    out = await wac.wa_send_text("x", "hi", "P", "T", check_kill_switch=False)
    assert out["ok"] is False and out["error_kind"] == "window_expired"


async def test_zalo_send_surfaces_error_kind(monkeypatch):
    from src.integrations import zalo_webhook as zw
    resp = _Resp(200, json.dumps({"error": -213, "message": "out of window"}))
    monkeypatch.setattr(zw.aiohttp, "ClientSession", lambda *a, **k: _Sess(resp))
    monkeypatch.setattr(zw.aiohttp, "ClientTimeout", lambda **k: None)
    monkeypatch.setattr("src.integrations.shared.rpa_send_guard.rpa_send_blocked",
                        lambda p, a, **k: (False, ""))
    out = await zw.zalo_send_text("U9", "hi", "T", account_id="OA")
    assert out["ok"] is False and out["error_kind"] == "window_expired"


# ── IG 24h 窗口回退（HUMAN_AGENT 重发） ──────────────────────────────────────

async def test_ig_window_fallback_retries_with_tag(monkeypatch):
    from src.integrations import instagram_webhook as ig
    calls = []

    async def _fake(igsid, text, ig_id, token, *, account_id="default",
                    check_kill_switch=True, messaging_type="RESPONSE", message_tag=None):
        calls.append({"messaging_type": messaging_type, "tag": message_tag})
        if messaging_type == "RESPONSE":
            return {"ok": False, "error_kind": "window_expired", "error": "outside window"}
        return {"ok": True, "data": {"message_id": "m2"}}

    monkeypatch.setattr(ig, "ig_send_text", _fake)
    out = await ig.ig_send_with_window_fallback("IGS1", "hi", "IGID", "T")
    assert out["ok"] is True
    assert len(calls) == 2
    assert calls[0]["messaging_type"] == "RESPONSE"
    assert calls[1]["messaging_type"] == "MESSAGE_TAG" and calls[1]["tag"] == "HUMAN_AGENT"


async def test_ig_window_fallback_no_retry_on_other_error(monkeypatch):
    from src.integrations import instagram_webhook as ig
    calls = []

    async def _fake(igsid, text, ig_id, token, *, account_id="default",
                    check_kill_switch=True, messaging_type="RESPONSE", message_tag=None):
        calls.append(messaging_type)
        return {"ok": False, "error_kind": "invalid_token", "error": "bad token"}

    monkeypatch.setattr(ig, "ig_send_text", _fake)
    out = await ig.ig_send_with_window_fallback("IGS1", "hi", "IGID", "T")
    assert out["ok"] is False and len(calls) == 1  # token 错误不重试


async def test_worker_send_surfaces_error_kind(monkeypatch):
    from src.integrations.official_api_worker import OfficialApiWorker
    import src.integrations.zalo_webhook as zw

    async def _fake(uid, text, token, *, message_type="cs", account_id="default",
                    check_kill_switch=True):
        return {"ok": False, "error_kind": "window_expired", "error": "oow"}

    monkeypatch.setattr(zw, "zalo_send_text", _fake)
    acc = {"platform": "zalo", "account_id": "OA",
           "meta": {"access_token": "T", "message_type": "cs"}}
    w = OfficialApiWorker(acc, {})
    await w.start()
    out = await w.send("zalo:user:U9", "alo")
    assert out["delivered"] is False and out["error_kind"] == "window_expired"
