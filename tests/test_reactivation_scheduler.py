"""ReactivationScheduler 单元测试。"""

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
from src.contacts.models import (
    CHANNEL_MESSENGER, STAGE_LINE_ENGAGED, STAGE_BONDED,
    STAGE_ENGAGED, STAGE_LINE_ACCEPTED,
)
from src.skills.reactivation_scheduler import ReactivationScheduler


@pytest.fixture
def env(tmp_path):
    store = ContactStore(db_path=tmp_path / "contacts.db")
    gw = ContactGateway(store, HandoffTokenService(store, ttl_seconds=3600), MergeService(store))
    sched = ReactivationScheduler(
        store, min_silent_days=3, min_intimacy=40.0, cooldown_days=7,
    )
    yield store, gw, sched
    store.close()


def _seed_journey(store, gw, *, stage, intimacy, updated_ago_days, fb_id="fb_1"):
    ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id=fb_id)
    now = int(time.time())
    updated_ts = now - int(updated_ago_days * 86400)
    with store._lock:
        store._conn.execute(
            "UPDATE journeys SET funnel_stage=?, intimacy_score=?, updated_at=? "
            "WHERE journey_id=?",
            (stage, intimacy, updated_ts, ctx.journey.journey_id),
        )
        store._conn.commit()
    return ctx.journey.journey_id


class TestCandidateSelection:
    def test_line_engaged_silent_4d_high_intimacy_selected(self, env):
        store, gw, sched = env
        jid = _seed_journey(store, gw, stage=STAGE_LINE_ENGAGED,
                            intimacy=60.0, updated_ago_days=4)
        cands = sched.list_candidates()
        assert len(cands) == 1
        assert cands[0].journey_id == jid
        assert cands[0].silent_days >= 3.9

    def test_not_silent_enough_excluded(self, env):
        store, gw, sched = env
        _seed_journey(store, gw, stage=STAGE_LINE_ENGAGED,
                      intimacy=60.0, updated_ago_days=2)  # 仅 2 天
        assert sched.list_candidates() == []

    def test_intimacy_too_low_excluded(self, env):
        store, gw, sched = env
        _seed_journey(store, gw, stage=STAGE_LINE_ENGAGED,
                      intimacy=20.0, updated_ago_days=5)
        assert sched.list_candidates() == []

    def test_wrong_stage_excluded(self, env):
        store, gw, sched = env
        # Stage ENGAGED 不在 LINE 漏斗下游
        _seed_journey(store, gw, stage=STAGE_ENGAGED,
                      intimacy=60.0, updated_ago_days=5)
        assert sched.list_candidates() == []

    def test_bonded_selected(self, env):
        store, gw, sched = env
        _seed_journey(store, gw, stage=STAGE_BONDED,
                      intimacy=80.0, updated_ago_days=5)
        assert len(sched.list_candidates()) == 1

    def test_line_accepted_but_silent_selected(self, env):
        store, gw, sched = env
        _seed_journey(store, gw, stage=STAGE_LINE_ACCEPTED,
                      intimacy=50.0, updated_ago_days=4)
        assert len(sched.list_candidates()) == 1


class TestCooldown:
    def test_recent_reactivation_excludes(self, env):
        store, gw, sched = env
        jid = _seed_journey(store, gw, stage=STAGE_LINE_ENGAGED,
                            intimacy=60.0, updated_ago_days=5)
        # 2 天前刚 reactivate 过 → cooldown 7 天内不再选
        with store._lock:
            import uuid
            store._conn.execute(
                "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
                "VALUES (?, ?, '', 'reactivation_sent', '{}', ?)",
                (uuid.uuid4().hex, jid, int(time.time()) - 2 * 86400),
            )
            store._conn.commit()
        assert sched.list_candidates() == []

    def test_old_reactivation_does_not_exclude(self, env):
        store, gw, sched = env
        jid = _seed_journey(store, gw, stage=STAGE_LINE_ENGAGED,
                            intimacy=60.0, updated_ago_days=5)
        # 10 天前 reactivate 过 → 超出 cooldown 7 天 → 仍可选
        with store._lock:
            import uuid
            store._conn.execute(
                "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
                "VALUES (?, ?, '', 'reactivation_sent', '{}', ?)",
                (uuid.uuid4().hex, jid, int(time.time()) - 10 * 86400),
            )
            store._conn.commit()
        cands = sched.list_candidates()
        assert len(cands) == 1
        assert cands[0].last_reactivation_ts > 0


class TestMarkSent:
    def test_mark_writes_event(self, env):
        store, gw, sched = env
        jid = _seed_journey(store, gw, stage=STAGE_LINE_ENGAGED,
                            intimacy=60.0, updated_ago_days=5)
        sched.mark_sent(jid, note="daily_morning_ping")
        events = store.list_events(jid)
        react = [e for e in events if e["event_type"] == "reactivation_sent"]
        assert len(react) == 1
        assert react[0]["payload"]["note"] == "daily_morning_ping"
        # 再调 list_candidates → 被 cooldown 排除
        assert sched.list_candidates() == []


class TestLimit:
    def test_limit_respected(self, tmp_path):
        store = ContactStore(db_path=tmp_path / "contacts.db")
        gw = ContactGateway(store, HandoffTokenService(store, ttl_seconds=3600),
                             MergeService(store))
        sched = ReactivationScheduler(store, min_silent_days=1, min_intimacy=0,
                                       cooldown_days=7, limit=3)
        # 造 5 个 eligible
        for i in range(5):
            _seed_journey(store, gw, stage=STAGE_LINE_ENGAGED,
                          intimacy=50.0, updated_ago_days=5, fb_id=f"fb_{i}")
        assert len(sched.list_candidates()) == 3
        store.close()
