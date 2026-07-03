"""M1：多平台登录方式（mode）+ 账号注册表 单测。"""

from __future__ import annotations

import os
import tempfile

from src.integrations import platform_login as pl
from src.integrations.account_registry import AccountRegistry


def test_list_modes_defaults():
    tg = {m["mode"]: m for m in pl.list_modes("telegram")}
    assert set(tg) == {"protocol", "web", "device"}
    assert tg["device"]["available"] is True          # device 内置可用
    assert tg["protocol"]["recommended"] is True       # TG 默认 protocol
    # 未注册 provider 的 protocol/web 默认不可用
    assert tg["web"]["available"] is False

    line = {m["mode"]: m for m in pl.list_modes("line")}
    # M7：LINE 新增 protocol(okline) 方式，默认推荐 protocol；device 仍内置可用
    assert set(line) == {"protocol", "device"}
    assert line["protocol"]["recommended"] is True
    assert line["device"]["available"] is True
    # 未注册 provider 时 protocol 默认不可用（灰显「未启用」）
    assert line["protocol"]["available"] is False


def test_list_modes_config_override():
    cfg = {"modes": ["device"], "default": "device"}
    modes = pl.list_modes("telegram", cfg)
    assert [m["mode"] for m in modes] == ["device"]
    assert modes[0]["recommended"] is True


def test_register_provider_makes_mode_available():
    assert pl.mode_available("whatsapp", "protocol") is False
    pl.register_login_provider("whatsapp", "protocol", lambda *a, **k: {"qr_url": "x"})
    try:
        assert pl.mode_available("whatsapp", "protocol") is True
        modes = {m["mode"]: m for m in pl.list_modes("whatsapp")}
        assert modes["protocol"]["available"] is True
    finally:
        pl._PROVIDERS.pop(pl._pkey("whatsapp", "protocol"), None)


def test_online_account_keys():
    status_map = {
        "wa_a": {"platform": "whatsapp", "account_id": "a", "running": True},
        "wa_b": {"platform": "whatsapp", "account_id": "b", "running": False},
        "tg": {"platform": "telegram", "account_id": "t", "running": True},
    }
    assert pl.online_account_keys(status_map, "whatsapp") == {"a"}
    assert pl.online_account_keys(status_map, "telegram") == {"t"}
    assert pl.online_account_keys(status_map, "line") == set()


def test_login_manager_lifecycle():
    mgr = pl.LoginManager()
    s = mgr.create("telegram", "", baseline={"old"}, mode="protocol")
    assert s.status == "pending"
    assert s.mode == "protocol"
    assert mgr.get(s.login_id) is s
    mgr.cancel(s.login_id)
    assert mgr.get(s.login_id) is None


def test_login_session_expiry(monkeypatch):
    s = pl.LoginSession(login_id="x", platform="telegram")
    assert s.is_expired() is False
    s.created_at -= (pl.TTL_SEC + 5)
    assert s.is_expired() is True


def _fresh_registry() -> AccountRegistry:
    p = os.path.join(tempfile.mkdtemp(), "acct.db")
    return AccountRegistry(p)


def test_registry_upsert_merge_keeps_unspecified_fields():
    r = _fresh_registry()
    r.upsert("telegram", "123", mode="protocol", label="A", status="pending")
    r.upsert("telegram", "123", status="online")  # 只改状态
    g = r.get("telegram", "123")
    assert g["mode"] == "protocol"
    assert g["label"] == "A"
    assert g["status"] == "online"
    assert g["last_online_at"] > 0


def test_registry_list_and_remove():
    r = _fresh_registry()
    r.upsert("telegram", "1", mode="protocol")
    r.upsert("whatsapp", "2", mode="protocol")
    assert len(r.list()) == 2
    assert len(r.list("telegram")) == 1
    r.remove("telegram", "1")
    assert len(r.list("telegram")) == 0
    assert len(r.list("telegram", include_removed=True)) == 1


def test_registry_set_status_invalid_ignored():
    r = _fresh_registry()
    r.upsert("telegram", "1", status="pending")
    r.set_status("telegram", "1", "bogus")
    assert r.get("telegram", "1")["status"] == "pending"
