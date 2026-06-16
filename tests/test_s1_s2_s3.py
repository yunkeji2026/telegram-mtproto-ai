"""
tests/test_s1_s2_s3.py
S1 A/B测试 + S2 异常检测 + S3 全链路追踪 测试套件

覆盖：
  S1：assign_variant / compute_ab_significance / ABTestingStore CRUD + 自动停止
  S2：_median / _mad / AnomalyDetector.detect_one / build_anomaly_alert_payload
  S3：new_trace_id / TraceTimeline / update_conv_meta trace 传播 / upsert_draft trace 传播
  API：/api/workspace/ab-tests / /api/workspace/anomaly / /api/workspace/trace
  Admin：路由清单（baseline 更新）
"""
import asyncio
import json
import sys
import time
import types
import uuid
from typing import Any, Dict
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# ── 确保 src 在路径上 ─────────────────────────────────────────
sys.path.insert(0, ".")

# ═══════════════════════════════════════════════════════════════
#  fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_store(tmp_path):
    """临时 InboxStore（每测独立文件）。"""
    from src.inbox.store import InboxStore
    db_path = str(tmp_path / "test.db")
    store = InboxStore(db_path)
    yield store
    store._conn.close()


def _make_api_auth():
    async def _auth(request: Request): return None
    return _auth


def _session_mw(role="master", agent_id="agent_test"):
    class Mw(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.scope["session"] = {"role": role, "agent_id": agent_id}
            return await call_next(request)
    return Mw


def _make_app(store, role="master"):
    """创建最小化 FastAPI 测试 app。"""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    app.add_middleware(_session_mw(role=role))
    app.state.inbox_store = store
    app.state.cfg = {
        "report": {
            "anomaly_detection": {
                "enabled": True,
                "sensitivity": 2.0,
                "baseline_days": 7,
                "metrics": ["csat_avg", "l3l4_rate"],
            }
        }
    }

    _auth = _make_api_auth()

    from src.web.routes.drafts_routes import (
        register_ab_testing_route,
        register_anomaly_route,
        register_trace_route,
    )
    register_ab_testing_route(app, api_auth=_auth)
    register_anomaly_route(app, api_auth=_auth)
    register_trace_route(app, api_auth=_auth)

    class _Client:
        def __init__(self):
            self._tc = TestClient(app, raise_server_exceptions=True)

        def get(self, url, **kw):
            return self._tc.get(url, **kw)

        def post(self, url, **kw):
            return self._tc.post(url, **kw)

    return _Client()


# ═══════════════════════════════════════════════════════════════
#  S1 — A/B 测试框架
# ═══════════════════════════════════════════════════════════════

class TestAssignVariant:
    def test_deterministic(self):
        from src.inbox.ab_testing import assign_variant
        v1 = assign_variant("conv_abc", "ab_001")
        v2 = assign_variant("conv_abc", "ab_001")
        assert v1 == v2, "相同输入应输出相同变体"

    def test_two_variants_only(self):
        from src.inbox.ab_testing import assign_variant
        variants = {assign_variant(f"conv_{i}", "test_x") for i in range(100)}
        assert variants <= {"A", "B"}

    def test_roughly_balanced(self):
        """大样本下，A/B 各约 50%。"""
        from src.inbox.ab_testing import assign_variant
        counts = {"A": 0, "B": 0}
        for i in range(1000):
            v = assign_variant(f"conv_{i:05d}", "balance_test")
            counts[v] += 1
        # 允许 ±10% 偏差
        assert 400 <= counts["A"] <= 600, f"A={counts['A']}，期望约 500"


class TestComputeABSignificance:
    def test_insufficient_sample(self):
        from src.inbox.ab_testing import compute_ab_significance
        r = compute_ab_significance(1, 1, 1, 1)
        assert r["significant"] is False
        assert "样本量不足" in r["note"]

    def test_no_difference(self):
        from src.inbox.ab_testing import compute_ab_significance
        r = compute_ab_significance(50, 40, 50, 40)
        assert r["significant"] is False
        assert r["z_score"] == 0.0

    def test_significant_winner_b(self):
        """B 满意率显著高于 A。"""
        from src.inbox.ab_testing import compute_ab_significance
        # A: 50%, B: 90%，大样本
        r = compute_ab_significance(200, 100, 200, 180)
        assert r["significant"] is True
        assert r["winner"] == "B"

    def test_significant_winner_a(self):
        from src.inbox.ab_testing import compute_ab_significance
        r = compute_ab_significance(200, 180, 200, 100)
        assert r["significant"] is True
        assert r["winner"] == "A"

    def test_not_significant_small_gap(self):
        from src.inbox.ab_testing import compute_ab_significance
        # A: 80%, B: 82%，差异很小
        r = compute_ab_significance(50, 40, 50, 41)
        assert r["significant"] is False


class TestABTestingStore:
    def test_create_and_list(self, tmp_store):
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(tmp_store)
        tid = ab.create_test(
            name="测试A",
            intent_filter="complaint",
            template_a_id="tpl_001",
            template_b_id="tpl_002",
            min_sample=10,
        )
        assert tid.startswith("ab_")
        tests = ab.list_tests()
        assert any(t["id"] == tid for t in tests)

    def test_get_test(self, tmp_store):
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(tmp_store)
        tid = ab.create_test(
            name="X", intent_filter="*",
            template_a_id="a", template_b_id="b",
        )
        t = ab.get_test(tid)
        assert t["name"] == "X"

    def test_record_assignment(self, tmp_store):
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(tmp_store)
        tid = ab.create_test(name="Y", intent_filter="q", template_a_id="a", template_b_id="b")
        ab.record_assignment(test_id=tid, conversation_id="conv_01", variant="A")
        ab.record_assignment(test_id=tid, conversation_id="conv_01", variant="A")  # 幂等
        with tmp_store._lock:
            count = tmp_store._conn.execute(
                "SELECT COUNT(*) FROM ab_assignments WHERE test_id=?", (tid,)
            ).fetchone()[0]
        assert count == 1  # 不重复插入

    def test_record_outcome_updates_stats(self, tmp_store):
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(tmp_store)
        tid = ab.create_test(name="Z", intent_filter="*", template_a_id="a", template_b_id="b")
        ab.record_assignment(test_id=tid, conversation_id="conv_A1", variant="A")
        ab.record_assignment(test_id=tid, conversation_id="conv_B1", variant="B")
        ab.record_outcome(conversation_id="conv_A1", csat_score=4.5)
        ab.record_outcome(conversation_id="conv_B1", csat_score=2.0)
        t = ab.get_test(tid)
        assert int(t["n_a"]) == 1
        assert int(t["n_b"]) == 1
        assert int(t["sat_a"]) == 1  # 4.5 >= 4
        assert int(t["sat_b"]) == 0  # 2.0 < 4

    def test_auto_stop_on_significance(self, tmp_store):
        """足够样本且显著差异时，测试自动停止。"""
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(tmp_store)
        tid = ab.create_test(name="Auto", intent_filter="*", template_a_id="a", template_b_id="b", min_sample=10)
        # A: 50% 满意, B: 100% 满意 → 显著
        for i in range(20):
            conv = f"convA_{i}"
            ab.record_assignment(test_id=tid, conversation_id=conv, variant="A")
            ab.record_outcome(conversation_id=conv, csat_score=2.0)
        for i in range(20):
            conv = f"convB_{i}"
            ab.record_assignment(test_id=tid, conversation_id=conv, variant="B")
            ab.record_outcome(conversation_id=conv, csat_score=5.0)
        t = ab.get_test(tid)
        assert t["status"] in ("winner_a", "winner_b", "stopped", "no_diff"), f"status={t['status']}"

    def test_stop_test_manual(self, tmp_store):
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(tmp_store)
        tid = ab.create_test(name="Manual", intent_filter="*", template_a_id="a", template_b_id="b")
        ok = ab.stop_test(tid)
        assert ok is True
        t = ab.get_test(tid)
        assert t["status"] == "stopped"

    def test_get_results_returns_significance(self, tmp_store):
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(tmp_store)
        tid = ab.create_test(name="R", intent_filter="*", template_a_id="a", template_b_id="b")
        results = ab.get_results(tid)
        assert "significance" in results
        assert "n_a" in results


# ═══════════════════════════════════════════════════════════════
#  S2 — 异常检测
# ═══════════════════════════════════════════════════════════════

class TestAnomalyUtils:
    def test_median_odd(self):
        from src.inbox.anomaly import _median
        assert _median([1, 3, 5]) == 3

    def test_median_even(self):
        from src.inbox.anomaly import _median
        assert _median([1, 2, 3, 4]) == 2.5

    def test_median_empty(self):
        from src.inbox.anomaly import _median
        assert _median([]) == 0.0

    def test_mad_zero_when_uniform(self):
        from src.inbox.anomaly import _mad
        assert _mad([3.0, 3.0, 3.0, 3.0]) == 0.0

    def test_mad_nonzero(self):
        from src.inbox.anomaly import _mad
        values = [1.0, 2.0, 3.0, 100.0]
        m = _mad(values)
        assert m > 0


class TestAnomalyDetector:
    def test_detect_no_anomaly_flat(self, tmp_store):
        from src.inbox.anomaly import AnomalyDetector
        cfg = {"report": {"anomaly_detection": {"enabled": True, "sensitivity": 2.0, "baseline_days": 7}}}
        det = AnomalyDetector(tmp_store, cfg)
        # 历史数据不足（< 3），不触发告警
        result = det.detect_one("csat_avg", 4.0)
        assert result.is_anomaly is False

    def test_detect_anomaly_with_mocked_history(self, tmp_store):
        from src.inbox.anomaly import AnomalyDetector
        cfg = {"report": {"anomaly_detection": {"enabled": True, "sensitivity": 2.0, "baseline_days": 7}}}
        det = AnomalyDetector(tmp_store, cfg)

        # 注入稳定历史，然后给一个极端当前值
        with patch.object(det, "_get_historical_values", return_value=[4.5, 4.6, 4.4, 4.5, 4.6, 4.3]):
            result = det.detect_one("csat_avg", 1.0)  # 大幅下跌
        assert result.is_anomaly is True
        assert result.direction == "down"
        assert result.score > 2.0

    def test_detect_no_anomaly_within_range(self, tmp_store):
        from src.inbox.anomaly import AnomalyDetector
        cfg = {"report": {"anomaly_detection": {"enabled": True, "sensitivity": 2.0, "baseline_days": 7}}}
        det = AnomalyDetector(tmp_store, cfg)

        with patch.object(det, "_get_historical_values", return_value=[4.0, 4.2, 4.1, 4.3, 4.0, 4.2]):
            result = det.detect_one("csat_avg", 4.1)  # 在正常范围内
        assert result.is_anomaly is False

    def test_result_to_dict(self, tmp_store):
        from src.inbox.anomaly import AnomalyDetector
        det = AnomalyDetector(tmp_store, {})
        with patch.object(det, "_get_historical_values", return_value=[]):
            r = det.detect_one("l3l4_rate", 30.0)
        d = r.to_dict()
        assert "metric" in d
        assert "is_anomaly" in d
        assert "current_fmt" in d

    def test_run_full_check_disabled(self, tmp_store):
        from src.inbox.anomaly import AnomalyDetector
        det = AnomalyDetector(tmp_store, {"report": {"anomaly_detection": {"enabled": False}}})
        results = det.run_full_check()
        assert results == []

    def test_run_full_check_enabled_no_data(self, tmp_store):
        from src.inbox.anomaly import AnomalyDetector
        det = AnomalyDetector(tmp_store, {
            "report": {"anomaly_detection": {"enabled": True, "sensitivity": 2.0, "metrics": ["csat_avg"]}}
        })
        results = det.run_full_check()
        # 无历史数据时，不触发告警但返回结果列表
        assert isinstance(results, list)
        assert all(not r.is_anomaly for r in results)


class TestBuildAnomalyAlertPayload:
    def test_no_anomaly_returns_none(self, tmp_store):
        from src.inbox.anomaly import AnomalyDetector, build_anomaly_alert_payload
        det = AnomalyDetector(tmp_store, {})
        with patch.object(det, "_get_historical_values", return_value=[]):
            results = det.run_full_check()
        payload = build_anomaly_alert_payload(results)
        assert payload is None

    def test_anomaly_builds_payload(self, tmp_store):
        from src.inbox.anomaly import AnomalyDetector, build_anomaly_alert_payload
        cfg = {"report": {"anomaly_detection": {
            "enabled": True, "sensitivity": 2.0, "metrics": ["csat_avg"]
        }}}
        det = AnomalyDetector(tmp_store, cfg)
        with patch.object(det, "_get_historical_values", return_value=[4.5, 4.6, 4.4, 4.5, 4.6, 4.3]):
            results = det.run_full_check({"csat_avg": 1.0})
        payload = build_anomaly_alert_payload(results)
        assert payload is not None
        assert payload["anomaly_count"] >= 1
        assert len(payload["anomalies"]) >= 1


# ═══════════════════════════════════════════════════════════════
#  S3 — 全链路追踪
# ═══════════════════════════════════════════════════════════════

class TestTraceId:
    def test_new_trace_id_format(self):
        from src.inbox.tracer import new_trace_id
        tid = new_trace_id()
        assert tid.startswith("trc_")
        assert len(tid) == 4 + 16  # "trc_" + 16 hex

    def test_new_trace_unique(self):
        from src.inbox.tracer import new_trace_id
        ids = {new_trace_id() for _ in range(20)}
        assert len(ids) == 20, "trace_id 应唯一"

    def test_get_or_create_reuses_existing(self):
        from src.inbox.tracer import get_or_create_trace_id
        existing = "trc_a1b2c3d4e5f6a7b8"
        assert get_or_create_trace_id(existing) == existing

    def test_get_or_create_generates_new(self):
        from src.inbox.tracer import get_or_create_trace_id
        tid = get_or_create_trace_id(None)
        assert tid.startswith("trc_")


class TestTraceIdPropagation:
    def test_update_conv_meta_generates_trace(self, tmp_store):
        """update_conv_meta 首次调用应生成 trace_id。"""
        conv_id = f"conv_trace_{uuid.uuid4().hex[:6]}"
        tmp_store.update_conv_meta(conv_id, platform="tg")
        meta = tmp_store.get_conv_meta(conv_id)
        assert meta is not None
        tid = meta.get("trace_id", "")
        assert tid.startswith("trc_"), f"expected trace_id, got {tid!r}"

    def test_update_conv_meta_inherits_trace(self, tmp_store):
        """二次调用应沿用已有 trace_id，不重新生成。"""
        conv_id = f"conv_inherit_{uuid.uuid4().hex[:6]}"
        tmp_store.update_conv_meta(conv_id, platform="tg")
        meta1 = tmp_store.get_conv_meta(conv_id)
        tid1 = meta1["trace_id"]

        tmp_store.update_conv_meta(conv_id, platform="tg", intent="complaint")
        meta2 = tmp_store.get_conv_meta(conv_id)
        assert meta2["trace_id"] == tid1, "trace_id 不应重置"

    def test_upsert_draft_propagates_trace(self, tmp_store):
        """upsert_draft 携带 trace_id 后可在 reply_drafts 中查询。"""
        draft_id = f"inbox:d_{uuid.uuid4().hex[:6]}"
        tid = "trc_" + "f" * 16
        tmp_store.upsert_draft({
            "draft_id": draft_id,
            "source_kind": "inbox",
            "source_id": f"src_{uuid.uuid4().hex[:6]}",
            "conversation_id": "conv_trace_x",
            "platform": "tg",
            "draft_text": "测试草稿",
            "peer_text": "来自用户",
            "trace_id": tid,
        })
        with tmp_store._lock:
            row = tmp_store._conn.execute(
                "SELECT trace_id FROM reply_drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
        assert row is not None
        assert row[0] == tid


class TestTraceTimeline:
    def test_invalid_trace_id(self, tmp_store):
        from src.inbox.tracer import TraceTimeline
        tl = TraceTimeline(tmp_store)
        r = tl.build("bad_id")
        assert "error" in r

    def test_not_found(self, tmp_store):
        from src.inbox.tracer import TraceTimeline
        tl = TraceTimeline(tmp_store)
        r = tl.build("trc_ffffffffffffffff")
        assert r["found"] is False

    def test_timeline_with_conv_meta(self, tmp_store):
        from src.inbox.tracer import TraceTimeline
        conv_id = f"conv_tl_{uuid.uuid4().hex[:6]}"
        tmp_store.update_conv_meta(conv_id, platform="wa")
        meta = tmp_store.get_conv_meta(conv_id)
        tid = meta["trace_id"]

        tl = TraceTimeline(tmp_store)
        r = tl.build(tid)
        assert r["found"] is True
        assert r["conversation_id"] == conv_id
        assert r["total_events"] >= 1

    def test_timeline_includes_draft(self, tmp_store):
        from src.inbox.tracer import TraceTimeline
        conv_id = f"conv_tl2_{uuid.uuid4().hex[:6]}"
        tmp_store.update_conv_meta(conv_id, platform="tg")
        meta = tmp_store.get_conv_meta(conv_id)
        tid = meta["trace_id"]

        tmp_store.upsert_draft({
            "source_kind": "inbox",
            "source_id": f"s_{uuid.uuid4().hex[:6]}",
            "conversation_id": conv_id,
            "platform": "tg",
            "draft_text": "hello",
            "peer_text": "hi",
            "trace_id": tid,
        })

        tl = TraceTimeline(tmp_store)
        r = tl.build(tid)
        types_in_tl = [e["type"] for e in r["events"]]
        assert "draft_created" in types_in_tl


# ═══════════════════════════════════════════════════════════════
#  API 测试
# ═══════════════════════════════════════════════════════════════

class TestABTestAPI:
    def test_list_ab_tests_supervisor(self, tmp_store):
        client = _make_app(tmp_store, role="master")
        r = client.get("/api/workspace/ab-tests")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True

    def test_list_ab_tests_non_supervisor_forbidden(self, tmp_store):
        client = _make_app(tmp_store, role="user")
        r = client.get("/api/workspace/ab-tests")
        assert r.status_code == 403

    def test_create_ab_test(self, tmp_store):
        client = _make_app(tmp_store, role="master")
        r = client.post("/api/workspace/ab-tests", json={
            "name": "Test S1", "intent_filter": "complaint",
            "template_a_id": "tpl_a", "template_b_id": "tpl_b",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["test_id"].startswith("ab_")

    def test_create_ab_test_missing_name(self, tmp_store):
        client = _make_app(tmp_store, role="master")
        r = client.post("/api/workspace/ab-tests", json={
            "template_a_id": "a", "template_b_id": "b",
        })
        assert r.status_code == 400

    def test_get_ab_results(self, tmp_store):
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(tmp_store)
        tid = ab.create_test(name="X", intent_filter="*", template_a_id="a", template_b_id="b")
        client = _make_app(tmp_store, role="master")
        r = client.get(f"/api/workspace/ab-tests/{tid}/results")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "significance" in data

    def test_get_ab_results_not_found(self, tmp_store):
        client = _make_app(tmp_store, role="master")
        r = client.get("/api/workspace/ab-tests/ab_notexist/results")
        assert r.status_code == 404

    def test_stop_ab_test(self, tmp_store):
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(tmp_store)
        tid = ab.create_test(name="Y", intent_filter="*", template_a_id="a", template_b_id="b")
        client = _make_app(tmp_store, role="master")
        r = client.post(f"/api/workspace/ab-tests/{tid}/stop")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "stopped"

    def test_stop_nonexistent_test(self, tmp_store):
        client = _make_app(tmp_store, role="master")
        r = client.post("/api/workspace/ab-tests/ab_ghost/stop")
        assert r.status_code == 404


class TestAnomalyAPI:
    def test_anomaly_supervisor_200(self, tmp_store):
        client = _make_app(tmp_store, role="master")
        r = client.get("/api/workspace/anomaly")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "anomaly_count" in data
        assert "metrics_checked" in data

    def test_anomaly_non_supervisor_403(self, tmp_store):
        client = _make_app(tmp_store, role="user")
        r = client.get("/api/workspace/anomaly")
        assert r.status_code == 403


class TestTraceAPI:
    def test_trace_not_found_404(self, tmp_store):
        client = _make_app(tmp_store, role="master")
        r = client.get("/api/workspace/trace/trc_0000000000000000")
        assert r.status_code == 404

    def test_trace_invalid_format_404(self, tmp_store):
        client = _make_app(tmp_store, role="master")
        r = client.get("/api/workspace/trace/invalid_id")
        assert r.status_code == 404

    def test_trace_found(self, tmp_store):
        conv_id = f"conv_api_{uuid.uuid4().hex[:6]}"
        tmp_store.update_conv_meta(conv_id, platform="tg")
        meta = tmp_store.get_conv_meta(conv_id)
        tid = meta["trace_id"]

        client = _make_app(tmp_store, role="master")
        r = client.get(f"/api/workspace/trace/{tid}")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["found"] is True
        assert data["conversation_id"] == conv_id

    def test_recent_traces_supervisor(self, tmp_store):
        client = _make_app(tmp_store, role="master")
        r = client.get("/api/workspace/trace?limit=5")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "traces" in data

    def test_recent_traces_non_supervisor_403(self, tmp_store):
        client = _make_app(tmp_store, role="user")
        r = client.get("/api/workspace/trace")
        assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════
#  Admin 路由清单测试（baseline）
# ═══════════════════════════════════════════════════════════════

_S_ROUTES = {
    "/api/workspace/ab-tests",
    "/api/workspace/ab-tests/{test_id}/results",
    "/api/workspace/ab-tests/{test_id}/stop",
    "/api/workspace/anomaly",
    "/api/workspace/trace",
    "/api/workspace/trace/{trace_id}",
}


def test_s_routes_in_admin(tmp_store):
    """S 线 API 应全部注册到 admin 的测试 app。"""
    from fastapi import FastAPI
    app = FastAPI()
    _auth = _make_api_auth()
    from src.web.routes.drafts_routes import (
        register_ab_testing_route,
        register_anomaly_route,
        register_trace_route,
    )
    register_ab_testing_route(app, api_auth=_auth)
    register_anomaly_route(app, api_auth=_auth)
    register_trace_route(app, api_auth=_auth)
    registered = {r.path for r in app.routes}
    missing = _S_ROUTES - registered
    assert not missing, f"缺少路由: {missing}"
