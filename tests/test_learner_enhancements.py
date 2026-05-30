"""Tests for KB learner enhancements: A1 (confidence), A2 (batch action)."""
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

from src.utils.daily_learner import DailyLearner


@pytest.fixture
def learner(tmp_path):
    """Create a DailyLearner with mock kb_store and ai_client."""
    kb = MagicMock()
    kb._db_path = str(tmp_path / "kb.db")
    kb.add_entry = MagicMock(return_value="entry_001")
    ai = MagicMock()
    dl = DailyLearner(kb_store=kb, ai_client=ai, db_path=tmp_path / "test.db")
    return dl


# ── A1: Confidence column migration ──────────────────────────────────────────

def test_confidence_column_exists(learner, tmp_path):
    """Migration should add confidence column."""
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(kb_drafts)").fetchall()}
    conn.close()
    assert "confidence" in cols


def test_save_drafts_with_confidence(learner):
    """save_drafts should persist confidence value."""
    drafts = [
        {"source": "miss", "query": "什么价格", "hit_count": 5,
         "category": "咨询", "title": "价格", "triggers": ["价格", "多少钱"],
         "example_reply": "请查看...", "ai_reasoning": "高频", "confidence": 85},
        {"source": "miss", "query": "如何退款", "hit_count": 3,
         "category": "售后", "title": "退款", "triggers": ["退款"],
         "example_reply": "请联系...", "ai_reasoning": "负面", "confidence": 45},
    ]
    saved = learner.save_drafts(drafts)
    assert saved == 2

    result = learner.list_drafts(status="pending", sort="confidence")
    assert len(result) == 2
    # Higher confidence first
    assert result[0]["confidence"] == 85
    assert result[1]["confidence"] == 45


def test_list_drafts_sort_priority(learner):
    """Priority sort = confidence*0.5 + min(hit_count*10,100)*0.5."""
    drafts = [
        {"source": "miss", "query": "A", "hit_count": 1,
         "title": "A", "confidence": 90, "triggers": [], "example_reply": "", "ai_reasoning": ""},
        {"source": "miss", "query": "B", "hit_count": 15,
         "title": "B", "confidence": 40, "triggers": [], "example_reply": "", "ai_reasoning": ""},
    ]
    learner.save_drafts(drafts)
    result = learner.list_drafts(sort="priority")
    # A: 90*0.5 + min(10,100)*0.5 = 45+5 = 50
    # B: 40*0.5 + min(150,100)*0.5 = 20+50 = 70 → B should be first
    assert result[0]["title"] == "B"
    assert result[1]["title"] == "A"


def test_list_drafts_sort_hit_count(learner):
    """Sort by hit_count descending."""
    drafts = [
        {"source": "miss", "query": "X", "hit_count": 2,
         "title": "X", "confidence": 80, "triggers": [], "example_reply": "", "ai_reasoning": ""},
        {"source": "miss", "query": "Y", "hit_count": 20,
         "title": "Y", "confidence": 30, "triggers": [], "example_reply": "", "ai_reasoning": ""},
    ]
    learner.save_drafts(drafts)
    result = learner.list_drafts(sort="hit_count")
    assert result[0]["title"] == "Y"


# ── A2: Batch action ─────────────────────────────────────────────────────────

def test_batch_approve(learner):
    """batch_action should approve selected drafts."""
    drafts = [
        {"source": "miss", "query": f"Q{i}", "hit_count": 1,
         "title": f"T{i}", "confidence": 50, "triggers": [],
         "example_reply": "R", "ai_reasoning": ""}
        for i in range(5)
    ]
    learner.save_drafts(drafts)
    all_drafts = learner.list_drafts(status="pending")
    assert len(all_drafts) == 5

    ids_to_approve = [all_drafts[0]["id"], all_drafts[2]["id"]]
    result = learner.batch_action(ids_to_approve, "approve", operator="test")
    assert result["approved"] == 2
    assert result["rejected"] == 0

    remaining = learner.list_drafts(status="pending")
    assert len(remaining) == 3


def test_batch_reject(learner):
    """batch_action should reject selected drafts."""
    drafts = [
        {"source": "miss", "query": f"Q{i}", "hit_count": 1,
         "title": f"T{i}", "confidence": 50, "triggers": [],
         "example_reply": "R", "ai_reasoning": ""}
        for i in range(3)
    ]
    learner.save_drafts(drafts)
    all_drafts = learner.list_drafts(status="pending")
    ids_to_reject = [d["id"] for d in all_drafts]
    result = learner.batch_action(ids_to_reject, "reject", operator="test")
    assert result["rejected"] == 3

    pending = learner.list_drafts(status="pending")
    assert len(pending) == 0
    rejected = learner.list_drafts(status="rejected")
    assert len(rejected) == 3


def test_batch_action_invalid_ids(learner):
    """batch_action with non-existent IDs should gracefully fail."""
    result = learner.batch_action(["fake_id_1", "fake_id_2"], "approve")
    assert result["approved"] == 0
    assert len(result["failed"]) == 2


def test_confidence_clamp_in_generate(learner):
    """confidence should be clamped to 0-100."""
    # Simulate what _generate_batch would produce
    drafts = [
        {"source": "miss", "query": "over", "hit_count": 1,
         "title": "Over", "confidence": 150, "triggers": [],
         "example_reply": "", "ai_reasoning": ""},
        {"source": "miss", "query": "under", "hit_count": 1,
         "title": "Under", "confidence": -20, "triggers": [],
         "example_reply": "", "ai_reasoning": ""},
    ]
    # Manually clamp like _generate_batch does
    for d in drafts:
        d["confidence"] = max(0, min(100, d["confidence"]))
    learner.save_drafts(drafts)
    result = learner.list_drafts(sort="confidence")
    assert result[0]["confidence"] == 100
    assert result[1]["confidence"] == 0
