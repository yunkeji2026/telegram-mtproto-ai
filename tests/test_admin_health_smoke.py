"""冒烟测试：健康巡检 / 告警状态端点（health_routes 抽出后可调不崩）。

验证 AdminRouteContext 新增的 domain_web_pages/domain_dashboard_widgets 正确注入，
两个端点优雅返回结构化结果，不 500。
"""

from __future__ import annotations


def test_health_check_smoke(auth_client):
    r = auth_client.get("/api/health-check")
    assert r.status_code == 200
    body = r.json()
    assert "score" in body and "issues" in body and "level_summary" in body
    assert body["status"] in ("ok", "warn", "critical")
    assert isinstance(body["score"], int)


def test_alert_status_smoke(auth_client):
    r = auth_client.get("/api/alert-status")
    assert r.status_code == 200
    body = r.json()
    assert "alerts" in body and "highest_level" in body and "alert_count" in body
    assert body["highest_level"] in ("ok", "warn", "critical")
    assert isinstance(body["alerts"], list)
