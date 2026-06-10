"""P51 — 关系阶段演进时间轴。"""

from src.inbox.models import InboxConversation
from src.inbox.stage_timeline import (
    build_contact_stage_summary,
    enrich_stage_audit_row,
)
from src.inbox.store import InboxStore


class TestStageTimelineEnrich:
    def test_enrich_confirm(self):
        ev = enrich_stage_audit_row({
            "action": "stage_confirm",
            "reason": "试探/升温 → intimate",
            "agent_id": "alice",
            "conversation_id": "conv_1",
            "ts": 100.0,
            "platform": "line",
        })
        assert ev["event_type"] == "stage_confirm"
        assert ev["meta"]["to_stage"] == "intimate"
        assert ev["meta"]["from_label"] == "试探/升温"
        assert ev["label"] == "确认进阶"

    def test_enrich_downgrade(self):
        ev = enrich_stage_audit_row({
            "action": "stage_downgrade",
            "reason": "[关系降级] 暧昧陪伴 → 试探/升温：客户态度变冷",
            "agent_id": "bob",
            "ts": 200.0,
        })
        assert ev["meta"]["to_stage"] == "warming"
        assert ev["meta"]["note"] == "客户态度变冷"

    def test_enrich_sync(self):
        ev = enrich_stage_audit_row({
            "action": "stage_sync",
            "reason": "对齐至 intimate（to_contact，2 会话）",
            "agent_id": "alice",
            "ts": 300.0,
        })
        assert ev["meta"]["to_stage"] == "intimate"
        assert ev["meta"]["synced"] == 2

    def test_summary_counts(self):
        events = [
            {"event_type": "stage_confirm", "ts": 10, "agent_id": "a"},
            {"event_type": "stage_downgrade", "ts": 20, "agent_id": "b"},
        ]
        s = build_contact_stage_summary(
            events,
            contact_rec={"confirmed_stage": "warming", "updated_by": "a", "updated_at": 20},
        )
        assert s["total_confirms"] == 1
        assert s["total_downgrades"] == 1
        assert s["current_stage"] == "warming"
        assert set(s["agent_ids"]) == {"a", "b"}


class TestStageTimelineStore:
    def _setup(self, tmp_path):
        store = InboxStore(tmp_path / "stl.db")
        store.upsert_conversation(InboxConversation(
            conversation_id="conv_a", platform="line", account_id="a",
            chat_key="a", contact_id="ct_stl",
        ))
        store.upsert_conversation(InboxConversation(
            conversation_id="conv_b", platform="whatsapp", account_id="b",
            chat_key="b", contact_id="ct_stl",
        ))
        return store

    def test_list_contact_stage_audits_by_conv(self, tmp_path):
        store = self._setup(tmp_path)
        store.record_draft_audit(
            "", action="stage_confirm", agent_id="alice",
            reason="初识 → warming", conversation_id="conv_a", ts=100,
        )
        rows = store.list_contact_stage_audits("ct_stl")
        assert len(rows) == 1
        assert rows[0]["action"] == "stage_confirm"

    def test_list_contact_stage_audits_by_draft_id(self, tmp_path):
        store = self._setup(tmp_path)
        store.record_draft_audit(
            "contact:ct_stl", action="stage_sync", agent_id="alice",
            reason="对齐至 intimate（to_contact，2 会话）", conversation_id="", ts=200,
        )
        rows = store.list_contact_stage_audits("ct_stl")
        assert len(rows) == 1
        assert rows[0]["action"] == "stage_sync"

    def test_ignores_other_contacts(self, tmp_path):
        store = self._setup(tmp_path)
        store.upsert_conversation(InboxConversation(
            conversation_id="conv_other", platform="line",
            contact_id="ct_other",
        ))
        store.record_draft_audit(
            "", action="stage_confirm", agent_id="bob",
            reason="初识 → warming", conversation_id="conv_other", ts=50,
        )
        store.record_draft_audit(
            "", action="stage_confirm", agent_id="alice",
            reason="试探/升温 → intimate", conversation_id="conv_a", ts=100,
        )
        rows = store.list_contact_stage_audits("ct_stl")
        assert len(rows) == 1
        assert rows[0]["conversation_id"] == "conv_a"
