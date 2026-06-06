"""C2 Copilot + C3 事件驱动 + D1 SLA 告警测试。

覆盖：
  C3 事件驱动 AutosendWorker
    - InboxStore.register_l2_callback 注册成功
    - upsert_draft L2 草稿触发回调
    - upsert_draft 非 L2 草稿不触发回调
    - notify_new_l2 无 loop 时安全降级
    - status_snapshot 包含 event_triggers 字段

  C2 AI Copilot API
    - GET /api/workspace/copilot 返回 intent/emotion/risk_level/next_step
    - 无文本时也能安全返回
    - kb_store 未挂载时 kb_matches=[]
    - quick_analyze 单元测试（规则层正确）

  D1 SLA 告警
    - GET /api/drafts/sla-overdue 主管 200 + 正确 count
    - GET /api/drafts/sla-overdue 非主管 403
    - 未超时的 L3 草稿不在 overdue 列表
    - 已超时的 L4 草稿在 overdue 列表
    - GET /api/drafts/risk-summary 包含 sla_overdue 字段（主管）
    - GET /api/drafts/risk-summary 非主管时 sla_overdue=-1

  openapi 基线
    - copilot / sla-overdue / autosend-status 全注册
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.store import InboxStore
from src.ai.chat_assistant_service import quick_analyze
from src.web.routes.drafts_routes import register_drafts_routes


# ──────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────

def _api_auth(request: Request) -> None: return None


def _make_app(store=None, role: str = "admin"):
    app = FastAPI()
    if role:
        @app.middleware("http")
        async def _inj(request: Request, call_next):
            request.scope["session"] = {"role": role, "user_id": "u1", "username": "u1"}
            return await call_next(request)

    register_drafts_routes(app, api_auth=_api_auth)

    if store is not None:
        app.state.inbox_store = store

        svc = MagicMock()
        svc.list_drafts.return_value = []
        svc.risk_summary.return_value = {"by_level": {}, "total_pending": 0}
        app.state.draft_service = svc

    return TestClient(app, raise_server_exceptions=True)


# ──────────────────────────────────────────────────────
# C3：InboxStore callback + AutosendWorker.notify_new_l2
# ──────────────────────────────────────────────────────

class TestC3EventDriven:
    def test_register_callback(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        fired = []
        store.register_l2_callback(lambda: fired.append(1))
        assert len(store._l2_callbacks) == 1

    def test_l2_upsert_fires_callback(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        fired = []
        store.register_l2_callback(lambda: fired.append(1))
        store.upsert_draft({
            "source_kind": "inbox",
            "autopilot_level": "L2",
            "risk_level": "low",
            "status": "pending",
        })
        assert fired == [1]

    def test_non_l2_upsert_no_callback(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        fired = []
        store.register_l2_callback(lambda: fired.append(1))
        store.upsert_draft({
            "source_kind": "inbox",
            "autopilot_level": "L3",
            "risk_level": "medium",
            "status": "pending",
        })
        assert fired == []

    def test_l4_upsert_no_callback(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        fired = []
        store.register_l2_callback(lambda: fired.append(1))
        store.upsert_draft({
            "source_kind": "inbox",
            "autopilot_level": "L4",
            "risk_level": "high",
        })
        assert fired == []

    def test_multiple_callbacks_all_fire(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        fired = []
        store.register_l2_callback(lambda: fired.append("a"))
        store.register_l2_callback(lambda: fired.append("b"))
        store.upsert_draft({"source_kind": "inbox", "autopilot_level": "L2"})
        assert set(fired) == {"a", "b"}

    def test_callback_exception_does_not_break_upsert(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        def _bad(): raise RuntimeError("callback error")
        store.register_l2_callback(_bad)
        # Should not raise; the draft should still be upserted
        draft_id = store.upsert_draft({"source_kind": "inbox", "autopilot_level": "L2"})
        assert draft_id.startswith("inbox:")

    def test_notify_new_l2_no_loop_safe(self):
        """notify_new_l2 在 loop 未初始化时安全降级（不抛异常）。"""
        svc = MagicMock()
        svc.list_drafts.return_value = []
        w = AutosendWorker(draft_service=svc)
        # _loop is None before run() is called
        w.notify_new_l2()  # Should not raise

    def test_status_snapshot_has_event_triggers(self):
        svc = MagicMock()
        svc.list_drafts.return_value = []
        w = AutosendWorker(draft_service=svc)
        snap = w.status_snapshot()
        assert "event_triggers" in snap
        assert snap["event_triggers"] == 0


# ──────────────────────────────────────────────────────
# C2：quick_analyze 单元 + Copilot API
# ──────────────────────────────────────────────────────

class TestQuickAnalyze:
    def test_returns_required_fields(self):
        result = quick_analyze("我想退款，订单有问题")
        assert "intent" in result
        assert "emotion" in result
        assert "risk_level" in result
        assert "risk_reasons" in result
        assert "next_step" in result
        assert "language" in result

    def test_empty_text_safe(self):
        result = quick_analyze("")
        assert isinstance(result["intent"], str)
        assert isinstance(result["risk_reasons"], list)

    def test_high_risk_text(self):
        result = quick_analyze("我要退款，银行卡密码是什么")
        assert result["risk_level"] == "high"
        assert len(result["risk_reasons"]) > 0

    def test_normal_text_low_risk(self):
        result = quick_analyze("你好，请问有什么可以帮到我的吗？")
        assert result["risk_level"] in {"low", "medium"}


class TestCopilotAPI:
    def test_returns_analysis(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_app(store=store, role="agent")
        r = c.get("/api/workspace/copilot?text=你好我有问题想咨询")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "intent" in body
        assert "emotion" in body
        assert "risk_level" in body
        assert "kb_matches" in body
        assert isinstance(body["kb_matches"], list)

    def test_empty_text_safe(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_app(store=store, role="agent")
        r = c.get("/api/workspace/copilot?text=")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_no_kb_store_returns_empty_matches(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_app(store=store, role="agent")
        r = c.get("/api/workspace/copilot?text=退款问题")
        body = r.json()
        assert body["kb_matches"] == []

    def test_with_draft_id_and_conv_id(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_app(store=store, role="agent")
        r = c.get("/api/workspace/copilot?text=帮我查单&draft_id=d1&conversation_id=c1")
        body = r.json()
        assert body["draft_id"] == "d1"
        assert body["conversation_id"] == "c1"


# ──────────────────────────────────────────────────────
# D1：SLA 过期告警 API
# ──────────────────────────────────────────────────────

class TestSLAOverdueAPI:
    def _make_svc_with_drafts(self, drafts):
        svc = MagicMock()
        svc.list_drafts.return_value = drafts
        svc.risk_summary.return_value = {
            "by_level": {}, "total_pending": len(drafts),
        }
        return svc

    def test_403_non_supervisor(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        app = FastAPI()
        @app.middleware("http")
        async def _inj(request: Request, call_next):
            request.scope["session"] = {"role": "agent"}
            return await call_next(request)
        register_drafts_routes(app, api_auth=_api_auth)
        app.state.draft_service = self._make_svc_with_drafts([])
        c = TestClient(app, raise_server_exceptions=True)
        r = c.get("/api/drafts/sla-overdue")
        assert r.status_code == 403

    def test_no_overdue_returns_zero(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        now = time.time()
        # L3 but only 1 minute old (within SLA)
        drafts = [{"autopilot_level": "L3", "status": "pending", "created_ts": now - 60}]
        app = FastAPI()
        @app.middleware("http")
        async def _inj(request: Request, call_next):
            request.scope["session"] = {"role": "admin"}
            return await call_next(request)
        register_drafts_routes(app, api_auth=_api_auth)
        app.state.draft_service = self._make_svc_with_drafts(drafts)
        c = TestClient(app, raise_server_exceptions=True)
        r = c.get("/api/drafts/sla-overdue?hours=4")
        body = r.json()
        assert body["ok"] is True
        assert body["count"] == 0

    def test_overdue_l4_counted(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        now = time.time()
        # L4, 5 hours old → overdue
        drafts = [
            {"autopilot_level": "L4", "status": "pending", "created_ts": now - 5*3600},
            {"autopilot_level": "L3", "status": "pending", "created_ts": now - 5*3600},
            {"autopilot_level": "L2", "status": "pending", "created_ts": now - 5*3600},  # L2 not counted
        ]
        app = FastAPI()
        @app.middleware("http")
        async def _inj(request: Request, call_next):
            request.scope["session"] = {"role": "master"}
            return await call_next(request)
        register_drafts_routes(app, api_auth=_api_auth)
        app.state.draft_service = self._make_svc_with_drafts(drafts)
        c = TestClient(app, raise_server_exceptions=True)
        r = c.get("/api/drafts/sla-overdue?hours=4")
        body = r.json()
        assert body["count"] == 2  # only L3 + L4

    def test_risk_summary_contains_sla_overdue_for_supervisor(self, tmp_path):
        now = time.time()
        drafts = [
            {"autopilot_level": "L4", "status": "pending", "created_ts": now - 5*3600},
        ]
        app = FastAPI()
        @app.middleware("http")
        async def _inj(request: Request, call_next):
            request.scope["session"] = {"role": "admin"}
            return await call_next(request)
        register_drafts_routes(app, api_auth=_api_auth)
        svc = MagicMock()
        svc.list_drafts.return_value = drafts
        svc.risk_summary.return_value = {"by_level": {"L4": 1}, "total_pending": 1}
        app.state.draft_service = svc
        c = TestClient(app, raise_server_exceptions=True)
        r = c.get("/api/drafts/risk-summary")
        body = r.json()
        assert body["ok"] is True
        assert "sla_overdue" in body
        assert body["sla_overdue"] == 1

    def test_risk_summary_sla_overdue_minus1_for_agent(self, tmp_path):
        app = FastAPI()
        @app.middleware("http")
        async def _inj(request: Request, call_next):
            request.scope["session"] = {"role": "agent"}
            return await call_next(request)
        register_drafts_routes(app, api_auth=_api_auth)
        svc = MagicMock()
        svc.list_drafts.return_value = []
        svc.risk_summary.return_value = {"by_level": {}, "total_pending": 0}
        app.state.draft_service = svc
        c = TestClient(app, raise_server_exceptions=True)
        r = c.get("/api/drafts/risk-summary")
        body = r.json()
        assert body["sla_overdue"] == -1


# ──────────────────────────────────────────────────────
# openapi 基线
# ──────────────────────────────────────────────────────

class TestOpenAPIBaseline:
    def test_copilot_registered(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_app(store=store)
        paths = c.get("/openapi.json").json().get("paths", {})
        assert "/api/workspace/copilot" in paths

    def test_sla_overdue_registered(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_app(store=store)
        paths = c.get("/openapi.json").json().get("paths", {})
        assert "/api/drafts/sla-overdue" in paths

    def test_autosend_status_registered(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_app(store=store)
        paths = c.get("/openapi.json").json().get("paths", {})
        assert "/api/drafts/autosend-status" in paths
