"""B2 风险 L0–L4 全档 + 强制审计闭环测试。

覆盖：
  - InboxStore: record_draft_audit / list_draft_audit
  - keyword_risk_level: 敏感词命中 / 无命中
  - DraftService.resolve_with_audit:
      L4 拦截（blocked audit）
      L4 force_override（审计 + 放行）
      L2 autosend（autosend 审计 + 转 approve）
      L3 正常审批写审计
      关键词强制升级 risk → L4 拦截
  - DraftService.risk_summary
  - DraftService.list_audit
  - drafts_routes: GET /api/drafts/risk-summary / GET /api/drafts/audit（权限） /
                   POST /resolve（L4 拦截）/ POST /force-override（主管专属）
"""

import time
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.drafts import (
    DraftService,
    keyword_risk_level,
    risk_to_autopilot,
    is_autosend_allowed,
    _max_risk,
)
from src.inbox.store import InboxStore
from src.web.routes.drafts_routes import register_drafts_routes


# ──────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────

def _make_draft_svc(store=None, adapter_ok=True):
    """构造 DraftService，可选含可控 source adapter。"""
    if adapter_ok:
        # 一个最小的 LINE adapter mock（list_drafts 返回一条草稿）
        line_svc = MagicMock()
        line_svc.account_id = "line-a"
        line_svc._merged_cfg = {"label": "LINE-A"}
        line_svc.list_pending.return_value = []
        line_svcs = [line_svc]
    else:
        line_svcs = []
    return DraftService(
        inbox_store=store,
        line_services=line_svcs,
        wa_services=[],
        messenger_service=None,
    )


def _make_routes_client(svc=None, role: str = ""):
    app = FastAPI()

    if role:
        @app.middleware("http")
        async def _inject(request: Request, call_next):
            request.scope["session"] = {
                "role": role, "user_id": "sup", "username": "sup",
            }
            return await call_next(request)

    def api_auth(request: Request):
        return True

    register_drafts_routes(app, api_auth=api_auth)
    if svc is not None:
        app.state.draft_service = svc
    return TestClient(app, raise_server_exceptions=True)


# ──────────────────────────────────────────────────────
# Store layer
# ──────────────────────────────────────────────────────

class TestDraftAuditStore:
    def test_record_and_list(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        ts = time.time()
        aid = store.record_draft_audit(
            "d-1", autopilot_level="L4", action="blocked",
            agent_id="agent-x", reason="test", risk_level="high",
            conversation_id="c-1",
        )
        assert aid >= 1

        rows = store.list_draft_audit(since_ts=ts - 10)
        assert len(rows) == 1
        r = rows[0]
        assert r["draft_id"] == "d-1"
        assert r["action"] == "blocked"
        assert r["risk_level"] == "high"

    def test_filter_by_draft_id(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.record_draft_audit("d-1", action="blocked")
        store.record_draft_audit("d-2", action="approved")
        rows = store.list_draft_audit(draft_id="d-1", since_ts=0)
        assert all(r["draft_id"] == "d-1" for r in rows)

    def test_filter_by_agent_id(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.record_draft_audit("d-1", action="approved", agent_id="alice")
        store.record_draft_audit("d-2", action="rejected", agent_id="bob")
        rows = store.list_draft_audit(agent_id="alice", since_ts=0)
        assert all(r["agent_id"] == "alice" for r in rows)

    def test_migration_idempotent(self, tmp_path):
        store1 = InboxStore(tmp_path / "inbox.db")
        store1.close()
        store2 = InboxStore(tmp_path / "inbox.db")
        store2.close()


# ──────────────────────────────────────────────────────
# keyword_risk_level
# ──────────────────────────────────────────────────────

class TestKeywordRiskLevel:
    @pytest.mark.parametrize("text,expected", [
        ("我想退款", "high"),
        ("请问可以refund吗", "high"),
        ("账号密码多少", "high"),
        ("验证码是多少", "high"),
        ("请问有优惠吗", "medium"),
        ("我要投诉", "medium"),
        ("折扣discount", "medium"),
        ("今天天气很好", None),
        ("普通咨询问题", None),
    ])
    def test_keyword_detection(self, text, expected):
        assert keyword_risk_level(text) == expected

    def test_high_overrides_medium(self):
        # text contains both high and medium patterns
        text = "退款 并且 投诉 你们"
        result = keyword_risk_level(text)
        assert result == "high"

    def test_empty_text(self):
        assert keyword_risk_level("") is None
        assert keyword_risk_level(None) is None


# ──────────────────────────────────────────────────────
# _max_risk helper
# ──────────────────────────────────────────────────────

class TestMaxRisk:
    def test_high_beats_medium(self):
        assert _max_risk("high", "medium") == "high"
        assert _max_risk("medium", "high") == "high"

    def test_medium_beats_low(self):
        assert _max_risk("medium", "low") == "medium"

    def test_none_b_returns_a(self):
        assert _max_risk("high", None) == "high"


# ──────────────────────────────────────────────────────
# risk_to_autopilot / is_autosend_allowed
# ──────────────────────────────────────────────────────

class TestRiskMappings:
    def test_L4_for_high(self):
        assert risk_to_autopilot("high", "auto_ai") == "L4"
        assert risk_to_autopilot("high", "review") == "L4"

    def test_L3_for_medium(self):
        assert risk_to_autopilot("medium", "auto_ai") == "L3"

    def test_L2_for_low_auto_ai(self):
        assert risk_to_autopilot("low", "auto_ai") == "L2"

    def test_L1_for_review(self):
        assert risk_to_autopilot("low", "review") == "L1"

    def test_L0_for_manual(self):
        assert risk_to_autopilot("low", "manual") == "L0"

    def test_autosend_only_L2(self):
        assert is_autosend_allowed("low", "auto_ai") is True
        assert is_autosend_allowed("medium", "auto_ai") is False
        assert is_autosend_allowed("high", "auto_ai") is False
        assert is_autosend_allowed("low", "review") is False


# ──────────────────────────────────────────────────────
# DraftService.resolve_with_audit (unit)
# ──────────────────────────────────────────────────────

class TestResolveWithAudit:
    def _svc_with_draft(self, tmp_path, draft: Dict[str, Any]):
        """构造含一条 inbox 草稿的 DraftService。"""
        store = InboxStore(tmp_path / "inbox.db")
        draft_id = store.upsert_draft({
            "source_kind": "inbox",
            "conversation_id": draft.get("conversation_id", "c-1"),
            "draft_text": draft.get("draft_text", "test reply"),
            "peer_text": draft.get("peer_text", ""),
            "risk_level": draft.get("risk_level", "low"),
            "autopilot_level": draft.get("autopilot_level", "L1"),
            "status": "pending",
        })
        svc = _make_draft_svc(store=store, adapter_ok=False)
        return svc, draft_id

    def test_L4_approve_blocked(self, tmp_path):
        svc, did = self._svc_with_draft(tmp_path, {
            "risk_level": "high", "autopilot_level": "L4",
        })
        result = svc.resolve_with_audit(did, "approve", by="agent-x")
        assert result["ok"] is False
        assert result.get("blocked") is True
        assert result.get("code") == 422

        audit = svc.list_audit(draft_id=did, since_ts=0)
        assert any(a["action"] == "blocked" for a in audit)

    def test_L4_approve_force_override_allowed(self, tmp_path):
        svc, did = self._svc_with_draft(tmp_path, {
            "risk_level": "high", "autopilot_level": "L4",
        })
        # mock resolve to return ok (adapter exists)
        svc._by_kind = {}  # no adapters; inbox source goes direct
        # Since this is inbox draft, resolve will try adapter — mock it
        orig_resolve = svc.resolve

        def _mock_resolve(draft_id, action, *, text="", by=""):
            return {"ok": True, "result": "mock sent"}
        svc.resolve = _mock_resolve

        result = svc.resolve_with_audit(did, "approve", by="sup", force_override=True)
        assert result["ok"] is True

        audit = svc.list_audit(draft_id=did, since_ts=0)
        assert any(a["action"] == "force_override" for a in audit)

    def test_L4_reject_always_allowed(self, tmp_path):
        """reject L4 草稿不应被拦截（拦截只针对发送动作）。"""
        svc, did = self._svc_with_draft(tmp_path, {
            "risk_level": "high", "autopilot_level": "L4",
        })
        orig_resolve = svc.resolve
        svc.resolve = lambda *a, **kw: {"ok": True}
        result = svc.resolve_with_audit(did, "reject", by="agent-x")
        assert result["ok"] is True

    def test_L2_autosend_writes_audit(self, tmp_path):
        svc, did = self._svc_with_draft(tmp_path, {
            "risk_level": "low", "autopilot_level": "L2",
        })
        svc.resolve = lambda *a, **kw: {"ok": True}
        result = svc.resolve_with_audit(did, "autosend", by="system")
        assert result["ok"] is True
        audit = svc.list_audit(draft_id=did, since_ts=0)
        assert any(a["action"] == "autosend" for a in audit)

    def test_keyword_upgrade_triggers_L4_block(self, tmp_path):
        """peer_text 含退款关键词 → risk 强制升级到 high → L4 拦截。"""
        svc, did = self._svc_with_draft(tmp_path, {
            "risk_level": "low", "autopilot_level": "L1",
            "peer_text": "我想退款",  # ← 触发关键词升级
        })
        result = svc.resolve_with_audit(did, "approve", by="agent-x")
        assert result["ok"] is False
        assert result.get("blocked") is True

    def test_L3_approve_writes_audit(self, tmp_path):
        svc, did = self._svc_with_draft(tmp_path, {
            "risk_level": "medium", "autopilot_level": "L3",
        })
        svc.resolve = lambda *a, **kw: {"ok": True}
        result = svc.resolve_with_audit(did, "approve", by="sup")
        assert result["ok"] is True
        audit = svc.list_audit(draft_id=did, since_ts=0)
        assert len(audit) >= 1

    def test_nonexistent_draft(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        svc = _make_draft_svc(store=store, adapter_ok=False)
        result = svc.resolve_with_audit("nonexistent:draft", "approve", by="x")
        assert result["ok"] is False
        assert result["code"] == 404


# ──────────────────────────────────────────────────────
# DraftService.risk_summary
# ──────────────────────────────────────────────────────

class TestRiskSummary:
    def test_empty_service(self):
        svc = _make_draft_svc(adapter_ok=False)
        summary = svc.risk_summary()
        assert summary["total_pending"] == 0
        assert "by_level" in summary

    def test_counts_inbox_drafts(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.upsert_draft({"source_kind": "inbox", "autopilot_level": "L4",
                            "risk_level": "high", "status": "pending"})
        store.upsert_draft({"source_kind": "inbox", "autopilot_level": "L1",
                            "risk_level": "low", "status": "pending",
                            "source_id": "different-id"})
        svc = _make_draft_svc(store=store, adapter_ok=False)
        summary = svc.risk_summary()
        assert summary["total_pending"] == 2
        assert summary["by_level"]["L4"] == 1
        assert summary["by_level"]["L1"] == 1


# ──────────────────────────────────────────────────────
# drafts_routes endpoints
# ──────────────────────────────────────────────────────

class TestDraftRoutesB2:
    def test_risk_summary_no_svc(self):
        c = _make_routes_client()
        r = c.get("/api/drafts/risk-summary")
        assert r.status_code == 503

    def test_risk_summary_ok(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        svc = _make_draft_svc(store=store)
        c = _make_routes_client(svc=svc)
        r = c.get("/api/drafts/risk-summary")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert "by_level" in r.json()

    def test_audit_requires_supervisor(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        svc = _make_draft_svc(store=store)
        c = _make_routes_client(svc=svc, role="agent")
        r = c.get("/api/drafts/audit")
        assert r.status_code == 403

    def test_audit_accessible_for_admin(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        svc = _make_draft_svc(store=store)
        c = _make_routes_client(svc=svc, role="admin")
        r = c.get("/api/drafts/audit")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "items" in body

    def test_resolve_L4_blocked(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        store.upsert_draft({
            "source_kind": "inbox", "risk_level": "high",
            "autopilot_level": "L4", "status": "pending",
        })
        svc = _make_draft_svc(store=store, adapter_ok=False)
        c = _make_routes_client(svc=svc)
        # find the draft_id
        drafts = svc.list_drafts(status="pending")
        assert drafts, "should have one draft"
        did = drafts[0]["draft_id"]
        r = c.post(f"/api/drafts/{did}/resolve", json={"action": "approve"})
        assert r.status_code == 422

    def test_force_override_requires_supervisor(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        svc = _make_draft_svc(store=store)
        c = _make_routes_client(svc=svc, role="agent")
        r = c.post("/api/drafts/fake-id/force-override", json={})
        assert r.status_code == 403

    def test_force_override_allowed_for_supervisor(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        did = store.upsert_draft({
            "source_kind": "inbox", "risk_level": "high",
            "autopilot_level": "L4", "status": "pending",
        })
        svc = _make_draft_svc(store=store, adapter_ok=False)
        # Patch resolve to succeed
        svc.resolve = lambda *a, **kw: {"ok": True, "result": "sent"}
        c = _make_routes_client(svc=svc, role="master")
        r = c.post(f"/api/drafts/{did}/force-override",
                   json={"action": "approve", "reason": "urgent exception"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_audit_no_svc_no_supervisor(self):
        """无 session → 非主管 → 403（supervisor check 先于 svc check）。"""
        c = _make_routes_client()
        r = c.get("/api/drafts/audit")
        assert r.status_code == 403


# ──────────────────────────────────────────────────────
# Route existence check (drafts_routes openapi)
# ──────────────────────────────────────────────────────

class TestDraftsRouteBaseline:
    def test_new_routes_in_openapi(self):
        svc = _make_draft_svc(adapter_ok=False)
        c = _make_routes_client(svc=svc)
        r = c.get("/openapi.json")
        paths = r.json().get("paths", {})
        assert "/api/drafts/risk-summary" in paths, "risk-summary missing"
        assert "/api/drafts/audit" in paths, "audit missing"
        assert "/api/drafts/{draft_id}/force-override" in paths, "force-override missing"
