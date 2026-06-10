"""声纹自助登记闭环回归。

纯函数（无网络/IO）：
  - build_enroll_payload / parse_voice_id  — 登记请求体 / 响应解析
  - qwen_profile_json_dict                 — qwen_tts_wrapper 消费的 voice-profile JSON
  - build_qwen_voice_profile               — 写回人设的 voice_profile（含 command_args 占位）

契约：
  - GET  /api/voice/cloned  — 无 key 时优雅返回 {ok:false, reason:"no_api_key"}（不 500）
  - POST /api/voice/enroll  — 缺 file/persona_id/preferred_name → 400；无 key → 优雅 reason
真实云端登记依赖 DASHSCOPE_API_KEY + 网络，不在单测内打真实外呼。
"""
import io

import pytest

from src.ai.voice_enroll import (
    ENROLLMENT_MODEL,
    build_delete_payload,
    build_enroll_payload,
    build_qwen_voice_profile,
    collect_local_voice_refs,
    copy_voice_profile,
    delete_cloned_voice,
    normalize_cloud_voice_list,
    parse_voice_id,
    purge_guard,
    qwen_profile_json_dict,
    reconcile_voice_assets,
    without_voice_profile,
)


def test_build_enroll_payload_shape():
    p = build_enroll_payload(data_uri="data:audio/wav;base64,AAAA", preferred_name="victor")
    assert p["model"] == ENROLLMENT_MODEL
    assert p["input"]["action"] == "create"
    assert p["input"]["preferred_name"] == "victor"
    assert p["input"]["audio"]["data"].startswith("data:audio/wav;base64,")


def test_parse_voice_id():
    assert parse_voice_id({"output": {"voice": "voice-abc-123"}}) == "voice-abc-123"
    assert parse_voice_id({"output": {}}) == ""
    assert parse_voice_id({}) == ""
    assert parse_voice_id(None) == ""


def test_qwen_profile_json_dict_consumable_by_wrapper():
    d = qwen_profile_json_dict(
        voice="v1", target_model="", reference_audio_path="/a/b.wav",
        region="cn", preferred_name="victor")
    # qwen_tts_wrapper._load_voice 读 voice / target_model
    assert d["provider"] == "qwen"
    assert d["voice"] == "v1"
    assert d["target_model"]  # 空 → 回落默认模型，不能为空


def test_build_qwen_voice_profile_ready_and_placeholders():
    vp = build_qwen_voice_profile(
        voice="v1", reference_audio_path="/a/b.wav",
        voice_profile_json_path="/a/qwen_x.json", speaker_id="x", region="cn")
    # /api/voice/profiles 的 ready 依赖 owner_consent + reference_audio_path
    assert vp["enabled"] is True
    assert vp["owner_consent"] is True
    assert vp["reference_audio_path"] == "/a/b.wav"
    assert vp["backend"] == "voice_clone_command"
    # TTSPipeline 运行时 .format(text=..,out=..)；{text}/{out} 必须保留
    assert "{text}" in vp["command_args"]
    assert "{out}" in vp["command_args"]
    assert "/a/qwen_x.json" in vp["command_args"]


def test_voice_cloned_graceful_without_key(auth_client, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    r = auth_client.get("/api/voice/cloned", follow_redirects=False)
    assert r.status_code == 200
    d = r.json()
    # 无 key → 优雅降级，不抛 500
    assert d.get("ok") in (False, True)
    if d.get("ok") is False:
        assert d.get("reason") in ("no_api_key", "list_failed")


def test_enroll_requires_fields(auth_client):
    # 缺 file → 400
    r = auth_client.post(
        "/api/voice/enroll",
        data={"persona_id": "default", "preferred_name": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_enroll_missing_persona_id(auth_client):
    r = auth_client.post(
        "/api/voice/enroll",
        data={"preferred_name": "x"},
        files={"file": ("a.wav", io.BytesIO(b"RIFF0000WAVE"), "audio/wav")},
        follow_redirects=False,
    )
    assert r.status_code == 400


# ── 生命周期：解绑 / 改绑 纯函数 ──────────────────────────────────────────────
def test_without_voice_profile_is_copy():
    src = {"name": "A", "voice_profile": {"voice": "v1"}}
    out = without_voice_profile(src)
    assert "voice_profile" not in out
    assert out["name"] == "A"
    assert "voice_profile" in src  # 不可原地改写入参


def test_copy_voice_profile():
    src = {"voice_profile": {"voice": "v1", "enabled": True}}
    dst = {"name": "B"}
    out = copy_voice_profile(src, dst)
    assert out["name"] == "B"
    assert out["voice_profile"]["voice"] == "v1"
    # 深拷一层：改 out 不影响 src
    out["voice_profile"]["voice"] = "vX"
    assert src["voice_profile"]["voice"] == "v1"


def test_copy_voice_profile_source_without_voice_noop():
    out = copy_voice_profile({"name": "A"}, {"name": "B"})
    assert "voice_profile" not in out


# ── 生命周期端点契约 ─────────────────────────────────────────────────────────
def test_unbind_missing_persona_404(auth_client):
    r = auth_client.delete("/api/voice/profiles/__no_such_persona__", follow_redirects=False)
    assert r.status_code == 404


def test_rebind_requires_both_ids(auth_client):
    r = auth_client.post(
        "/api/voice/rebind", json={"from_persona_id": "a"}, follow_redirects=False)
    assert r.status_code == 400


def test_rebind_same_id_rejected(auth_client):
    r = auth_client.post(
        "/api/voice/rebind",
        json={"from_persona_id": "a", "to_persona_id": "a"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_rebind_missing_source_404(auth_client):
    r = auth_client.post(
        "/api/voice/rebind",
        json={"from_persona_id": "__no_src__", "to_persona_id": "__no_dst__"},
        follow_redirects=False,
    )
    assert r.status_code == 404


# ── 云端真删：纯请求体 + 入参校验 ─────────────────────────────────────────────
def test_build_delete_payload_shape():
    p = build_delete_payload("voice-xyz")
    assert p["model"] == ENROLLMENT_MODEL
    assert p["input"]["action"] == "delete"
    assert p["input"]["voice"] == "voice-xyz"


def test_delete_cloned_voice_missing_voice_raises():
    # 有 key 但 voice 空 → ValueError（在任何网络调用之前）
    with pytest.raises(ValueError):
        delete_cloned_voice(voice="", api_key="dummy-key")


def test_unbind_purge_param_on_missing_persona_404(auth_client):
    # purge_cloud 仍需先存在人设；不存在 → 404（不触发云端调用）
    r = auth_client.delete(
        "/api/voice/profiles/__no_such_persona__?purge_cloud=1", follow_redirects=False)
    assert r.status_code == 404


# ── 资产对账：纯函数 ─────────────────────────────────────────────────────────
def test_normalize_cloud_voice_list_qwen_and_cosyvoice():
    items = normalize_cloud_voice_list([
        {"voice": "q1", "target_model": "m1"},
        {"voice_id": "cosy-1", "status": "OK"},
        {"bad": True},
    ])
    assert [x["voice"] for x in items] == ["q1", "cosy-1"]


def test_collect_local_voice_refs_skips_empty_voice():
    refs = collect_local_voice_refs([
        {"persona_id": "a", "name": "A", "persona": {"voice_profile": {"voice": "v1"}}},
        {"persona_id": "b", "name": "B", "persona": {"voice_profile": {"voice": ""}}},
        {"persona_id": "c", "name": "C", "persona": {}},
    ])
    assert list(refs.keys()) == ["v1"]
    assert refs["v1"][0]["persona_id"] == "a"


def test_reconcile_voice_assets_orphans_shared_dangling():
    cloud = [{"voice": "orph"}, {"voice": "shared"}, {"voice": "linked"}]
    local = {
        "shared": [{"persona_id": "p1", "name": "P1"}, {"persona_id": "p2", "name": "P2"}],
        "linked": [{"persona_id": "p3", "name": "P3"}],
        "gone": [{"persona_id": "p4", "name": "P4"}],
    }
    r = reconcile_voice_assets(cloud, local)
    assert [x["voice"] for x in r["orphans"]] == ["orph"]
    assert r["orphans"][0]["ref_count"] == 0
    assert r["shared"][0]["ref_count"] == 2
    assert r["linked"][0]["ref_count"] == 1
    assert [x["voice"] for x in r["dangling"]] == ["gone"]
    assert r["summary"]["orphan_count"] == 1
    assert r["summary"]["dangling_count"] == 1


def test_purge_guard_blocks_in_use():
    refs = {"v1": [{"persona_id": "p1", "name": "P1"}]}
    blocked = purge_guard("v1", refs, force=False)
    assert blocked["allowed"] is False
    assert blocked["reason"] == "in_use"
    assert blocked["ref_count"] == 1
    allowed = purge_guard("v1", refs, force=True)
    assert allowed["allowed"] is True


def test_purge_guard_allows_orphan():
    assert purge_guard("orph", {}, force=False)["allowed"] is True


# ── 对账端点契约 ─────────────────────────────────────────────────────────────
def test_reconcile_contract(auth_client):
    r = auth_client.get("/api/voice/reconcile", follow_redirects=False)
    assert r.status_code == 200
    d = r.json()
    assert "summary" in d
    assert "orphans" in d and "shared" in d and "dangling" in d and "linked" in d
    if d.get("ok"):
        assert isinstance(d["summary"], dict)


def test_purge_requires_voice(auth_client):
    r = auth_client.post("/api/voice/purge", json={}, follow_redirects=False)
    assert r.status_code == 400


def test_purge_orphans_contract(auth_client):
    r = auth_client.post("/api/voice/purge-orphans", json={}, follow_redirects=False)
    assert r.status_code == 200
    d = r.json()
    # 无 key 时 ok:false；有 key 时 ok:true — 两种均合法
    assert d.get("ok") in (True, False)
    if d.get("ok"):
        assert "deleted" in d and isinstance(d["deleted"], list)
