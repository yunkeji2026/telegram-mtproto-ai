"""M5：Messenger 网页模式（Playwright 微服务）Python 桥接 单测（不联网）。"""

from __future__ import annotations

import asyncio
import os
import tempfile

from src.integrations import platform_login as pl
from src.integrations import messenger_web_login as mgw
from src.integrations.account_registry import AccountRegistry


def test_service_base_url_default_and_override():
    assert mgw.service_base_url({}) == "http://127.0.0.1:8791"
    cfg = {"platform_login": {"messenger": {"web_url": "http://h:9/"}}}
    assert mgw.service_base_url(cfg) == "http://h:9"


def test_web_enabled_flag():
    assert mgw.web_enabled({}) is False
    assert mgw.web_enabled(
        {"platform_login": {"messenger": {"web_enabled": True}}}) is True


def test_normalize_status():
    assert mgw._normalize_status("open") == "authorized"
    assert mgw._normalize_status("connected") == "authorized"
    assert mgw._normalize_status("scanned") == "scanned"
    assert mgw._normalize_status("timeout") == "expired"
    assert mgw._normalize_status("logged_out") == "failed"
    assert mgw._normalize_status("whatever") == "pending"


def test_maybe_register_gating():
    mgw._registered = False
    pl._PROVIDERS.pop(pl._pkey("messenger", "web"), None)
    assert mgw.maybe_register({}) is False
    assert pl.mode_available("messenger", "web") is False
    try:
        assert mgw.maybe_register(
            {"platform_login": {"messenger": {"web_enabled": True}}}) is True
        assert pl.mode_available("messenger", "web") is True
    finally:
        mgw._registered = False
        pl._PROVIDERS.pop(pl._pkey("messenger", "web"), None)


def test_provider_flow_authorized(monkeypatch):
    # 伪造 Node/Playwright 微服务的 HTTP 响应
    async def fake_post(url, payload, timeout=20.0):
        if url.endswith("/login/start"):
            return {"login_id": "msg_abc", "qr_image": "data:image/png;base64,xxx",
                    "status": "pending"}
        return {"ok": True}

    calls = {"n": 0}

    async def fake_get(url, timeout=20.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "pending"}
        return {"status": "open", "account_id": "100012345678",
                "name": "Alice", "avatar_url": "http://x/a.jpg"}

    monkeypatch.setattr(mgw, "_post_json", fake_post)
    monkeypatch.setattr(mgw, "_get_json", fake_get)

    reg = AccountRegistry(os.path.join(tempfile.mkdtemp(), "mg.db"))
    monkeypatch.setattr(mgw, "get_account_registry", lambda: reg)

    async def run():
        provider = mgw.make_provider(
            {"platform_login": {"messenger": {"web_url": "http://x"}}})
        info = await provider(None, "messenger", "web", "")
        assert info["qr_image"].startswith("data:image/png")
        poll = info["poll"]
        r1 = await poll(None)
        assert r1["status"] == "pending"
        r2 = await poll(None)
        assert r2["status"] == "authorized"
        assert r2["account_id"] == "100012345678"

    asyncio.run(run())
    # 登录成功应已写入注册表
    g = reg.get("messenger", "100012345678")
    assert g and g["mode"] == "web" and g["status"] == "online"


def test_provider_start_service_down(monkeypatch):
    async def boom(url, payload, timeout=20.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mgw, "_post_json", boom)

    async def run():
        provider = mgw.make_provider({})
        info = await provider(None, "messenger", "web", "")
        assert "instruction" in info
        assert "poll" not in info  # 服务不可达 → 仅返回提示，不进入轮询

    asyncio.run(run())
