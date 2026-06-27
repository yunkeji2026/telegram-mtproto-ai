"""只读聚合草稿指标端点 + rates_vs_generated 补全 的回归。

校准回路闭合（D4）：
  - GET /api/drafts/pipeline-metrics **只需 token、不需主管会话** → 非主管也 200。
  - 对照 GET /api/drafts/autosend-status 仍要求主管 → 非主管 403。
  - 端点返回 draft_pipeline 聚合块，且在 OpenAPI schema 中注册。

指标补全：
  - MetricsStore.get_inbox_draft_metrics().rates_vs_generated 含 fast_path / empty
    （此前漏列，导致下游脚本/仪表盘读到 None 误判为 0）。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.web.routes.drafts_routes import register_drafts_routes
from src.monitoring.metrics_store import MetricsStore


def _api_auth(request: Request) -> None:
    return None


def _make_app(role: str = "admin"):
    app = FastAPI()
    if role:
        @app.middleware("http")
        async def _inj(request: Request, call_next):
            request.scope["session"] = {"role": role, "user_id": "u1", "username": "u1"}
            return await call_next(request)
    register_drafts_routes(app, api_auth=_api_auth)
    return TestClient(app, raise_server_exceptions=False)


class TestPipelineMetricsEndpoint:
    def test_token_only_no_supervisor_required(self):
        """非主管角色（坐席）也能 200 拿到 pipeline-metrics（只读聚合，token 足够）。"""
        c = _make_app(role="agent")
        r = c.get("/api/drafts/pipeline-metrics")
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert "draft_pipeline" in body

    def test_autosend_status_still_supervisor_gated(self):
        """对照：旧端点对非主管仍 403（证明新端点确实闭合了纯 token 的缺口）。"""
        c = _make_app(role="agent")
        assert c.get("/api/drafts/autosend-status").status_code == 403

    def test_pipeline_metrics_in_openapi(self):
        c = _make_app()
        paths = c.get("/openapi.json").json().get("paths", {})
        assert "/api/drafts/pipeline-metrics" in paths

    def test_not_shadowed_by_draft_id_wildcard(self):
        """pipeline-metrics 必须在 /{draft_id} 之前注册，否则会被通配吞掉。"""
        c = _make_app()
        r = c.get("/api/drafts/pipeline-metrics")
        # 若被 /{draft_id} 截获且无 draft_service → 503；正确路由命中应为 200。
        assert r.status_code == 200


class TestDraftRatesCompleteness:
    def test_rates_include_fast_path_and_empty(self):
        ms = MetricsStore()
        for _ in range(10):
            ms.record_inbox_draft_event("generated")
        for _ in range(7):
            ms.record_inbox_draft_event("fast_path")
        for _ in range(2):
            ms.record_inbox_draft_event("empty")
        rates = ms.get_inbox_draft_metrics()["rates_vs_generated"]
        assert rates["fast_path"] == 0.7
        assert rates["empty"] == 0.2
        assert "memory_hit" in rates  # 既有键不回归

    def test_rates_empty_when_no_generated(self):
        ms = MetricsStore()
        assert ms.get_inbox_draft_metrics()["rates_vs_generated"] == {}
