"""ContactStore 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore
from src.contacts.models import (
    CHANNEL_MESSENGER,
    CHANNEL_LINE,
    STAGE_INITIAL,
    STAGE_ENGAGED,
)


@pytest.fixture
def store(tmp_path) -> ContactStore:
    s = ContactStore(db_path=tmp_path / "contacts.db")
    yield s
    s.close()


class TestContact:
    def test_create_and_get(self, store):
        c = store.create_contact(primary_name="Alice", language_hint="zh")
        got = store.get_contact(c.contact_id)
        assert got is not None
        assert got.primary_name == "Alice"
        assert got.language_hint == "zh"
        assert got.created_at > 0

    def test_update_fields(self, store):
        c = store.create_contact(primary_name="Bob")
        assert store.update_contact(c.contact_id, primary_name="Bob2", notes="VIP")
        g = store.get_contact(c.contact_id)
        assert g.primary_name == "Bob2"
        assert g.notes == "VIP"

    def test_update_no_fields_noop(self, store):
        c = store.create_contact(primary_name="X")
        assert store.update_contact(c.contact_id) is False

    def test_list_order_by_last_active(self, store):
        a = store.create_contact(primary_name="A")
        b = store.create_contact(primary_name="B")
        store.update_contact(a.contact_id, last_active_at=99999999999)
        lst = store.list_contacts(limit=10)
        assert lst[0].contact_id == a.contact_id

    def test_count(self, store):
        assert store.count_contacts() == 0
        store.create_contact()
        store.create_contact()
        assert store.count_contacts() == 2


class TestChannelIdentity:
    def test_ensure_creates_contact_and_journey(self, store):
        contact, ci, created = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER,
            account_id="acc-A",
            external_id="fb_111",
            display_name="Alice",
            language_hint="zh",
        )
        assert created is True
        assert ci.contact_id == contact.contact_id
        assert ci.channel == CHANNEL_MESSENGER
        assert ci.attribution_confidence == 1.0
        assert ci.direction == "first_seen"

        # Journey 自动建
        j = store.get_journey_by_contact(contact.contact_id)
        assert j is not None
        assert j.funnel_stage == STAGE_INITIAL

        # contact_created 事件落库
        events = store.list_events(j.journey_id)
        assert any(e["event_type"] == "contact_created" for e in events)

    def test_ensure_idempotent(self, store):
        _, ci1, c1 = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="acc-A", external_id="fb_222")
        _, ci2, c2 = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="acc-A", external_id="fb_222")
        assert c1 is True and c2 is False
        assert ci1.channel_identity_id == ci2.channel_identity_id

    def test_ensure_different_channels_different_ci(self, store):
        _, ci_m, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="x")
        _, ci_l, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="x")
        assert ci_m.channel_identity_id != ci_l.channel_identity_id
        assert ci_m.contact_id != ci_l.contact_id  # 没合并前是两个独立 Contact

    def test_unknown_channel_rejected(self, store):
        with pytest.raises(ValueError):
            store.ensure_channel_identity(channel="twitter", account_id="a", external_id="x")

    def test_get_ci_by_external(self, store):
        _, ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="acc", external_id="fb_99")
        found = store.get_ci_by_external(CHANNEL_MESSENGER, "acc", "fb_99")
        assert found is not None and found.channel_identity_id == ci.channel_identity_id
        assert store.get_ci_by_external(CHANNEL_MESSENGER, "acc", "fb_missing") is None


class TestRelinkMerge:
    def test_relink_migrates_ci_and_deletes_orphan_contact(self, store):
        # Messenger 侧先建
        m_contact, m_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_123",
            display_name="Alice",
        )
        # LINE 侧建一个独立 Contact
        l_contact, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_abc",
            display_name="Alice",
        )
        assert l_contact.contact_id != m_contact.contact_id

        # 合并：LINE ci 迁到 Messenger 的 contact
        ok = store.relink_channel_identity(
            ci_id=l_ci.channel_identity_id,
            new_contact_id=m_contact.contact_id,
            linked_via="token",
            attribution_confidence=0.95,
        )
        assert ok is True

        # LINE ci 的 contact_id 已更新
        l_ci_fresh = store.get_channel_identity(l_ci.channel_identity_id)
        assert l_ci_fresh.contact_id == m_contact.contact_id
        assert l_ci_fresh.linked_via == "token"
        assert l_ci_fresh.direction == "linked_from"

        # 孤岛 LINE contact 被回收
        assert store.get_contact(l_contact.contact_id) is None

        # 合并事件落在 Messenger 的 journey 上
        m_journey = store.get_journey_by_contact(m_contact.contact_id)
        events = store.list_events(m_journey.journey_id)
        assert any(e["event_type"] == "channel_identity_merged" for e in events)

    def test_relink_preserves_old_journey_events(self, store):
        m_contact, _, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        l_contact, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        l_journey = store.get_journey_by_contact(l_contact.contact_id)
        # 在老 journey 写一个历史事件
        store.append_event(
            journey_id=l_journey.journey_id, event_type="msg_in",
            payload={"text": "你好"},
        )
        store.relink_channel_identity(
            ci_id=l_ci.channel_identity_id,
            new_contact_id=m_contact.contact_id,
            linked_via="token",
            attribution_confidence=0.95,
        )
        m_journey = store.get_journey_by_contact(m_contact.contact_id)
        evs = store.list_events(m_journey.journey_id, limit=50)
        # 老 journey 的 msg_in 被搬过来了
        assert any(e["event_type"] == "msg_in" for e in evs)

    def test_relink_noop_same_contact(self, store):
        _, m_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="x")
        # 已经属于该 contact，relink 到自己应该 noop 返回 False
        ok = store.relink_channel_identity(
            ci_id=m_ci.channel_identity_id,
            new_contact_id=m_ci.contact_id,
            linked_via="token",
            attribution_confidence=1.0,
        )
        assert ok is False

    def test_relink_to_nonexistent_contact_raises(self, store):
        _, m_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="x")
        with pytest.raises(ValueError):
            store.relink_channel_identity(
                ci_id=m_ci.channel_identity_id,
                new_contact_id="nonexistent",
                linked_via="token",
                attribution_confidence=1.0,
            )


class TestJourneyAndEvents:
    def test_update_journey_fields(self, store):
        c, _, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="x")
        j = store.get_journey_by_contact(c.contact_id)
        store.update_journey(j.journey_id, funnel_stage=STAGE_ENGAGED, intimacy_score=50.0)
        j2 = store.get_journey_by_contact(c.contact_id)
        assert j2.funnel_stage == STAGE_ENGAGED
        assert j2.intimacy_score == 50.0
        assert j2.updated_at >= j.updated_at

    def test_update_journey_rejects_unknown_field(self, store):
        c, _, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="x")
        j = store.get_journey_by_contact(c.contact_id)
        with pytest.raises(ValueError):
            store.update_journey(j.journey_id, unknown_field="x")

    def test_append_event_and_list(self, store):
        c, _, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="x")
        j = store.get_journey_by_contact(c.contact_id)
        eid = store.append_event(
            journey_id=j.journey_id,
            event_type="msg_in",
            payload={"text": "hi"},
            trace_id="trace-1",
        )
        assert eid
        evs = store.list_events(j.journey_id)
        tops = [e for e in evs if e["event_type"] == "msg_in"]
        assert tops and tops[0]["payload"]["text"] == "hi"
        assert tops[0]["trace_id"] == "trace-1"


class TestMergeReviewQueue:
    def test_enqueue_and_list_and_resolve(self, store):
        _, ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_x")
        c2 = store.create_contact(primary_name="target")
        rid = store.enqueue_merge_review(
            candidate_ci_id=ci.channel_identity_id,
            target_contact_id=c2.contact_id,
            confidence=0.7,
            breakdown={"name_match": 0.25, "lang_match": 0.2},
        )
        pending = store.list_pending_reviews()
        assert len(pending) == 1
        assert pending[0]["review_id"] == rid
        assert store.resolve_review(rid, status="approved", resolved_by="admin") is True
        assert store.list_pending_reviews() == []

    def test_resolve_bad_status(self, store):
        _, ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_x")
        c2 = store.create_contact()
        rid = store.enqueue_merge_review(
            candidate_ci_id=ci.channel_identity_id,
            target_contact_id=c2.contact_id,
            confidence=0.7, breakdown={},
        )
        with pytest.raises(ValueError):
            store.resolve_review(rid, status="weird")
