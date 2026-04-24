"""Web 管理面板 — 版本对比 & 快照回滚测试"""

import yaml
import pytest


@pytest.fixture()
def with_snapshot(config_dir, auth_client):
    """在 snapshots 目录中预置一个模板快照，供 diff/rollback 测试使用"""
    snap_dir = config_dir / "snapshots"
    snap_dir.mkdir(exist_ok=True)
    snap_content = yaml.dump(
        {"greeting": ["snap-hello"], "farewell": "snap-bye"},
        allow_unicode=True,
    )
    snap_file = snap_dir / "templates_20260101_120000.yaml"
    snap_file.write_text(snap_content, encoding="utf-8")
    return "templates_20260101_120000"


class TestDiffPage:
    def test_diff_page_loads(self, auth_client):
        resp = auth_client.get("/diff")
        assert resp.status_code == 200

    def test_diff_with_same_snapshot(self, auth_client, with_snapshot):
        """同一快照对比自身，应显示无差异"""
        snap_id = with_snapshot
        resp = auth_client.get(f"/diff?a={snap_id}&b={snap_id}")
        assert resp.status_code == 200
        content = resp.content.decode("utf-8", errors="replace")
        assert "相同" in content or "0" in content or "same" in content.lower()

    def test_diff_shows_changes(self, auth_client, with_snapshot, config_dir):
        """两个不同快照对比应显示差异行数"""
        snap_dir = config_dir / "snapshots"
        snap2 = yaml.dump(
            {"greeting": ["modified-hello", "extra"], "farewell": "modified-bye"},
            allow_unicode=True,
        )
        (snap_dir / "templates_20260101_130000.yaml").write_text(snap2, encoding="utf-8")

        snap_a = with_snapshot
        snap_b = "templates_20260101_130000"
        resp = auth_client.get(f"/diff?a={snap_a}&b={snap_b}")
        assert resp.status_code == 200
        content = resp.content.decode("utf-8", errors="replace")
        # 应有添加/删除行
        assert "+" in content or "新增" in content or "row-add" in content

    def test_diff_viewer_forbidden(self, viewer_client):
        """viewer 角色不应能访问版本对比页"""
        resp = viewer_client.get("/diff", follow_redirects=False)
        assert resp.status_code in (302, 303, 403)


class TestSnapshotRollback:
    def test_rollback_valid_snapshot(self, auth_client, with_snapshot, config_dir):
        """回滚到有效快照应成功，并备份当前文件"""
        snap_id = with_snapshot
        resp = auth_client.post(
            "/api/rollback",
            json={"snapshot_id": snap_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("snapshot") == snap_id
        assert data.get("restored")

        # 验证当前 templates.yaml 内容已更新
        current = yaml.safe_load((config_dir / "templates.yaml").read_text(encoding="utf-8"))
        assert current.get("farewell") == "snap-bye"

        # 验证原文件已备份
        backup = config_dir / "templates.yaml.pre_rollback"
        assert backup.exists()

    def test_rollback_creates_audit_entry(self, auth_client, with_snapshot, audit_store):
        """回滚操作应产生审计记录"""
        snap_id = with_snapshot
        auth_client.post("/api/rollback", json={"snapshot_id": snap_id})
        entries = audit_store.query(action="rollback")
        assert len(entries) >= 1
        assert any(e.get("target") == snap_id or e.get("old_val") == snap_id
                   for e in entries)

    def test_rollback_nonexistent_snapshot_404(self, auth_client):
        """回滚不存在的快照应返回 404"""
        resp = auth_client.post(
            "/api/rollback",
            json={"snapshot_id": "no_such_snapshot_99999"},
        )
        assert resp.status_code == 404

    def test_rollback_missing_snapshot_id_400(self, auth_client):
        """缺少 snapshot_id 应返回 400"""
        resp = auth_client.post(
            "/api/rollback",
            json={},
        )
        assert resp.status_code == 400

    def test_rollback_viewer_forbidden(self, viewer_client, with_snapshot):
        """viewer 角色不应能执行回滚"""
        resp = viewer_client.post(
            "/api/rollback",
            json={"snapshot_id": with_snapshot},
        )
        assert resp.status_code in (302, 303, 403)

    def test_rollback_invalidates_cache(self, auth_client, with_snapshot):
        """回滚后重新读取模板 API 应返回快照内容"""
        auth_client.post("/api/rollback", json={"snapshot_id": with_snapshot})
        resp = auth_client.get("/api/templates")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("farewell") == "snap-bye"
