"""
Phase 13 集成测试
- 会话管理：create / touch / revoke / list / cleanup
- AI 自动生成端点注册
- Markdown 导出端点注册
- users.html session UI
- knowledge.html AI 生成 UI
"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

_pass = 0
_fail = 0

def ok(msg):
    global _pass; _pass += 1
    print(f"  PASS  {msg}")

def fail(msg, exc=""):
    global _fail; _fail += 1
    print(f"  FAIL  {msg}" + (f"  [{exc}]" if exc else ""))

# ─────────────────────────────────────────────
# T1: WebUserStore session 管理
# ─────────────────────────────────────────────
def main():
    global _pass, _fail
    print("\n[T1] WebUserStore session management")
    try:
        import tempfile
        from src.utils.web_user_store import WebUserStore
        td = tempfile.mkdtemp()
        store = WebUserStore(Path(td) / "users.db")
        store.create_user("testuser", "password123", "admin")
    
        # 创建 session
        jti1 = store.create_session("testuser", "admin", "127.0.0.1", "Chrome/120")
        assert jti1 and len(jti1) == 32
        ok(f"create_session: {jti1[:8]}…")
    
        jti2 = store.create_session("testuser", "admin", "192.168.1.1", "Firefox/121")
        ok(f"create_session (second): {jti2[:8]}…")
    
        # touch session
        valid = store.touch_session(jti1)
        assert valid, "touch_session 应返回 True"
        ok("touch_session 返回 True（session 有效）")
    
        # list sessions
        sessions = store.list_sessions()
        assert len(sessions) == 2
        ok(f"list_sessions 返回 {len(sessions)} 条")
    
        # revoke single session
        store.revoke_session(jti1)
        valid_after = store.touch_session(jti1)
        assert not valid_after, "撤销后 touch 应返回 False"
        ok("revoke_session 后 touch 返回 False")
    
        sessions_after = store.list_sessions()
        assert len(sessions_after) == 1
        ok(f"revoke 后 list_sessions 仅返回 {len(sessions_after)} 条")
    
        # revoke all
        jti3 = store.create_session("testuser", "admin", "10.0.0.1", "Safari")
        store.revoke_all_sessions("testuser")
        sessions_final = store.list_sessions()
        assert len(sessions_final) == 0
        ok("revoke_all_sessions 后无活跃 session")
    
        # cleanup
        store.cleanup_old_sessions(0)  # 清理所有（days=0 测试用）
        ok("cleanup_old_sessions 执行无错")
    except Exception as e:
        fail("Session 管理测试失败", str(e))
    
    # ─────────────────────────────────────────────
    # T2: admin.py 新端点注册
    # ─────────────────────────────────────────────
    print("\n[T2] New API endpoints in admin.py")
    try:
        admin_src = (ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
        checks = [
            ("/api/sessions",            "GET sessions list"),
            ("/api/sessions/{jti}/revoke", "POST revoke session"),
            ("/api/sessions/revoke-all", "POST revoke all"),
            ("/api/kb/ai-generate",      "POST KB AI generate"),
            ("/api/kb/export-markdown",  "GET KB Markdown export"),
            ("create_session",           "Login creates session"),
            ("revoke_session",           "Logout revokes session"),
            ("_check_session_valid",     "Auth checks session validity"),
            ("response_format",          "AI generate JSON format"),
            ("json.loads(raw)",          "JSON parsing from AI response"),
        ]
        for token, desc in checks:
            assert token in admin_src, f"缺少: {token}"
            ok(f"找到: {desc}")
    except Exception as e:
        fail("admin.py 端点检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # T3: users.html session 管理 UI
    # ─────────────────────────────────────────────
    print("\n[T3] users.html session management UI")
    try:
        users_src = (ROOT / "src" / "web" / "templates" / "users.html").read_text(encoding="utf-8")
        items = [
            ("sessions-list", "sessions 容器"),
            ("loadSessions",  "loadSessions 函数"),
            ("revokeSession", "revokeSession 函数"),
            ("revokeAllSessions", "revokeAllSessions 函数"),
            ("踢出", "踢出按钮文本"),
            ("/api/sessions", "sessions API 调用"),
        ]
        for token, desc in items:
            assert token in users_src, f"缺少: {token}"
            ok(f"找到: {desc}")
    except Exception as e:
        fail("users.html 检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # T4: knowledge.html AI 生成 UI
    # ─────────────────────────────────────────────
    print("\n[T4] knowledge.html AI generate UI")
    try:
        kb_src = (ROOT / "src" / "web" / "templates" / "knowledge.html").read_text(encoding="utf-8")
        items = [
            ("aiGenerate",        "aiGenerate 函数"),
            ("ai-gen-topic",      "AI 生成输入框"),
            ("ai-gen-btn",        "AI 生成按钮"),
            ("ai-gen-status",     "AI 生成状态文字"),
            ("/api/kb/ai-generate", "API 调用"),
            ("exportMarkdown",    "exportMarkdown 函数"),
            ("/api/kb/export-markdown", "Markdown 导出 API"),
            ("AI 智能填写",         "AI 生成区标题"),
        ]
        for token, desc in items:
            assert token in kb_src, f"缺少: {token}"
            ok(f"找到: {desc}")
    except Exception as e:
        fail("knowledge.html 检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # T5: WebUserStore DDL session 表存在
    # ─────────────────────────────────────────────
    print("\n[T5] WebUserStore session table DDL")
    try:
        from src.utils.web_user_store import WebUserStore
        store_src = (ROOT / "src" / "utils" / "web_user_store.py").read_text(encoding="utf-8")
        assert "web_sessions" in store_src
        ok("web_sessions 表定义存在")
        assert "jti" in store_src
        ok("jti 字段存在")
        assert "revoked" in store_src
        ok("revoked 字段存在")
        assert "def touch_session" in store_src
        ok("touch_session 方法")
        assert "def revoke_all_sessions" in store_src
        ok("revoke_all_sessions 方法")
        assert "def cleanup_old_sessions" in store_src
        ok("cleanup_old_sessions 方法")
    except Exception as e:
        fail("web_user_store.py 检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # T6: Markdown 导出 admin.py 逻辑
    # ─────────────────────────────────────────────
    print("\n[T6] Markdown export logic")
    try:
        admin_src = (ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
        assert "text/markdown" in admin_src
        ok("Markdown mime type 设置")
        assert "kb_entries WHERE enabled=1" in admin_src
        ok("仅导出启用条目")
        assert "ORDER BY category" in admin_src
        ok("按分类排序")
        assert "## " in admin_src or '"## "' in admin_src  # Markdown heading
        ok("Markdown 分类标题格式")
    except Exception as e:
        fail("Markdown export 检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # 汇总
    # ─────────────────────────────────────────────
    print(f"\n{'='*45}")
    print(f"  结果: {_pass} PASS / {_fail} FAIL")
    print(f"{'='*45}")
    return 0 if _fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
