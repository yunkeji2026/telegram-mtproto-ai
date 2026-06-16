"""P50 — 客户级关系阶段同步与冲突检测。"""

import pytest

from src.inbox.contact_rel_stage import (
    detect_stage_conflict,
    enrich_with_contact_stage,
    highest_stage,
    stage_index,
)
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore


def _conv(cid: str, contact_id: str = "ct_1") -> InboxConversation:
    return InboxConversation(
        conversation_id=cid,
        platform="line",
        account_id="a",
        chat_key=cid,
        display_name="User",
        contact_id=contact_id,
    )


class TestContactRelStageHelpers:
    def test_stage_index_and_highest(self):
        assert stage_index("intimate") > stage_index("warming")
        assert highest_stage(["initial", "warming", "intimate"]) == "intimate"

    def test_detect_conflict_multi_conv(self):
        c = detect_stage_conflict("warming", {"c1": "warming", "c2": "intimate"})
        assert c["has_conflict"] is True
        assert "不一致" in "".join(c["reasons"])
        assert c["show_to_highest"] is True
        assert c["highest_stage"] == "intimate"

    def test_detect_no_conflict_aligned(self):
        c = detect_stage_conflict("warming", {"c1": "warming", "c2": "warming"})
        assert c["has_conflict"] is False

    def test_contact_lagging_hints(self):
        c = detect_stage_conflict("warming", {"c1": "warming", "c2": "intimate"})
        assert c["contact_lagging"] is True
        assert c["show_to_contact"] is True
        assert c["highest_stage_label"] == "暧昧陪伴"

    def test_enrich_with_contact_stage(self):
        base = {"stage": "warming", "stage_label": "升温"}
        out = enrich_with_contact_stage(
            base,
            contact_stage="warming",
            contact_updated_by="alice",
            conflict={"has_conflict": False},
        )
        assert out["contact_stage"] == "warming"
        assert out["contact_updated_by"] == "alice"
        assert out["stage_conflict"] is False


class TestContactRelStageStore:
    def test_contact_stage_crud(self, tmp_path):
        store = InboxStore(tmp_path / "crs.db")
        store.set_contact_rel_stage("ct_1", "warming", updated_by="bob")
        rec = store.get_contact_rel_stage("ct_1")
        assert rec["confirmed_stage"] == "warming"
        assert rec["updated_by"] == "bob"

    def test_list_conv_stages_for_contact(self, tmp_path):
        store = InboxStore(tmp_path / "crs2.db")
        store.upsert_conversation(_conv("conv_a"))
        store.upsert_conversation(_conv("conv_b"))
        store.confirm_rel_stage("conv_a", "warming")
        store.confirm_rel_stage("conv_b", "intimate")
        stages = store.list_conv_rel_stages_for_contact("ct_1")
        assert stages["conv_a"] == "warming"
        assert stages["conv_b"] == "intimate"

    def test_sync_convs_to_stage(self, tmp_path):
        store = InboxStore(tmp_path / "crs3.db")
        store.upsert_conversation(_conv("conv_x"))
        store.upsert_conversation(_conv("conv_y"))
        store.confirm_rel_stage("conv_x", "initial")
        store.confirm_rel_stage("conv_y", "warming")
        n = store.sync_convs_to_stage("ct_1", "intimate")
        assert n == 2
        stages = store.list_conv_rel_stages_for_contact("ct_1")
        assert stages["conv_x"] == "intimate"
        assert stages["conv_y"] == "intimate"

    def test_confirm_rel_stage_with_contact_syncs_all(self, tmp_path):
        store = InboxStore(tmp_path / "crs4.db")
        store.upsert_conversation(_conv("conv_1"))
        store.upsert_conversation(_conv("conv_2"))
        store.confirm_rel_stage("conv_1", "initial")
        store.confirm_rel_stage("conv_2", "warming")
        store.confirm_rel_stage_with_contact(
            "conv_1", "ct_1", "intimate", updated_by="alice", sync_all_convs=True,
        )
        contact = store.get_contact_rel_stage("ct_1")
        assert contact["confirmed_stage"] == "intimate"
        stages = store.list_conv_rel_stages_for_contact("ct_1")
        assert stages["conv_1"] == "intimate"
        assert stages["conv_2"] == "intimate"

    def test_reunion_ack_at_contact_level(self, tmp_path):
        store = InboxStore(tmp_path / "crs5.db")
        store.set_contact_rel_stage(
            "ct_1", "warming", updated_by="alice", reunion_ack_ts=123.0,
        )
        rec = store.get_contact_rel_stage("ct_1")
        assert rec["reunion_ack_ts"] == 123.0
