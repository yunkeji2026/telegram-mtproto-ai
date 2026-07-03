"""IntimacyEngine 单元测试。"""

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
from src.contacts.models import CHANNEL_MESSENGER
from src.skills.intimacy_engine import IntimacyEngine


@pytest.fixture
def env(tmp_path):
    store = ContactStore(db_path=tmp_path / "contacts.db")
    gw = ContactGateway(store, HandoffTokenService(store, ttl_seconds=3600), MergeService(store))
    engine = IntimacyEngine(store)
    yield store, gw, engine
    store.close()


def _fake_events(store, journey_id, pattern, *, start_ts=None):
    """按 pattern 写 msg_in/msg_out 事件（直接改 events 表）。

    pattern: list of (event_type, ts_offset_seconds_from_start)
    """
    start = start_ts if start_ts is not None else int(time.time())
    for et, off in pattern:
        with store._lock:
            import uuid, json as _json
            store._conn.execute(
                "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
                "VALUES (?, ?, '', ?, '{}', ?)",
                (uuid.uuid4().hex, journey_id, et, start + off),
            )
            store._conn.commit()


class TestEmptyJourney:
    def test_empty_score_zero(self, env):
        store, gw, eng = env
        ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        bd = eng.compute_intimacy(ctx.journey.journey_id)
        # contact_created 事件也会被读，但没有 msg_in/msg_out
        assert bd.score == 0.0
        assert bd.turn_count_in == 0


class TestTurnCount:
    def test_one_msg_in(self, env):
        store, gw, eng = env
        ctx = gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi")
        bd = eng.compute_intimacy(ctx.journey.journey_id)
        # turns = 1/20 = 0.05 * 0.25 = 0.0125
        # mutuality = 0（没 msg_out）
        # days = 1/5 = 0.2 * 0.25 = 0.05
        # recency ≈ 1 * 0.25 = 0.25
        # total ≈ 0.3125 → 31.3
        assert 30 < bd.score < 35
        assert bd.turn_count_in == 1
        assert bd.turn_count_out == 0


class TestMutuality:
    def test_balanced_high_mutuality(self, env):
        store, gw, eng = env
        ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        jid = ctx.journey.journey_id
        # 5 in + 5 out — events at start..start+270, compute at start+300
        # （明确传 now 以避免 "事件相对 now 在未来" 被过滤 / 2026-05-17）
        import time as _t
        start = int(_t.time())
        pattern = []
        for i in range(5):
            pattern.append(("msg_in", i * 60))
            pattern.append(("msg_out", i * 60 + 30))
        _fake_events(store, jid, pattern, start_ts=start)
        bd = eng.compute_intimacy(jid, now=start + 300)
        assert bd.contributions["mutuality"] == 0.25   # 满
        # turns=5/20=0.0625 权重 0.25 → 0.0156
        # days=1/5=0.2 → 0.05 * 0.25 贡献 0.05
        # recency ≈ 1 * 0.25
        assert bd.score > 55

    def test_one_sided_low_mutuality(self, env):
        store, gw, eng = env
        ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        jid = ctx.journey.journey_id
        # 10 in, 0 out
        import time as _t
        start = int(_t.time())
        _fake_events(
            store, jid, [("msg_in", i * 60) for i in range(10)], start_ts=start,
        )
        bd = eng.compute_intimacy(jid, now=start + 600)
        assert bd.contributions["mutuality"] == 0.0


class TestActiveDays:
    def test_5_days_sat(self, env):
        store, gw, eng = env
        ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        jid = ctx.journey.journey_id
        now = int(time.time())
        # 5 条 msg_in 分布在 5 个不同日
        pattern = [("msg_in", -i * 86400) for i in range(5)]  # 0, -1d, -2d, ...
        _fake_events(store, jid, pattern, start_ts=now)
        bd = eng.compute_intimacy(jid, now=now)
        assert bd.active_days_7d == 5
        assert bd.contributions["active_days_7d"] == 0.25


class TestRecency:
    def test_fresh_msg_full_recency(self, env):
        store, gw, eng = env
        ctx = gw.on_message(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
                             direction="in", text_preview="hi")
        bd = eng.compute_intimacy(ctx.journey.journey_id)
        # 刚刚发的 → recency 几乎 1
        assert bd.contributions["recency"] >= 0.24

    def test_14_days_old_half_recency(self, env):
        store, gw, eng = env
        ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        jid = ctx.journey.journey_id
        now = int(time.time())
        _fake_events(store, jid, [("msg_in", -14 * 86400)], start_ts=now)
        bd = eng.compute_intimacy(jid, now=now)
        # 14 天半衰期 → recency ≈ 0.5，加权 0.125
        assert 0.11 < bd.contributions["recency"] < 0.14

    def test_never_active_zero_recency(self, env):
        store, gw, eng = env
        ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        bd = eng.compute_intimacy(ctx.journey.journey_id)
        assert bd.contributions["recency"] == 0.0


class TestRefresh:
    def test_refresh_writes_to_journey(self, env):
        store, gw, eng = env
        ctx = gw.on_message(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
                             direction="in", text_preview="hi")
        bd = eng.refresh_journey_intimacy(ctx.journey.journey_id)
        j = store.get_journey(ctx.journey.journey_id)
        assert j.intimacy_score == bd.score
        assert j.intimacy_updated_at > 0


class TestCappedScore:
    def test_even_perfect_stays_bounded(self, env):
        store, gw, eng = env
        ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        jid = ctx.journey.journey_id
        now = int(time.time())
        # 大量双向消息 + 7 天活跃
        pattern = []
        for d in range(7):
            for i in range(3):
                pattern.append(("msg_in", -d * 86400 + i * 60))
                pattern.append(("msg_out", -d * 86400 + i * 60 + 30))
        _fake_events(store, jid, pattern, start_ts=now)
        bd = eng.compute_intimacy(jid, now=now)
        assert 0 <= bd.score <= 100
        # 应该很高
        assert bd.score > 80


class TestSilenceDecay:
    """P-W3D2.4 (2026-05-05) 沉默衰减测试。
    防"长期沉默用户仍被 reactivation 骚扰"。"""

    def _build_active_then_silent(self, store, gw, days_silent: int):
        """构造一个曾活跃 30 轮、然后沉默 N 天的 journey。"""
        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_x",
        )
        jid = ctx.journey.journey_id
        now = int(time.time())
        active_start = now - days_silent * 86400
        # 在活跃期前后插 30 轮双向消息（10 个不同日 × 3 轮）
        pattern = []
        for d in range(10):
            for i in range(3):
                pattern.append(("msg_in", -d * 86400 + i * 60))
                pattern.append(("msg_out", -d * 86400 + i * 60 + 30))
        _fake_events(store, jid, pattern, start_ts=active_start)
        return jid, now

    def test_no_decay_within_grace_period(self, env):
        """7 天 grace 内不衰减。"""
        store, gw, eng = env
        jid, now = self._build_active_then_silent(store, gw, days_silent=5)
        bd = eng.compute_intimacy(jid, now=now)
        # 5 天 < 7 天 grace → silence_decay = 1.0
        assert bd.contributions["silence_decay"] == 1.0

    def test_decay_after_30_days_silence(self, env):
        """沉默 30 天后衰减明显。"""
        store, gw, eng = env
        jid, now = self._build_active_then_silent(store, gw, days_silent=30)
        bd = eng.compute_intimacy(jid, now=now)
        # 30 天 = 23 天衰减期 ≈ 3.3 周 × 0.95^x ≈ 0.85
        assert 0.75 < bd.contributions["silence_decay"] < 0.90
        # score 应明显下降
        assert bd.score < 60

    def test_decay_after_60_days_pushes_below_reactivation_threshold(self, env):
        """沉默 60 天后 score < 40（reactivation 默认阈值）。"""
        store, gw, eng = env
        jid, now = self._build_active_then_silent(store, gw, days_silent=60)
        bd = eng.compute_intimacy(jid, now=now)
        # 60 天 = 53 天衰减 ≈ 7.6 周 × 0.95^7.6 ≈ 0.68
        assert bd.contributions["silence_decay"] < 0.75
        # 触发 reactivation 退出（< 40 阈值）
        assert bd.score < 40

    def test_breakdown_contains_silence_decay_field(self, env):
        """contributions 字典必含 silence_decay 让看板可观察。"""
        store, gw, eng = env
        ctx = gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_y",
            direction="in", text_preview="hi",
        )
        bd = eng.compute_intimacy(ctx.journey.journey_id)
        assert "silence_decay" in bd.contributions


class TestRefreshStaleJourneys:
    """沉默衰减「物化」：把 compute 的 live 衰减周期性写回 stored intimacy_score 列，
    修「沉默 journey 无 msg_in → stored 列冻结高分 → reactivation 反复捞死号」。"""

    def _active_journey(self, store, gw, external_id, *, start):
        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id=external_id)
        jid = ctx.journey.journey_id
        pattern = []
        for d in range(3):
            for k in range(4):
                pattern.append(("msg_in", d * 86400 + k * 60))
                pattern.append(("msg_out", d * 86400 + k * 60 + 30))
        _fake_events(store, jid, pattern, start_ts=start)
        return jid

    def test_materializes_decay_into_stored_column(self, env):
        store, gw, eng = env
        start = 1_700_000_000
        jid = self._active_journey(store, gw, "fb_stale", start=start)
        last_active = start + 2 * 86400 + 300
        eng.refresh_journey_intimacy(jid, now=last_active)
        s0 = store.get_journey(jid).intimacy_score
        assert s0 > 0
        assert store.get_journey(jid).intimacy_updated_at == last_active

        # 60 天沉默后物化衰减
        now2 = last_active + 60 * 86400
        assert eng.refresh_stale_journeys(now=now2, stale_after_s=3600) == 1
        j = store.get_journey(jid)
        assert j.intimacy_score < s0            # 衰减已写回 stored 列
        assert j.intimacy_updated_at == now2

    def test_skips_fresh_and_never_computed(self, env):
        store, gw, eng = env
        start = 1_700_000_000
        # fresh：刚 refresh，intimacy_updated_at=start 未过期
        jid = self._active_journey(store, gw, "fb_fresh", start=start)
        eng.refresh_journey_intimacy(jid, now=start)
        # never-computed：on_peer_seen 默认 intimacy_score=0 / intimacy_updated_at=0
        gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_zero")

        # cutoff = (start+10) - 3600 < start → fresh 不过期；zero 的 updated_at=0 被排除
        assert eng.refresh_stale_journeys(now=start + 10, stale_after_s=3600) == 0

    def test_idempotent_within_stale_window(self, env):
        store, gw, eng = env
        start = 1_700_000_000
        jid = self._active_journey(store, gw, "fb_x", start=start)
        eng.refresh_journey_intimacy(jid, now=start + 100)
        now2 = start + 100 + 10 * 86400
        assert eng.refresh_stale_journeys(now=now2, stale_after_s=3600) == 1
        # 刚写回 → intimacy_updated_at=now2 未过期，同一时刻再扫为 0
        assert eng.refresh_stale_journeys(now=now2, stale_after_s=3600) == 0

    def test_limit_caps_per_iteration(self, env):
        store, gw, eng = env
        start = 1_700_000_000
        for i in range(3):
            jid = self._active_journey(store, gw, f"fb_{i}", start=start)
            eng.refresh_journey_intimacy(jid, now=start)
        now2 = start + 30 * 86400
        # 单轮上限 2 → 先刷 2 个，剩 1 个下轮再刷
        assert eng.refresh_stale_journeys(now=now2, stale_after_s=3600, limit=2) == 2
        assert eng.refresh_stale_journeys(now=now2, stale_after_s=3600, limit=2) == 1

    def test_count_stale_matches_refresh_filter(self, env):
        store, gw, eng = env
        start = 1_700_000_000
        for i in range(3):
            jid = self._active_journey(store, gw, f"fb_{i}", start=start)
            eng.refresh_journey_intimacy(jid, now=start)
        now2 = start + 30 * 86400
        # gauge 不受 limit 截断、不写库：3 个全过期
        assert eng.count_stale_journeys(now=now2, stale_after_s=3600) == 3
        # 刷掉 2 个（limit=2）→ 积压降到 1
        assert eng.refresh_stale_journeys(now=now2, stale_after_s=3600, limit=2) == 2
        assert eng.count_stale_journeys(now=now2, stale_after_s=3600) == 1

    def test_count_stale_excludes_fresh_and_zero(self, env):
        store, gw, eng = env
        start = 1_700_000_000
        jid = self._active_journey(store, gw, "fb_fresh", start=start)
        eng.refresh_journey_intimacy(jid, now=start)
        gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_zero")
        # fresh 未过期 + zero 的 updated_at=0 → 积压 0
        assert eng.count_stale_journeys(now=start + 10, stale_after_s=3600) == 0
