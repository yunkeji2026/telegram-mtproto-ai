"""journey_fsm — 合法转移表 + 时间驱动降级。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.contacts.gateway import ContactGateway
from src.contacts.journey_fsm import (
    is_transition_allowed,
    transit,
    list_journeys_eligible_for_decay,
    apply_silence_decay,
    SILENCE_DECAY_RULES,
    STAGE_TRANSITIONS,
)
from src.contacts.models import (
    CHANNEL_MESSENGER,
    STAGE_ENGAGED, STAGE_HANDOFF_READY, STAGE_HANDOFF_SENT,
    STAGE_INITIAL, STAGE_LINE_ACCEPTED, STAGE_LINE_ADDED, STAGE_LINE_ENGAGED,
    STAGE_LOST_HANDOFF, STAGE_LOST_LINE_SILENT,
)


@pytest.fixture
def store(tmp_path):
    s = ContactStore(db_path=tmp_path / "contacts.db")
    yield s
    s.close()


@pytest.fixture
def gateway(store):
    return ContactGateway(store, HandoffTokenService(store, ttl_seconds=3600), MergeService(store))


class TestAllowedTable:
    def test_initial_to_engaged(self):
        assert is_transition_allowed(STAGE_INITIAL, STAGE_ENGAGED)

    def test_initial_to_handoff_sent_blocked(self):
        assert not is_transition_allowed(STAGE_INITIAL, STAGE_HANDOFF_SENT)

    def test_same_stage_is_noop_allowed(self):
        assert is_transition_allowed(STAGE_ENGAGED, STAGE_ENGAGED)

    def test_undefined_target_default_allowed(self):
        # LOST_HANDOFF 不在 STAGE_TRANSITIONS 里 → 任意前驱都允许
        assert is_transition_allowed(STAGE_HANDOFF_SENT, STAGE_LOST_HANDOFF)


class TestTransit:
    def _make_journey(self, gateway, stage=STAGE_INITIAL):
        ctx = gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        return ctx.journey.journey_id

    def test_allowed_transition_writes(self, store, gateway):
        jid = self._make_journey(gateway)
        assert transit(store, journey_id=jid, to_stage=STAGE_ENGAGED) is True
        j = store.get_journey(jid)
        assert j.funnel_stage == STAGE_ENGAGED

    def test_blocked_transition_noop(self, store, gateway):
        jid = self._make_journey(gateway)
        assert transit(store, journey_id=jid, to_stage=STAGE_HANDOFF_SENT) is False
        j = store.get_journey(jid)
        assert j.funnel_stage == STAGE_INITIAL

    def test_noop_same_stage(self, store, gateway):
        jid = self._make_journey(gateway)
        assert transit(store, journey_id=jid, to_stage=STAGE_INITIAL) is True


class TestSilenceDecay:
    def _setup_journey_at(self, store, gateway, stage, updated_ago_seconds):
        ctx = gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id=f"fb_{stage}")
        # 直接 SQL 把 stage 和 updated_at 改到想要的状态
        past = int(time.time()) - updated_ago_seconds
        with store._lock:
            store._conn.execute(
                "UPDATE journeys SET funnel_stage=?, updated_at=? WHERE journey_id=?",
                (stage, past, ctx.journey.journey_id),
            )
            store._conn.commit()
        return ctx.journey.journey_id

    def test_handoff_sent_decays_after_72h(self, store, gateway):
        jid = self._setup_journey_at(store, gateway, STAGE_HANDOFF_SENT,
                                      updated_ago_seconds=73 * 3600)
        eligible = list_journeys_eligible_for_decay(store)
        assert any(j[0] == jid for j in eligible)
        applied = apply_silence_decay(store)
        assert applied >= 1
        j = store.get_journey(jid)
        assert j.funnel_stage == STAGE_LOST_HANDOFF
        # 事件落了 silence_decay
        events = store.list_events(jid)
        types = [e["event_type"] for e in events]
        assert "silence_decay" in types

    def test_handoff_sent_not_yet_decays(self, store, gateway):
        jid = self._setup_journey_at(store, gateway, STAGE_HANDOFF_SENT,
                                      updated_ago_seconds=10 * 3600)  # 10h < 72h
        eligible = list_journeys_eligible_for_decay(store)
        assert not any(j[0] == jid for j in eligible)

    def test_line_added_decays_after_24h(self, store, gateway):
        jid = self._setup_journey_at(store, gateway, STAGE_LINE_ADDED,
                                      updated_ago_seconds=25 * 3600)
        apply_silence_decay(store)
        j = store.get_journey(jid)
        assert j.funnel_stage == STAGE_LOST_LINE_SILENT

    def test_handoff_ready_decays_after_7d(self, store, gateway):
        jid = self._setup_journey_at(store, gateway, STAGE_HANDOFF_READY,
                                      updated_ago_seconds=8 * 24 * 3600)
        apply_silence_decay(store)
        j = store.get_journey(jid)
        assert j.funnel_stage == STAGE_ENGAGED   # 回退半格

    def test_dry_run_does_not_change_state(self, store, gateway):
        jid = self._setup_journey_at(store, gateway, STAGE_HANDOFF_SENT,
                                      updated_ago_seconds=73 * 3600)
        count = apply_silence_decay(store, dry_run=True)
        assert count >= 1
        j = store.get_journey(jid)
        assert j.funnel_stage == STAGE_HANDOFF_SENT   # 不变

    def test_idempotent(self, store, gateway):
        """连续跑两次 apply_silence_decay，不应重复降级。"""
        jid = self._setup_journey_at(store, gateway, STAGE_HANDOFF_SENT,
                                      updated_ago_seconds=73 * 3600)
        apply_silence_decay(store)
        count2 = apply_silence_decay(store)
        assert count2 == 0   # 已在 LOST_HANDOFF，不再匹配规则
