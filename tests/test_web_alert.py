"""Web 管理面板 — 紧急告警 API 测试"""

import yaml
import pytest


class TestAlertStatus:
    def test_alert_status_returns_structure(self, auth_client):
        """GET /api/alert-status 返回正确结构"""
        resp = auth_client.get("/api/alert-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "alerts" in data
        assert "highest_level" in data
        assert "alert_count" in data
        assert data["highest_level"] in ("ok", "warn", "critical")
        assert isinstance(data["alerts"], list)
        assert data["alert_count"] == len(data["alerts"])

    def test_alert_items_have_required_fields(self, auth_client):
        """每条告警应包含必要字段"""
        resp = auth_client.get("/api/alert-status")
        for alert in resp.json()["alerts"]:
            assert "level" in alert
            assert "type" in alert
            assert "title" in alert
            assert "body" in alert
            assert alert["level"] in ("critical", "warn", "info")

    def test_alert_level_valid(self, auth_client):
        """告警级别应为合法值之一"""
        resp = auth_client.get("/api/alert-status")
        data = resp.json()
        # channel_health 以 'active' 为满分状态，测试配置用 '正常' 会产生 warn/critical
        # 这是正确的告警行为；只需确保级别有效
        assert data["highest_level"] in ("ok", "warn", "critical")

    def test_alert_empty_templates_no_legacy_config_alert(self, app, config_dir):
        """templates.yaml 为空时不再产生 config 类告警（话术已统一到 KB，见 admin alert-status 注释）。"""
        from starlette.testclient import TestClient
        (config_dir / "templates.yaml").write_text("{}", encoding="utf-8")

        with TestClient(app, raise_server_exceptions=True) as c:
            c.headers.update({"Authorization": "Bearer test-token-123"})
            c.post("/login", data={"auth_token": "test-token-123"}, follow_redirects=True)
            c.get("/api/templates")
            resp = c.get("/api/alert-status")
            assert resp.status_code == 200
            data = resp.json()
            types = [a["type"] for a in data["alerts"]]
            assert "config" not in types

    def test_alert_unauthenticated_redirects(self, client):
        """未登录时访问应重定向"""
        resp = client.get("/api/alert-status", follow_redirects=False)
        assert resp.status_code in (302, 303, 401, 403)

    def test_dashboard_contains_alert_banner_element(self, auth_client):
        """仪表盘页面应包含告警横幅 HTML 元素（完整模式下 / 返回 dashboard）"""
        auth_client.cookies.set("ui_mode", "full")
        resp = auth_client.get("/", follow_redirects=False)
        if resp.status_code in (302, 303):
            resp = auth_client.get("/", follow_redirects=True)
        assert resp.status_code == 200
        body = resp.content.decode("utf-8", errors="replace")
        assert "alert-banner" in body
        assert "loadAlerts" in body
