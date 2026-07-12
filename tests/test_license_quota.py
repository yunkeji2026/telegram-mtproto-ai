"""P0-4 免费试用（字符额度）测试：C2 payload 扩展 / C1 计量层 / C3 强制 / C4 粘贴激活。

- C2：``included_chars``/``trial`` 经 issue→verify→to_dict 全链路透传；``gate.quota_exceeded`` 纯函数。
- C1：``LicenseQuotaStore`` 按 (lic_id, 日, 类目) 聚合、跨实例（重启）持久、remaining 口径。
- C3：``check_license_quota`` 三态（不限/软超额 warn-only/enforce 硬阻断）+ 翻译与 TTS 热路接线。
- C4：``POST /api/admin/license/activate`` 验签通过才写盘 reload；坏 key 不落盘。
"""

import time
from types import SimpleNamespace

import pytest

from src.licensing import (
    LicenseManager,
    generate_keypair,
    issue_license,
    quota_exceeded,
)
from src.licensing.quota_store import (
    QUOTA_EXCEEDED_ERROR,
    LicenseQuotaStore,
    check_license_quota,
    configure_license_quota_store,
    record_license_chars,
    reset_license_quota_store,
)


@pytest.fixture()
def keypair():
    return generate_keypair()


@pytest.fixture(autouse=True)
def _clean_quota_singleton():
    """每个用例前后清空 quota store 单例，避免串味/误写真实 config 目录。"""
    reset_license_quota_store()
    yield
    reset_license_quota_store()


def _fake_status(*, licensed=True, included=100, lic_id="LIC-1",
                 enforce=False, state="active"):
    return SimpleNamespace(
        licensed=licensed, included_chars=included, lic_id=lic_id,
        enforce=enforce, state=state,
    )


# ── C2：payload 扩展 ─────────────────────────────────────────────────────────

def test_payload_included_chars_and_trial_roundtrip(keypair):
    """included_chars/trial 经签发→验签→status→to_dict 全链路透传。"""
    token = issue_license(
        {"sub": "ACME", "plan": "basic", "exp": int(time.time()) + 86400,
         "included_chars": 50000, "trial": True},
        keypair["private_hex"],
    )
    st = LicenseManager(
        license_token=token, public_key_hex=keypair["public_hex"],
    ).status()
    assert st.state == "active"
    assert st.included_chars == 50000
    assert st.trial is True
    d = st.to_dict()
    assert d["included_chars"] == 50000
    assert d["trial"] is True


def test_payload_chars_default_zero_unlimited(keypair):
    """省略 included_chars → 0（不限）；trial 默认 False。"""
    token = issue_license({"sub": "X", "plan": "pro"}, keypair["private_hex"])
    st = LicenseManager(
        license_token=token, public_key_hex=keypair["public_hex"],
    ).status()
    assert st.included_chars == 0
    assert st.trial is False


def test_license_tool_issue_supports_chars_and_trial(keypair, tmp_path):
    """CLI ``issue --chars N --trial`` 把额度/试用标记写进 payload。"""
    import scripts.license_tool as lt

    priv = tmp_path / "vendor.key"
    priv.write_text(keypair["private_hex"], encoding="utf-8")
    out = tmp_path / "license.key"
    args = SimpleNamespace(
        priv=str(priv), sub="ACME", plan="basic", days=30,
        seats=0, channels="", features="", lic_id="T-1",
        chars="12345", trial=True, out=str(out),
    )
    lt._cmd_issue(args)
    st = LicenseManager(
        license_token=out.read_text(encoding="utf-8").strip(),
        public_key_hex=keypair["public_hex"],
    ).status()
    assert st.included_chars == 12345
    assert st.trial is True
    assert st.lic_id == "T-1"


def test_quota_exceeded_pure_function():
    """gate.quota_exceeded：仅 active/grace + included>0 + used>=included 时 True。"""
    assert quota_exceeded("active", 100, 100) is True
    assert quota_exceeded("grace", 101, 100) is True
    assert quota_exceeded("active", 99, 100) is False
    assert quota_exceeded("active", 999, 0) is False        # 不限
    assert quota_exceeded("expired", 999, 100) is False     # 过期走只读锁语义
    assert quota_exceeded("unlicensed", 999, 100) is False  # 社区模式无额度概念
    assert quota_exceeded("active", "garbage", "junk") is False


# ── C1：计量层（SQLite 持久化）───────────────────────────────────────────────

def test_quota_store_record_and_aggregate(tmp_path):
    store = LicenseQuotaStore(tmp_path / "q.db")
    store.record("L1", "translation", 100)
    store.record("L1", "translation", 50)
    store.record("L1", "tts", 30)
    store.record("L2", "tts", 999)   # 其它授权不串味
    assert store.used_chars("L1") == 180
    assert store.used_chars("L1", "translation") == 150
    assert store.used_chars("L1", "tts") == 30
    u = store.usage("L1")
    assert u["total"] == 180
    assert u["by_category"] == {"translation": 150, "tts": 30}
    assert u["today"] == 180


def test_quota_store_persists_across_reopen(tmp_path):
    """同一 db 文件重开（模拟重启）→ 累计不丢。"""
    p = tmp_path / "q.db"
    LicenseQuotaStore(p).record("L1", "tts", 42)
    store2 = LicenseQuotaStore(p)
    assert store2.used_chars("L1") == 42


def test_quota_store_remaining_and_edge_cases(tmp_path):
    store = LicenseQuotaStore(tmp_path / "q.db")
    store.record("L1", "tts", 70)
    assert store.remaining("L1", 100) == 30
    store.record("L1", "tts", 200)   # 超额后下限截断为 0
    assert store.remaining("L1", 100) == 0
    assert store.remaining("L1", 0) is None      # 不限
    store.record("L1", "tts", 0)     # 非正数忽略
    store.record("L1", "tts", -5)
    assert store.used_chars("L1") == 270


def test_record_license_chars_noop_without_quota(tmp_path, monkeypatch):
    """无额度授权（unlicensed / included=0）→ 不建库零 IO。"""
    import src.licensing.quota_store as qs

    configure_license_quota_store(db_path=tmp_path / "q.db")
    monkeypatch.setattr(qs, "_current_status",
                        lambda: _fake_status(licensed=False, included=0))
    record_license_chars("tts", 100)
    assert qs.get_license_quota_store() is None
    monkeypatch.setattr(qs, "_current_status",
                        lambda: _fake_status(licensed=True, included=0))
    record_license_chars("tts", 100)
    assert qs.get_license_quota_store() is None


# ── C3：额度闸门 ─────────────────────────────────────────────────────────────

def test_check_quota_unlimited_always_allowed(tmp_path, monkeypatch):
    import src.licensing.quota_store as qs

    configure_license_quota_store(db_path=tmp_path / "q.db")
    monkeypatch.setattr(qs, "_current_status", lambda: _fake_status(included=0))
    out = check_license_quota()
    assert out["allowed"] is True and out["exceeded"] is False


def test_check_quota_soft_vs_enforced(tmp_path, monkeypatch):
    """额度用尽：enforce 关 → 仅 warn 放行；enforce 开 → 阻断。"""
    import src.licensing.quota_store as qs

    configure_license_quota_store(db_path=tmp_path / "q.db")
    st_soft = _fake_status(included=100, enforce=False)
    monkeypatch.setattr(qs, "_current_status", lambda: st_soft)
    record_license_chars("translation", 100)   # 恰好耗尽
    out = check_license_quota()
    assert out["exceeded"] is True and out["allowed"] is True
    assert out["used"] == 100 and out["included"] == 100 and out["remaining"] == 0

    st_hard = _fake_status(included=100, enforce=True)
    monkeypatch.setattr(qs, "_current_status", lambda: st_hard)
    out2 = check_license_quota()
    assert out2["exceeded"] is True and out2["allowed"] is False


def test_check_quota_under_limit_allowed(tmp_path, monkeypatch):
    import src.licensing.quota_store as qs

    configure_license_quota_store(db_path=tmp_path / "q.db")
    st = _fake_status(included=100, enforce=True)
    monkeypatch.setattr(qs, "_current_status", lambda: st)
    record_license_chars("tts", 60)
    out = check_license_quota()
    assert out["allowed"] is True and out["remaining"] == 40


async def test_translation_blocked_and_metered(tmp_path, monkeypatch):
    """翻译热路：enforce+超额 → 引擎调用前被拦（稳定错误码）；未超额时成功翻译记账。"""
    import src.licensing.quota_store as qs
    from src.ai.translation_service import TranslationService

    configure_license_quota_store(db_path=tmp_path / "q.db")
    st = _fake_status(included=10, enforce=True)
    monkeypatch.setattr(qs, "_current_status", lambda: st)
    record_license_chars("translation", 10)   # 耗尽

    svc = TranslationService()   # 无引擎/无 key：若未被额度拦会走 no-engine 分支
    res = await svc.translate("hello world", target_lang="zh")
    assert res.ok is False
    assert res.error == QUOTA_EXCEEDED_ERROR
    assert res.provider == "license"


async def test_tts_blocked_and_metered(tmp_path, monkeypatch):
    """TTS 热路：enforce+超额 → 合成前被拦；正常合成成功后按文本字符记账。"""
    import src.licensing.quota_store as qs
    from src.ai.tts_pipeline import TTSPipeline, TTSResult

    configure_license_quota_store(db_path=tmp_path / "q.db")
    st = _fake_status(included=100, enforce=True, lic_id="L-TTS")
    monkeypatch.setattr(qs, "_current_status", lambda: st)

    pipe = TTSPipeline({"enabled": True, "backend": "edge_tts"})

    async def _fake_uncached(text, *, voice=None, timeout_sec=30.0, spec=None):
        return TTSResult(ok=True, text=text, provider="fake", voice="v",
                         format="mp3", audio_path="")

    monkeypatch.setattr(pipe, "_synthesize_uncached", _fake_uncached)

    r1 = await pipe.synthesize("hello")          # 5 字符 → 记账
    assert r1.ok is True
    assert qs.get_license_quota_store().used_chars("L-TTS") == 5

    record_license_chars("tts", 95)              # 合计 100 = 耗尽
    r2 = await pipe.synthesize("more text")
    assert r2.ok is False
    assert r2.error == QUOTA_EXCEEDED_ERROR


# ── C4：粘贴激活路由 ─────────────────────────────────────────────────────────

def _activate_app(tmp_path, keypair):
    """最小 app + 指向测试公钥/临时 license 文件的 manager 单例。"""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from src.web.routes.license_routes import register_license_routes

    lic_path = tmp_path / "license.key"
    mgr = LicenseManager(
        license_path=str(lic_path), public_key_hex=keypair["public_hex"],
    )
    app = FastAPI()
    register_license_routes(app, api_auth=lambda r: None)
    return TestClient(app), mgr, lic_path


@pytest.fixture()
def activate_env(tmp_path, keypair, monkeypatch):
    client, mgr, lic_path = _activate_app(tmp_path, keypair)
    import src.licensing as lic_pkg

    monkeypatch.setattr(lic_pkg, "get_license_manager", lambda **kw: mgr)
    return client, mgr, lic_path


def test_activate_requires_key(activate_env):
    client, _, lic_path = activate_env
    r = client.post("/api/admin/license/activate", json={})
    assert r.status_code == 400
    assert not lic_path.exists()


def test_activate_rejects_invalid_key(activate_env):
    """乱码/签名不符 → 400 且不写盘（防把现有授权顶坏）。"""
    client, _, lic_path = activate_env
    r = client.post("/api/admin/license/activate", json={"key": "not-a-token"})
    assert r.status_code == 400
    assert not lic_path.exists()


def test_activate_rejects_expired_key(activate_env, keypair):
    client, _, lic_path = activate_env
    token = issue_license(
        {"sub": "X", "plan": "basic",
         "exp": int(time.time()) - 60 * 86400, "grace_days": 7},
        keypair["private_hex"],
    )
    r = client.post("/api/admin/license/activate", json={"key": token})
    assert r.status_code == 400
    assert not lic_path.exists()


def test_activate_valid_key_writes_and_reloads(activate_env, keypair):
    """有效 key → 写 license 文件 + reload 后 state=active + 响应带 quota 快照。"""
    client, mgr, lic_path = activate_env
    token = issue_license(
        {"sub": "ACME", "plan": "pro", "exp": int(time.time()) + 30 * 86400,
         "included_chars": 9000, "trial": True, "lic_id": "ACT-1"},
        keypair["private_hex"],
    )
    r = client.post("/api/admin/license/activate", json={"key": token})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["state"] == "active"
    assert d["included_chars"] == 9000 and d["trial"] is True
    assert "quota" in d
    assert lic_path.read_text(encoding="utf-8").strip() == token
    assert mgr.status().state == "active"


def test_get_license_includes_quota_snapshot(activate_env):
    client, _, _ = activate_env
    r = client.get("/api/admin/license")
    assert r.status_code == 200
    q = r.json().get("quota")
    assert isinstance(q, dict)
    for k in ("included_chars", "used_chars", "remaining_chars", "exceeded"):
        assert k in q


def test_preview_token_does_not_touch_disk(tmp_path, keypair):
    """preview_token 只验签出快照，不写盘不动缓存。"""
    lic_path = tmp_path / "license.key"
    mgr = LicenseManager(
        license_path=str(lic_path), public_key_hex=keypair["public_hex"],
    )
    token = issue_license({"sub": "X", "plan": "pro"}, keypair["private_hex"])
    st = mgr.preview_token(token)
    assert st.state == "active"
    assert not lic_path.exists()
    assert mgr.status().state == "unlicensed"   # 单例自身状态未被污染


# ── C9：send-caps 语音模式注解（顺带回归）────────────────────────────────────

def test_send_caps_shape(auth_client):
    """send-caps 契约：can_media/can_voice 分字段 + voice_mode 三态。"""
    r = auth_client.get(
        "/api/unified-inbox/send-caps?platform=telegram&account_id=nope",
        follow_redirects=False,
    )
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert "can_media" in d and "can_voice" in d
    assert d["voice_mode"] in ("composer", "auto_only", "none")
    # 无在线协议 worker 的账号：不支持直发
    assert d["can_media"] is False and d["can_voice"] is False
