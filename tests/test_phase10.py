"""
Phase 10 集成测试
- AdminUserStore + setup 流程
- audit 热力图数据聚合
- CSV 往返（引用 phase9 的 store 工厂）
"""
import json
import sys
import sqlite3
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ────────────────────────────────────────────────────────────
# AdminUserStore bootstrap 流程
# ────────────────────────────────────────────────────────────

def test_setup_bootstrap_flow():
    """模拟 /setup 向导: 无用户 → 创建 master → 验证登录"""
    from utils.admin_user_store import AdminUserStore
    td = tempfile.mkdtemp()
    store = AdminUserStore(Path(td) / "setup_test.db")

    # 无用户时 has_users=False
    assert not store.has_users()

    # 创建第一个 master 账户
    uid = store.create_user("adminuser", "securePass1!", role="superadmin")
    assert uid

    # has_users 现在为 True
    assert store.has_users()

    # 验证登录
    user = store.verify("adminuser", "securePass1!")
    assert user is not None
    assert user["role"] == "superadmin"

    # 重复 bootstrap 不创建新用户
    store.ensure_bootstrap("admin2", "pass")
    assert len(store.list_users()) == 1


# ────────────────────────────────────────────────────────────
# audit 热力图数据查询
# ────────────────────────────────────────────────────────────

def _fresh_audit():
    from utils.audit_store import AuditStore
    td = tempfile.mkdtemp()
    store = AuditStore(Path(td) / "audit.db")
    return store


def test_audit_activity_aggregation():
    """向 audit_store 写入跨天记录，检查热力图数据聚合"""
    store = _fresh_audit()

    # 写入今天的操作
    for _ in range(5):
        store.log("admin", "update_template", "key1")

    # 写入昨天（手动插入以控制 ts）
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    for _ in range(3):
        store._conn.execute(
            "INSERT INTO audit_log (ts, user_id, action, target) VALUES (?, ?, ?, ?)",
            (yesterday + " 10:00:00", "admin", "update_channel", "ch1"),
        )
    store._conn.commit()

    # 模拟 /api/audit/activity 查询
    rows = store._conn.execute(
        "SELECT DATE(ts) as day, COUNT(*) as cnt "
        "FROM audit_log "
        "WHERE ts >= date('now', '-84 days') "
        "GROUP BY day ORDER BY day",
    ).fetchall()

    day_map = {r["day"]: r["cnt"] for r in rows}
    today = time.strftime("%Y-%m-%d")

    assert day_map.get(today, 0) == 5, f"Today: {day_map}"
    assert day_map.get(yesterday, 0) == 3, f"Yesterday: {day_map}"


def test_audit_activity_empty():
    """空 audit store 应该返回空 day_map 而不出错"""
    store = _fresh_audit()
    rows = store._conn.execute(
        "SELECT DATE(ts) as day, COUNT(*) as cnt "
        "FROM audit_log "
        "WHERE ts >= date('now', '-84 days') "
        "GROUP BY day ORDER BY day",
    ).fetchall()
    assert rows == []


# ────────────────────────────────────────────────────────────
# system-info 数据结构验证
# ────────────────────────────────────────────────────────────

def test_system_info_fields():
    """验证 AdminUserStore 提供 user_count() 方法（供 system-info 使用）"""
    from utils.web_user_store import WebUserStore
    td = tempfile.mkdtemp()
    store = WebUserStore(Path(td) / "users.db")
    # 初始无用户
    assert store.user_count() == 0
    store.create_user("u1", "pass123456", "master")
    assert store.user_count() == 1


# ────────────────────────────────────────────────────────────
# KB CSV 健全性（引用 Phase 9 功能）
# ────────────────────────────────────────────────────────────

def test_csv_bom_present():
    """确保 export_csv 输出含 UTF-8 BOM（Excel 识别需要）"""
    from utils.kb_store import KnowledgeBaseStore
    td = tempfile.mkdtemp()
    store = KnowledgeBaseStore(Path(td) / "kb.db")
    store.add_entry({
        "title": "BOM测试", "category": "测试",
        "scenario": "验证BOM", "steps": "步骤"
    })
    csv_text = store.export_csv()
    assert csv_text.startswith("\ufeff"), "CSV 应以 BOM 开头"


# ────────────────────────────────────────────────────────────
# 运行
# ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_setup_bootstrap_flow,
        test_audit_activity_aggregation,
        test_audit_activity_empty,
        test_system_info_fields,
        test_csv_bom_present,
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
    print(f"Phase 10 Tests: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
