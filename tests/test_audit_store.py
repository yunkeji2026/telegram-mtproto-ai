"""AuditStore SQLite + JSONL 迁移 测试"""

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.audit_store import AuditStore


@pytest.fixture
def store(tmp_path):
    return AuditStore(db_path=tmp_path / "audit.db")


class TestBasicOps:
    def test_log_and_query(self, store):
        store.log("u1", "update_template", "greeting", "old", "new")
        entries = store.query(limit=10)
        assert len(entries) == 1
        assert entries[0]["action"] == "update_template"
        assert entries[0]["user_id"] == "u1"

    def test_last_entry(self, store):
        store.log("u1", "a1")
        store.log("u2", "a2")
        last = store.last_entry()
        assert last["action"] == "a2"
        assert last["user_id"] == "u2"

    def test_query_filter_action(self, store):
        store.log("u1", "update_rate")
        store.log("u1", "rollback")
        store.log("u1", "update_rate")
        results = store.query(action="update_rate")
        assert all(r["action"] == "update_rate" for r in results)
        assert len(results) == 2

    def test_query_filter_user(self, store):
        store.log("admin", "a1")
        store.log("other", "a2")
        results = store.query(user_id="admin")
        assert len(results) == 1
        assert results[0]["user_id"] == "admin"

    def test_query_limit(self, store):
        for i in range(20):
            store.log("u1", f"action_{i}")
        assert len(store.query(limit=5)) == 5

    def test_empty_query(self, store):
        assert store.query() == []
        assert store.last_entry() is None


class TestJSONLMigration:
    def test_migrate_from_jsonl(self, tmp_path):
        jsonl = tmp_path / "audit_log.jsonl"
        entries = [
            {"ts": "2026-01-01 10:00:00", "user": "admin", "action": "update_rate",
             "target": "ep", "old": "0.5%", "new": "1.0%", "snap": "exchange_rates_111"},
            {"ts": "2026-01-01 10:01:00", "user": "admin", "action": "rollback",
             "target": "", "old": "", "new": "", "snap": ""},
        ]
        jsonl.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries), encoding="utf-8")

        store = AuditStore(db_path=tmp_path / "audit.db", legacy_jsonl_path=jsonl)
        results = store.query(limit=100)
        assert len(results) == 2
        assert results[0]["action"] == "update_rate"
        assert results[0]["snapshot_id"] == "exchange_rates_111"
        assert not jsonl.exists()
        assert (tmp_path / "audit_log.jsonl.bak").exists()

    def test_no_double_migrate(self, tmp_path):
        jsonl = tmp_path / "audit_log.jsonl"
        jsonl.write_text(json.dumps({"ts": "", "user": "u", "action": "a",
                                     "target": "", "old": "", "new": "", "snap": ""}), encoding="utf-8")
        store = AuditStore(db_path=tmp_path / "audit.db", legacy_jsonl_path=jsonl)
        assert len(store.query()) == 1
        (tmp_path / "audit_log.jsonl.bak").rename(jsonl)
        store2 = AuditStore(db_path=tmp_path / "audit.db", legacy_jsonl_path=jsonl)
        assert len(store2.query()) == 1
