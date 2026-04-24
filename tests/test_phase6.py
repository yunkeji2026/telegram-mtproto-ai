"""Phase 6 integration tests: version snapshot, hybrid search context builder"""
import sys
import pathlib
import json

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from utils.kb_store import KnowledgeBaseStore


def test_version_snapshot(tmp_path):
    db = tmp_path / "test.db"
    store = KnowledgeBaseStore(db)

    eid = store.add_entry({
        "title": "订单查询",
        "category": "订单查询",
        "triggers": ["查单"],
        "scenario": "",
        "steps": "步骤1",
        "principles": "",
        "example_reply_zh": "",
    })

    # 1. 手动保存快照
    vid1 = store.save_version(eid, editor="admin")
    assert vid1 is not None, "save_version should return version id"

    # 2. 更新条目
    store.update_entry(eid, {"title": "已更新标题", "steps": "新步骤"})

    # 3. 列出版本
    vers = store.list_versions(eid)
    assert len(vers) >= 1, "should have at least 1 version"

    # 4. 快照内容正确
    ver = store.get_version(vid1)
    assert ver is not None
    snap = ver["snapshot"]
    assert snap["title"] == "订单查询", f"snapshot wrong: {snap['title']}"

    # 5. 恢复版本
    ok = store.restore_version(vid1, editor="admin")
    assert ok

    entry = store.get_entry(eid)
    assert entry["title"] == "订单查询", f"restored title wrong: {entry['title']}"

    print("version snapshot tests PASSED")


def test_build_ai_context_from_result(tmp_path):
    db = tmp_path / "test.db"
    store = KnowledgeBaseStore(db)

    store.add_entry({
        "title": "查询余额",
        "category": "余额汇率",
        "triggers": ["余额", "钱"],
        "scenario": "用户询问余额",
        "steps": "告知余额查询入口",
        "principles": "不透露具体金额",
        "example_reply_zh": "请在 App 中查看余额",
    })

    result = store.search("余额查询")
    ctx = store.build_ai_context_from_result(result)
    assert "查询余额" in ctx or "余额" in ctx, f"context should contain kb entry: {ctx[:100]}"

    # search_mode should be in context
    assert len(ctx) > 0

    print("build_ai_context_from_result tests PASSED")


def test_version_limit(tmp_path):
    """每个条目最多保留 10 个版本"""
    db = tmp_path / "test.db"
    store = KnowledgeBaseStore(db)

    eid = store.add_entry({
        "title": "测试", "category": "其他",
        "triggers": [], "scenario": "", "steps": "", "principles": "",
    })

    for i in range(15):
        store.save_version(eid, editor=f"editor_{i}")

    vers = store.list_versions(eid)
    assert len(vers) <= 10, f"should keep max 10 versions, got {len(vers)}"
    print(f"version limit test PASSED (kept {len(vers)} versions)")


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tp = pathlib.Path(td)
        test_version_snapshot(tp / "t1")
        test_build_ai_context_from_result(tp / "t2")
        test_version_limit(tp / "t3")
    print("\n=== ALL PHASE 6 TESTS PASSED ===")
