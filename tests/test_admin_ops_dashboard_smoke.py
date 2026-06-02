"""冒烟测试：运营仪表盘只读端点（ops_dashboard_routes 抽出后可调不崩）。

event_tracker=None（conftest app fixture）→ 各端点优雅降级，均不得 500。
"""

from __future__ import annotations


def test_notifications_smoke(auth_client):
    r = auth_client.get("/api/notifications")
    assert r.status_code == 200
    body = r.json()
    assert "notifications" in body and "unread" in body
    assert isinstance(body["notifications"], list)


def test_snapshots_smoke(auth_client):
    r = auth_client.get("/api/snapshots")
    assert r.status_code == 200
    body = r.json()
    assert "snapshots" in body and "total" in body
    assert isinstance(body["snapshots"], list)


def test_snapshots_prefix_filter_smoke(auth_client):
    r = auth_client.get("/api/snapshots?prefix=templates&limit=5")
    assert r.status_code == 200
    assert "snapshots" in r.json()


def test_trigger_decisions_smoke(auth_client):
    r = auth_client.get("/api/trigger-decisions")
    assert r.status_code == 200
    body = r.json()
    assert "decisions" in body and "total" in body
    assert isinstance(body["decisions"], list)
