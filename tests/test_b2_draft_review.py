"""B2 续：草稿审批工作台页面路由 + bulk-autosend 测试。

覆盖：
  - POST /api/drafts/bulk-autosend（L2 批量自动发）
  - GET  /workspace/drafts（页面路由存在）
  - GET  /workspace/draft-audit（主管可访问 / 非主管重定向）
  - openapi 基线：bulk-autosend 出现在 API 路由中
  - register_drafts_page_routes 注册正确
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService
from src.web.routes.drafts_routes import (
    register_drafts_routes,
    register_drafts_page_routes,
)


# ──────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────

class _Templates:
    """最小 Jinja2Templates stub。"""
    def TemplateResponse(self, request, name, context):
        return HTMLResponse(content=f"<html><body>{name}</body></html>")


def _make_app(store=None, role: str = "", include_pages: bool = False):
    """构造含 API 路由（+可选页面路由）的 TestClient。"""
    app = FastAPI()

    if role:
        @app.middleware("http")
        async def _inject(request: Request, call_next):
            request.scope["session"] = {
                "role": role, "user_id": "u1", "username": "u1",
            }
            return await call_next(request)

    def api_auth(request: Request) -> None:  # noqa: ARG001
        return None

    def page_auth(request: Request) -> None:  # noqa: ARG001
        return None

    register_drafts_routes(app, api_auth=api_auth)

    if include_pages:
        register_drafts_page_routes(
            app,
            page_auth=page_auth,
            templates=_Templates(),
        )

    if store is not None:
        app.state.draft_service = DraftService(
            inbox_store=store,
            line_services=[],
            wa_services=[],
            messenger_service=None,
        )

    return TestClient(app, raise_server_exceptions=True)


# ──────────────────────────────────────────────────────
# bulk-autosend
# ──────────────────────────────────────────────────────

class TestBulkAutosend:
    def test_no_svc_503(self):
        c = _make_app()
        r = c.post("/api/drafts/bulk-autosend", json={})
        assert r.status_code == 503

    def test_no_l2_drafts_returns_zero(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        # Only L4 draft, should not be autosent
        store.upsert_draft({
            "source_kind": "inbox", "autopilot_level": "L4",
            "risk_level": "high", "status": "pending",
        })
        c = _make_app(store=store)
        r = c.post("/api/drafts/bulk-autosend", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["sent"] == 0

    def test_l2_draft_gets_autosent(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.upsert_draft({
            "source_kind": "inbox", "autopilot_level": "L2",
            "risk_level": "low", "status": "pending",
            "draft_text": "Hello, your order is confirmed.",
        })
        svc = DraftService(
            inbox_store=store, line_services=[], wa_services=[], messenger_service=None,
        )
        # Patch resolve so it returns ok without platform call
        svc.resolve = lambda did, action, **kw: {"ok": True}

        app = FastAPI()

        def _api_auth(request: Request) -> None: return None  # noqa: E704
        register_drafts_routes(app, api_auth=_api_auth)
        app.state.draft_service = svc

        c = TestClient(app, raise_server_exceptions=True)
        r = c.post("/api/drafts/bulk-autosend", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["sent"] >= 1
        assert body["errors"] == 0

    def test_mixed_levels_only_sends_l2(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.upsert_draft({
            "source_kind": "inbox", "autopilot_level": "L2",
            "risk_level": "low", "status": "pending",
            "source_id": "s1", "draft_text": "auto reply",
        })
        store.upsert_draft({
            "source_kind": "inbox", "autopilot_level": "L3",
            "risk_level": "medium", "status": "pending",
            "source_id": "s2",
        })
        svc = DraftService(
            inbox_store=store, line_services=[], wa_services=[], messenger_service=None,
        )
        svc.resolve = lambda did, action, **kw: {"ok": True}

        app = FastAPI()

        def _api_auth(request: Request) -> None: return None  # noqa: E704
        register_drafts_routes(app, api_auth=_api_auth)
        app.state.draft_service = svc

        c = TestClient(app, raise_server_exceptions=True)
        r = c.post("/api/drafts/bulk-autosend", json={})
        body = r.json()
        # Only 1 L2 draft sent, L3 not touched
        assert body["sent"] == 1


# ──────────────────────────────────────────────────────
# Page routes
# ──────────────────────────────────────────────────────

class TestDraftPageRoutes:
    def test_workspace_drafts_returns_200(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_app(store=store, include_pages=True)
        r = c.get("/workspace/drafts", follow_redirects=False)
        assert r.status_code == 200
        assert b"draft_review.html" in r.content

    def test_workspace_draft_audit_supervisor(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_app(store=store, role="admin", include_pages=True)
        r = c.get("/workspace/draft-audit", follow_redirects=False)
        assert r.status_code == 200
        assert b"draft_audit_page.html" in r.content

    def test_workspace_draft_audit_non_supervisor_redirect(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_app(store=store, role="agent", include_pages=True)
        r = c.get("/workspace/draft-audit", follow_redirects=False)
        # Non-supervisor → 302 redirect to /workspace/drafts
        assert r.status_code == 302
        assert "/workspace/drafts" in r.headers.get("location", "")

    def test_page_routes_without_store_still_render(self):
        """页面路由不依赖 draft_service 可用性（渲染模板，API 调用由前端发起）。"""
        c = _make_app(include_pages=True)
        r = c.get("/workspace/drafts", follow_redirects=False)
        assert r.status_code == 200


# ──────────────────────────────────────────────────────
# openapi 基线
# ──────────────────────────────────────────────────────

class TestBulkAutosendRouteBaseline:
    def test_bulk_autosend_in_openapi(self):
        c = _make_app()
        r = c.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json().get("paths", {})
        assert "/api/drafts/bulk-autosend" in paths, "bulk-autosend 未注册"

    def test_page_routes_in_openapi(self):
        c = _make_app(include_pages=True)
        r = c.get("/openapi.json")
        paths = r.json().get("paths", {})
        assert "/workspace/drafts" in paths
        assert "/workspace/draft-audit" in paths
