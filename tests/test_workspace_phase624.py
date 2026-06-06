"""Phase 6-24：定向升级 / 指定主管指派测试。

覆盖：
  - InboxStore：set_escalation_assigned / count_assigned_escalations / list_my_escalations
  - /api/workspace/escalations/mine  (主管可见 / 普通坐席空列表)
  - /api/workspace/escalation/{id}/assign  (reassign；主管专属)
  - /api/workspace/me  端点基线（is_supervisor / role 字段）
  - 路由基线：两条新路由出现在 openapi.json
"""

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


# ──────────────────────────────────────────────────────
# 共用 fixtures
# ──────────────────────────────────────────────────────

class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page not used")


class _Cfg:
    config = {"workspace": {"claim_ttl_sec": 300, "presence_stale_sec": 60}}


def _make_client(inbox_store=None, role: str = ""):
    """构造带可选角色注入的 TestClient。"""
    app = FastAPI()

    if role:
        @app.middleware("http")
        async def _inject_session(request: Request, call_next):
            request.scope["session"] = {
                "role": role, "username": "sup", "user_id": "sup",
            }
            return await call_next(request)

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

    return TestClient(app, raise_server_exceptions=True)


# ──────────────────────────────────────────────────────
# Store 层单元测试
# ──────────────────────────────────────────────────────

class TestStoreAssigned:
    def test_set_escalation_assigned_basic(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        # 先落一条升级记录
        store.record_escalation("conv-1", reason="unclaimed", agent_id="", wait_sec=600)
        rows = store.list_escalations(since_ts=0)
        assert rows, "应有一条升级记录"
        esc_id = rows[0]["id"]

        ok = store.set_escalation_assigned(esc_id, "sup-alice")
        assert ok is True

        updated = store.list_escalations(since_ts=0)
        assert updated[0]["assigned_to"] == "sup-alice"

    def test_set_escalation_assigned_nonexistent(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        ok = store.set_escalation_assigned(99999, "sup-bob")
        assert ok is False

    def test_count_assigned_escalations(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        now = time.time()
        store.record_escalation("c-1", ts=now - 100)
        store.record_escalation("c-2", ts=now - 200)
        rows = store.list_escalations(since_ts=0)
        for r in rows:
            store.set_escalation_assigned(r["id"], "sup-alice")

        n = store.count_assigned_escalations("sup-alice", since_ts=now - 3600)
        assert n == 2

        n2 = store.count_assigned_escalations("sup-bob", since_ts=now - 3600)
        assert n2 == 0

    def test_list_my_escalations(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        now = time.time()
        store.record_escalation("c-A", reason="unclaimed", wait_sec=500, ts=now - 60)
        store.record_escalation("c-B", reason="holder_offline", wait_sec=700, ts=now - 120)
        rows = store.list_escalations(since_ts=0)
        for r in rows:
            store.set_escalation_assigned(r["id"], "sup-alice")

        mine = store.list_my_escalations("sup-alice", since_ts=now - 3600, limit=10)
        assert len(mine) == 2
        cids = {m["conversation_id"] for m in mine}
        assert cids == {"c-A", "c-B"}

        # assigned_to 字段正确
        assert all(m["assigned_to"] == "sup-alice" for m in mine)

    def test_list_my_escalations_empty_for_other(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.record_escalation("c-X", reason="unclaimed")
        rows = store.list_escalations(since_ts=0)
        store.set_escalation_assigned(rows[0]["id"], "sup-alice")

        mine = store.list_my_escalations("sup-bob", since_ts=0)
        assert mine == []

    def test_migration_idempotent(self, tmp_path):
        """重复初始化不报错（模拟旧库）。"""
        store1 = InboxStore(tmp_path / "inbox.db")
        store1.close()
        store2 = InboxStore(tmp_path / "inbox.db")
        store2.close()


# ──────────────────────────────────────────────────────
# /api/workspace/escalations/mine  端点测试
# ──────────────────────────────────────────────────────

class TestEscalationsMineEndpoint:
    def test_supervisor_gets_items(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        now = time.time()
        store.record_escalation("c-1", reason="unclaimed", wait_sec=800, ts=now - 30)
        rows = store.list_escalations(since_ts=0)
        store.set_escalation_assigned(rows[0]["id"], "sup")  # "sup" = session user_id

        c = _make_client(inbox_store=store, role="admin")
        r = c.get("/api/workspace/escalations/mine")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        # session user_id is "sup" (injected in middleware)
        assert body["total"] >= 1
        assert any(it["assigned_to"] == "sup" for it in body["items"])

    def test_non_supervisor_gets_empty(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.record_escalation("c-1", reason="unclaimed")
        c = _make_client(inbox_store=store, role="agent")
        r = c.get("/api/workspace/escalations/mine")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["items"] == []

    def test_no_store_returns_empty(self):
        c = _make_client(role="admin")
        r = c.get("/api/workspace/escalations/mine")
        assert r.status_code == 200
        assert r.json()["items"] == []


# ──────────────────────────────────────────────────────
# /api/workspace/escalation/{id}/assign  端点测试
# ──────────────────────────────────────────────────────

class TestEscalationAssignEndpoint:
    def test_supervisor_can_assign(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.record_escalation("c-1", reason="unclaimed", wait_sec=700)
        rows = store.list_escalations(since_ts=0)
        esc_id = rows[0]["id"]

        c = _make_client(inbox_store=store, role="master")
        r = c.post(
            f"/api/workspace/escalation/{esc_id}/assign",
            json={"agent_id": "sup-carol"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["assigned_to"] == "sup-carol"

        updated = store.list_escalations(since_ts=0)
        assert updated[0]["assigned_to"] == "sup-carol"

    def test_non_supervisor_forbidden(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.record_escalation("c-1", reason="unclaimed")
        rows = store.list_escalations(since_ts=0)
        esc_id = rows[0]["id"]

        c = _make_client(inbox_store=store, role="agent")
        r = c.post(
            f"/api/workspace/escalation/{esc_id}/assign",
            json={"agent_id": "sup-carol"},
        )
        assert r.status_code == 403

    def test_assign_nonexistent_esc_returns_404(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_client(inbox_store=store, role="admin")
        r = c.post(
            "/api/workspace/escalation/99999/assign",
            json={"agent_id": "sup-carol"},
        )
        assert r.status_code == 404

    def test_assign_empty_agent_id_returns_400(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.record_escalation("c-1", reason="unclaimed")
        rows = store.list_escalations(since_ts=0)
        esc_id = rows[0]["id"]

        c = _make_client(inbox_store=store, role="admin")
        r = c.post(
            f"/api/workspace/escalation/{esc_id}/assign",
            json={"agent_id": ""},
        )
        assert r.status_code == 400

    def test_no_store_503(self):
        c = _make_client(role="admin")
        r = c.post(
            "/api/workspace/escalation/1/assign",
            json={"agent_id": "sup-x"},
        )
        assert r.status_code == 503


# ──────────────────────────────────────────────────────
# /api/workspace/me  端点基线（Phase 6-23 延续）
# ──────────────────────────────────────────────────────

class TestWorkspaceMeEndpoint:
    def test_me_returns_is_supervisor_for_admin(self):
        c = _make_client(role="admin")
        r = c.get("/api/workspace/me")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["is_supervisor"] is True
        assert body["role"] == "admin"

    def test_me_returns_not_supervisor_for_agent(self):
        c = _make_client(role="agent")
        r = c.get("/api/workspace/me")
        assert r.status_code == 200
        body = r.json()
        assert body["is_supervisor"] is False

    def test_me_contains_agent_id(self):
        c = _make_client(role="master")
        r = c.get("/api/workspace/me")
        body = r.json()
        assert "agent_id" in body
        assert "display_name" in body


# ──────────────────────────────────────────────────────
# 路由基线：新端点出现在 openapi schema
# ──────────────────────────────────────────────────────

class TestRouteBaseline624:
    def test_new_routes_in_openapi(self):
        c = _make_client()
        r = c.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json().get("paths", {})
        assert "/api/workspace/escalations/mine" in paths, \
            "/api/workspace/escalations/mine 未注册"
        assert "/api/workspace/escalation/{esc_id}/assign" in paths, \
            "/api/workspace/escalation/{esc_id}/assign 未注册"
