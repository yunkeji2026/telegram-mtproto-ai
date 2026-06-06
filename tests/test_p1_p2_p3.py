"""
P1 (CRM Sync) + P2 (KB Archive) + P3 (Multi-tenant Workspace) 测试套件
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import FastAPI


# ──────────────────────────── helpers ────────────────────────────

def _fresh_store(tmp_path):
    from src.inbox.store import InboxStore
    return InboxStore(str(tmp_path / f"test_{uuid.uuid4().hex[:6]}.db"))


def _make_api_auth(role="master"):
    async def _auth(request: Request):
        return None
    return _auth


def _session_middleware(role="master", user_id="tester", workspace_id="default"):
    class Mw(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.scope["session"] = {
                "role": role,
                "user_id": user_id,
                "workspace_id": workspace_id,
            }
            return await call_next(request)
    return Mw


# ══════════════════════════════════════════════════════════════════
# P2: KB Archive Tests
# ══════════════════════════════════════════════════════════════════

def _make_kb_archive_app(tmp_path, role="master"):
    from fastapi import FastAPI
    from src.web.routes.drafts_routes import register_kb_archive_route

    app = FastAPI()
    app.add_middleware(_session_middleware(role=role))

    store = _fresh_store(tmp_path)
    # 插入一条草稿供测试
    store.upsert_draft({
        "draft_id": "inbox:p2_draft",
        "conversation_id": "p2_conv",
        "peer_text": "客户问快递在哪",
        "draft_text": "您好，包裹预计明天到达。",
        "source_kind": "inbox",
    })
    store.update_conv_meta("p2_conv", intent="物流查询", platform="telegram")

    kb = MagicMock()
    kb.add_entry = MagicMock(return_value="kb_001")

    app.state.inbox_store = store
    app.state.kb_store = kb

    register_kb_archive_route(app, api_auth=_make_api_auth(role))
    return app, kb, store


class TestP2KbArchive:
    def test_kb_archive_success(self, tmp_path):
        app, kb, _ = _make_kb_archive_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=True)
        r = client.post("/api/workspace/kb-archive", json={
            "draft_id": "inbox:p2_draft",
            "title": "物流查询标准回复",
            "category": "物流",
        })
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["entry_id"] == "kb_001"
        assert d["title"] == "物流查询标准回复"
        assert d["draft_id"] == "inbox:p2_draft"

    def test_kb_archive_calls_add_entry_with_correct_fields(self, tmp_path):
        app, kb, _ = _make_kb_archive_app(tmp_path)
        client = TestClient(app)
        client.post("/api/workspace/kb-archive", json={
            "draft_id": "inbox:p2_draft",
            "title": "测试条目",
            "triggers": ["快递", "物流"],
        })
        kb.add_entry.assert_called_once()
        call_data = kb.add_entry.call_args[0][0]
        assert call_data["title"] == "测试条目"
        assert "快递" in call_data["triggers"]
        assert "物流查询标准回复" != call_data["title"]  # sanity

    def test_kb_archive_uses_intent_as_trigger_fallback(self, tmp_path):
        app, kb, store = _make_kb_archive_app(tmp_path)
        client = TestClient(app)
        client.post("/api/workspace/kb-archive", json={
            "draft_id": "inbox:p2_draft",
            "title": "回复标题",
            # 不提供 triggers，应回退到 conv_meta.last_intent = "物流查询"
        })
        call_data = kb.add_entry.call_args[0][0]
        assert "物流查询" in call_data["triggers"]

    def test_kb_archive_example_reply_from_draft_text(self, tmp_path):
        app, kb, _ = _make_kb_archive_app(tmp_path)
        client = TestClient(app)
        client.post("/api/workspace/kb-archive", json={
            "draft_id": "inbox:p2_draft",
            "title": "x",
        })
        call_data = kb.add_entry.call_args[0][0]
        assert "明天到达" in call_data["example_reply_zh"]

    def test_kb_archive_403_for_agent(self, tmp_path):
        app, _, _ = _make_kb_archive_app(tmp_path, role="agent")
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/workspace/kb-archive", json={"draft_id": "x", "title": "y"})
        assert r.status_code == 403

    def test_kb_archive_400_missing_title(self, tmp_path):
        app, _, _ = _make_kb_archive_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/workspace/kb-archive", json={"draft_id": "inbox:p2_draft"})
        assert r.status_code == 400

    def test_kb_archive_records_audit(self, tmp_path):
        app, kb, store = _make_kb_archive_app(tmp_path)
        client = TestClient(app)
        client.post("/api/workspace/kb-archive", json={
            "draft_id": "inbox:p2_draft",
            "title": "测试",
        })
        # 审计记录应被写入
        logs = store.list_draft_audit(draft_id="inbox:p2_draft")
        assert any(row.get("action") == "kb_archived" for row in logs)

    def test_kb_archive_503_no_kb_store(self, tmp_path):
        from fastapi import FastAPI
        from src.web.routes.drafts_routes import register_kb_archive_route
        app = FastAPI()
        app.add_middleware(_session_middleware(role="master"))
        app.state.inbox_store = _fresh_store(tmp_path)
        # 不设置 kb_store
        register_kb_archive_route(app, api_auth=_make_api_auth())
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/workspace/kb-archive", json={"draft_id": "x", "title": "y"})
        assert r.status_code == 503


# ══════════════════════════════════════════════════════════════════
# P1: CRM Sync — draft_resolved event 发布测试
# ══════════════════════════════════════════════════════════════════

class TestP1CrmSync:
    def test_draft_resolved_event_published_on_approve(self, tmp_path):
        from src.inbox.drafts import DraftService

        store = _fresh_store(tmp_path)
        store.upsert_draft({
            "draft_id": "inbox:p1d",
            "conversation_id": "p1c",
            "peer_text": "hi",
            "draft_text": "hello",
            "source_kind": "inbox",
            "autopilot_level": "review",
        })
        store.update_conv_meta("p1c", intent="问候", emotion="neutral", platform="telegram")

        published_events = []

        class FakeEB:
            def publish(self, evt_type, data):
                published_events.append((evt_type, data))

        svc = DraftService(inbox_store=store)

        with patch("src.integrations.shared.event_bus.get_event_bus", return_value=FakeEB()):
            result = svc.resolve_with_audit("inbox:p1d", action="approve", by="sup1", text="hello")

        assert result.get("ok") is True, result
        resolved_events = [(t, d) for t, d in published_events if t == "draft_resolved"]
        assert len(resolved_events) >= 1
        evt_data = resolved_events[0][1]
        assert evt_data["draft_id"] == "inbox:p1d"
        assert evt_data["conversation_id"] == "p1c"
        assert evt_data["agent_id"] == "sup1"
        assert evt_data["action"] == "approve"
        assert evt_data["intent"] == "问候"
        assert evt_data["platform"] == "telegram"

    def test_draft_resolved_event_not_published_on_reject(self, tmp_path):
        from src.inbox.drafts import DraftService

        store = _fresh_store(tmp_path)
        store.upsert_draft({
            "draft_id": "inbox:p1r",
            "conversation_id": "p1cr",
            "peer_text": "hi",
            "draft_text": "hello",
            "source_kind": "inbox",
            "autopilot_level": "review",
        })

        published_events = []

        class FakeEB:
            def publish(self, evt_type, data):
                published_events.append((evt_type, data))

        svc = DraftService(inbox_store=store)

        with patch("src.integrations.shared.event_bus.get_event_bus", return_value=FakeEB()):
            result = svc.resolve_with_audit("inbox:p1r", action="reject", by="sup1", text="")

        assert result.get("ok") is True
        resolved_events = [(t, d) for t, d in published_events if t == "draft_resolved"]
        assert len(resolved_events) == 0

    def test_draft_resolved_event_published_on_autosend(self, tmp_path):
        from src.inbox.drafts import DraftService

        store = _fresh_store(tmp_path)
        store.upsert_draft({
            "draft_id": "inbox:p1as",
            "conversation_id": "p1cas",
            "peer_text": "hi",
            "draft_text": "auto reply",
            "source_kind": "inbox",
            "autopilot_level": "auto_ai",
        })

        published_events = []

        class FakeEB:
            def publish(self, evt_type, data):
                published_events.append((evt_type, data))

        svc = DraftService(inbox_store=store)

        with patch("src.integrations.shared.event_bus.get_event_bus", return_value=FakeEB()):
            result = svc.resolve_with_audit("inbox:p1as", action="autosend", by="bot", text="auto reply")

        assert result.get("ok") is True
        resolved_events = [(t, d) for t, d in published_events if t == "draft_resolved"]
        assert len(resolved_events) >= 1

    def test_webhook_notifier_alias_crm_sync(self):
        from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message

        assert "crm_sync" in _EVENT_ALIASES
        aliases = _EVENT_ALIASES["crm_sync"]
        assert "draft_resolved" in aliases["types"]

    def test_webhook_notifier_draft_resolved_message_format(self):
        from src.inbox.webhook_notifier import _build_message

        data = {
            "draft_id": "inbox:x",
            "conversation_id": "conv:y",
            "agent_id": "agent007",
            "action": "approve",
            "platform": "telegram",
            "intent": "下单",
            "emotion": "happy",
            "csat": 4.5,
            "risk_level": "low",
            "text_preview": "您好，订单已确认。",
        }
        title, text = _build_message("draft_resolved", data)
        assert "telegram" in title
        assert "agent007" in text
        assert "下单" in text
        assert "4.5" in text

    def test_crm_sync_csat_none_when_not_scored(self, tmp_path):
        from src.inbox.drafts import DraftService

        store = _fresh_store(tmp_path)
        store.upsert_draft({
            "draft_id": "inbox:p1ns",
            "conversation_id": "p1cns",
            "peer_text": "hi",
            "draft_text": "hello",
            "source_kind": "inbox",
            "autopilot_level": "review",
        })
        # 不写 conv_meta → csat_score 不存在

        published_events = []

        class FakeEB:
            def publish(self, evt_type, data):
                published_events.append((evt_type, data))

        svc = DraftService(inbox_store=store)

        with patch("src.integrations.shared.event_bus.get_event_bus", return_value=FakeEB()):
            svc.resolve_with_audit("inbox:p1ns", action="approve", by="sup", text="hello")

        resolved = [d for t, d in published_events if t == "draft_resolved"]
        assert len(resolved) >= 1
        # csat 应为 None（未评分）
        assert resolved[0].get("csat") is None


# ══════════════════════════════════════════════════════════════════
# P3: Multi-tenant Workspace Tests
# ══════════════════════════════════════════════════════════════════

class TestP3Workspace:
    def test_upsert_and_list_workspaces(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.upsert_workspace("ws_a", display_name="团队A")
        store.upsert_workspace("ws_b", display_name="团队B", config={"max_agents": 10})
        ws_list = store.list_workspaces()
        ids = [w["workspace_id"] for w in ws_list]
        assert "ws_a" in ids
        assert "ws_b" in ids

    def test_workspace_config_persisted(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.upsert_workspace("ws_cfg", config={"timezone": "Asia/Shanghai"})
        ws_list = store.list_workspaces()
        ws = next(w for w in ws_list if w["workspace_id"] == "ws_cfg")
        assert ws["config"]["timezone"] == "Asia/Shanghai"

    def test_workspace_update_overwrites(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.upsert_workspace("ws_up", display_name="旧名")
        store.upsert_workspace("ws_up", display_name="新名")
        ws_list = store.list_workspaces()
        ws = next(w for w in ws_list if w["workspace_id"] == "ws_up")
        assert ws["display_name"] == "新名"

    def test_get_workspace_stats_empty(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.upsert_workspace("ws_empty")
        stats = store.get_workspace_stats("ws_empty")
        assert stats["workspace_id"] == "ws_empty"
        assert stats["conversation_count"] == 0
        assert stats["audit_count"] == 0
        assert stats["avg_csat"] is None

    def test_get_workspace_stats_with_data(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.update_conv_meta("conv1", workspace_id="ws_x", platform="telegram")
        store.update_conv_meta("conv2", workspace_id="ws_x", platform="line")
        # 写一条不同 workspace 的数据，应不被统计
        store.update_conv_meta("conv3", workspace_id="ws_other", platform="telegram")

        stats = store.get_workspace_stats("ws_x")
        assert stats["conversation_count"] == 2

    def test_conversation_meta_workspace_id_default(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.update_conv_meta("conv_def", platform="telegram")
        # 默认 workspace_id 应为 'default'
        with store._conn as c:
            row = c.execute(
                "SELECT workspace_id FROM conversation_meta WHERE conversation_id=?",
                ("conv_def",)
            ).fetchone()
        assert row[0] == "default"

    def test_conversation_meta_workspace_id_custom(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.update_conv_meta("conv_ws", platform="telegram", workspace_id="ws_custom")
        with store._conn as c:
            row = c.execute(
                "SELECT workspace_id FROM conversation_meta WHERE conversation_id=?",
                ("conv_ws",)
            ).fetchone()
        assert row[0] == "ws_custom"


def _make_workspace_api_app(tmp_path, role="master"):
    from fastapi import FastAPI
    from src.web.routes.drafts_routes import register_workspace_route

    app = FastAPI()
    app.add_middleware(_session_middleware(role=role))

    store = _fresh_store(tmp_path)
    store.upsert_workspace("ws_existing", display_name="已存在")
    app.state.inbox_store = store

    register_workspace_route(app, api_auth=_make_api_auth(role))
    return app, store


class TestP3WorkspaceAPI:
    def test_list_workspaces_supervisor(self, tmp_path):
        app, _ = _make_workspace_api_app(tmp_path, role="master")
        client = TestClient(app)
        r = client.get("/api/workspace/workspaces")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        ids = [w["workspace_id"] for w in d["workspaces"]]
        assert "ws_existing" in ids

    def test_list_workspaces_403_agent(self, tmp_path):
        app, _ = _make_workspace_api_app(tmp_path, role="agent")
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/workspace/workspaces")
        assert r.status_code == 403

    def test_create_workspace_via_api(self, tmp_path):
        app, store = _make_workspace_api_app(tmp_path)
        client = TestClient(app)
        r = client.post("/api/workspace/workspaces", json={
            "workspace_id": "ws_new_api",
            "display_name": "API 创建的团队",
            "config": {"region": "CN"},
        })
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["workspace_id"] == "ws_new_api"
        # 验证确实写入了
        ws_list = store.list_workspaces()
        ids = [w["workspace_id"] for w in ws_list]
        assert "ws_new_api" in ids

    def test_create_workspace_400_empty_id(self, tmp_path):
        app, _ = _make_workspace_api_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/workspace/workspaces", json={"workspace_id": ""})
        assert r.status_code == 400

    def test_create_workspace_403_agent(self, tmp_path):
        app, _ = _make_workspace_api_app(tmp_path, role="agent")
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/workspace/workspaces", json={"workspace_id": "ws_x"})
        assert r.status_code == 403

    def test_workspace_stats_endpoint(self, tmp_path):
        app, store = _make_workspace_api_app(tmp_path)
        client = TestClient(app)
        r = client.get("/api/workspace/workspaces/ws_existing/stats")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["workspace_id"] == "ws_existing"
        assert "conversation_count" in d

    def test_workspace_stats_includes_correct_conv_count(self, tmp_path):
        app, store = _make_workspace_api_app(tmp_path)
        # 给 ws_existing 写两条会话
        store.update_conv_meta("cA", workspace_id="ws_existing", platform="tg")
        store.update_conv_meta("cB", workspace_id="ws_existing", platform="tg")
        client = TestClient(app)
        r = client.get("/api/workspace/workspaces/ws_existing/stats")
        assert r.status_code == 200
        d = r.json()
        assert d["conversation_count"] == 2

    def test_workspace_list_shows_stats_field(self, tmp_path):
        app, store = _make_workspace_api_app(tmp_path)
        client = TestClient(app)
        r = client.get("/api/workspace/workspaces")
        d = r.json()
        for ws in d["workspaces"]:
            assert "stats" in ws
            assert "conversation_count" in ws["stats"]

    def test_workspace_current_field_from_session(self, tmp_path):
        from fastapi import FastAPI
        from src.web.routes.drafts_routes import register_workspace_route

        app = FastAPI()
        app.add_middleware(_session_middleware(role="master", workspace_id="ws_existing"))
        store = _fresh_store(tmp_path)
        store.upsert_workspace("ws_existing")
        app.state.inbox_store = store
        register_workspace_route(app, api_auth=_make_api_auth())
        client = TestClient(app)
        r = client.get("/api/workspace/workspaces")
        assert r.json()["current"] == "ws_existing"
