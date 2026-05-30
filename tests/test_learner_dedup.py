"""Tests for A3: Semantic duplicate detection in DailyLearner."""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

from src.utils.kb_store import KnowledgeBaseStore
from src.utils.daily_learner import DailyLearner


@pytest.fixture
def kb_store(tmp_path):
    """Create a fresh KnowledgeBaseStore in a temp dir."""
    db = tmp_path / "kb.db"
    store = KnowledgeBaseStore(db_path=db)
    return store


@pytest.fixture
def learner(kb_store, tmp_path):
    """Create DailyLearner with a real kb_store and mock AI client."""
    ai = MagicMock()
    ai.generate_response = AsyncMock(return_value="[]")
    dl = DailyLearner(kb_store, ai, db_path=tmp_path / "kb.db")
    return dl


# ── Schema migration ─────────────────────────────────────────────────

def test_dup_columns_exist(learner):
    """Migration should create dup_entry_id, dup_entry_title, dup_score columns."""
    with learner._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(kb_drafts)").fetchall()}
    assert "dup_entry_id" in cols
    assert "dup_entry_title" in cols
    assert "dup_score" in cols


# ── Trigger overlap detection ─────────────────────────────────────────

def test_trigger_overlap_detected(learner, kb_store):
    """Draft with same triggers as existing KB entry should be flagged."""
    kb_store.add_entry({
        "title": "退货政策",
        "triggers": ["退货", "退款", "换货"],
        "category": "售后",
        "example_reply_zh": "可以在7天内退货",
    })
    # Force index rebuild for BM25
    KnowledgeBaseStore._index_dirty = True

    draft = {
        "title": "退货流程",
        "triggers": ["退货", "退款"],
        "query": "怎么退货",
    }
    dup = learner.check_duplicate(draft)
    assert dup is not None
    assert dup["method"] == "trigger_overlap"
    assert dup["score"] >= 0.4


def test_no_trigger_overlap(learner, kb_store):
    """Draft with unrelated triggers should not be flagged by trigger layer."""
    kb_store.add_entry({
        "title": "退货政策",
        "triggers": ["退货", "退款"],
        "category": "售后",
        "example_reply_zh": "7天退货",
    })
    KnowledgeBaseStore._index_dirty = True

    draft = {
        "title": "物流查询",
        "triggers": ["快递", "物流", "运费"],
        "query": "快递到哪了",
    }
    # Trigger layer should return None (no overlap)
    result = learner._check_trigger_overlap(["快递", "物流", "运费"])
    assert result is None


# ── BM25 text match detection ─────────────────────────────────────────

def test_bm25_match_detected(learner, kb_store):
    """Draft with highly similar text should be caught by BM25 layer."""
    kb_store.add_entry({
        "title": "产品价格查询",
        "triggers": ["价格", "多少钱", "报价"],
        "category": "销售",
        "scenario": "客户询问产品价格",
        "example_reply_zh": "请告诉我您感兴趣的产品型号",
    })
    KnowledgeBaseStore._index_dirty = True

    draft = {
        "title": "产品价格咨询",
        "triggers": ["报价", "价格"],
        "query": "产品价格多少钱",
    }
    dup = learner.check_duplicate(draft)
    # Should be caught by either trigger overlap or BM25
    assert dup is not None
    assert dup["score"] > 0


# ── No duplicate ──────────────────────────────────────────────────────

def test_no_duplicate_for_unique_draft(learner, kb_store):
    """Completely unrelated draft should return None."""
    kb_store.add_entry({
        "title": "退货政策",
        "triggers": ["退货"],
        "category": "售后",
        "example_reply_zh": "7天退货",
    })
    KnowledgeBaseStore._index_dirty = True

    draft = {
        "title": "天气预报",
        "triggers": ["天气", "气温"],
        "query": "明天天气怎么样",
    }
    dup = learner.check_duplicate(draft)
    # Should not be flagged
    assert dup is None


# ── save_drafts populates dup columns ─────────────────────────────────

def test_save_drafts_marks_duplicates(learner, kb_store):
    """save_drafts should populate dup columns when duplicate detected."""
    entry_id = kb_store.add_entry({
        "title": "退货说明",
        "triggers": ["退货", "退款"],
        "category": "售后",
        "example_reply_zh": "请在7日内申请退货",
    })
    KnowledgeBaseStore._index_dirty = True

    drafts = [{
        "source": "miss",
        "query": "怎么退货退款",
        "title": "退货流程",
        "triggers": ["退货", "退款"],
        "example_reply": "测试回复",
        "hit_count": 3,
        "confidence": 60,
    }]
    saved = learner.save_drafts(drafts)
    assert saved == 1

    # Check the saved draft has dup info
    all_drafts = learner.list_drafts(status="pending")
    assert len(all_drafts) == 1
    d = all_drafts[0]
    assert d["dup_score"] > 0
    assert d["dup_entry_id"] != ""
    assert d["dup_entry_title"] != ""


# ── recheck_duplicate updates columns ─────────────────────────────────

def test_recheck_duplicate_clears_when_entry_deleted(learner, kb_store):
    """After KB entry is deleted, recheck should clear the dup flag."""
    entry_id = kb_store.add_entry({
        "title": "退货说明",
        "triggers": ["退货"],
        "category": "售后",
        "example_reply_zh": "7天退货",
    })
    KnowledgeBaseStore._index_dirty = True

    drafts = [{
        "source": "miss", "query": "退货",
        "title": "退货", "triggers": ["退货"],
        "example_reply": "x", "hit_count": 1, "confidence": 50,
    }]
    learner.save_drafts(drafts)

    # Delete the KB entry
    kb_store.delete_entry(entry_id)
    KnowledgeBaseStore._index_dirty = True

    # Recheck should clear the flag
    all_d = learner.list_drafts(status="pending")
    dup = learner.recheck_duplicate(all_d[0]["id"])
    assert dup is None

    # Verify columns cleared
    updated = learner.get_draft(all_d[0]["id"])
    assert updated["dup_score"] == 0
    assert updated["dup_entry_id"] == ""


# ── stats includes dup_flagged ────────────────────────────────────────

def test_stats_includes_dup_flagged(learner, kb_store):
    """stats() should report dup_flagged count."""
    kb_store.add_entry({
        "title": "退货说明",
        "triggers": ["退货"],
        "category": "售后",
        "example_reply_zh": "7天退货",
    })
    KnowledgeBaseStore._index_dirty = True

    learner.save_drafts([{
        "source": "miss", "query": "退货",
        "title": "退货", "triggers": ["退货"],
        "example_reply": "x", "hit_count": 1, "confidence": 50,
    }])
    learner.save_drafts([{
        "source": "miss", "query": "火星探测",
        "title": "火星", "triggers": ["火星", "航天"],
        "example_reply": "y", "hit_count": 1, "confidence": 50,
    }])

    s = learner.stats()
    assert s["pending"] == 2
    assert s["dup_flagged"] >= 1
    assert "dup_flagged" in s


# ── Empty draft handling ──────────────────────────────────────────────

def test_check_duplicate_empty_draft(learner):
    """Empty draft should not crash, should return None."""
    dup = learner.check_duplicate({"title": "", "triggers": "", "query": ""})
    assert dup is None
