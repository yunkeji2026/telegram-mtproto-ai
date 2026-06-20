"""Phase G2：官方通道（LINE / Messenger / WhatsApp Cloud）Kill-Switch 护栏覆盖。

把 Phase C「真·全局」延伸到官方 API：global/platform 冻结时官方通道也不物理外发。
不联网——冻结时断言「不发起 HTTP」即可证明守卫生效。
"""
import pytest

from src.ops.kill_switch import KillSwitch
import src.ops.kill_switch as ksmod
from src.integrations import line_webhook as lw
from src.integrations import facebook_webhook as fbw


def _install_ks(monkeypatch, tmp_path, scope="global"):
    ks = KillSwitch(tmp_path / "rf.db")
    ks.set(scope, reason="emergency")
    monkeypatch.setattr(ksmod, "_singleton", ks, raising=False)
    return ks


def _explode_session(*a, **k):
    raise AssertionError("kill-switch 生效时不应发起 HTTP")


# ── LINE ─────────────────────────────────────────────────────────────────────

async def test_line_reply_blocked_global(monkeypatch, tmp_path):
    _install_ks(monkeypatch, tmp_path, "global")
    monkeypatch.setattr(lw.aiohttp, "ClientSession", _explode_session)
    assert await lw.line_reply("tok", "hi", "AT") is False


async def test_line_push_blocked_platform(monkeypatch, tmp_path):
    _install_ks(monkeypatch, tmp_path, "platform:line")
    monkeypatch.setattr(lw.aiohttp, "ClientSession", _explode_session)
    assert await lw.line_push("U123", "hi", "AT") is False


async def test_line_push_not_blocked_other_platform(monkeypatch, tmp_path):
    # 冻结的是 messenger，不应影响 line（验证 platform 作用域精确）
    _install_ks(monkeypatch, tmp_path, "platform:messenger")
    captured = {"sent": False}

    class _R:
        status = 200
        async def text(self): return "{}"
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _S:
        def post(self, *a, **k):
            captured["sent"] = True
            return _R()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(lw.aiohttp, "ClientSession", lambda *a, **k: _S())
    monkeypatch.setattr(lw.aiohttp, "ClientTimeout", lambda **k: None)
    assert await lw.line_push("U", "hi", "AT") is True
    assert captured["sent"] is True


# ── Messenger ────────────────────────────────────────────────────────────────

async def test_fb_send_blocked_global(monkeypatch, tmp_path):
    _install_ks(monkeypatch, tmp_path, "global")
    monkeypatch.setattr(fbw.aiohttp, "ClientSession", _explode_session)
    out = await fbw.fb_send_message("psid", "hi", "PAGETOKEN")
    assert out["ok"] is False and out["error"].startswith("kill_switch:")


async def test_fb_send_account_scope(monkeypatch, tmp_path):
    _install_ks(monkeypatch, tmp_path, "account:messenger:PAGE1")
    monkeypatch.setattr(fbw.aiohttp, "ClientSession", _explode_session)
    out = await fbw.fb_send_message("psid", "hi", "PAGETOKEN", account_id="PAGE1")
    assert out["ok"] is False and "kill_switch" in out["error"]


async def test_fb_send_check_disabled_bypasses_guard(monkeypatch, tmp_path):
    # check_kill_switch=False 时即便冻结也尝试发（供特殊运维路径）
    _install_ks(monkeypatch, tmp_path, "global")
    captured = {"sent": False}

    class _R:
        status = 200
        async def text(self): return '{"message_id":"m"}'
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _S:
        def post(self, *a, **k):
            captured["sent"] = True
            return _R()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(fbw.aiohttp, "ClientSession", lambda *a, **k: _S())
    monkeypatch.setattr(fbw.aiohttp, "ClientTimeout", lambda **k: None)
    out = await fbw.fb_send_message("psid", "hi", "T", check_kill_switch=False)
    assert out["ok"] is True and captured["sent"] is True
