"""冒烟测试：信息/日志/分析 页面路由（page_routes 抽出后可渲染不崩）。

验证 AdminRouteContext 新增的 templates/log_buffer 注入正确，页面 200 渲染。
"""

from __future__ import annotations


def test_help_page(auth_client):
    r = auth_client.get("/help")
    assert r.status_code == 200


def test_logs_page(auth_client):
    r = auth_client.get("/logs")
    assert r.status_code == 200


def test_analytics_page(auth_client):
    r = auth_client.get("/analytics")
    assert r.status_code == 200


def test_cases_page(auth_client):
    r = auth_client.get("/cases")
    assert r.status_code == 200


def test_training_page(auth_client):
    # 培训幻灯片文件可能未部署 → 404；关键是不 500
    r = auth_client.get("/training")
    assert r.status_code in (200, 404)


def test_audit_page(auth_client):
    r = auth_client.get("/audit")
    assert r.status_code == 200


def test_audit_export(auth_client):
    r = auth_client.get("/audit/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")


def test_diff_page(auth_client):
    r = auth_client.get("/diff")
    assert r.status_code == 200


def test_developer_page(auth_client):
    r = auth_client.get("/developer")
    assert r.status_code == 200
