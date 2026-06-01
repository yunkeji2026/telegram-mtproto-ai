"""/api/drafts 路由测试（Phase B）。"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService
from src.web.routes.drafts_routes import register_drafts_routes


class LineSvc:
    account_id = "line-a"
    _merged_cfg = {"label": "LINE-A"}

    def __init__(self):
        self.calls = []

    def list_pending(self, *, status=None, limit=50):
        return [{
            "id": 11, "chat_key": "lk", "chat_name": "Line User",
            "peer_text": "hi", "draft_reply": "你好", "status": status or "pending", "ts": 100,
        }]

    def resolve_pending(self, pending_id, *, action, final_reply=None, by=""):
        self.calls.append((pending_id, action, final_reply, by))
        return {"id": pending_id, "status": "approved"}


def _client(with_service=True):
    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_drafts_routes(app, api_auth=api_auth)
    store = None
    if with_service:
        store = InboxStore(":memory:")
        app.state.draft_service = DraftService(inbox_store=store, line_services=[LineSvc()])
    return TestClient(app), store


def test_list_endpoint_returns_drafts():
    c, store = _client()
    resp = c.get("/api/drafts?status=pending&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["count"] == 1
    assert data["drafts"][0]["draft_id"] == "line_pending:line-a:11"
    store.close()


def test_stats_endpoint():
    c, store = _client()
    resp = c.get("/api/drafts/stats")
    assert resp.status_code == 200
    assert resp.json()["stats"]["total_pending"] == 1
    store.close()


def test_get_single_draft():
    c, store = _client()
    resp = c.get("/api/drafts/line_pending:line-a:11")
    assert resp.status_code == 200
    assert resp.json()["draft"]["chat_name"] == "Line User"
    store.close()


def test_get_missing_draft_404():
    c, store = _client()
    resp = c.get("/api/drafts/line_pending:line-a:999")
    assert resp.status_code == 404
    store.close()


def test_resolve_endpoint_dispatches():
    c, store = _client()
    resp = c.post("/api/drafts/line_pending:line-a:11/resolve", json={"action": "approve", "by": "op"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    store.close()


def test_resolve_invalid_action_400():
    c, store = _client()
    resp = c.post("/api/drafts/line_pending:line-a:11/resolve", json={"action": "boom"})
    assert resp.status_code == 400
    store.close()


def test_endpoints_503_without_service():
    c, _ = _client(with_service=False)
    assert c.get("/api/drafts").status_code == 503
    assert c.get("/api/drafts/stats").status_code == 503
