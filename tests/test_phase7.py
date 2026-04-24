"""Phase 7 integration tests: query log, hit rate analytics, embed stats"""
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from utils.kb_store import KnowledgeBaseStore


def test_query_log_and_analytics(tmp_path):
    db = tmp_path / "test.db"
    store = KnowledgeBaseStore(db)

    # Add test entry
    eid = store.add_entry({
        "title": "查询订单",
        "category": "订单查询",
        "triggers": ["查单"],
        "scenario": "", "steps": "", "principles": "",
    })

    # Log 5 hits + 3 misses
    for i in range(5):
        store.log_query("查单", hit=True, search_mode="bm25", category="订单查询", lang="zh")
    for i in range(3):
        store.log_query("随便问的", hit=False, search_mode="bm25", category="", lang="zh")
    for i in range(2):
        store.log_query("查单同义词", hit=True, search_mode="hybrid", category="订单查询", lang="zh")

    # Get analytics
    result = store.get_query_analytics(hours=24)
    totals = result["totals"]

    assert totals["total"] == 10, f"Expected 10 queries, got {totals['total']}"
    assert totals["hits"] == 7, f"Expected 7 hits, got {totals['hits']}"
    assert totals["hit_pct"] == 70, f"Expected 70%, got {totals['hit_pct']}"
    assert totals["hybrid"] == 2, f"Expected 2 hybrid, got {totals['hybrid']}"
    assert totals["hybrid_pct"] == 20
    assert len(totals["top_categories"]) > 0
    assert totals["top_categories"][0]["cat"] == "订单查询"

    print(f"query analytics: {totals}")
    print("test_query_log_and_analytics PASSED")


def test_today_hit_rate(tmp_path):
    db = tmp_path / "test.db"
    store = KnowledgeBaseStore(db)

    store.log_query("q1", hit=True)
    store.log_query("q2", hit=True)
    store.log_query("q3", hit=False)

    hr = store.get_today_hit_rate()
    assert hr["total"] == 3
    assert hr["hits"] == 2
    assert hr["hit_pct"] == 67

    print(f"today hit rate: {hr}")
    print("test_today_hit_rate PASSED")


def test_rolling_cleanup(tmp_path):
    """7-day rolling cleanup: old records should be deleted"""
    db = tmp_path / "test.db"
    store = KnowledgeBaseStore(db)

    # Insert an old record directly
    import sqlite3
    conn = sqlite3.connect(str(db))
    old_ts = time.time() - 8 * 86400  # 8 days ago
    conn.execute(
        "INSERT INTO kb_query_log (id,query,hit,search_mode,category,lang,ts) "
        "VALUES (?,?,?,?,?,?,?)",
        ("old1", "old_query", 1, "bm25", "", "zh", old_ts)
    )
    conn.commit()
    conn.close()

    # Insert a new record - triggers cleanup
    store.log_query("new_query", hit=True)

    # Old record should be gone
    hr = store.get_today_hit_rate()
    # only new_query is within 24h
    assert hr["total"] == 1, f"Old records should have been cleaned up, got {hr['total']}"

    print("test_rolling_cleanup PASSED")


if __name__ == "__main__":
    import tempfile
    td = pathlib.Path("C:/Users/victor002/AppData/Local/Temp/kb_phase7_test")
    td.mkdir(exist_ok=True)

    for i, fn in enumerate([test_query_log_and_analytics,
                             test_today_hit_rate,
                             test_rolling_cleanup]):
        d = td / str(i)
        d.mkdir(exist_ok=True)
        fn(d)

    print("\n=== ALL PHASE 7 TESTS PASSED ===")
