"""P48 — @mention 智能路由单元测试。"""
import json
import time

import pytest

from src.inbox.mention_router import MentionRouter
from src.inbox.store import InboxStore


class TestMentionRouter:
    def _router(self):
        presence = [
            {"agent_id": "alice", "display_name": "Alice", "status": "online"},
            {"agent_id": "bob", "display_name": "Bob", "status": "busy"},
            {"agent_id": "super1", "display_name": "主管", "status": "online"},
        ]
        workloads = [
            {"agent_id": "alice", "active_convs": 1, "status": "online"},
            {"agent_id": "bob", "active_convs": 4, "status": "busy"},
            {"agent_id": "super1", "active_convs": 2, "status": "online"},
        ]
        users = [
            {"username": "super1", "role": "master", "display_name": "主管", "enabled": 1},
            {"username": "alice", "role": "agent", "display_name": "Alice", "enabled": 1},
        ]
        return MentionRouter(
            None,
            presence=presence,
            workloads=workloads,
            qa_stats=[
                {"agent_id": "alice", "avg_score": 85, "agent_name": "Alice"},
            ],
            users=users,
            stage_confirm={"alice": {"intimate": 3}},
            mention_counts={"alice": 5},
        )

    def test_suggest_prefers_stage_expert(self):
        r = self._router().suggest(stage="intimate", stage_label="亲密")
        ids = [s["agent_id"] for s in r["suggestions"]]
        assert ids[0] == "alice"
        assert any("亲密" in x for s in r["suggestions"] for x in s["reasons"])

    def test_auto_cc_supervisor_on_high_churn(self):
        r = self._router().suggest(stage="intimate", churn_level="high")
        assert r["auto_cc"]
        assert r["auto_cc"][0]["agent_id"] == "super1"

    def test_query_filter(self):
        r = self._router().suggest(stage="warming", query="bob")
        assert len(r["suggestions"]) == 1
        assert r["suggestions"][0]["agent_id"] == "bob"


class TestMentionStore:
    def test_stage_confirm_counts(self, tmp_path):
        store = InboxStore(tmp_path / "m.db")
        store.record_draft_audit(
            "", action="stage_confirm", agent_id="alice",
            reason="熟悉 → intimate", conversation_id="c1",
        )
        counts = store.get_agent_stage_confirm_counts(since_ts=0)
        assert counts["alice"]["intimate"] == 1

    def test_mention_counts(self, tmp_path):
        store = InboxStore(tmp_path / "m2.db")
        store.add_conv_note("c1", "请帮忙", agent_id="a", agent_name="A", mentions=["bob"])
        store.add_conv_note("c2", "再看", agent_id="a", agent_name="A", mentions=["bob", "bob"])
        counts = store.get_agent_mention_counts(since_ts=0)
        assert counts["bob"] == 3

    def test_recent_mention_note(self, tmp_path):
        store = InboxStore(tmp_path / "m3.db")
        store.add_conv_note("c1", "请 bob 协助", agent_id="a", agent_name="A", mentions=["bob"])
        note = store.get_recent_mention_note("c1", "bob")
        assert note is not None
        assert "协助" in note["body"]
