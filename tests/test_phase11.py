"""
Phase 11 集成测试
- 全局系统配置 (settings) 路由 & 字段过滤
- 知识库分析报告 (kb_report) HTML 生成
- 知识条目图片附件 CRUD (db layer)
- 种子数据导入 (seed_kb_examples)
"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ─────────────────────────────────────────────
# 辅助工具
# ─────────────────────────────────────────────
_pass = 0
_fail = 0

def ok(msg):
    global _pass; _pass += 1
    print(f"  PASS  {msg}")

def fail(msg, exc=""):
    global _fail; _fail += 1
    print(f"  FAIL  {msg}" + (f"  [{exc}]" if exc else ""))

# ─────────────────────────────────────────────
# T1: kb_report 生成
# ─────────────────────────────────────────────
def main():
    global _pass, _fail
    print("\n[T1] KB Report HTML generation")
    try:
        import tempfile
        from src.utils.kb_store import KnowledgeBaseStore
        td_obj = tempfile.mkdtemp()
        td = Path(td_obj)
        store = KnowledgeBaseStore(td / "kb.db")
        store.add_entry({
            "title": "测试条目", "category": "测试",
            "triggers": json.dumps(["test"]),
            "example_reply_zh": "这是一条测试回复",
        })
        from src.web.kb_report import build_kb_report
        html = build_kb_report(store, audit_store=None)
        del store  # 释放 SQLite 连接
        assert "<!DOCTYPE html>" in html, "缺少 HTML 声明"
        ok("kb_report 生成了合法 HTML")
        assert "知识库分析报告" in html
        ok("标题包含正确文本")
        assert "打印" in html or "print" in html.lower()
        ok("包含打印按钮")
        assert "<svg" in html
        ok("包含 SVG 图表")
    except Exception as e:
        fail("kb_report 生成异常", str(e))
    
    # ─────────────────────────────────────────────
    # T2: 图片附件数据层
    # ─────────────────────────────────────────────
    print("\n[T2] KB image attachment DB layer")
    try:
        import tempfile
        from src.utils.kb_store import KnowledgeBaseStore
        td2 = tempfile.mkdtemp()
        store = KnowledgeBaseStore(Path(td2) / "kb2.db")
        eid = store.add_entry({
            "title": "含图条目", "category": "测试",
            "triggers": json.dumps([]),
        })
        ok(f"创建测试条目: {eid[:8]}…")
    
        img_id = store.add_entry_image(eid, "test.png", "说明", 1024)
        ok(f"add_entry_image: {img_id[:8]}…")
    
        imgs = store.get_entry_images(eid)
        assert len(imgs) == 1
        assert imgs[0]["filename"] == "test.png"
        assert imgs[0]["size_bytes"] == 1024
        ok("get_entry_images 返回正确数据")
    
        fname = store.delete_entry_image(img_id)
        assert fname == "test.png"
        ok(f"delete_entry_image 返回文件名: {fname}")
    
        imgs_after = store.get_entry_images(eid)
        assert len(imgs_after) == 0
        ok("删除后图片列表为空")
    
        store.add_entry_image(eid, "a.jpg", "", 512)
        store.add_entry_image(eid, "b.jpg", "", 512)
        names = store.delete_all_entry_images(eid)
        assert set(names) == {"a.jpg", "b.jpg"}
        ok(f"delete_all_entry_images 返回 {len(names)} 个文件名")
        del store
    except Exception as e:
        fail("图片附件数据层异常", str(e))
    
    # ─────────────────────────────────────────────
    # T3: 种子数据导入
    # ─────────────────────────────────────────────
    print("\n[T3] Seed data import")
    try:
        import tempfile
        from src.utils.kb_store import KnowledgeBaseStore, seed_kb_examples
        td3 = tempfile.mkdtemp()
        store = KnowledgeBaseStore(Path(td3) / "kb3.db")
    
        res = seed_kb_examples(store, "ecommerce")
        assert res["added"] > 0, f"导入数量为 0"
        ok(f"ecommerce 导入: added={res['added']}, skipped={res['skipped']}")
    
        res2 = seed_kb_examples(store, "ecommerce")
        assert res2["added"] == 0 and res2["skipped"] > 0
        ok(f"重复导入全部跳过: skipped={res2['skipped']}")
    
        res3 = seed_kb_examples(store, "saas")
        assert res3["added"] > 0
        ok(f"saas 导入: added={res3['added']}")
    
        ecom_count = res["added"]
        saas_count = res3["added"]
        del store
    
        store2 = KnowledgeBaseStore(Path(td3) / "kb4.db")
        res4 = seed_kb_examples(store2, "all")
        assert res4["added"] >= ecom_count + saas_count
        ok(f"all 导入总计: added={res4['added']}")
        del store2
    except Exception as e:
        fail("种子数据导入异常", str(e))
    
    # ─────────────────────────────────────────────
    # T4: settings.html 模板语法
    # ─────────────────────────────────────────────
    print("\n[T4] settings.html template check")
    try:
        tpl_file = ROOT / "src" / "web" / "templates" / "settings.html"
        assert tpl_file.exists(), "settings.html 不存在"
        content = tpl_file.read_text(encoding="utf-8")
        assert "saveSection" in content
        ok("包含 saveSection JS 函数")
        assert "testApiConn" in content
        ok("包含 testApiConn JS 函数")
        assert "testWebhook" in content
        ok("包含 testWebhook JS 函数")
        assert "ai-api_key" in content
        ok("包含 AI Key 字段")
        assert "wb-session_max_age" in content
        ok("包含会话超时字段")
        assert "notif-webhook_url" in content
        ok("包含 Webhook URL 字段")
        assert "tg-process_private" in content
        ok("包含 Bot 私聊开关字段")
    except Exception as e:
        fail("settings.html 检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # T5: 路由注册检查
    # ─────────────────────────────────────────────
    print("\n[T5] API routes registered in admin.py")
    try:
        admin_src = (ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
        routes = [
            ("/settings", "GET /settings route"),
            ("/api/settings/save", "POST /api/settings/save"),
            ("/api/settings/test-webhook", "GET /api/settings/test-webhook"),
            ("/api/kb/report", "GET /api/kb/report"),
            ("/kb-images/{filename}", "GET /kb-images static"),
            ("/api/kb/entries/{entry_id}/images", "POST image upload"),
            ("/api/kb/images/{img_id}", "DELETE image"),
            ("/api/kb/seed", "POST seed data"),
        ]
        for route, desc in routes:
            assert route in admin_src, f"缺少路由: {route}"
            ok(f"找到路由: {desc}")
    except Exception as e:
        fail("路由注册检查失败", str(e))
    
    # ─────────────────────────────────────────────
    # 汇总
    # ─────────────────────────────────────────────
    print(f"\n{'='*45}")
    print(f"  结果: {_pass} PASS / {_fail} FAIL")
    print(f"{'='*45}")
    return 0 if _fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
