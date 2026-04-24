"""Web 管理面板 — 话术模板管理测试"""

import pytest


class TestTemplateRead:
    def test_templates_page_loads(self, auth_client):
        resp = auth_client.get("/templates")
        assert resp.status_code == 200
        body = resp.content.decode("utf-8", errors="replace")
        assert "greeting" in body or "模板" in body

    def test_api_get_all_templates(self, auth_client):
        resp = auth_client.get("/api/templates")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "greeting" in data
        assert "farewell" in data

    def test_api_template_values_correct(self, auth_client):
        resp = auth_client.get("/api/templates")
        data = resp.json()
        # greeting 是列表
        assert isinstance(data["greeting"], list)
        assert len(data["greeting"]) > 0
        # farewell 是字符串
        assert isinstance(data["farewell"], str)


class TestTemplateUpdate:
    def test_api_update_template_string(self, auth_client):
        """更新字符串类型模板"""
        resp = auth_client.put(
            "/api/templates/farewell",
            json={"value": "see you later"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # 验证已持久化
        get_resp = auth_client.get("/api/templates")
        assert get_resp.json()["farewell"] == "see you later"

    def test_api_update_template_list(self, auth_client):
        """更新列表类型模板"""
        new_greetings = ["hi there!", "hey!", "hello friend"]
        resp = auth_client.put(
            "/api/templates/greeting",
            json={"value": new_greetings},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        get_resp = auth_client.get("/api/templates")
        assert get_resp.json()["greeting"] == new_greetings

    def test_api_update_nonexistent_template_404(self, auth_client):
        """更新不存在的模板键应返回 404"""
        resp = auth_client.put(
            "/api/templates/no_such_key",
            json={"value": "test"},
        )
        assert resp.status_code == 404

    def test_api_update_template_missing_value_400(self, auth_client):
        """缺少 value 字段应返回 400"""
        resp = auth_client.put(
            "/api/templates/farewell",
            json={"wrong_key": "test"},
        )
        assert resp.status_code == 400

    def test_form_update_template_ajax(self, auth_client):
        """通过表单 + JSON Accept 头保存模板"""
        resp = auth_client.post(
            "/templates/update",
            data={"key": "farewell", "value": "bye bye"},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "farewell" in data["msg"]

    def test_update_persists_across_requests(self, auth_client):
        """模板更新应在多次请求之间持久化（写入文件）"""
        auth_client.put("/api/templates/farewell", json={"value": "persistent-value"})

        resp1 = auth_client.get("/api/templates")
        resp2 = auth_client.get("/api/templates")

        assert resp1.json()["farewell"] == "persistent-value"
        assert resp2.json()["farewell"] == "persistent-value"

    def test_update_creates_audit_entry(self, auth_client, audit_store):
        """模板更新应在审计日志中记录"""
        auth_client.put("/api/templates/farewell", json={"value": "audit-test"})
        entries = audit_store.query(action="update_template")
        assert len(entries) >= 1
        assert any(e["target"] == "farewell" for e in entries)
