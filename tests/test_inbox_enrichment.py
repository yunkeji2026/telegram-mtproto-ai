"""Phase R2：inbox_enrichment 纯函数 + InboxStore 批量查询单测。"""
from __future__ import annotations

import sys
import time as _t
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.inbox_enrichment import (
    build_inbox_block,
    health_board_sort_key,
    inbox_enrichment_batch_for_journeys,
    inbox_enrichment_for_conv_ids,
    inbox_sort_tiebreak_key,
    parse_churn_level,
    pick_primary_conversation,
)
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore


def test_parse_churn_level_json_and_plain():
    assert parse_churn_level("high") == "high"
    raw = '{"level": "medium", "reasons": ["silent"], "ts": 1}'
    assert parse_churn_level(raw) == "medium"
    assert parse_churn_level("") == ""


def test_inbox_sort_tiebreak_key_ordering():
    high_rising = inbox_sort_tiebreak_key(
        {"churn_risk": "high", "emotion_trend": "rising"})
    low_stable = inbox_sort_tiebreak_key(
        {"churn_risk": "low", "emotion_trend": "stable"})
    assert high_rising < low_stable
    assert inbox_sort_tiebreak_key(None) == (2, 3)


def test_health_board_sort_key_with_tiebreak():
    a = {"value_at_risk": True, "score": 40,
         "inbox": {"churn_risk": "high", "emotion_trend": "rising"}}
    b = {"value_at_risk": True, "score": 40,
         "inbox": {"churn_risk": "low", "emotion_trend": "stable"}}
    assert health_board_sort_key(a, inbox_tiebreak=True) < health_board_sort_key(
        b, inbox_tiebreak=True)
    # 关 tie-break 时 inbox 不影响
    assert health_board_sort_key(a) == health_board_sort_key(b)


def test_pick_primary_by_last_ts():
    conv_map = {
        "a:1:x": {"conversation_id": "a:1:x", "last_ts": 100},
        "a:1:y": {"conversation_id": "a:1:y", "last_ts": 200},
    }
    primary, matched = pick_primary_conversation(["a:1:x", "a:1:y", "missing"], conv_map)
    assert matched == 2
    assert primary["conversation_id"] == "a:1:y"


def test_build_inbox_block_compact_vs_full():
    conv = {"conversation_id": "tg:a:u", "last_ts": 1.0, "unread": 3, "last_text": "hi"}
    meta = {"emotion_trend": "rising", "churn_risk": "high", "last_intent": "complaint",
            "last_emotion": "anger", "last_risk": "medium", "msg_count": 5, "summary": "s"}
    compact = build_inbox_block(conv, meta, compact=True)
    assert compact == {
        "conversation_id": "tg:a:u",
        "emotion_trend": "rising",
        "churn_risk": "high",
        "last_intent": "complaint",
    }
    full = build_inbox_block(conv, meta, matched=2, compact=False)
    assert full["conversations_matched"] == 2
    assert full["unread"] == 3
    assert full["summary"] == "s"


def test_inbox_store_batch_methods():
    store = InboxStore(":memory:")
    c1 = "messenger:a:peer1"
    c2 = "messenger:a:peer2"
    store.upsert_conversation(InboxConversation(
        conversation_id=c1, platform="messenger", account_id="a",
        chat_key="peer1", last_ts=_t.time()))
    store.upsert_conversation(InboxConversation(
        conversation_id=c2, platform="messenger", account_id="a",
        chat_key="peer2", last_ts=_t.time() - 10))
    store.update_conv_meta(c1, platform="messenger", intent="a", emotion="anger")
    store.update_conv_meta(c1, platform="messenger", intent="b", emotion="anger")
    with store._lock:
        store._conn.execute(
            "UPDATE conversation_meta SET churn_risk=? WHERE conversation_id=?",
            ("medium", c1),
        )
        store._conn.commit()

    conv_map = store.get_conversations_for_ids([c1, c2, "nope"])
    assert set(conv_map) == {c1, c2}
    meta_map = store.get_conv_meta_for_ids([c1, c2])
    assert meta_map[c1]["churn_risk"] == "medium"
    assert meta_map[c1]["emotion_trend"] in ("rising", "stable", "falling")


def test_batch_for_journeys_uses_contact_map():
    store = InboxStore(":memory:")
    cid = "messenger:acc:bob"
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="messenger", account_id="acc",
        chat_key="bob", last_ts=_t.time()))
    store.update_conv_meta(cid, platform="messenger", intent="chitchat", emotion="neutral")
    with store._lock:
        store._conn.execute(
            "UPDATE conversation_meta SET churn_risk=? WHERE conversation_id=?",
            ("low", cid),
        )
        store._conn.commit()

    out = inbox_enrichment_batch_for_journeys(
        ["j1"], {"j1": "contact-1"},
        {"contact-1": [cid]}, store, compact=True,
    )
    assert out["j1"]["emotion_trend"] in ("rising", "stable", "falling")
    assert out["j1"]["churn_risk"] == "low"
    assert out["j2"] is None if "j2" in out else True


def test_enrichment_for_conv_ids_none_when_no_match():
    assert inbox_enrichment_for_conv_ids(["x"], {}, {}) is None
