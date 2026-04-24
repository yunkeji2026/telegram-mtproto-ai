"""Web 管理面板 — 通用 API 端点测试
包含：仪表盘、通道 API、审计 API、通知中心、配置摘要
"""

import pytest


class TestDashboard:
    def test_dashboard_loads(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200
        content = resp.content.decode("utf-8", errors="replace")
        # 应包含统计卡片
        assert "stat-card" in content or "uptime" in content.lower() or "模板" in content

    def test_dashboard_contains_channel_health(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200
        content = resp.content.decode("utf-8", errors="replace")
        # 通道健康区域应存在
        assert "health" in content.lower() or "通道" in content


class TestChannelApi:
    def test_get_channels(self, auth_client):
        resp = auth_client.get("/api/channels")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "ep" in data
        assert "usdt" in data

    def test_update_channel_fee_rate(self, auth_client):
        resp = auth_client.put(
            "/api/channels/ep",
            json={"fee_rate": "0.8%"},
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

        # 验证已持久化
        get_resp = auth_client.get("/api/channels")
        assert get_resp.json()["ep"]["fee_rate"] == "0.8%"

    def test_update_nonexistent_channel_404(self, auth_client):
        resp = auth_client.put(
            "/api/channels/no_such_channel",
            json={"fee_rate": "1%"},
        )
        assert resp.status_code == 404

    def test_channels_page_loads(self, auth_client):
        resp = auth_client.get("/channels")
        assert resp.status_code == 200
        assert b"ep" in resp.content or b"EP" in resp.content


class TestAuditApi:
    def test_audit_page_loads(self, auth_client):
        resp = auth_client.get("/audit")
        assert resp.status_code == 200

    def test_api_audit_returns_list(self, auth_client):
        resp = auth_client.get("/api/audit")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_audit_records_after_action(self, auth_client, audit_store):
        """执行操作后审计记录应增加"""
        before = len(audit_store.query())
        auth_client.put("/api/channels/ep", json={"fee_rate": "1.1%"})
        after = len(audit_store.query())
        assert after > before

    def test_api_audit_filter_by_action(self, auth_client):
        """审计 API 支持 action 过滤参数"""
        auth_client.put("/api/channels/ep", json={"status": "正常"})
        resp = auth_client.get("/api/audit?action=update_channel")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            assert all(e["action"] == "update_channel" for e in data)


class TestNotificationsApi:
    def test_notifications_returns_list(self, auth_client):
        resp = auth_client.get("/api/notifications")
        assert resp.status_code == 200
        data = resp.json()
        assert "notifications" in data
        assert "unread" in data
        assert isinstance(data["notifications"], list)
        assert isinstance(data["unread"], int)

    def test_notifications_structure(self, auth_client):
        resp = auth_client.get("/api/notifications")
        notifs = resp.json()["notifications"]
        for n in notifs:
            assert "id" in n
            assert "type" in n
            assert "level" in n
            assert "title" in n
            assert "body" in n

    def test_notifications_unauthenticated_redirects(self, client):
        resp = client.get("/api/notifications", follow_redirects=False)
        assert resp.status_code in (302, 303, 401, 403)


class TestConfigSummaryApi:
    def test_config_summary(self, auth_client):
        """/api/config/summary 返回 templates 和 channels 字典"""
        resp = auth_client.get("/api/config/summary")
        assert resp.status_code == 200
        data = resp.json()
        # 实际返回 {templates: {...}, channels: {...}}
        assert "templates" in data
        assert "channels" in data
        assert isinstance(data["templates"], dict)
        assert isinstance(data["channels"], dict)
        # 基本内容验证
        assert "greeting" in data["templates"]
        assert "ep" in data["channels"]


class TestImportExport:
    def test_export_config(self, auth_client):
        """GET /export 返回 zip 配置包"""
        resp = auth_client.get("/export")
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "zip" in ct or "octet-stream" in ct or len(resp.content) > 0

    def test_import_page_loads(self, auth_client):
        resp = auth_client.get("/import")
        assert resp.status_code == 200
