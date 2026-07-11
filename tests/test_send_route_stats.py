"""出站「路由去向」观测（SendRouteStats）单测。

守：编排器接管 vs 回落适配器的按平台计数、回落率、prom 输出、平台上限、消毒、单例。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.send_route_stats import SendRouteStats, get_send_route_stats
from src.web.routes.drafts_routes import register_metrics_route


def test_record_and_dump_basic():
    s = SendRouteStats()
    s.record("messenger", "adapter")
    s.record("messenger", "adapter")
    s.record("messenger", "orchestrator")
    s.record("telegram", "orchestrator")
    d = s.dump()
    assert d["total"] == 4
    assert d["adapter_total"] == 2 and d["orchestrator_total"] == 2
    assert d["fallback_rate"] == 0.5
    mp = d["by_platform"]["messenger"]
    assert mp["adapter"] == 2 and mp["orchestrator"] == 1 and mp["total"] == 3
    assert mp["fallback_rate"] == round(2 / 3, 4)
    # by_platform 按 total 降序：messenger(3) 排在 telegram(1) 前
    assert list(d["by_platform"].keys())[0] == "messenger"


def test_invalid_route_counts_as_adapter():
    """非法 route 保守归 adapter（回落才是要盯的风险面，不能漏计）。"""
    s = SendRouteStats()
    s.record("line", "weird")
    assert s.dump()["by_platform"]["line"]["adapter"] == 1


def test_platform_sanitized():
    s = SendRouteStats()
    s.record("  Messenger! ", "adapter")  # 归一小写 + 去非法字符
    assert "messenger" in s.dump()["by_platform"]
    s.record("", "adapter")
    assert "unknown" in s.dump()["by_platform"]


def test_prom_format():
    s = SendRouteStats()
    s.record("messenger", "adapter")
    prom = s.dump_prom()
    assert 'inbox_send_routed_total{platform="messenger",route="adapter"} 1' in prom
    assert 'inbox_send_routed_total{platform="messenger",route="orchestrator"} 0' in prom
    assert "# TYPE inbox_send_routed_total counter" in prom


def test_platform_cap_overflow():
    s = SendRouteStats()
    for i in range(40):
        s.record(f"p{i}", "adapter")
    d = s.dump()
    assert "__other__" in d["by_platform"]  # 超上限归入溢出桶
    assert d["total"] == 40  # 计数不丢


def test_reset():
    s = SendRouteStats()
    s.record("x", "adapter")
    s.reset()
    d = s.dump()
    assert d["total"] == 0 and d["by_platform"] == {}


def test_empty_dump_is_safe():
    d = SendRouteStats().dump()
    assert d["total"] == 0 and d["fallback_rate"] == 0.0 and d["by_platform"] == {}


def test_singleton_identity():
    assert get_send_route_stats() is get_send_route_stats()


# ── 端到端：记录 → /api/workspace/metrics 读出（JSON + Prometheus） ──────────────

def _auth(r: Request):  # 模块级 + Request 注解，避免 PEP563 下 FastAPI 误判为 query 参数
    return True


def _metrics_app():
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    register_metrics_route(app, api_auth=_auth)
    return TestClient(app, raise_server_exceptions=True)


def test_metrics_endpoint_includes_send_routes():
    get_send_route_stats().reset()
    get_send_route_stats().record("messenger", "adapter")
    get_send_route_stats().record("telegram", "orchestrator")
    m = _metrics_app().get("/api/workspace/metrics").json()
    sr = m.get("send_routes")
    assert sr is not None
    assert sr["by_platform"]["messenger"]["adapter"] == 1
    assert sr["adapter_total"] == 1 and sr["orchestrator_total"] == 1


def test_metrics_prometheus_includes_send_routes():
    get_send_route_stats().reset()
    get_send_route_stats().record("messenger", "adapter")
    r = _metrics_app().get("/api/workspace/metrics?format=prometheus")
    assert r.status_code == 200
    assert "inbox_send_routed_total" in r.text
    assert 'platform="messenger",route="adapter"} 1' in r.text
