"""P1 账号身份化：account_self_profile 单测。

覆盖纯函数（extract / merge / read）+ flag 判定 + enrich_from_user 的
flag-off 短路与 flag-on read-merge-write（用假 registry，不触真号/真库）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.integrations import account_self_profile as sp


# ── 纯函数：extract_self_profile ─────────────────────────────────────────────

def test_extract_full_name_and_username():
    u = SimpleNamespace(first_name="Lin", last_name="Xiaoyu",
                        username="@linxy", phone_number="8613800000000")
    got = sp.extract_self_profile(u)
    assert got == {"self_name": "Lin Xiaoyu", "self_username": "linxy"}
    # 手机号不入 meta（PII 面收敛）
    assert "self_phone" not in got


def test_extract_name_falls_back_to_username_when_no_name():
    u = SimpleNamespace(first_name="", last_name="", username="botlike")
    got = sp.extract_self_profile(u)
    assert got["self_name"] == "botlike"
    assert got["self_username"] == "botlike"


def test_extract_only_first_name():
    u = SimpleNamespace(first_name="阿龙", last_name=None, username=None)
    got = sp.extract_self_profile(u)
    assert got == {"self_name": "阿龙"}


def test_extract_empty_user_returns_empty():
    assert sp.extract_self_profile(None) == {}
    assert sp.extract_self_profile(SimpleNamespace()) == {}


def test_extract_truncates_long_values():
    u = SimpleNamespace(first_name="x" * 200, username="y" * 200)
    got = sp.extract_self_profile(u)
    assert len(got["self_name"]) == 60
    assert len(got["self_username"]) == 60


# ── 纯函数：merge / read ─────────────────────────────────────────────────────

def test_merge_preserves_other_keys():
    existing = {"session_string": "SECRET", "phone": "123"}
    merged = sp.merge_self_profile_meta(existing, {"self_name": "A", "self_username": "a"})
    assert merged["session_string"] == "SECRET"   # 不能被覆盖/丢失
    assert merged["phone"] == "123"
    assert merged["self_name"] == "A"
    # 不改原 dict
    assert "self_name" not in existing


def test_merge_empty_profile_noop():
    existing = {"session_string": "S"}
    assert sp.merge_self_profile_meta(existing, {}) == {"session_string": "S"}


def test_merge_empty_value_does_not_overwrite():
    existing = {"self_name": "Old"}
    merged = sp.merge_self_profile_meta(existing, {"self_name": ""})
    assert merged["self_name"] == "Old"


def test_read_self_profile_from_meta():
    meta = {"self_name": "N", "self_username": "u", "self_avatar": "/x.jpg",
            "session_string": "S", "phone": "1"}
    assert sp.read_self_profile_from_meta(meta) == {
        "self_name": "N", "self_username": "u", "self_avatar": "/x.jpg"}
    assert sp.read_self_profile_from_meta({}) == {}


# ── flags ────────────────────────────────────────────────────────────────────

def test_flags_default_off():
    assert sp.self_profile_enabled(None) is False
    assert sp.self_profile_enabled({}) is False
    assert sp.self_avatar_enabled({}) is False


def test_flags_on():
    cfg = {"accounts": {"self_profile": {"enabled": True, "avatar": True}}}
    assert sp.self_profile_enabled(cfg) is True
    assert sp.self_avatar_enabled(cfg) is True


# ── enrich_from_user ─────────────────────────────────────────────────────────

class _FakeRegistry:
    def __init__(self, existing_meta=None):
        self._meta = dict(existing_meta or {})
        self.upserts = []

    def get(self, platform, account_id):
        return {"meta": dict(self._meta)}

    def upsert(self, platform, account_id, *, meta=None, **kw):
        self.upserts.append((platform, account_id, meta))
        self._meta = dict(meta or {})
        return {"meta": self._meta}


async def test_enrich_disabled_short_circuits(monkeypatch):
    reg = _FakeRegistry()
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry",
                        lambda *a, **k: reg)
    u = SimpleNamespace(first_name="Lin", username="linxy")
    got = await sp.enrich_from_user("telegram", "123", u, config={})
    assert got == {}
    assert reg.upserts == []   # 未启用绝不写库


async def test_enrich_enabled_read_merge_write(monkeypatch):
    reg = _FakeRegistry(existing_meta={"session_string": "KEEP", "phone": "999"})
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry",
                        lambda *a, **k: reg)
    cfg = {"accounts": {"self_profile": {"enabled": True}}}  # avatar 关
    u = SimpleNamespace(first_name="Lin", last_name="Xy", username="linxy")
    got = await sp.enrich_from_user("telegram", "123", u, config=cfg)
    assert got == {"self_name": "Lin Xy", "self_username": "linxy"}
    assert len(reg.upserts) == 1
    _, _, written = reg.upserts[0]
    assert written["session_string"] == "KEEP"   # 敏感字段保住
    assert written["phone"] == "999"
    assert written["self_name"] == "Lin Xy"


async def test_enrich_skips_write_when_unchanged(monkeypatch):
    # 既有 meta 已含相同 self_* → 幂等跳过，不再写库（不 bump updated_at）
    reg = _FakeRegistry(existing_meta={"self_name": "Lin Xy", "self_username": "linxy",
                                       "session_string": "KEEP"})
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry",
                        lambda *a, **k: reg)
    cfg = {"accounts": {"self_profile": {"enabled": True}}}
    u = SimpleNamespace(first_name="Lin", last_name="Xy", username="linxy")
    got = await sp.enrich_from_user("telegram", "123", u, config=cfg)
    assert got == {"self_name": "Lin Xy", "self_username": "linxy"}
    assert reg.upserts == []   # 无变化 → 零写入


async def test_enrich_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry", _boom)
    cfg = {"accounts": {"self_profile": {"enabled": True}}}
    u = SimpleNamespace(first_name="Lin", username="linxy")
    got = await sp.enrich_from_user("telegram", "123", u, config=cfg)
    assert got == {}   # 异常吞掉，返回空


# ── P2: 头像缓存击穿 + 变更检测（纯函数） ─────────────────────────────────────

def test_photo_file_ref():
    assert sp.photo_file_ref(SimpleNamespace(photo=SimpleNamespace(big_file_id="B"))) == "B"
    assert sp.photo_file_ref(
        SimpleNamespace(photo=SimpleNamespace(small_file_id="S", big_file_id=None))) == "S"
    assert sp.photo_file_ref(SimpleNamespace(photo=None)) == ""
    assert sp.photo_file_ref(SimpleNamespace()) == ""


def test_avatar_cache_key_deterministic_and_busts():
    assert sp.avatar_cache_key("FID1") == sp.avatar_cache_key("FID1")   # 同图恒定
    assert sp.avatar_cache_key("FID1") != sp.avatar_cache_key("FID2")   # 换图即变
    assert sp.avatar_cache_key("") == ""
    assert len(sp.avatar_cache_key("x")) == 8


def test_build_avatar_url():
    assert sp.build_avatar_url("telegram", "123", "FID1") == \
        "/static/persona_avatars/self_telegram_123.jpg?v=" + sp.avatar_cache_key("FID1")
    assert sp.build_avatar_url("telegram", "123", "") == \
        "/static/persona_avatars/self_telegram_123.jpg"


def test_avatar_needs_refresh():
    assert sp.avatar_needs_refresh({}, "FID1") is True                       # 从没存过
    assert sp.avatar_needs_refresh(
        {"self_avatar_fid": "FID1", "self_avatar": "/x"}, "FID1") is False    # 未变
    assert sp.avatar_needs_refresh(
        {"self_avatar_fid": "OLD", "self_avatar": "/x"}, "FID1") is True      # 换图
    assert sp.avatar_needs_refresh({"self_avatar_fid": "FID1"}, "FID1") is True  # 有指纹无URL
    assert sp.avatar_needs_refresh({}, "") is False                          # 无 fid


# ── P2: 头像下载/复用 + 计数 ──────────────────────────────────────────────────

class _FakeClient:
    def __init__(self):
        self.downloads = 0

    async def download_media(self, file_ref, file_name=None):
        self.downloads += 1
        return file_name or "saved.jpg"


async def test_enrich_downloads_avatar_and_cache_busts(monkeypatch, tmp_path):
    sp.reset_self_profile_stats()
    reg = _FakeRegistry()
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry",
                        lambda *a, **k: reg)
    cfg = {"accounts": {"self_profile": {"enabled": True, "avatar": True}}}
    cl = _FakeClient()
    u = SimpleNamespace(first_name="Lin", username="linxy",
                        photo=SimpleNamespace(big_file_id="FID1"))
    got = await sp.enrich_from_user("telegram", "123", u,
                                    config=cfg, client=cl, avatar_dir=str(tmp_path))
    assert cl.downloads == 1
    assert got["self_avatar"].startswith("/static/persona_avatars/self_telegram_123.jpg?v=")
    st = sp.get_self_profile_stats()
    assert st["avatar_downloaded"] == 1 and st["written"] == 1
    _, _, written = reg.upserts[0]
    assert written["self_avatar_fid"] == "FID1"          # 内部指纹持久化
    assert "self_avatar_fid" not in sp.read_self_profile_from_meta(written)  # 不外泄


async def test_enrich_reuses_avatar_when_unchanged(monkeypatch, tmp_path):
    sp.reset_self_profile_stats()
    existing = {"self_name": "Lin", "self_username": "linxy",
                "self_avatar": "/static/persona_avatars/self_telegram_123.jpg?v=abc",
                "self_avatar_fid": "FID1"}
    reg = _FakeRegistry(existing_meta=existing)
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry",
                        lambda *a, **k: reg)
    cfg = {"accounts": {"self_profile": {"enabled": True, "avatar": True}}}
    cl = _FakeClient()
    u = SimpleNamespace(first_name="Lin", username="linxy",
                        photo=SimpleNamespace(big_file_id="FID1"))
    await sp.enrich_from_user("telegram", "123", u,
                              config=cfg, client=cl, avatar_dir=str(tmp_path))
    assert cl.downloads == 0     # 指纹未变 → 不重下
    assert reg.upserts == []     # 无变化 → 不写库
    st = sp.get_self_profile_stats()
    assert st["avatar_reused"] == 1 and st["skipped"] == 1


def test_stats_reset():
    sp.reset_self_profile_stats()
    st = sp.get_self_profile_stats()
    assert set(st) == {"calls", "written", "skipped",
                       "avatar_downloaded", "avatar_reused", "errors"}
    assert all(v == 0 for v in st.values())
