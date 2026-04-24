"""Phase 8 integration tests: export/import, maintenance advice, health score"""
import sys
import pathlib
import json

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from utils.kb_store import KnowledgeBaseStore


def _fresh(tmp_path, name="test.db") -> KnowledgeBaseStore:
    db = tmp_path / name
    if db.exists():
        db.unlink()
    return KnowledgeBaseStore(db)


def test_export_import_roundtrip(tmp_path):
    src = _fresh(tmp_path, "src.db")

    # Seed data
    src.add_entry({
        "title": "查询订单",
        "category": "订单查询",
        "triggers": ["查单", "查订单"],
        "scenario": "用户查询",
        "steps": "请提供订单号",
        "principles": "保持耐心",
        "example_reply_zh": "您好，请告知订单号",
    })
    src.add_entry({
        "title": "余额查询",
        "category": "余额汇率",
        "triggers": ["余额"],
        "scenario": "", "steps": "告知入口", "principles": "", "example_reply_zh": "",
    })
    src.add_error_code({
        "code": "X999",
        "explanation_zh": "测试错误码",
        "suggestion_zh": "联系客服",
    })

    # Export
    data = src.export_all()
    assert data["version"] == "1.0"
    assert len(data["entries"]) == 2
    assert len(data["error_codes"]) == 1
    assert all("embedding" not in e for e in data["entries"])
    print(f"export: {len(data['entries'])} entries, {len(data['error_codes'])} error codes")

    # Import into new DB
    dst = _fresh(tmp_path, "dst.db")
    result = dst.import_from_data(data, mode="skip")
    assert result["added"] == 3   # 2 entries + 1 error code
    assert result["skipped"] == 0
    print(f"import: {result}")

    # Verify imported
    entries = dst.list_entries()
    assert len(entries) == 2

    # Import again (skip mode) - should skip all
    result2 = dst.import_from_data(data, mode="skip")
    assert result2["added"] == 0
    assert result2["skipped"] >= 2
    print(f"re-import skip mode: {result2}")

    # Update mode
    data["entries"][0]["steps"] = "新步骤"
    result3 = dst.import_from_data(data, mode="update")
    assert result3["updated"] >= 1
    print(f"update mode: {result3}")

    print("test_export_import_roundtrip PASSED")


def test_import_dedup_by_title(tmp_path):
    """导入时按 title 去重，ID 不同但 title 相同 → 视为重复"""
    store = _fresh(tmp_path)
    store.add_entry({
        "id": "old_id_111",
        "title": "重复条目测试",
        "category": "其他",
        "triggers": [],
        "scenario": "", "steps": "原始步骤", "principles": "",
    })

    # Try to import a different ID with same title
    data = {
        "version": "1.0",
        "entries": [{
            "id": "new_id_222",    # Different ID
            "title": "重复条目测试",  # Same title
            "category": "其他",
            "triggers": [],
            "scenario": "", "steps": "新步骤", "principles": "",
        }],
    }
    result = store.import_from_data(data, mode="skip")
    assert result["skipped"] == 1, f"Should skip duplicate title, got {result}"
    print("test_import_dedup_by_title PASSED")


def test_maintenance_advice(tmp_path):
    store = _fresh(tmp_path)

    # Entry with NO triggers (should be HIGH priority)
    store.add_entry({
        "title": "无触发词条目",
        "category": "其他",
        "triggers": [],   # Empty!
        "scenario": "", "steps": "步骤", "principles": "",
    })

    # Normal entry
    store.add_entry({
        "title": "正常条目",
        "category": "订单查询",
        "triggers": ["查单"],
        "scenario": "正常", "steps": "正常步骤", "principles": "正常",
    })

    advice_data = store.get_maintenance_advice()
    assert "score" in advice_data
    assert "advice" in advice_data
    assert "grade" in advice_data
    assert advice_data["score"] <= 100

    high_advice = [a for a in advice_data["advice"] if a["priority"] == "high"]
    assert len(high_advice) >= 1, "Should flag entry with no triggers as HIGH"
    assert high_advice[0]["type"] == "no_triggers"

    print(f"health score: {advice_data['score']} ({advice_data['grade']})")
    print(f"advice items: {len(advice_data['advice'])}")
    print("test_maintenance_advice PASSED")


def test_health_score_calculation(tmp_path):
    """健康分应随问题增多而下降"""
    store = _fresh(tmp_path)

    # Perfect KB: one well-configured entry
    store.add_entry({
        "title": "完美条目",
        "category": "订单查询",
        "triggers": ["查单", "查订单"],
        "scenario": "用户查询订单",
        "steps": "提供订单号后查询系统",
        "principles": "保持礼貌",
        "example_reply_zh": "请提供订单号",
    })

    data = store.get_maintenance_advice()
    initial_score = data["score"]

    # Add problematic entries
    for i in range(3):
        store.add_entry({
            "title": f"问题条目{i}",
            "category": "其他",
            "triggers": [],  # No triggers - HIGH priority
            "scenario": "", "steps": "", "principles": "",
        })

    data2 = store.get_maintenance_advice()
    assert data2["score"] < initial_score, \
        f"Score should decrease after adding problematic entries: {initial_score} -> {data2['score']}"
    print(f"score with problems: {data2['score']} (was {initial_score})")
    print("test_health_score_calculation PASSED")


if __name__ == "__main__":
    import tempfile

    td = pathlib.Path("C:/Users/victor002/AppData/Local/Temp/kb_phase8_test")
    td.mkdir(exist_ok=True)

    for i, fn in enumerate([
        test_export_import_roundtrip,
        test_import_dedup_by_title,
        test_maintenance_advice,
        test_health_score_calculation,
    ]):
        d = td / str(i)
        d.mkdir(exist_ok=True)
        fn(d)

    print("\n=== ALL PHASE 8 TESTS PASSED ===")
