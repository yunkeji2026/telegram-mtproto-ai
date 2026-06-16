"""P54 — Copilot 采纳率统计。"""

import json

from src.inbox.copilot_stats import (
    aggregate_copilot_stats,
    classify_adoption,
    encode_adopt,
    encode_impression,
)
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore


class TestCopilotStatsHelpers:
    def test_classify_adoption(self):
        assert classify_adoption("你好呀", "你好呀") == "exact"
        assert classify_adoption("你好呀，最近怎么样", "你好呀，最近怎么样？") == "partial"
        assert classify_adoption("你好", "完全不同的内容") == "edit"

    def test_aggregate_stats(self):
        rows = [
            {
                "action": "copilot_impression", "agent_id": "alice",
                "reason": encode_impression(trigger="reunion", suggestion_count=3),
                "ts": 100, "conversation_id": "c1",
            },
            {
                "action": "copilot_adopt", "agent_id": "alice",
                "reason": encode_adopt(match="exact", source="reunion", trigger="reunion"),
                "ts": 200, "conversation_id": "c1",
            },
        ]
        stats = aggregate_copilot_stats(rows)
        assert stats["total_impressions"] == 1
        assert stats["total_adoptions"] == 1
        assert stats["overall_rate"] == 100.0
        assert stats["agents"][0]["agent_id"] == "alice"
        assert len(stats["replays"]) == 1


class TestCopilotStatsStore:
    def test_impression_and_adopt_roundtrip(self, tmp_path):
        store = InboxStore(tmp_path / "cp.db")
        store.upsert_conversation(InboxConversation(
            conversation_id="conv_cp", platform="line", contact_id="ct1",
        ))
        store.record_copilot_impression(
            "conv_cp", "alice", trigger="stage_advance", stage="warming",
            polished=True, suggestion_count=4, top_source="stage_advance",
        )
        store.record_copilot_adopt(
            "conv_cp", "alice", match="exact", source="stage_advance",
            trigger="stage_advance", stage="warming",
            suggested_preview="感觉我们越来越熟悉了", sent_preview="感觉我们越来越熟悉了",
        )
        stats = store.get_copilot_stats(since_ts=0)
        assert stats["total_impressions"] == 1
        assert stats["total_adoptions"] == 1
        assert stats["by_trigger"]["stage_advance"]["adoptions"] == 1

    def test_agent_filter(self, tmp_path):
        store = InboxStore(tmp_path / "cp2.db")
        store.record_copilot_impression("c1", "alice", trigger="open")
        store.record_copilot_impression("c2", "bob", trigger="open")
        stats = store.get_copilot_stats(since_ts=0, agent_id="alice")
        assert stats["total_impressions"] == 1
