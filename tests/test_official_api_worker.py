"""Phase G 延伸：官方 API 出站 worker（mode=official）单测。

凭证解析（meta 优先 / config 回退）、healthy 门控、send 分发到对应官方助手、
编排器注册门控 + ORCHESTRATED_MODES 含 official。
"""
import pytest

from src.integrations import official_api_worker as oaw
from src.integrations.official_api_worker import (
    OfficialApiWorker, official_enabled, register_official_workers,
)


def _acc(platform, account_id="acct1", meta=None):
    return {"platform": platform, "account_id": account_id, "mode": "official",
            "meta": meta or {}}


# ── 凭证解析 ─────────────────────────────────────────────────────────────────

def test_creds_from_config_fallback():
    w = OfficialApiWorker(_acc("line"), {"line": {"channel_access_token": "LINETOK"}})
    assert w._creds()["access_token"] == "LINETOK"
    assert w._creds_ok() is True

def test_creds_meta_overrides_config():
    w = OfficialApiWorker(
        _acc("messenger", meta={"page_access_token": "META_TOK"}),
        {"facebook_messenger": {"page_access_token": "CFG_TOK"}})
    assert w._creds()["access_token"] == "META_TOK"

def test_whatsapp_needs_both_token_and_phone_id():
    w = OfficialApiWorker(_acc("whatsapp"), {"whatsapp_cloud": {"access_token": "T"}})
    assert w._creds_ok() is False  # 缺 phone_number_id
    w2 = OfficialApiWorker(_acc("whatsapp"),
                           {"whatsapp_cloud": {"access_token": "T", "phone_number_id": "P"}})
    assert w2._creds_ok() is True


# ── 生命周期门控 ─────────────────────────────────────────────────────────────

async def test_start_fails_without_creds():
    w = OfficialApiWorker(_acc("line"), {})
    with pytest.raises(RuntimeError):
        await w.start()
    assert await w.healthy() is False

async def test_start_ok_and_status():
    w = OfficialApiWorker(_acc("line"), {"line": {"channel_access_token": "T"}})
    await w.start()
    assert await w.healthy() is True
    assert w.status()["type"] == "line_official"
    await w.stop()
    assert await w.healthy() is False


# ── send 分发 ────────────────────────────────────────────────────────────────

async def test_send_line_routes_to_line_push(monkeypatch):
    captured = {}
    async def _fake_push(to, text, token, *, account_id="default", **k):
        captured.update(to=to, text=text, token=token, account_id=account_id)
        return True
    import src.integrations.line_webhook as lw
    monkeypatch.setattr(lw, "line_push", _fake_push)

    w = OfficialApiWorker(_acc("line", account_id="bot1"),
                          {"line": {"channel_access_token": "LT"}})
    res = await w.send("Uxxx", "hello")
    assert res["delivered"] is True
    assert captured == {"to": "Uxxx", "text": "hello", "token": "LT", "account_id": "bot1"}


async def test_send_whatsapp_routes_to_wa_send_text(monkeypatch):
    captured = {}
    async def _fake_wa(to, text, phone_id, token, **k):
        captured.update(to=to, phone_id=phone_id, token=token)
        return {"ok": True, "data": {"messages": [{"id": "wamid.OUT"}]}}
    import src.integrations.whatsapp_cloud as wac
    monkeypatch.setattr(wac, "wa_send_text", _fake_wa)

    w = OfficialApiWorker(_acc("whatsapp"),
                          {"whatsapp_cloud": {"access_token": "T", "phone_number_id": "PN"}})
    res = await w.send("8613800138000", "hi")
    assert res["delivered"] is True and res["message_id"] == "wamid.OUT"
    assert captured == {"to": "8613800138000", "phone_id": "PN", "token": "T"}


async def test_send_messenger_routes_to_fb(monkeypatch):
    async def _fake_fb(psid, text, token, **k):
        return {"ok": True, "data": {"message_id": "mid.1"}}
    import src.integrations.facebook_webhook as fbw
    monkeypatch.setattr(fbw, "fb_send_with_window_fallback", _fake_fb)

    w = OfficialApiWorker(_acc("messenger"),
                          {"facebook_messenger": {"page_access_token": "PT"}})
    res = await w.send("PSID", "hi")
    assert res["delivered"] is True


# ── 编排器集成 ───────────────────────────────────────────────────────────────

def test_official_enabled_via_channel_block():
    assert official_enabled({"whatsapp_cloud": {"enabled": True}}, "whatsapp") is True
    assert official_enabled({"line": {"enabled": False}}, "line") is False

def test_official_enabled_via_platform_login():
    cfg = {"platform_login": {"official": {"messenger": {"enabled": True}}}}
    assert official_enabled(cfg, "messenger") is True


def test_official_mode_in_orchestrated_modes():
    from src.integrations.account_orchestrator import ORCHESTRATED_MODES, worker_supported
    assert "official" in ORCHESTRATED_MODES


def test_register_official_workers_gated(monkeypatch):
    import src.integrations.account_orchestrator as ao
    # 用隔离的注册表，避免污染全局
    monkeypatch.setattr(ao, "_WORKER_FACTORIES", {}, raising=False)
    register_official_workers({"whatsapp_cloud": {"enabled": True}})
    assert ao.get_worker_factory("whatsapp", "official") is not None
    # 未启用的平台不注册
    assert ao.get_worker_factory("line", "official") is None
