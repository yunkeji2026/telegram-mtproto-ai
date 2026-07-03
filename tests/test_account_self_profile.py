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


# ── P4: dict / pushname 抽取（多平台通用） ────────────────────────────────────

def test_extract_from_dict_pushname():
    # WhatsApp Baileys 只给单一 pushname（dict 形态）
    assert sp.extract_self_profile({"pushname": "小雨"}) == {"self_name": "小雨"}


def test_extract_from_dict_name_and_username():
    got = sp.extract_self_profile({"name": "Lin Xiaoyu", "username": "@linxy"})
    assert got == {"self_name": "Lin Xiaoyu", "self_username": "linxy"}


def test_extract_display_name_alias():
    # LINE displayName / Messenger name 走同一 name 别名回落
    assert sp.extract_self_profile({"display_name": "阿龙"}) == {"self_name": "阿龙"}


# ── P4: enrich_from_fields（通用富集入口） ────────────────────────────────────

async def test_enrich_from_fields_disabled_short_circuits(monkeypatch):
    reg = _FakeRegistry()
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry",
                        lambda *a, **k: reg)
    got = await sp.enrich_from_fields("whatsapp", "wa1", name="小雨", config={})
    assert got == {}
    assert reg.upserts == []


async def test_enrich_from_fields_writes_name_and_avatar_url(monkeypatch):
    reg = _FakeRegistry(existing_meta={"baileys_login_id": "L1"})
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry",
                        lambda *a, **k: reg)
    cfg = {"accounts": {"self_profile": {"enabled": True}}}
    got = await sp.enrich_from_fields(
        "whatsapp", "wa1", name="小雨", avatar_url="https://x/pic.jpg", config=cfg)
    assert got["self_name"] == "小雨"
    assert got["self_avatar"] == "https://x/pic.jpg"   # 远端直链，不本地下载
    _, _, written = reg.upserts[0]
    assert written["baileys_login_id"] == "L1"          # 既有字段保住
    assert written["self_name"] == "小雨"


async def test_enrich_from_fields_empty_is_noop(monkeypatch):
    reg = _FakeRegistry()
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry",
                        lambda *a, **k: reg)
    cfg = {"accounts": {"self_profile": {"enabled": True}}}
    got = await sp.enrich_from_fields("whatsapp", "wa1", name="", config=cfg)
    assert got == {}
    assert reg.upserts == []


async def test_enrich_from_fields_idempotent_skip(monkeypatch):
    reg = _FakeRegistry(existing_meta={"self_name": "小雨"})
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry",
                        lambda *a, **k: reg)
    cfg = {"accounts": {"self_profile": {"enabled": True}}}
    got = await sp.enrich_from_fields("whatsapp", "wa1", name="小雨", config=cfg)
    assert got == {"self_name": "小雨"}
    assert reg.upserts == []   # 无变化 → 零写入


# ── P4: cleanup_avatar（账号移除回收） ────────────────────────────────────────

def test_cleanup_avatar_deletes_file(tmp_path):
    fn = sp._safe_avatar_filename("telegram", "123")
    f = tmp_path / fn
    f.write_bytes(b"img")
    assert f.exists()
    assert sp.cleanup_avatar("telegram", "123", avatar_dir=str(tmp_path)) is True
    assert not f.exists()


def test_cleanup_avatar_missing_file_ok(tmp_path):
    # 无头像文件也算成功（幂等，删无可删）
    assert sp.cleanup_avatar("telegram", "nope", avatar_dir=str(tmp_path)) is True


# ── P5: sweep_orphan_avatars（孤儿清扫） ──────────────────────────────────────

def test_sweep_removes_only_orphans(tmp_path):
    keep = tmp_path / sp._safe_avatar_filename("telegram", "123")
    orphan = tmp_path / sp._safe_avatar_filename("whatsapp", "gone")
    keep.write_bytes(b"a")
    orphan.write_bytes(b"b")
    # known 用 (platform, account_id) 元组集合
    res = sp.sweep_orphan_avatars({("telegram", "123")}, avatar_dir=str(tmp_path))
    assert res == {"scanned": 2, "removed": 1}
    assert keep.exists()          # 活跃账号头像保留
    assert not orphan.exists()    # 孤儿删除


def test_sweep_accepts_filename_keys(tmp_path):
    f = tmp_path / sp._safe_avatar_filename("telegram", "123")
    f.write_bytes(b"a")
    # known 用文件名基（含/不含 .jpg 都可）
    res = sp.sweep_orphan_avatars({"self_telegram_123"}, avatar_dir=str(tmp_path))
    assert res["removed"] == 0 and f.exists()


def test_sweep_missing_dir_noop():
    res = sp.sweep_orphan_avatars({("telegram", "1")}, avatar_dir="/no/such/dir/xyz")
    assert res == {"scanned": 0, "removed": 0}


def test_sweep_empty_known_removes_all(tmp_path):
    (tmp_path / sp._safe_avatar_filename("telegram", "1")).write_bytes(b"a")
    (tmp_path / sp._safe_avatar_filename("line", "2")).write_bytes(b"b")
    res = sp.sweep_orphan_avatars(set(), avatar_dir=str(tmp_path))
    assert res == {"scanned": 2, "removed": 2}


# ── P4: Prometheus dump ───────────────────────────────────────────────────────

def test_dump_self_profile_prom():
    sp.reset_self_profile_stats()
    txt = sp.dump_self_profile_prom()
    assert "# TYPE account_self_profile_total counter" in txt
    assert 'account_self_profile_total{op="written"} 0' in txt
    assert 'account_self_profile_total{op="avatar_downloaded"} 0' in txt
