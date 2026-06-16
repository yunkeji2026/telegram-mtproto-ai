"""M3：WhatsApp Baileys 协议登录 Python 桥接 单测（不联网）。"""

from __future__ import annotations

import asyncio
import os
import tempfile

from src.integrations import platform_login as pl
from src.integrations import whatsapp_baileys_login as wab
from src.integrations.account_registry import AccountRegistry


def test_service_base_url_default_and_override():
    assert wab.service_base_url({}) == "http://127.0.0.1:8790"
    cfg = {"platform_login": {"whatsapp": {"baileys_url": "http://h:9/"}}}
    assert wab.service_base_url(cfg) == "http://h:9"


def test_protocol_enabled_flag():
    assert wab.protocol_enabled({}) is False
    assert wab.protocol_enabled(
        {"platform_login": {"whatsapp": {"protocol_enabled": True}}}) is True


def test_normalize_status():
    assert wab._normalize_status("open") == "authorized"
    assert wab._normalize_status("connected") == "authorized"
    assert wab._normalize_status("scanned") == "scanned"
    assert wab._normalize_status("timeout") == "expired"
    assert wab._normalize_status("logged_out") == "failed"
    assert wab._normalize_status("whatever") == "pending"


def test_maybe_register_gating():
    wab._registered = False
    pl._PROVIDERS.pop(pl._pkey("whatsapp", "protocol"), None)
    assert wab.maybe_register({}) is False
    assert pl.mode_available("whatsapp", "protocol") is False
    try:
        assert wab.maybe_register(
            {"platform_login": {"whatsapp": {"protocol_enabled": True}}}) is True
        assert pl.mode_available("whatsapp", "protocol") is True
    finally:
        wab._registered = False
        pl._PROVIDERS.pop(pl._pkey("whatsapp", "protocol"), None)


def test_provider_flow_authorized(monkeypatch):
    # 伪造 Node 微服务的 HTTP 响应
    async def fake_post(url, payload, timeout=20.0):
        if url.endswith("/login/start"):
            return {"login_id": "wa_abc", "qr_image": "data:image/png;base64,xxx"}
        return {"ok": True}

    calls = {"n": 0}

    async def fake_get(url, timeout=20.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "pending"}
        return {"status": "open", "account_id": "8613800000000"}

    monkeypatch.setattr(wab, "_post_json", fake_post)
    monkeypatch.setattr(wab, "_get_json", fake_get)

    reg = AccountRegistry(os.path.join(tempfile.mkdtemp(), "wa.db"))
    monkeypatch.setattr(wab, "get_account_registry", lambda: reg)

    async def run():
        provider = wab.make_provider(
            {"platform_login": {"whatsapp": {"baileys_url": "http://x"}}})
        info = await provider(None, "whatsapp", "protocol", "")
        assert info["qr_image"].startswith("data:image/png")
        poll = info["poll"]
        r1 = await poll(None)
        assert r1["status"] == "pending"
        r2 = await poll(None)
        assert r2["status"] == "authorized"
        assert r2["account_id"] == "8613800000000"

    asyncio.run(run())
    # 登录成功应已写入注册表
    g = reg.get("whatsapp", "8613800000000")
    assert g and g["mode"] == "protocol" and g["status"] == "online"


def test_provider_start_service_down(monkeypatch):
    async def boom(url, payload, timeout=20.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(wab, "_post_json", boom)

    async def run():
        provider = wab.make_provider({})
        info = await provider(None, "whatsapp", "protocol", "")
        assert "instruction" in info
        assert "poll" not in info  # 服务不可达 → 仅返回提示，不进入轮询

    asyncio.run(run())
