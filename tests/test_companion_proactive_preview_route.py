"""陪伴主动话题·预览 API 契约：/api/companion/proactive/preview。

覆盖：预览回调缺失（未就绪）→ available=false 不报错；挂上回调 → 透传其返回；
回调抛错 → ok=false 软失败。预览只读、不触发发送。
"""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.integrations.companion_sample_store import CompanionSampleStore
from src.web.routes.companion_proactive_routes import (
    register_companion_proactive_routes,
)


def _client():
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_companion_proactive_routes(app, api_auth=_auth)
    return app, TestClient(app)


def test_preview_unavailable_when_not_mounted():
    _app, client = _client()
    r = client.get("/api/companion/proactive/preview")
    body = r.json()
    assert body["ok"] is True
    assert body["available"] is False
    assert body["plans"] == []


def test_preview_passthrough_when_mounted():
    app, client = _client()
    captured = {}

    def _preview(limit=50):
        captured["limit"] = limit
        return {
            "enabled": False, "dry_run": True, "scanned": 3, "candidates": 1,
            "care_dedup_active": True,
            "plans": [{
                "conversation_id": "telegram:default:123", "platform": "telegram",
                "mode": "follow_up", "fact": "在备考",
                "context_facts": ["养了只猫"], "silent_hours": 48.0,
                "would_send_this_tick": True,
            }],
        }

    app.state.companion_proactive_preview = _preview
    r = client.get("/api/companion/proactive/preview?limit=20")
    body = r.json()
    assert body["ok"] is True and body["available"] is True
    assert captured["limit"] == 20
    assert body["candidates"] == 1
    assert body["plans"][0]["fact"] == "在备考"
    assert body["plans"][0]["context_facts"] == ["养了只猫"]
    assert body["plans"][0]["would_send_this_tick"] is True
    assert body["care_dedup_active"] is True


def test_preview_soft_fails_when_callback_raises():
    app, client = _client()

    def _boom(limit=50):
        raise RuntimeError("kaboom")

    app.state.companion_proactive_preview = _boom
    r = client.get("/api/companion/proactive/preview")
    body = r.json()
    assert body["ok"] is False
    assert body["plans"] == []


# ── 试发采样 /sample ────────────────────────────────────────────────────

def test_sample_unavailable_when_not_mounted():
    _app, client = _client()
    r = client.post("/api/companion/proactive/sample", json={"conversation_id": "x"})
    body = r.json()
    assert body["ok"] is True
    assert body["available"] is False
    assert body["generated"] is False


def test_sample_requires_conversation_id():
    app, client = _client()

    async def _gen(cid):
        return {"generated": True, "text": "在么"}

    app.state.companion_proactive_generate = _gen
    r = client.post("/api/companion/proactive/sample", json={})
    body = r.json()
    assert body["ok"] is False
    assert body["generated"] is False


def test_sample_passthrough_generated_text():
    app, client = _client()
    captured = {}

    async def _gen(cid):
        captured["cid"] = cid
        return {
            "generated": True, "text": "上次你说在备考，后来还顺利吗？",
            "mode": "follow_up", "fact": "在备考",
            "context_facts": ["养了只猫"], "silent_hours": 48.0,
        }

    app.state.companion_proactive_generate = _gen
    r = client.post("/api/companion/proactive/sample",
                    json={"conversation_id": "telegram:default:123"})
    body = r.json()
    assert body["ok"] is True and body["available"] is True
    assert captured["cid"] == "telegram:default:123"
    assert body["generated"] is True
    assert "备考" in body["text"]
    assert body["fact"] == "在备考"


def test_sample_soft_fails_when_callback_raises():
    app, client = _client()

    async def _gen(cid):
        raise RuntimeError("kaboom")

    app.state.companion_proactive_generate = _gen
    r = client.post("/api/companion/proactive/sample", json={"conversation_id": "x"})
    body = r.json()
    assert body["ok"] is False
    assert body["generated"] is False


# ── 评分回流 /rate + /samples ────────────────────────────────────────────

def test_rate_and_samples_stats():
    app, client = _client()
    store = CompanionSampleStore(":memory:")
    app.state.companion_sample_store = store
    sid = store.record_sample(mode="follow_up", text="在么")

    r = client.post(f"/api/companion/proactive/sample/{sid}/rate",
                    json={"rating": "up", "note": "自然"})
    assert r.json()["ok"] is True and r.json()["rating"] == "up"

    s = client.get("/api/companion/proactive/samples").json()
    assert s["ok"] is True and s["available"] is True
    assert s["stats"]["up"] == 1 and s["stats"]["up_rate"] == 1.0
    assert len(s["items"]) == 1


def test_rate_rejects_bad_rating():
    app, client = _client()
    store = CompanionSampleStore(":memory:")
    app.state.companion_sample_store = store
    sid = store.record_sample(text="x")
    r = client.post(f"/api/companion/proactive/sample/{sid}/rate",
                    json={"rating": "meh"})
    assert r.json()["ok"] is False


def test_rate_unavailable_without_store():
    _app, client = _client()
    r = client.post("/api/companion/proactive/sample/1/rate", json={"rating": "up"})
    assert r.json()["ok"] is False


def test_samples_unavailable_without_store():
    _app, client = _client()
    s = client.get("/api/companion/proactive/samples").json()
    assert s["ok"] is True and s["available"] is False
    assert s["items"] == []


def test_tuning_advice_from_ratings():
    app, client = _client()
    store = CompanionSampleStore(":memory:")
    app.state.companion_sample_store = store
    for _ in range(7):
        store.rate(store.record_sample(mode="follow_up", text="在么"), "down",
                   edited_text="上次你说在备考，后来顺利吗？")
    for _ in range(3):
        store.rate(store.record_sample(mode="follow_up", text="hi"), "up")

    d = client.get("/api/companion/proactive/tuning-advice").json()
    assert d["ok"] is True and d["available"] is True
    adv = d["advice"]
    assert adv["overall"]["verdict"] == "low"
    assert adv["few_shot"]["improved"]  # 差评改写被收集为 few-shot 候选


def test_tuning_advice_unavailable_without_store():
    _app, client = _client()
    d = client.get("/api/companion/proactive/tuning-advice").json()
    assert d["ok"] is True and d["available"] is False
