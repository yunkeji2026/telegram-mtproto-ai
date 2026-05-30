"""Tests for src.integrations.rpa_shared — pure helpers used by 3 RPA platforms.

Covers:
- compute_intent_tag: keyword-based classifier with word-boundary fix (P11-C)
- extract_chat_name: chat_key → display name extraction (P12-A)
- count_runs_for_chat_name: cross-platform identity match SQL (P12-A)
- compute_intent_stats: P10-C intent distribution
- sessions_from_rows: P7-C session grouping
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from src.integrations.rpa_shared import (
    compute_intent_stats,
    compute_intent_tag,
    count_runs_for_chat_name,
    extract_chat_name,
    sessions_from_rows,
)


# ════════════════════════════════════════════════════════════════════════
# compute_intent_tag — P11-C word-boundary regression
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text,expected",
    [
        # CJK substring matches still work
        ("你好", "greeting"),
        ("订单什么时候发货", "purchase"),
        ("退款投诉", "support"),
        ("请问产品多少钱", "purchase"),  # 多少钱 hits purchase first
        # Word-boundary: short ASCII must NOT match substrings
        ("this is a test", "general"),       # 'hi' inside 'this'
        ("whatsapp not working", "support"), # 'what' inside 'whatsapp' — but 'not working' wins
        ("the show goes on", "general"),     # 'how' inside 'show'
        ("busy day", "general"),             # 'buy' inside 'busy'
        # Word-boundary: ASCII keywords DO match at boundaries
        ("Hi there!", "greeting"),
        ("hello world", "greeting"),
        ("buy this", "purchase"),
        ("how much?", "inquiry"),  # 'how' wins (inquiry checked before greeting)
        # Multilingual greetings
        ("สวัสดี", "greeting"),
        ("xin chào bạn", "greeting"),
        ("안녕", "greeting"),
        # Empty / None-ish
        ("", "general"),
        ("   ", "general"),
        # Mixed
        ("My package is broken", "support"),
        ("refund please", "support"),
    ],
)
def test_compute_intent_tag(text: str, expected: str) -> None:
    assert compute_intent_tag(text) == expected


# ════════════════════════════════════════════════════════════════════════
# extract_chat_name — P12-A
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "chat_key,expected",
    [
        # Real platform formats
        ("wa:acc_main:Alice", "Alice"),
        ("wa:acc1:Bob Wang", "Bob Wang"),
        ("messenger_rpa:Carol", "Carol"),
        ("acc_X:Dave", "Dave"),
        # LINE user IDs (U + 32 hex)
        ("line:user:U1234567890abcdef1234567890abcdef", ""),
        ("U1234567890abcdef1234567890abcdef", ""),
        # Facebook numeric IDs
        ("fb:user:1000123", ""),
        ("line:user:123456789012", ""),
        # Placeholders
        ("line_rpa:default", ""),
        ("messenger_rpa:unknown", ""),
        ("wa:acc1:anonymous", ""),
        # Edge cases
        ("", ""),
        (":", ""),
        ("noColon", "noColon"),
        ("group_chat:Family Group", "Family Group"),
        # CJK / Thai / Vietnamese names should pass through
        ("wa:acc1:张伟", "张伟"),
        ("wa:acc1:สมชาย", "สมชาย"),
    ],
)
def test_extract_chat_name(chat_key: str, expected: str) -> None:
    assert extract_chat_name(chat_key) == expected


# ════════════════════════════════════════════════════════════════════════
# count_runs_for_chat_name — P12-A
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def runs_db() -> sqlite3.Connection:
    """In-memory DB with a runs-like table seeded for matching tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE runs("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, chat_key TEXT,"
        " peer_text TEXT, intent_tag TEXT DEFAULT 'general'"
        ")"
    )
    now = time.time()
    rows = [
        (now - 10, "wa:acc1:Alice", "hi"),
        (now - 5,  "wa:acc1:Alice", "how much"),
        (now - 3,  "wa:acc2:Alice", "still here"),   # same name, different account
        (now - 8,  "wa:acc1:Bob",   "hello"),
        (now - 6,  "wa:acc1:Bob",   ""),              # empty peer_text → excluded
        (now - 4,  "messenger_rpa:Carol", "hey"),
    ]
    conn.executemany(
        "INSERT INTO runs(ts, chat_key, peer_text) VALUES(?,?,?)", rows
    )
    return conn


def test_count_runs_basic(runs_db: sqlite3.Connection) -> None:
    """Matches across multiple accounts via :name suffix."""
    r = count_runs_for_chat_name(runs_db, "runs", "Alice")
    assert r["total_turns"] == 3  # 2 in acc1 + 1 in acc2
    assert r["last_ts"] > 0
    assert ":Alice" in r["sample_chat_key"]


def test_count_runs_excludes_empty_peer(runs_db: sqlite3.Connection) -> None:
    """peer_text='' rows must be filtered out (consistent with other queries)."""
    r = count_runs_for_chat_name(runs_db, "runs", "Bob")
    assert r["total_turns"] == 1  # only non-empty row


def test_count_runs_empty_name(runs_db: sqlite3.Connection) -> None:
    r = count_runs_for_chat_name(runs_db, "runs", "")
    assert r == {"total_turns": 0, "last_ts": 0.0, "sample_chat_key": ""}


def test_count_runs_no_match(runs_db: sqlite3.Connection) -> None:
    r = count_runs_for_chat_name(runs_db, "runs", "NoSuchPerson")
    assert r["total_turns"] == 0


def test_count_runs_rejects_bad_identifier(runs_db: sqlite3.Connection) -> None:
    """SQL injection guard — table/column names must pass whitelist."""
    with pytest.raises(ValueError):
        count_runs_for_chat_name(runs_db, "runs; DROP TABLE runs--", "x")
    with pytest.raises(ValueError):
        count_runs_for_chat_name(runs_db, "runs", "x", peer_col="bad col")


# ════════════════════════════════════════════════════════════════════════
# compute_intent_stats — P10-C
# ════════════════════════════════════════════════════════════════════════


def test_compute_intent_stats(runs_db: sqlite3.Connection) -> None:
    """All seeded rows fall in 168h window → distribution + total."""
    runs_db.execute("UPDATE runs SET intent_tag='purchase' WHERE chat_key LIKE '%:Alice'")
    runs_db.execute("UPDATE runs SET intent_tag='greeting' WHERE chat_key LIKE '%:Bob' AND peer_text!=''")
    runs_db.execute("UPDATE runs SET intent_tag='general'  WHERE chat_key LIKE '%:Carol'")
    runs_db.commit()
    stats = compute_intent_stats(runs_db, "runs", window_hours=168.0)
    assert stats["total_turns"] == 5  # 3 Alice + 1 Bob (non-empty) + 1 Carol
    assert stats["distribution"]["purchase"] == 3
    assert stats["distribution"]["greeting"] == 1
    assert stats["distribution"]["general"] == 1


def test_compute_intent_stats_rejects_bad_table() -> None:
    with pytest.raises(ValueError):
        compute_intent_stats(None, "DROP TABLE runs--", window_hours=24.0)  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════════════
# sessions_from_rows — P7-C (already battle-tested but lock down contract)
# ════════════════════════════════════════════════════════════════════════


def test_sessions_from_rows_groups_by_gap() -> None:
    """Two clusters separated by > 4h → 2 sessions."""
    now = time.time()
    rows = [
        {"ts": now - 86400, "peer_text": "morning question", "reply_text": "sure",
         "ok": 1, "intent_tag": "inquiry"},
        {"ts": now - 86400 + 60, "peer_text": "follow-up", "reply_text": "yes",
         "ok": 1, "intent_tag": "inquiry"},
        # gap > 4h
        {"ts": now - 3600, "peer_text": "later question", "reply_text": "k",
         "ok": 0, "intent_tag": "purchase"},
    ]
    out = sessions_from_rows(rows, gap_sec=14400)
    assert len(out) == 2
    # reverse-sorted (most-recent session first)
    assert out[0]["dominant_intent"] == "purchase"
    assert out[1]["turn_count"] == 2
    assert out[1]["ok_count"] == 2


def test_sessions_from_rows_empty() -> None:
    assert sessions_from_rows([]) == []
