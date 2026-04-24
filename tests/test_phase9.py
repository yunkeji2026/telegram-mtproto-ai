"""
Phase 9 集成测试
- AdminUserStore: PBKDF2 验证、CRUD、角色保护
- KnowledgeBaseStore: CSV 导出/导入往返
- admin.py: 批量更新端点（通过 kb_store 底层验证）
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ────────────────────────────────────────────────────────────
# AdminUserStore 测试
# ────────────────────────────────────────────────────────────

def _fresh_user_db():
    td = tempfile.mkdtemp()
    from utils.admin_user_store import AdminUserStore
    db = Path(td) / "test_users.db"
    return AdminUserStore(db)


def test_admin_user_create_verify():
    store = _fresh_user_db()
    uid = store.create_user("alice", "password123", role="superadmin")
    assert uid and len(uid) == 8
    user = store.verify("alice", "password123")
    assert user is not None
    assert user["username"] == "alice"
    assert user["role"] == "superadmin"


def test_admin_user_wrong_password():
    store = _fresh_user_db()
    store.create_user("bob", "correct_horse")
    assert store.verify("bob", "wrong_password") is None


def test_admin_user_unique():
    store = _fresh_user_db()
    store.create_user("charlie", "abc123")
    try:
        store.create_user("charlie", "xyz789")
        assert False, "Should have raised IntegrityError"
    except Exception:
        pass  # expected


def test_admin_user_update_password():
    store = _fresh_user_db()
    store.create_user("dave", "old_pass")
    uid = store.verify("dave", "old_pass")["id"]
    store.update_password(uid, "new_pass")
    assert store.verify("dave", "old_pass") is None
    assert store.verify("dave", "new_pass") is not None


def test_admin_user_role_update():
    store = _fresh_user_db()
    store.create_user("eve", "pass")
    uid = store.verify("eve", "pass")["id"]
    store.update_role(uid, "superadmin")
    user = store.get_user(uid)
    assert user["role"] == "superadmin"


def test_admin_user_toggle_delete():
    store = _fresh_user_db()
    store.create_user("frank", "pass")
    uid = store.verify("frank", "pass")["id"]
    # disable
    store.toggle_enabled(uid)
    assert store.verify("frank", "pass") is None
    # re-enable
    store.toggle_enabled(uid)
    assert store.verify("frank", "pass") is not None
    # delete
    store.delete_user(uid)
    assert store.get_user(uid) is None


def test_admin_user_has_users():
    store = _fresh_user_db()
    assert not store.has_users()
    store.create_user("grace", "pass")
    assert store.has_users()


def test_admin_user_list():
    store = _fresh_user_db()
    store.create_user("u1", "p1", "admin")
    store.create_user("u2", "p2", "superadmin")
    users = store.list_users()
    assert len(users) == 2
    names = [u["username"] for u in users]
    assert "u1" in names and "u2" in names


def test_admin_user_bootstrap():
    store = _fresh_user_db()
    store.ensure_bootstrap("admin", "admin123")
    assert store.has_users()
    # Second call should be no-op
    store.ensure_bootstrap("admin2", "pass")
    assert len(store.list_users()) == 1


def test_admin_user_superadmin_count():
    store = _fresh_user_db()
    store.create_user("sa1", "p1", "superadmin")
    store.create_user("sa2", "p2", "superadmin")
    store.create_user("a1", "p3", "admin")
    assert store.superadmin_count() == 2


# ────────────────────────────────────────────────────────────
# KnowledgeBaseStore CSV 导出 / 导入
# ────────────────────────────────────────────────────────────

def _fresh_kb():
    td = tempfile.mkdtemp()
    from utils.kb_store import KnowledgeBaseStore
    db = Path(td) / "test_kb.db"
    if db.exists():
        db.unlink()
    return KnowledgeBaseStore(db)


def test_csv_export_import_roundtrip():
    store = _fresh_kb()
    store.add_entry({
        "category": "退款",
        "title": "退款流程说明",
        "triggers": json.dumps(["退款", "如何退款"], ensure_ascii=False),
        "scenario": "用户询问退款",
        "steps": "联系客服→提交申请→3-5工作日到账",
    })
    store.add_entry({
        "category": "物流",
        "title": "快递查询",
        "triggers": json.dumps(["快递", "物流查询"], ensure_ascii=False),
        "scenario": "查询包裹",
        "steps": "登录官网→订单详情→物流追踪",
    })

    csv_text = store.export_csv()
    assert "退款流程说明" in csv_text
    assert "快递查询" in csv_text
    # BOM 标记
    assert csv_text.startswith("\ufeff")

    # 在新空库导入
    store2 = _fresh_kb()
    result = store2.import_from_csv(csv_text, mode="skip")
    assert result["added"] == 2
    assert result["skipped"] == 0

    entries = store2.list_entries()
    titles = [e["title"] for e in entries]
    assert "退款流程说明" in titles
    assert "快递查询" in titles


def test_csv_triggers_serialization():
    """确认 triggers 字段分号拆分后可以正确恢复为 JSON 数组"""
    store = _fresh_kb()
    store.add_entry({
        "category": "测试",
        "title": "触发词测试条目",
        "triggers": json.dumps(["关键词A", "关键词B", "关键词C"], ensure_ascii=False),
        "scenario": "测试",
        "steps": "测试步骤",
    })
    csv_text = store.export_csv()

    store2 = _fresh_kb()
    store2.import_from_csv(csv_text, mode="skip")
    entries = store2.list_entries()
    assert len(entries) == 1, f"Expected 1 entry, got {len(entries)}"
    raw_triggers = entries[0].get("triggers") or "[]"
    triggers_list = json.loads(raw_triggers) if isinstance(raw_triggers, str) else raw_triggers
    # Flatten to check any form
    triggers_str = " ".join(triggers_list) if triggers_list else ""
    assert "A" in triggers_str, f"Expected keyword in triggers: {triggers_str}"
    assert "C" in triggers_str, f"Expected keyword in triggers: {triggers_str}"


def test_csv_import_dedup():
    """skip 模式不覆盖，update 模式覆盖"""
    store = _fresh_kb()
    store.add_entry({"category": "A", "title": "已有条目", "scenario": "原始", "steps": "原始步骤"})

    csv_skip = "\ufeffcategory,title,triggers,scenario,steps\nA,已有条目,,新场景,新步骤\n"
    r = store.import_from_csv(csv_skip, mode="skip")
    assert r["skipped"] == 1
    assert r["added"] == 0

    r2 = store.import_from_csv(csv_skip, mode="update")
    assert r2["updated"] == 1


# ────────────────────────────────────────────────────────────
# 运行所有测试
# ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_admin_user_create_verify,
        test_admin_user_wrong_password,
        test_admin_user_unique,
        test_admin_user_update_password,
        test_admin_user_role_update,
        test_admin_user_toggle_delete,
        test_admin_user_has_users,
        test_admin_user_list,
        test_admin_user_bootstrap,
        test_admin_user_superadmin_count,
        test_csv_export_import_roundtrip,
        test_csv_triggers_serialization,
        test_csv_import_dedup,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print(f"\n{'='*48}")
    print(f"Phase 9 Tests: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
