"""Web 管理面板 — 认证 & RBAC 测试"""

import pytest


class TestLoginFlow:
    """登录 / 登出 核心流程"""

    def test_unauthenticated_redirects_to_login(self, client):
        """未登录访问首页应被重定向到登录页"""
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/login" in resp.headers["location"]

    def test_login_page_accessible(self, client):
        """登录页本身应始终可访问"""
        resp = client.get("/login")
        assert resp.status_code == 200
        body = resp.content.decode("utf-8", errors="replace").lower()
        assert "login" in body or "bot" in body or "登录" in body

    def test_token_login_success(self, client):
        """正确的 auth_token 应成功登录并跳转到首页"""
        resp = client.post(
            "/login",
            data={"auth_token": "test-token-123"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "/login" not in str(resp.url)

    def test_token_login_failure(self, client):
        """错误的 token 应停留在登录页并显示错误"""
        resp = client.post(
            "/login",
            data={"auth_token": "wrong-token"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.content.decode("utf-8", errors="replace")
        assert "token" in body.lower() or "错误" in body or "error" in body.lower()

    def test_user_login_success(self, auth_client):
        """auth_client fixture 验证用户名/密码登录成功"""
        resp = auth_client.get("/")
        assert resp.status_code == 200

    def test_logout_clears_session(self, auth_client):
        """登出后再访问首页应被重定向"""
        # 确认已登录
        r1 = auth_client.get("/")
        assert r1.status_code == 200

        # 登出
        auth_client.get("/logout", follow_redirects=True)

        # 再次访问应该被重定向
        r2 = auth_client.get("/", follow_redirects=False)
        assert r2.status_code in (302, 303)

    def test_wrong_password_fails(self, client):
        """错误密码应返回登录失败"""
        resp = client.post(
            "/login",
            data={"username": "admin", "password": "wrong-password"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # 应该仍在登录页
        body = resp.content.decode("utf-8", errors="replace")
        assert "错误" in body or "invalid" in body.lower() or "login" in str(resp.url).lower()


class TestRBACPages:
    """页面访问的角色控制"""

    def test_master_accesses_dashboard(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200

    def test_master_accesses_templates(self, auth_client):
        resp = auth_client.get("/templates")
        assert resp.status_code == 200

    def test_master_accesses_users(self, auth_client):
        resp = auth_client.get("/users")
        assert resp.status_code == 200

    def test_master_accesses_diff(self, auth_client):
        resp = auth_client.get("/diff")
        assert resp.status_code == 200

    def test_viewer_can_access_dashboard(self, viewer_client):
        resp = viewer_client.get("/")
        assert resp.status_code == 200

    def test_viewer_cannot_access_users(self, viewer_client):
        """viewer 不应能访问用户管理页"""
        resp = viewer_client.get("/users", follow_redirects=False)
        assert resp.status_code in (302, 303, 403)

    def test_viewer_cannot_access_diff(self, viewer_client):
        """viewer 不应能访问版本对比页"""
        resp = viewer_client.get("/diff", follow_redirects=False)
        assert resp.status_code in (302, 303, 403)


class TestRBACApiWrite:
    """API 写权限控制"""

    def test_viewer_cannot_update_template_api(self, viewer_client):
        """viewer 角色调用模板写 API 应返回 403"""
        resp = viewer_client.put(
            "/api/templates/greeting",
            json={"value": "hacked"},
        )
        assert resp.status_code == 403

    def test_viewer_cannot_update_channel_api(self, viewer_client):
        """viewer 角色调用通道写 API 应返回 403"""
        resp = viewer_client.put(
            "/api/channels/ep",
            json={"fee_rate": "99%"},
        )
        assert resp.status_code == 403

    def test_master_can_read_templates_api(self, auth_client):
        """master 应能读取模板 API"""
        resp = auth_client.get("/api/templates")
        assert resp.status_code == 200
        data = resp.json()
        assert "greeting" in data
