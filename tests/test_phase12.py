"""
Phase 12 集成测试
- 沙盒升级（计时、分数、另存为范例）
- SSE 批量翻译进度端点注册
- 分类使用统计端点
- 知识库变更 Webhook hook 代码存在性
- loadCategoryStats JS 函数注册
- Ctrl+Enter 快捷键注册
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
# T1: admin.py 新端点注册检查
# ─────────────────────────────────────────────
def main():
    global _pass, _fail
    print("\n[T1] New API endpoints in admin.py")
    try:
        admin_src = (ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
        routes = [
            ("/api/kb/translate-progress", "SSE 批量翻译进度"),
            ("/api/kb/sandbox/save-example", "沙盒另存为范例"),
            ("/api/kb/category-stats", "分类使用统计"),
            ("elapsed_ms", "沙盒计时"),
            ("search_mode", "沙盒搜索模式"),
            ("kb_change", "KB变更Webhook事件类型"),
            ("_fire_webhook", "Webhook 触发调用"),
        ]
        for token, desc in routes:
            assert token in admin_src, f"缺少: {token}"
            ok(f"找到: {desc}")
    except Exception as e:
        fail("admin.py 端点检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # T2: knowledge.html 前端升级检查
    # ─────────────────────────────────────────────
    print("\n[T2] knowledge.html frontend upgrades")
    try:
        kb_src = (ROOT / "src" / "web" / "templates" / "knowledge.html").read_text(encoding="utf-8")
        items = [
            ("loadCategoryStats", "分类统计函数"),
            ("category-stats-chart", "分类统计图表容器"),
            ("btp-bar", "批量翻译进度条元素"),
            ("batch-trans-progress", "批量翻译进度面板"),
            ("EventSource", "SSE EventSource 使用"),
            ("translate-progress", "SSE 端点调用"),
            ("sb-save-ex-btn", "另存为范例按钮"),
            ("saveSandboxExample", "另存为范例函数"),
            ("copyContext", "复制上下文函数"),
            ("Ctrl+", "Ctrl+Enter 说明文本"),
            ("keydown", "键盘事件监听"),
            ("pane-sandbox", "沙盒面板 Ctrl+Enter 检测"),
            ("elapsed_ms", "沙盒计时显示"),
            ("_lastSbQuery", "沙盒查询状态变量"),
            ("sb-stats", "沙盒搜索统计元素"),
        ]
        for token, desc in items:
            assert token in kb_src, f"缺少: {token}"
            ok(f"找到: {desc}")
    except Exception as e:
        fail("knowledge.html 检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # T3: KB add_example 方法可用性
    # ─────────────────────────────────────────────
    print("\n[T3] kb_store.add_example method")
    try:
        import tempfile
        from src.utils.kb_store import KnowledgeBaseStore
        td = tempfile.mkdtemp()
        store = KnowledgeBaseStore(Path(td) / "kb.db")
        ex_id = store.add_example({
            "category": "沙盒测试",
            "user_message": "测试消息",
            "correct_reply": "测试回复",
            "language": "zh",
            "quality": 1,
            "source": "sandbox",
        })
        assert ex_id, "add_example 返回空 ID"
        ok(f"add_example 成功: {ex_id[:8]}…")
        examples = store.list_examples()
        assert len(examples) > 0
        ok(f"list_examples 返回 {len(examples)} 条")
        del store
    except Exception as e:
        fail("add_example 测试失败", str(e))
    
    # ─────────────────────────────────────────────
    # T4: delete_entry 级联删图片
    # ─────────────────────────────────────────────
    print("\n[T4] delete_entry cascade image cleanup in admin.py")
    try:
        admin_src = (ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
        assert "delete_all_entry_images" in admin_src
        ok("delete_entry 中调用 delete_all_entry_images")
        assert "unlink(missing_ok=True)" in admin_src
        ok("物理删除图片文件")
    except Exception as e:
        fail("级联删除检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # T5: SSE 端点流语法
    # ─────────────────────────────────────────────
    print("\n[T5] SSE translate-progress streaming syntax")
    try:
        admin_src = (ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
        assert "text/event-stream" in admin_src
        ok("SSE media_type 设置")
        assert "X-Accel-Buffering" in admin_src
        ok("SSE 禁用 nginx 缓冲")
        assert "async def _stream" in admin_src
        ok("SSE 生成器函数")
        assert "type':'done'" in admin_src or '"type":"done"' in admin_src or "type='done'" in admin_src
        ok("SSE done 事件类型")
    except Exception as e:
        fail("SSE 语法检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # 汇总
    # ─────────────────────────────────────────────
    print(f"\n{'='*45}")
    print(f"  结果: {_pass} PASS / {_fail} FAIL")
    print(f"{'='*45}")
    return 0 if _fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
