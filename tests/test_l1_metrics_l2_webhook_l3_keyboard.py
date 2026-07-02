"""L1 全局指标 API + L2 WebhookNotifier + L3 快捷键面板 测试。

L2 WebhookNotifier:
  - 事件别名 → types/levels 正确展开
  - draft_created L4 → 匹配 L4_created / all
  - draft_created L2 → 不匹配 L4_created
  - sla_breach 事件 → 匹配 sla_breach / all
  - _build_message 构造正确
  - _fmt_dingtalk / _fmt_feishu / _fmt_wecom 格式正确
  - 速率限制：同 key 第二次调用被阻塞
  - status_snapshot 字段完整

L1 /api/workspace/metrics:
  - 非主管 → 403
  - JSON 格式包含关键字段
  - Prometheus 格式返回 text/plain + # HELP 行
  - 路由在 inventory 中

L3 快捷键:
  - dashboard 包含"一键 L2"按钮
  - draft_review 包含"? 键"快捷键面板 JS
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService
from src.inbox.template_seeds import SEED_TEMPLATES
from src.inbox.webhook_notifier import (
    WebhookNotifier,
    _build_message,
    _fmt_dingtalk,
    _fmt_feishu,
    _fmt_wecom,
    _fmt_json,
    _EVENT_ALIASES,
    _RateLimiter,
)
from src.web.routes.drafts_routes import register_metrics_route, register_drafts_routes


# ─────────────────────────────────────
# helpers
# ─────────────────────────────────────

def _make_store():
    s = InboxStore(":memory:")
    s.seed_templates(SEED_TEMPLATES)
    return s


def _make_svc(store):
    return DraftService(
        inbox_store=store, line_services=[], wa_services=[], messenger_service=None
    )


def _make_metrics_app(store=None, svc=None, role="admin"):
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": role, "user_id": "u1"}
        return await call_next(req)

    def api_auth(r: Request): return True

    register_drafts_routes(app, api_auth=api_auth)
    register_metrics_route(app, api_auth=api_auth)
    if store:
        app.state.inbox_store = store
    if svc:
        app.state.draft_service = svc
    return TestClient(app, raise_server_exceptions=True)


# ─────────────────────────────────────
# L2: 事件别名 + 格式化器
# ─────────────────────────────────────

class TestL2EventAliases:
    def test_l4_created_alias(self):
        rule = _EVENT_ALIASES["L4_created"]
        assert "draft_created" in rule["types"]
        assert "L4" in rule["levels"]

    def test_l2_not_in_l4_alias(self):
        rule = _EVENT_ALIASES["L4_created"]
        assert "L2" not in rule["levels"]

    def test_sla_breach_alias(self):
        rule = _EVENT_ALIASES["sla_breach"]
        assert "draft_sla_breach" in rule["types"]

    def test_all_alias_matches_none(self):
        """all 别名 types=None 表示匹配所有"""
        rule = _EVENT_ALIASES["all"]
        assert rule["types"] is None

    def test_reassigned_alias(self):
        rule = _EVENT_ALIASES["reassigned"]
        assert "draft_reassigned" in rule["types"]

    def test_backlog_summary_alias(self):
        rule = _EVENT_ALIASES["backlog_summary"]
        assert "draft_backlog_summary" in rule["types"]


class TestL2BuildMessage:
    def test_draft_created_l4(self):
        title, text = _build_message("draft_created", {
            "autopilot_level": "L4", "platform": "line",
            "risk_level": "high", "peer_text": "我要投诉",
        })
        assert "L4" in title
        assert "🚨" in title
        assert "line" in text

    def test_sla_breach(self):
        title, text = _build_message("draft_sla_breach", {
            "autopilot_level": "L3", "wait_min": 45, "sla_hours": 4,
            "peer_text_preview": "退款问题",
        })
        assert "SLA" in title
        assert "45" in text

    def test_draft_reassigned(self):
        title, text = _build_message("draft_reassigned", {
            "from_agent": "alice", "to_agent_name": "Bob", "reason": "agent_offline"
        })
        assert "再分配" in title
        assert "alice" in text

    def test_backlog_summary(self):
        title, text = _build_message("draft_backlog_summary", {
            "count": 7, "l3": 3, "l4": 4, "sla_hours": 4,
        })
        assert "7" in title
        assert "L3=3" in text and "L4=4" in text

    def test_unknown_event(self):
        title, text = _build_message("some_new_event", {"foo": "bar"})
        assert "some_new_event" in title


class TestL2Formatters:
    def test_dingtalk_format(self):
        body = _fmt_dingtalk("⚠️ 测试", "**内容**", {})
        d = json.loads(body)
        assert d["msgtype"] == "markdown"
        assert "markdown" in d
        assert d["markdown"]["title"] == "⚠️ 测试"

    def test_feishu_format(self):
        body = _fmt_feishu("标题", "内容", {})
        d = json.loads(body)
        assert d["msg_type"] == "text"
        assert "content" in d
        assert "标题" in d["content"]["text"]

    def test_wecom_format(self):
        body = _fmt_wecom("标题", "内容", {})
        d = json.loads(body)
        assert d["msgtype"] == "markdown"
        assert "标题" in d["markdown"]["content"]

    def test_json_format(self):
        body = _fmt_json("标题", "内容", {"foo": "bar"})
        d = json.loads(body)
        assert d["title"] == "标题"
        assert d["data"]["foo"] == "bar"


class TestL2RateLimiter:
    def test_first_call_allowed(self):
        rl = _RateLimiter(window_sec=60)
        assert rl.allow("key1") is True

    def test_second_call_blocked(self):
        rl = _RateLimiter(window_sec=60)
        rl.allow("key1")
        assert rl.allow("key1") is False

    def test_different_keys_independent(self):
        rl = _RateLimiter(window_sec=60)
        assert rl.allow("key1") is True
        assert rl.allow("key2") is True  # different key → allowed

    def test_expired_key_allowed_again(self):
        rl = _RateLimiter(window_sec=0.01)  # 10ms window
        rl.allow("key1")
        time.sleep(0.02)
        assert rl.allow("key1") is True


class TestL2WebhookNotifier:
    def test_status_snapshot_fields(self):
        n = WebhookNotifier(config=[])
        snap = n.status_snapshot()
        assert "running" in snap
        assert "webhooks" in snap
        assert "total_sent" in snap
        assert "total_errors" in snap

    def test_matchers_built_correctly(self):
        cfg = [{"url": "http://x", "format": "dingtalk", "events": ["L4_created"]}]
        n = WebhookNotifier(config=cfg)
        assert len(n._matchers) == 1
        m = n._matchers[0]
        assert m["fmt"] == "dingtalk"
        assert "L4" in m["levels"]

    def test_matcher_all_events(self):
        cfg = [{"url": "http://x", "format": "json", "events": ["all"]}]
        n = WebhookNotifier(config=cfg)
        m = n._matchers[0]
        assert m["types"] is None  # 全部事件

    def test_l4_event_matches_l4_webhook(self):
        """L4 draft_created 事件应匹配 L4_created webhook"""
        cfg = [{"url": "http://x", "format": "json", "events": ["L4_created"]}]
        n = WebhookNotifier(config=cfg)
        evt = {"type": "draft_created", "data": {"autopilot_level": "L4"}}
        matched = []
        for m in n._matchers:
            etype = evt["type"]
            level = evt["data"]["autopilot_level"]
            if m["types"] is not None and etype not in m["types"]: continue
            if m["levels"] is not None and level not in m["levels"]: continue
            matched.append(m)
        assert len(matched) == 1

    def test_l2_event_does_not_match_l4_webhook(self):
        """L2 draft_created 不应匹配 L4_created webhook"""
        cfg = [{"url": "http://x", "format": "json", "events": ["L4_created"]}]
        n = WebhookNotifier(config=cfg)
        evt = {"type": "draft_created", "data": {"autopilot_level": "L2"}}
        matched = []
        for m in n._matchers:
            etype = evt["type"]
            level = evt["data"]["autopilot_level"]
            if m["types"] is not None and etype not in m["types"]: continue
            if m["levels"] is not None and level not in m["levels"]: continue
            matched.append(m)
        assert len(matched) == 0

    def test_sla_breach_event_matches(self):
        cfg = [{"url": "http://x", "format": "json", "events": ["sla_breach"]}]
        n = WebhookNotifier(config=cfg)
        evt = {"type": "draft_sla_breach", "data": {"autopilot_level": "L3", "wait_min": 30}}
        matched = []
        for m in n._matchers:
            etype = evt["type"]
            level = evt["data"].get("autopilot_level", "")
            if m["types"] is not None and etype not in m["types"]: continue
            if m["levels"] is not None and level not in m["levels"]: continue
            matched.append(m)
        assert len(matched) == 1

    def test_empty_config_no_matchers(self):
        n = WebhookNotifier(config=[])
        assert n._matchers == []

    def test_stop_signals_event(self):
        n = WebhookNotifier(config=[])
        n.stop()
        assert n._stop_evt.is_set()


# ─────────────────────────────────────
# L1: Metrics API
# ─────────────────────────────────────

class TestL1MetricsAPI:
    def test_non_supervisor_403(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_metrics_app(store, svc, role="agent")
        r = client.get("/api/workspace/metrics")
        assert r.status_code == 403

    def test_supervisor_json_response(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_metrics_app(store, svc, role="admin")
        r = client.get("/api/workspace/metrics")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "autosend" in d
        assert "sla_watcher" in d
        assert "webhook" in d

    def test_prometheus_format_content_type(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_metrics_app(store, svc, role="master")
        r = client.get("/api/workspace/metrics?format=prometheus")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")

    def test_prometheus_format_has_help(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_metrics_app(store, svc, role="admin")
        r = client.get("/api/workspace/metrics?format=prometheus")
        assert r.status_code == 200
        text = r.text
        assert "# HELP" in text
        assert "# TYPE" in text
        assert "ws_autosend_running" in text
        assert "ws_sla_breach_events_total" in text
        assert "ws_drafts_pending_total" in text

    def test_prometheus_format_has_webhook_metrics(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_metrics_app(store, svc, role="admin")
        r = client.get("/api/workspace/metrics?format=prometheus")
        assert "ws_webhook_total_sent" in r.text

    def test_auto_claim_default_when_no_worker(self):
        """无 AutoClaimWorker 接线时，auto_claim 字段降级为 running:False。"""
        store = _make_store()
        svc = _make_svc(store)
        client = _make_metrics_app(store, svc, role="admin")
        r = client.get("/api/workspace/metrics")
        assert r.status_code == 200
        d = r.json()
        assert "auto_claim" in d
        assert d["auto_claim"].get("running") is False

    def test_auto_claim_snapshot_surfaces(self):
        """接线 AutoClaimWorker 后，status_snapshot 字段透出到 JSON。"""
        store = _make_store()
        svc = _make_svc(store)
        client = _make_metrics_app(store, svc, role="admin")

        class _FakeACW:
            def status_snapshot(self):
                return {"running": True, "total_claimed": 7,
                        "total_lang_matched": 4}

        client.app.state.auto_claim_worker = _FakeACW()
        r = client.get("/api/workspace/metrics")
        d = r.json()
        assert d["auto_claim"]["running"] is True
        assert d["auto_claim"]["total_claimed"] == 7
        assert d["auto_claim"]["total_lang_matched"] == 4

    def test_prometheus_has_auto_claim_gauges(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_metrics_app(store, svc, role="admin")

        class _FakeACW:
            def status_snapshot(self):
                return {"running": True, "total_claimed": 3,
                        "total_lang_matched": 2}

        client.app.state.auto_claim_worker = _FakeACW()
        r = client.get("/api/workspace/metrics?format=prometheus")
        text = r.text
        assert "ws_auto_claim_running 1" in text
        assert "ws_auto_claim_total 3" in text
        assert "ws_auto_claim_lang_matched_total 2" in text

    def test_json_format_has_drafts(self):
        store = _make_store()
        svc = _make_svc(store)
        # 插入一条草稿
        store.upsert_draft({
            "source_kind": "inbox", "source_id": "m-1",
            "conversation_id": "m-1", "platform": "line",
            "account_id": "acc", "chat_key": "u1",
            "autopilot_level": "L3", "risk_level": "medium",
            "draft_text": "test", "peer_text": "test",
            "status": "pending",
        })
        client = _make_metrics_app(store, svc, role="admin")
        r = client.get("/api/workspace/metrics")
        assert r.status_code == 200
        d = r.json()
        assert d.get("drafts", {}).get("pending_total", -1) >= 0

    def test_inventory_includes_metrics(self):
        with open("tests/test_admin_route_inventory.py", encoding="utf-8") as f:
            content = f.read()
        assert "/api/workspace/metrics\tGET" in content


# ─────────────────────────────────────
# L3: 快捷键面板 + 快速操作
# ─────────────────────────────────────

class TestL3KeyboardAndQuickActions:
    def test_draft_review_has_keyboard_help(self):
        """draft_review.html 包含 ? 键快捷键面板 JS"""
        with open("src/web/templates/draft_review.html", encoding="utf-8") as f:
            html = f.read()
        assert "dr-help-overlay" in html
        assert "快捷键" in html
        assert "e.key==='?'" in html

    def test_draft_review_keyboard_shortcuts_listed(self):
        with open("src/web/templates/draft_review.html", encoding="utf-8") as f:
            html = f.read()
        # 基本快捷键应在面板中
        assert "Approve" in html or "批准" in html
        assert "Reject" in html or "拒绝" in html

    def test_dashboard_has_quick_l2_button(self):
        """dashboard.html 包含一键 L2 按钮"""
        with open("src/web/templates/workspace_dashboard.html", encoding="utf-8") as f:
            html = f.read()
        assert "db-quick-approve" in html
        assert "bulk-autosend" in html

    def test_dashboard_has_metrics_widget(self):
        """dashboard.html 包含 L1 系统指标 widget"""
        with open("src/web/templates/workspace_dashboard.html", encoding="utf-8") as f:
            html = f.read()
        assert "db-metrics-sec" in html
        assert "loadMetrics" in html
        assert "format=prometheus" in html

    def test_dashboard_metrics_widget_has_auto_claim(self):
        """L1 指标卡须呈现 auto_claim 自动派单（状态 + 量 + 语言命中）。"""
        with open("src/web/templates/workspace_dashboard.html", encoding="utf-8") as f:
            html = f.read()
        assert "d.auto_claim" in html
        assert "自动派单" in html
        assert "total_lang_matched" in html
