"""Phase 5：坐席 presence + 会话租约 + web 漏斗指标。"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page not used")


class _Cfg:
    config = {
        "workspace": {"claim_ttl_sec": 300, "presence_stale_sec": 60},
    }


def _client(inbox_store=None):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=page_auth, api_auth=api_auth,
        templates=_Templates(), config_manager=_Cfg(),
    )
    if inbox_store is not None:
        app.state.inbox_store = inbox_store
    return TestClient(app)


def test_presence_and_claim_lifecycle(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    c = _client(inbox_store=store)

    r = c.post("/api/workspace/presence", json={"status": "online", "display_name": "Alice"})
    assert r.status_code == 200
    assert r.json()["presence"]["status"] == "online"

    r2 = c.get("/api/workspace/presence")
    assert r2.status_code == 200
    assert len(r2.json()["agents"]) >= 1

    cid = "line:default:room1"
    claim = c.post("/api/workspace/claim", json={"conversation_id": cid, "force": False})
    assert claim.status_code == 200
    assert claim.json()["ok"] is True
    assert claim.json()["claim"]["conversation_id"] == cid

    # 第二坐席抢锁应失败
    c2 = TestClient(c.app)
    c2.post("/api/workspace/presence", json={"status": "online", "display_name": "Bob"})
    # 模拟另一 session：改 middleware 不好，直接 force=False 同一 agent 测 renew
    renew = c.post("/api/workspace/claim/renew", json={"conversation_id": cid})
    assert renew.json()["ok"] is True

    rel = c.post("/api/workspace/claim/release", json={"conversation_id": cid})
    assert rel.json()["ok"] is True
    assert rel.json()["released"] is True

    claims = c.get("/api/workspace/claims")
    assert all(cl["conversation_id"] != cid for cl in claims.json()["claims"])
    store.close()


def test_claim_conflict_then_force(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.set_conversation_claim("web:web:v1", "agent-a", agent_name="A", ttl_sec=300)
    c = _client(inbox_store=store)

    blocked = c.post("/api/workspace/claim", json={"conversation_id": "web:web:v1"})
    assert blocked.json()["ok"] is False
    assert blocked.json()["reason"] == "already_claimed"

    forced = c.post("/api/workspace/claim", json={"conversation_id": "web:web:v1", "force": True})
    assert forced.json()["ok"] is True
    store.close()


def test_web_funnel_metrics_endpoint():
    c = _client(inbox_store=None)
    r = c.get("/api/workspace/metrics/web-funnel")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "web_sessions" in data["metrics"]
