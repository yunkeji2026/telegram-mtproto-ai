"""M7：LINE 协议模式（okline）Python 桥接 单测（不联网，用假 OkLine）。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

from src.integrations import platform_login as pl
from src.integrations import line_protocol_login as lpl
from src.integrations.account_registry import AccountRegistry


class _FakeLoginResult:
    success = True
    mid = "u_test123"
    display_message = ""


class _FakeOkLine:
    """最小假 OkLine：qr_login 立刻回调 on_qr 并返回成功结果。"""

    def __init__(self, *a, **k):
        self.saved = ""

    def qr_login(self, *, on_qr=None, on_pin=None, wait_seconds=170.0):
        if on_qr:
            on_qr("line://qr/abc")
        return _FakeLoginResult()

    def save_tokens(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        self.saved = path

    def get_profile(self, sync_reason: int = 2):
        return {"displayName": "Tester", "picturePath": "/avatar.jpg"}

    def close(self):
        pass


def test_protocol_enabled_flag():
    assert lpl.protocol_enabled({}) is False
    assert lpl.protocol_enabled(
        {"platform_login": {"line": {"protocol_enabled": True}}}) is True


def test_line_picture_url_builds_stable_obs_link():
    # 相对 picturePath → 拼成 obs CDN 直链（OBS_BASE 或回落硬编码域名，均 http 且以路径结尾）
    u = lpl.line_picture_url("/p/av1")
    assert u.startswith("http") and u.endswith("/p/av1")


def test_line_picture_url_passthrough_and_empty():
    assert lpl.line_picture_url("https://obs.line-scdn.net/x") == "https://obs.line-scdn.net/x"
    assert lpl.line_picture_url("") == ""
    assert lpl.line_picture_url(None) == ""


def test_self_profile_fields_uses_picture_url():
    # 自身资料富集：显名 + 头像直链（复用 line_picture_url，回归防抽取重构改坏）
    name, avatar = lpl._self_profile_fields(_FakeOkLine())
    assert name == "Tester"
    assert avatar.startswith("http") and avatar.endswith("/avatar.jpg")


def test_sessions_dir_and_tokens_path():
    assert lpl.sessions_dir({}).replace("\\", "/").endswith("sessions/line")
    cfg = {"platform_login": {"line": {"sessions_dir": "/tmp/ln"}}}
    assert lpl.sessions_dir(cfg) == "/tmp/ln"
    assert lpl.tokens_path(cfg, "u_1").replace("\\", "/") == "/tmp/ln/u_1.json"


def test_is_okline_available_returns_bool():
    assert isinstance(lpl.is_okline_available(), bool)


def test_drive_qr_login_success_writes_state_and_tokens():
    d = tempfile.mkdtemp()
    cfg = {"platform_login": {"line": {"sessions_dir": d}}}
    state: dict = {}
    client = _FakeOkLine()
    lpl._drive_qr_login(client, state, cfg)
    assert state["status"] == "authorized"
    assert state["mid"] == "u_test123"
    assert state["qr_url"] == "line://qr/abc"
    assert state["name"] == "Tester"
    # tokens 应已落盘
    assert os.path.exists(lpl.tokens_path(cfg, "u_test123"))


def test_maybe_register_gating(monkeypatch):
    lpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("line", "protocol"), None)
    assert lpl.maybe_register({}) is False
    assert pl.mode_available("line", "protocol") is False
    # okline 可用时开闸即注册
    monkeypatch.setattr(lpl, "is_okline_available", lambda: True)
    try:
        assert lpl.maybe_register(
            {"platform_login": {"line": {"protocol_enabled": True}}}) is True
        assert pl.mode_available("line", "protocol") is True
    finally:
        lpl._registered = False
        pl._PROVIDERS.pop(pl._pkey("line", "protocol"), None)


def test_maybe_register_skips_when_okline_missing(monkeypatch):
    lpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("line", "protocol"), None)
    monkeypatch.setattr(lpl, "is_okline_available", lambda: False)
    assert lpl.maybe_register(
        {"platform_login": {"line": {"protocol_enabled": True}}}) is False
    assert pl.mode_available("line", "protocol") is False


def test_provider_flow_authorized_persists(monkeypatch):
    import okline
    monkeypatch.setattr(okline, "OkLine", _FakeOkLine)
    monkeypatch.setattr(lpl, "is_okline_available", lambda: True)

    d = tempfile.mkdtemp()
    reg = AccountRegistry(os.path.join(d, "line.db"))
    monkeypatch.setattr(lpl, "get_account_registry", lambda: reg)
    cfg = {"platform_login": {"line": {"sessions_dir": d}}}

    async def run():
        provider = lpl.make_provider(cfg)
        info = await provider(None, "line", "protocol", "")
        assert "poll" in info
        poll = info["poll"]
        # 后台线程很快完成；轮询直到 authorized
        for _ in range(50):
            r = await poll(None)
            if r["status"] == "authorized":
                break
            time.sleep(0.1)
        assert r["status"] == "authorized"
        assert r["account_id"] == "u_test123"

    asyncio.run(run())
    g = reg.get("line", "u_test123")
    assert g and g["mode"] == "protocol" and g["status"] == "online"


def test_provider_okline_missing_returns_instruction(monkeypatch):
    monkeypatch.setattr(lpl, "is_okline_available", lambda: False)

    async def run():
        provider = lpl.make_provider({})
        info = await provider(None, "line", "protocol", "")
        assert "instruction" in info
        assert "poll" not in info

    asyncio.run(run())
