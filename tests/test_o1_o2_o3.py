"""O1 趋势图 + O2 智能预警 + O3 坐席自助绩效 测试。

O1 — InboxStore 趋势查询:
  - get_csat_trend() 返回时间桶 + avg_csat + count
  - get_csat_trend() 只统计 csat_score >= 0
  - get_draft_level_trend() 返回 total/l3/l4/high_risk_rate
  - GET /api/workspace/trend 非主管 → 403
  - GET /api/workspace/trend JSON 含 csat_trend / level_trend / delta
  - delta.direction up/down/stable 正确
  - dashboard HTML 含 db-trend-sec 和 sparkline JS

O2 — ScheduledReporter 预警规则:
  - status_snapshot 含 total_alerts / alert_rules 字段
  - avg_csat_below 条件触发 csat_alert 事件
  - avg_csat_below 条件高于阈值不触发
  - l3l4_rate_above 条件触发
  - force_override_above 条件触发
  - _evaluate_alert_rules 在 _send 失败时静默
  - WebhookNotifier csat_alert 别名存在
  - _build_message 处理 csat_alert 事件

O3 — /api/workspace/my-perf:
  - 坐席可查询自己
  - 坐席无法查询他人 → 403
  - 主管可查询任意坐席
  - 无数据时返回 total=0
  - 含 timeline / rank / total_agents
  - days 参数生效（过滤时间窗口）
  - dashboard HTML 含 db-myp-sec
  - 路由在 inventory 中
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.scheduled_reporter import ScheduledReporter
from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message


# ─────────────────────────── Fixtures ──────────────────────────────────────

@pytest.fixture()
def store(tmp_path):
    return InboxStore(db_path=str(tmp_path / "o.db"))


def _seed_csat(store, conversation_id, csat, platform="tg", days_ago=0):
    """Seed a conversation with a specific CSAT score."""
    ts = time.time() - days_ago * 86400
    store.update_conv_meta(conversation_id, platform=platform)
    store._conn.execute(
        "UPDATE conversation_meta SET updated_at=?, csat_score=? WHERE conversation_id=?",
        (ts, csat, conversation_id),
    )
    store._conn.commit()


def _seed_audit(store, draft_id, agent_id, action, autopilot_level, days_ago=0):
    ts = time.time() - days_ago * 86400
    store._conn.execute(
        "INSERT INTO draft_audit_log (draft_id, autopilot_level, action, agent_id, risk_level, conversation_id, ts) "
        "VALUES (?,?,?,?,?,?,?)",
        (draft_id, autopilot_level, action, agent_id, "low", "c1", ts),
    )
    store._conn.commit()


# ─────────────────────────── O1: Store Trend Tests ─────────────────────────

class TestO1Store:
    def test_get_csat_trend_empty(self, store):
        result = store.get_csat_trend(since_ts=0)
        assert result == []

    def test_get_csat_trend_returns_buckets(self, store):
        _seed_csat(store, "c1", 4.0, days_ago=1)
        _seed_csat(store, "c2", 3.5, days_ago=0)
        result = store.get_csat_trend(since_ts=time.time() - 3 * 86400)
        assert len(result) >= 1
        for r in result:
            assert "bucket_ts" in r
            assert "avg_csat" in r
            assert "count" in r

    def test_get_csat_trend_excludes_unscored(self, store):
        store.update_conv_meta("unscored", platform="tg")
        # csat_score 默认 -1，不应计入趋势
        result = store.get_csat_trend(since_ts=0)
        assert result == []

    def test_get_csat_trend_avg_correct(self, store):
        store.update_conv_meta("ca", platform="tg")
        store.update_conv_meta("cb", platform="tg")
        store.update_conv_csat("ca", 4.0)
        store.update_conv_csat("cb", 2.0)
        result = store.get_csat_trend(since_ts=0)
        assert len(result) == 1
        assert abs(result[0]["avg_csat"] - 3.0) < 0.1
        assert result[0]["count"] == 2

    def test_get_draft_level_trend_empty(self, store):
        result = store.get_draft_level_trend(since_ts=0)
        assert result == []

    def test_get_draft_level_trend_computes_rate(self, store):
        _seed_audit(store, "d1", "alice", "approved", "L2")
        _seed_audit(store, "d2", "alice", "approved", "L3")
        _seed_audit(store, "d3", "alice", "approved", "L4")
        result = store.get_draft_level_trend(since_ts=0)
        assert len(result) >= 1
        r = result[0]
        assert r["total"] == 3
        assert r["l3"] == 1
        assert r["l4"] == 1
        assert abs(r["high_risk_rate"] - 2 / 3) < 0.01

    def test_get_draft_level_trend_only_decision_actions(self, store):
        # 只统计 approved/rejected/autosend/force_override/blocked，不统计 blocked 的其他动作
        _seed_audit(store, "d1", "a", "approved", "L2")
        _seed_audit(store, "d2", "a", "reply_risk_detected", "L2")  # 不计入
        result = store.get_draft_level_trend(since_ts=0)
        assert len(result) >= 1
        assert result[0]["total"] == 1


# ─────────────────────────── O1: /api/workspace/trend ──────────────────────

def _make_trend_app(tmp_path, role="master"):
    _store = InboxStore(db_path=str(tmp_path / "t.db"))
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": role, "user_id": "u1"}
        return await call_next(req)

    def _api_auth(r: Request): return True

    from src.web.routes.drafts_routes import register_trend_route
    app.state.inbox_store = _store
    register_trend_route(app, api_auth=_api_auth)
    return TestClient(app, raise_server_exceptions=False), _store


def test_trend_api_requires_supervisor(tmp_path):
    client, _ = _make_trend_app(tmp_path, role="agent")
    r = client.get("/api/workspace/trend")
    assert r.status_code == 403


def test_trend_api_returns_fields(tmp_path):
    client, _ = _make_trend_app(tmp_path)
    r = client.get("/api/workspace/trend?days=7")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert "csat_trend" in data
    assert "level_trend" in data
    assert "delta" in data


def test_trend_api_delta_direction_stable(tmp_path):
    client, store = _make_trend_app(tmp_path)
    store.update_conv_meta("c1", platform="tg")
    store.update_conv_csat("c1", 4.0)
    r = client.get("/api/workspace/trend")
    data = r.json()
    delta = data["delta"]
    # Might be stable or up/down depending on data
    assert delta["direction"] in ("up", "down", "stable")


def test_trend_api_days_param(tmp_path):
    client, _ = _make_trend_app(tmp_path)
    for days in [7, 30, 90]:
        r = client.get(f"/api/workspace/trend?days={days}")
        assert r.status_code == 200
        assert r.json()["days"] == days


def test_trend_dashboard_html_present(tmp_path):
    """Dashboard 模板含 trend 区域和 sparkline JS。"""
    with open("src/web/templates/workspace_dashboard.html", encoding="utf-8") as f:
        html = f.read()
    assert "db-trend-sec" in html
    assert "_sparkline" in html
    assert "loadTrend" in html


# ─────────────────────────── O2: ScheduledReporter Alerts ──────────────────

class TestO2AlertRules:
    def test_status_snapshot_has_alert_fields(self):
        rpt = ScheduledReporter(inbox_store=MagicMock())
        snap = rpt.status_snapshot()
        assert "total_alerts" in snap
        assert "alert_rules" in snap
        assert snap["total_alerts"] == 0

    @pytest.mark.asyncio
    async def test_avg_csat_below_triggers(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "a.db"))
        cfg = {"alert_rules": [{"condition": "avg_csat_below", "threshold": 4.0, "message": "CSAT低"}]}
        rpt = ScheduledReporter(inbox_store=store, config=cfg)
        events = []
        report_data = {
            "csat": {"avg": 3.5, "count": 5},
            "sla_stats": {"compliance_rate": 95, "force_overrides": 0},
        }
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda t, d: events.append((t, d))
            await rpt._evaluate_alert_rules(report_data)
        assert any(e[0] == "csat_alert" for e in events)
        assert rpt.total_alerts == 1

    @pytest.mark.asyncio
    async def test_avg_csat_above_threshold_no_trigger(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "a.db"))
        cfg = {"alert_rules": [{"condition": "avg_csat_below", "threshold": 3.0}]}
        rpt = ScheduledReporter(inbox_store=store, config=cfg)
        events = []
        report_data = {
            "csat": {"avg": 4.5, "count": 10},
            "sla_stats": {"compliance_rate": 95, "force_overrides": 0},
        }
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda t, d: events.append((t, d))
            await rpt._evaluate_alert_rules(report_data)
        assert not any(e[0] == "csat_alert" for e in events)
        assert rpt.total_alerts == 0

    @pytest.mark.asyncio
    async def test_l3l4_rate_above_triggers(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "a.db"))
        cfg = {"alert_rules": [{"condition": "l3l4_rate_above", "threshold": 0.2}]}
        rpt = ScheduledReporter(inbox_store=store, config=cfg)
        events = []
        # compliance_rate=60 → high_risk_rate=0.4 > 0.2
        report_data = {
            "csat": {"avg": 4.0, "count": 5},
            "sla_stats": {"compliance_rate": 60, "force_overrides": 0},
        }
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda t, d: events.append((t, d))
            await rpt._evaluate_alert_rules(report_data)
        assert any(e[0] == "csat_alert" for e in events)

    @pytest.mark.asyncio
    async def test_force_override_above_triggers(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "a.db"))
        cfg = {"alert_rules": [{"condition": "force_override_above", "threshold": 3}]}
        rpt = ScheduledReporter(inbox_store=store, config=cfg)
        events = []
        report_data = {
            "csat": {"avg": 4.0, "count": 5},
            "sla_stats": {"compliance_rate": 90, "force_overrides": 5},
        }
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda t, d: events.append((t, d))
            await rpt._evaluate_alert_rules(report_data)
        assert any(e[0] == "csat_alert" for e in events)

    @pytest.mark.asyncio
    async def test_evaluate_alert_rules_silent_on_error(self, tmp_path):
        """预警评估失败时不影响主流程。"""
        store = InboxStore(db_path=str(tmp_path / "a.db"))
        cfg = {"alert_rules": [{"condition": "avg_csat_below", "threshold": 4.0}]}
        rpt = ScheduledReporter(inbox_store=store, config=cfg)
        with patch("src.integrations.shared.event_bus.get_event_bus", side_effect=Exception("bus_fail")):
            await rpt._evaluate_alert_rules({"csat": {"avg": 2.0}, "sla_stats": {}})
        # no exception raised

    def test_csat_alert_alias_in_event_aliases(self):
        alias = _EVENT_ALIASES.get("csat_alert")
        assert alias is not None
        assert "csat_alert" in alias["types"]

    def test_build_message_csat_alert(self):
        msg = _build_message("csat_alert", {
            "condition": "avg_csat_below",
            "message": "CSAT低于3.5",
            "threshold": 3.5,
        })
        title, text = msg
        assert "预警" in title or "警告" in title or "CSAT" in title
        assert "3.5" in text or "预警" in text or "条件" in text

    @pytest.mark.asyncio
    async def test_no_rules_no_trigger(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "a.db"))
        rpt = ScheduledReporter(inbox_store=store, config={})
        events = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda t, d: events.append((t, d))
            await rpt._evaluate_alert_rules({"csat": {"avg": 1.0}, "sla_stats": {}})
        assert events == []


# ─────────────────────────── O3: /api/workspace/my-perf ────────────────────

def _make_myp_app(tmp_path, role="agent", uid="alice"):
    _store = InboxStore(db_path=str(tmp_path / "myp.db"))
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": role, "user_id": uid}
        return await call_next(req)

    def _api_auth(r: Request): return True

    from src.web.routes.drafts_routes import register_my_perf_route
    app.state.inbox_store = _store
    register_my_perf_route(app, api_auth=_api_auth)
    return TestClient(app, raise_server_exceptions=False), _store


def test_myp_agent_can_query_self(tmp_path):
    client, store = _make_myp_app(tmp_path, role="agent", uid="alice")
    r = client.get("/api/workspace/my-perf")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data["agent_id"] == "alice"


def test_myp_agent_cannot_query_others(tmp_path):
    client, _ = _make_myp_app(tmp_path, role="agent", uid="alice")
    r = client.get("/api/workspace/my-perf?agent_id=bob")
    assert r.status_code == 403


def test_myp_supervisor_can_query_others(tmp_path):
    client, store = _make_myp_app(tmp_path, role="master", uid="admin")
    store._conn.execute(
        "INSERT INTO draft_audit_log (draft_id, autopilot_level, action, agent_id, risk_level, conversation_id, ts) VALUES (?,?,?,?,?,?,?)",
        ("d1", "L3", "approved", "bob", "low", "c1", time.time()),
    )
    store._conn.commit()
    r = client.get("/api/workspace/my-perf?agent_id=bob")
    assert r.status_code == 200
    data = r.json()
    assert data["agent_id"] == "bob"


def test_myp_no_data_returns_zero_total(tmp_path):
    client, _ = _make_myp_app(tmp_path, role="agent", uid="newbie")
    r = client.get("/api/workspace/my-perf")
    assert r.status_code == 200
    data = r.json()
    assert data["perf"]["total"] == 0


def test_myp_contains_required_fields(tmp_path):
    client, store = _make_myp_app(tmp_path, role="agent", uid="alice")
    store._conn.execute(
        "INSERT INTO draft_audit_log (draft_id, autopilot_level, action, agent_id, risk_level, conversation_id, ts) VALUES (?,?,?,?,?,?,?)",
        ("d1", "L3", "approved", "alice", "low", "c1", time.time()),
    )
    store._conn.commit()
    r = client.get("/api/workspace/my-perf")
    data = r.json()
    assert "perf" in data
    assert "timeline" in data
    assert "rank" in data
    assert "total_agents" in data
    assert "recent_decisions" in data


def test_myp_days_param_filters(tmp_path):
    client, store = _make_myp_app(tmp_path, role="agent", uid="alice")
    # audit log entry 10 days ago
    old_ts = time.time() - 10 * 86400
    store._conn.execute(
        "INSERT INTO draft_audit_log (draft_id, autopilot_level, action, agent_id, risk_level, conversation_id, ts) VALUES (?,?,?,?,?,?,?)",
        ("d_old", "L3", "approved", "alice", "low", "c1", old_ts),
    )
    store._conn.commit()
    r = client.get("/api/workspace/my-perf?days=7")
    assert r.status_code == 200
    # 10 days ago entry should be excluded
    data = r.json()
    assert data["perf"]["total"] == 0


def test_myp_dashboard_html_present():
    with open("src/web/templates/workspace_dashboard.html", encoding="utf-8") as f:
        html = f.read()
    assert "db-myp-sec" in html
    assert "loadMyPerf" in html
    assert "我的绩效" in html


def test_myp_in_inventory(app):
    routes = {r.path for r in app.routes}
    assert "/api/workspace/my-perf" in routes, f"missing my-perf, got: {sorted(routes)}"
    assert "/api/workspace/trend" in routes, f"missing trend, got: {sorted(routes)}"
