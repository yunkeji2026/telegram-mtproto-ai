# -*- coding: utf-8 -*-
"""mobile_bridge 核心逻辑单元测试。

覆盖：
- _sync_one 幂等写入（trace_id 去重）
- watermark 只推进成功行
- count_by_state
- enqueue_writeback + _drain_writeback_queue（含幂等 409 处理）
- retry_dead_letter
- list_writeback_queue
- status() 包含 dead_letter_count
"""
import sqlite3
import time
import unittest
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── 轻量 Store 桩，只实现 bridge 用到的接口 ──────────────────────────
def _make_mock_store(tmp_path: Path):
    """在 tmp_path 建一个真实 contacts.db（含完整 bridge DDL），
    返回一个能被 MobileBridgeService 使用的 store 桩。"""
    db_path = tmp_path / "contacts.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # bridge 需要的三张表
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS journey_events (
        event_id     TEXT PRIMARY KEY,
        journey_id   TEXT NOT NULL,
        trace_id     TEXT NOT NULL DEFAULT '',
        event_type   TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        ts           INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_events_trace ON journey_events(trace_id)
        WHERE trace_id != '';

    CREATE TABLE IF NOT EXISTS mobile_bridge_sync_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS mobile_bridge_writeback_queue (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        handoff_id    TEXT NOT NULL,
        action        TEXT NOT NULL,
        by_user       TEXT NOT NULL DEFAULT '',
        notes         TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'pending',
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT DEFAULT '',
        next_retry_at REAL NOT NULL DEFAULT 0,
        created_at    REAL NOT NULL DEFAULT (unixepoch()),
        delivered_at  REAL
    );
    """)
    conn.commit()

    store = SimpleNamespace()
    store._lock = Lock()
    store._conn = conn

    # ensure_channel_identity stub
    contact = SimpleNamespace(contact_id="cid-001")
    ci = SimpleNamespace()
    store.ensure_channel_identity = MagicMock(return_value=(contact, ci, True))

    # get_journey_by_contact stub
    journey = SimpleNamespace(journey_id="jid-001")
    store.get_journey_by_contact = MagicMock(return_value=journey)

    # append_event stub — 写入真实 DB 以便 trace_id 检验
    def _append_event(*, journey_id, event_type, payload=None, trace_id=""):
        import uuid
        eid = str(uuid.uuid4())
        with store._lock:
            conn.execute(
                "INSERT INTO journey_events(event_id,journey_id,trace_id,event_type,payload_json,ts)"
                " VALUES(?,?,?,?,?,?)",
                (eid, journey_id, trace_id, event_type, "{}", int(time.time())),
            )
            conn.commit()
        return eid

    store.append_event = _append_event
    return store


# ── 辅助：构建 MobileBridgeService（不启动 async loop） ──────────────
def _make_bridge(tmp_path: Path, openclaw_path: str = "nonexistent.db"):
    from src.contacts.mobile_bridge import MobileBridgeService
    store = _make_mock_store(tmp_path)
    bridge = MobileBridgeService(
        contacts_store=store,
        openclaw_db_path=openclaw_path,
        mobile_api_base="http://127.0.0.1:19999",
        poll_interval_sec=999,
    )
    return bridge, store


# ── 辅助：构建最简 openclaw.db ────────────────────────────────────────
def _make_openclaw(tmp_path: Path, rows: list) -> str:
    path = tmp_path / "openclaw.db"
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE lead_handoffs (
            handoff_id       TEXT PRIMARY KEY,
            canonical_id     TEXT,
            state            TEXT,
            channel          TEXT DEFAULT '',
            source_agent     TEXT DEFAULT '',
            source_device    TEXT DEFAULT '',
            target_agent     TEXT DEFAULT '',
            snippet_sent     TEXT DEFAULT '',
            state_notes      TEXT DEFAULT '',
            state_updated_at TEXT,
            created_at       TEXT
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO lead_handoffs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (r["handoff_id"], r.get("canonical_id","cid"),
             r.get("state","pending"), r.get("channel",""),
             r.get("source_agent",""), r.get("source_device",""),
             r.get("target_agent",""), r.get("snippet_sent",""),
             r.get("state_notes",""), r.get("state_updated_at","2024-01-01T00:00:00Z"),
             r.get("created_at","2024-01-01T00:00:00Z")),
        )
    conn.commit()
    conn.close()
    return str(path)


# ═══════════════════════════════ Tests ═══════════════════════════════

class TestSyncOneIdempotency:
    """_sync_one 对相同 (handoff_id, state) 只写一次 journey_event。"""

    def test_first_sync_writes_event(self, tmp_path):
        bridge, store = _make_bridge(tmp_path)
        row = {"handoff_id": "h1", "canonical_id": "cid1", "state": "pending",
               "state_updated_at": "2024-01-02T00:00:00Z"}
        assert bridge._sync_one(row) is True
        with store._lock:
            cnt = store._conn.execute(
                "SELECT COUNT(*) FROM journey_events WHERE trace_id=?",
                ("mobile_bridge:h1:pending",)
            ).fetchone()[0]
        assert cnt == 1

    def test_second_sync_is_noop(self, tmp_path):
        bridge, store = _make_bridge(tmp_path)
        row = {"handoff_id": "h1", "canonical_id": "cid1", "state": "pending",
               "state_updated_at": "2024-01-02T00:00:00Z"}
        bridge._sync_one(row)
        bridge._sync_one(row)   # 第二次应跳过
        with store._lock:
            cnt = store._conn.execute(
                "SELECT COUNT(*) FROM journey_events WHERE trace_id=?",
                ("mobile_bridge:h1:pending",)
            ).fetchone()[0]
        assert cnt == 1, "同一 trace_id 不应写入两次"

    def test_different_states_write_separate_events(self, tmp_path):
        bridge, store = _make_bridge(tmp_path)
        for state in ("pending", "acknowledged", "completed"):
            row = {"handoff_id": "h2", "canonical_id": "cid2", "state": state,
                   "state_updated_at": f"2024-01-0{state[:1]}T00:00:00Z"}
            bridge._sync_one(row)
        with store._lock:
            cnt = store._conn.execute(
                "SELECT COUNT(*) FROM journey_events WHERE journey_id=?",
                ("jid-001",)
            ).fetchone()[0]
        assert cnt == 3


class TestWatermark:
    """watermark 只在成功行时推进。"""

    def test_watermark_advances_on_success(self, tmp_path):
        oc_path = _make_openclaw(tmp_path, [
            {"handoff_id": "h1", "canonical_id": "c1", "state": "pending",
             "state_updated_at": "2024-06-01T10:00:00Z"},
        ])
        bridge, _ = _make_bridge(tmp_path, openclaw_path=oc_path)
        bridge._sync_batch()
        assert bridge._get_watermark() == "2024-06-01T10:00:00Z"

    def test_watermark_stays_on_all_fail(self, tmp_path):
        oc_path = _make_openclaw(tmp_path, [
            {"handoff_id": "h1", "canonical_id": "", "state": "pending",  # canonical_id 空 → 失败
             "state_updated_at": "2024-06-01T10:00:00Z"},
        ])
        bridge, _ = _make_bridge(tmp_path, openclaw_path=oc_path)
        bridge._sync_batch()
        assert bridge._get_watermark() == "1970-01-01T00:00:00Z", "全部失败时 watermark 不应前进"


class TestWritebackQueue:
    """enqueue_writeback + _drain_writeback_queue + retry_dead_letter。"""

    def test_enqueue_returns_positive_id(self, tmp_path):
        bridge, _ = _make_bridge(tmp_path)
        rid = bridge.enqueue_writeback("h-abc", "acknowledge", by="admin")
        assert rid > 0

    def test_drain_delivers_on_success(self, tmp_path):
        bridge, store = _make_bridge(tmp_path)
        bridge.enqueue_writeback("h-ok", "complete", by="admin")
        with patch.object(bridge, "_call_mobile_api", return_value={"ok": True}):
            ok, fail = bridge._drain_writeback_queue()
        assert ok == 1 and fail == 0
        with store._lock:
            row = store._conn.execute(
                "SELECT status FROM mobile_bridge_writeback_queue WHERE handoff_id='h-ok'"
            ).fetchone()
        assert row[0] == "delivered"

    def test_drain_increments_attempt_on_failure(self, tmp_path):
        bridge, store = _make_bridge(tmp_path)
        bridge.enqueue_writeback("h-fail", "reject", by="admin")
        with patch.object(bridge, "_call_mobile_api", side_effect=RuntimeError("timeout")):
            ok, fail = bridge._drain_writeback_queue()
        assert ok == 0 and fail == 1
        with store._lock:
            row = store._conn.execute(
                "SELECT status, attempt_count FROM mobile_bridge_writeback_queue WHERE handoff_id='h-fail'"
            ).fetchone()
        assert row[0] == "pending"
        assert row[1] == 1

    def test_dead_letter_after_max_attempts(self, tmp_path):
        from src.contacts.mobile_bridge import _MAX_ATTEMPTS
        bridge, store = _make_bridge(tmp_path)
        bridge.enqueue_writeback("h-dead", "acknowledge", by="admin")
        # 强制 attempt_count 到最大次数 - 1
        with store._lock:
            store._conn.execute(
                "UPDATE mobile_bridge_writeback_queue SET attempt_count=? WHERE handoff_id='h-dead'",
                (_MAX_ATTEMPTS - 1,)
            )
            store._conn.commit()
        with patch.object(bridge, "_call_mobile_api", side_effect=RuntimeError("gone")):
            bridge._drain_writeback_queue()
        with store._lock:
            row = store._conn.execute(
                "SELECT status FROM mobile_bridge_writeback_queue WHERE handoff_id='h-dead'"
            ).fetchone()
        assert row[0] == "dead_letter"

    def test_409_treated_as_idempotent_success(self, tmp_path):
        import urllib.error
        bridge, store = _make_bridge(tmp_path)
        bridge.enqueue_writeback("h-409", "complete", by="admin")

        class FakeHTTPError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("url", 409, "Conflict", {}, None)
            def read(self): return b'{"detail":"already completed"}'

        with patch("urllib.request.urlopen", side_effect=FakeHTTPError()):
            ok, fail = bridge._drain_writeback_queue()
        assert ok == 1 and fail == 0

    def test_retry_dead_letter_resets_to_pending(self, tmp_path):
        bridge, store = _make_bridge(tmp_path)
        bridge.enqueue_writeback("h-dl", "reject", by="admin")
        with store._lock:
            store._conn.execute(
                "UPDATE mobile_bridge_writeback_queue SET status='dead_letter', attempt_count=5"
                " WHERE handoff_id='h-dl'"
            )
            store._conn.commit()
        ok = bridge.retry_dead_letter(1)
        assert ok is True
        with store._lock:
            row = store._conn.execute(
                "SELECT status, attempt_count FROM mobile_bridge_writeback_queue WHERE id=1"
            ).fetchone()
        assert row[0] == "pending"
        assert row[1] == 0


class TestStatus:
    """status() 正确汇总 dead_letter 和 pending 数量。"""

    def test_status_ok_when_no_dead_letters(self, tmp_path):
        bridge, _ = _make_bridge(tmp_path)
        s = bridge.status()
        assert s["ok"] is True
        assert s["writeback_dead_letter"] == 0

    def test_status_not_ok_when_dead_letter_exists(self, tmp_path):
        bridge, store = _make_bridge(tmp_path)
        bridge.enqueue_writeback("h-dl", "acknowledge")
        with store._lock:
            store._conn.execute(
                "UPDATE mobile_bridge_writeback_queue SET status='dead_letter'"
            )
            store._conn.commit()
        s = bridge.status()
        assert s["ok"] is False
        assert s["writeback_dead_letter"] == 1


class TestCountByState:
    """count_by_state 从 openclaw.db 聚合。"""

    def test_count_returns_correct_state_counts(self, tmp_path):
        oc_path = _make_openclaw(tmp_path, [
            {"handoff_id": "h1", "state": "pending", "state_updated_at": "2024-01-01T00:00:00Z"},
            {"handoff_id": "h2", "state": "pending", "state_updated_at": "2024-01-01T00:00:01Z"},
            {"handoff_id": "h3", "state": "completed", "state_updated_at": "2024-01-01T00:00:02Z"},
        ])
        bridge, _ = _make_bridge(tmp_path, openclaw_path=oc_path)
        counts = bridge.count_by_state()
        assert counts.get("pending") == 2
        assert counts.get("completed") == 1

    def test_count_returns_empty_when_db_missing(self, tmp_path):
        bridge, _ = _make_bridge(tmp_path, openclaw_path="no_such.db")
        assert bridge.count_by_state() == {}
