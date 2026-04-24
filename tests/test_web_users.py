"""Web 管理面板 — 用户管理 & 密码修改测试"""

import pytest


class TestUserCRUD:
    def test_users_page_loads(self, auth_client):
        resp = auth_client.get("/users")
        assert resp.status_code == 200

    def test_create_user_success(self, auth_client):
        resp = auth_client.post(
            "/users/create",
            data={
                "username": "newuser1",
                "password": "secure123",
                "role": "viewer",
                "display_name": "New User",
            },
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("username") == "newuser1"

    def test_create_duplicate_user_fails(self, auth_client):
        # 创建第一次
        auth_client.post(
            "/users/create",
            data={"username": "dupuser", "password": "pass123", "role": "viewer"},
            headers={"Accept": "application/json"},
        )
        # 再次创建同名用户
        resp = auth_client.post(
            "/users/create",
            data={"username": "dupuser", "password": "pass123", "role": "viewer"},
            headers={"Accept": "application/json"},
        )
        data = resp.json()
        assert data.get("ok") is False

    def test_create_user_short_password_fails(self, auth_client):
        resp = auth_client.post(
            "/users/create",
            data={"username": "pwdtest", "password": "abc", "role": "viewer"},
            headers={"Accept": "application/json"},
        )
        data = resp.json()
        assert data.get("ok") is False
        assert "6" in str(data.get("detail", "")) or "密码" in str(data.get("detail", ""))

    def test_newly_created_user_can_login(self, client, auth_client):
        """创建新用户后，新用户应能成功登录"""
        auth_client.post(
            "/users/create",
            data={"username": "logintest", "password": "loginpass123", "role": "viewer"},
        )
        resp = client.post(
            "/login",
            data={"username": "logintest", "password": "loginpass123"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "/login" not in str(resp.url)

    def test_change_role_ajax(self, auth_client):
        """通过 AJAX 修改用户角色（正确路由 /users/update/{user_id}）"""
        create_resp = auth_client.post(
            "/users/create",
            data={"username": "roletest", "password": "rolepass123", "role": "viewer"},
            headers={"Accept": "application/json"},
        )
        assert create_resp.json().get("ok")
        user_id = create_resp.json().get("id")
        assert user_id is not None

        resp = auth_client.post(
            f"/users/update/{user_id}",
            data={"role": "admin"},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("role") == "admin"

    def test_toggle_enable_disable(self, auth_client):
        """通过 /users/update/{id} 启用/禁用用户"""
        create_resp = auth_client.post(
            "/users/create",
            data={"username": "toggletest", "password": "togglepass123", "role": "viewer"},
            headers={"Accept": "application/json"},
        )
        user_id = create_resp.json().get("id")
        assert user_id is not None

        r1 = auth_client.post(
            f"/users/update/{user_id}",
            data={"enabled": "0"},
            headers={"Accept": "application/json"},
        )
        assert r1.status_code == 200
        data = r1.json()
        assert data.get("ok") is True
        assert not data.get("enabled")  # SQLite 返回 0 或 False

    def test_delete_user(self, auth_client):
        """通过 /users/delete/{id} 删除用户"""
        create_resp = auth_client.post(
            "/users/create",
            data={"username": "deletetest", "password": "deletepass123", "role": "viewer"},
            headers={"Accept": "application/json"},
        )
        user_id = create_resp.json().get("id")
        assert user_id is not None

        resp = auth_client.post(
            f"/users/delete/{user_id}",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_deleted_user_cannot_login(self, client, auth_client):
        """已删除的用户不应能登录"""
        create_resp = auth_client.post(
            "/users/create",
            data={"username": "todelete", "password": "todelete123", "role": "viewer"},
            headers={"Accept": "application/json"},
        )
        user_id = create_resp.json().get("id")
        auth_client.post(f"/users/{user_id}/delete", headers={"Accept": "application/json"})

        resp = client.post(
            "/login",
            data={"username": "todelete", "password": "todelete123"},
            follow_redirects=True,
        )
        # 应停留在登录页（失败）
        body = resp.content.decode("utf-8", errors="replace")
        assert "错误" in body or "login" in str(resp.url).lower() or "/login" in str(resp.url)


class TestPasswordChange:
    def test_change_password_success(self, auth_client):
        """正确的旧密码 → 成功修改"""
        resp = auth_client.post(
            "/api/change-password",
            json={
                "old_password": "test-token-123",
                "new_password": "newpass123",
            },
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_change_password_wrong_old(self, auth_client):
        """错误的旧密码应返回 400"""
        resp = auth_client.post(
            "/api/change-password",
            json={
                "old_password": "totally-wrong",
                "new_password": "newpass456",
            },
        )
        assert resp.status_code == 400

    def test_change_password_too_short(self, auth_client):
        """新密码不足 6 位应返回 400"""
        resp = auth_client.post(
            "/api/change-password",
            json={
                "old_password": "test-token-123",
                "new_password": "abc",
            },
        )
        assert resp.status_code == 400

    def test_unauthenticated_cannot_change_password(self, client):
        """未登录时调用修改密码 API 应返回 401/403"""
        resp = client.post(
            "/api/change-password",
            json={"old_password": "test-token-123", "new_password": "newpass123"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303, 401, 403)
