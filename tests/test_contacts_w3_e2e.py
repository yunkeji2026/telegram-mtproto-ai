"""W3 扩展 E2E：FSM decay + IntimacyEngine + Readiness + Reactivation 全打通。

新增场景：
  W3-S1. 对话积累 → intimacy 递增 → readiness 告别触发 → handoff 签发
  W3-S2. handoff 发出后 72h+ 沉默 → 自动降级 LOST_HANDOFF
  W3-S3. LINE_ENGAGED 后 4 天沉默 → 进入 reactivation 候选 → runner ping 后 cooldown
  W3-S4. 完整漏斗：INITIAL→ENGAGED→HANDOFF_READY→HANDOFF_SENT→LINE_ENGAGED + Funnel 统计
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.contacts.gateway import ContactGateway
from src.contacts.journey_fsm import apply_silence_decay
from src.contacts.models import (
    CHANNEL_MESSENGER, CHANNEL_LINE,
    STAGE_ENGAGED, STAGE_HANDOFF_READY, STAGE_HANDOFF_SENT,
    STAGE_LINE_ENGAGED, STAGE_LOST_HANDOFF,
)
from src.skills.intimacy_engine import IntimacyEngine
from src.skills.handoff_readiness import HandoffReadinessScorer
from src.skills.reactivation_scheduler import ReactivationScheduler


@pytest.fixture
def env(tmp_path):
    store = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(store, ttl_seconds=72 * 3600)
    merge = MergeService(store)
    gw = ContactGateway(store, handoff, merge)
    intim = IntimacyEngine(store)
    scorer = HandoffReadinessScorer(store, intim, turn_saturation=3, open_threshold=70.0)
    reactivator = ReactivationScheduler(
        store, min_silent_days=3, min_intimacy=40.0, cooldown_days=7)
    yield store, gw, intim, scorer, reactivator
    store.close()


def _seed_multi_day_chat(store, gw, *, fb_id, days, msgs_per_day):
    """造跨天聊天：用 fake events 让 active_days 真实反映。"""
    ctx = gw.on_peer_seen(
        channel=CHANNEL_MESSENGER, account_id="acc", external_id=fb_id,
        display_name="Alice", language_hint="zh", timezone_hint="Asia/Shanghai",
    )
    store.update_contact(ctx.contact.contact_id,
                          primary_name="Alice",
                          language_hint="zh", timezone_hint="Asia/Shanghai")
    jid = ctx.journey.journey_id
    now = int(time.time())
    with store._lock:
        for d in range(days):
            for i in range(msgs_per_day):
                for et in ("msg_in", "msg_out"):
                    store._conn.execute(
                        "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
                        "VALUES (?, ?, '', ?, '{}', ?)",
                        (uuid.uuid4().hex, jid, et, now - d * 86400 - i * 60),
                    )
        # 推到 ENGAGED（模拟 on_message 已推过）
        store._conn.execute(
            "UPDATE journeys SET funnel_stage=?, updated_at=? WHERE journey_id=?",
            (STAGE_ENGAGED, now, jid))
        store._conn.commit()
    return ctx


class TestW3S1_ReadinessDrivenHandoff:
    def test_intimacy_grows_readiness_opens_handoff_fires(self, env):
        store, gw, intim, scorer, _ = env
        ctx = _seed_multi_day_chat(
            store, gw, fb_id="fb_alice", days=5, msgs_per_day=4)
        jid = ctx.journey.journey_id

        # 验证 intimacy 爬高
        bd = intim.refresh_journey_intimacy(jid)
        assert bd.score >= 70

        # 普通对话：readiness 不开窗
        d1 = scorer.evaluate(jid, latest_in_text="你今天吃了啥")
        assert d1.score >= 70
        assert d1.window_open is False

        # 告别场景：readiness 开窗 + 业务层签发 token
        d2 = scorer.evaluate(jid, latest_in_text="我去睡啦 晚安～")
        assert d2.window_open is True

        # 业务层：开窗后签发 token
        tok = gw.issue_handoff(messenger_ci_id=ctx.channel_identity.channel_identity_id)
        assert tok.token

        # Journey 进入 HANDOFF_READY
        j2 = store.get_journey(jid)
        assert j2.funnel_stage == STAGE_HANDOFF_READY


class TestW3S2_DecayAfterHandoffSent:
    def test_handoff_sent_72h_silence_lost(self, env):
        store, gw, _, _, _ = env
        ctx = _seed_multi_day_chat(
            store, gw, fb_id="fb_bob", days=3, msgs_per_day=2)
        tok = gw.issue_handoff(messenger_ci_id=ctx.channel_identity.channel_identity_id)
        gw.on_handoff_sent(
            messenger_ci_id=ctx.channel_identity.channel_identity_id, token=tok.token)

        # 把 updated_at 设成 73 小时前（模拟沉默）
        past = int(time.time()) - 73 * 3600
        with store._lock:
            store._conn.execute(
                "UPDATE journeys SET updated_at=? WHERE journey_id=?",
                (past, ctx.journey.journey_id))
            store._conn.commit()

        count = apply_silence_decay(store)
        assert count >= 1
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == STAGE_LOST_HANDOFF
        # 留下 silence_decay 事件
        events = store.list_events(j.journey_id)
        assert any(e["event_type"] == "silence_decay" for e in events)


class TestW3S3_ReactivationAfterLineEngaged:
    def test_line_engaged_silent_gets_reactivated(self, env):
        store, gw, _, _, reactivator = env
        # 先走到 LINE_ENGAGED
        ctx = _seed_multi_day_chat(
            store, gw, fb_id="fb_cindy", days=5, msgs_per_day=3)
        tok = gw.issue_handoff(messenger_ci_id=ctx.channel_identity.channel_identity_id)
        gw.on_handoff_sent(
            messenger_ci_id=ctx.channel_identity.channel_identity_id, token=tok.token)
        # LINE 首条带 token → 合并 → LINE_ENGAGED
        outcome = gw.on_line_first_text(
            account_id="acc", external_id="line_cindy",
            text=f"hi {tok.token}", display_name="Alice",
        )
        assert outcome.merged is True

        j = store.get_journey_by_contact(outcome.contact_id)
        assert j.funnel_stage == STAGE_LINE_ENGAGED

        # 现在模拟沉默 5 天
        past = int(time.time()) - 5 * 86400
        with store._lock:
            store._conn.execute(
                "UPDATE journeys SET updated_at=?, intimacy_score=60.0 "
                "WHERE journey_id=?",
                (past, j.journey_id))
            store._conn.commit()

        cands = reactivator.list_candidates()
        matched = [c for c in cands if c.journey_id == j.journey_id]
        assert len(matched) == 1
        assert matched[0].silent_days >= 4.5

        # runner ping 完后打标
        reactivator.mark_sent(j.journey_id, note="morning_ping")
        assert reactivator.list_candidates() == []  # cooldown 排除


class TestW3S4_FullFunnelWithStats:
    def test_funnel_counts_reflect_reality(self, env):
        store, gw, _, _, _ = env
        # 3 人不同进度
        # Alice: LINE_ENGAGED（完整引流）
        ctx_a = _seed_multi_day_chat(
            store, gw, fb_id="fb_a", days=3, msgs_per_day=3)
        tok = gw.issue_handoff(messenger_ci_id=ctx_a.channel_identity.channel_identity_id)
        gw.on_handoff_sent(
            messenger_ci_id=ctx_a.channel_identity.channel_identity_id, token=tok.token)
        gw.on_line_first_text(
            account_id="acc", external_id="line_a",
            text=f"hi {tok.token}", display_name="Alice",
        )
        # Bob: HANDOFF_SENT（待加）
        ctx_b = _seed_multi_day_chat(
            store, gw, fb_id="fb_b", days=2, msgs_per_day=2)
        tok2 = gw.issue_handoff(messenger_ci_id=ctx_b.channel_identity.channel_identity_id)
        gw.on_handoff_sent(
            messenger_ci_id=ctx_b.channel_identity.channel_identity_id, token=tok2.token)
        # Cindy: ENGAGED（还在聊）
        _seed_multi_day_chat(
            store, gw, fb_id="fb_c", days=1, msgs_per_day=1)

        stats = store.count_journeys_by_stage()
        assert stats.get(STAGE_LINE_ENGAGED, 0) == 1
        assert stats.get(STAGE_HANDOFF_SENT, 0) == 1
        assert stats.get(STAGE_ENGAGED, 0) == 1
        assert store.count_contacts() == 3    # 合并后 Alice+LINE 只算 1

        channels = store.count_channel_identities_by_channel()
        assert channels.get(CHANNEL_MESSENGER, 0) == 3
        assert channels.get(CHANNEL_LINE, 0) == 1


class TestW3S6_DecayReactivationCoordination:
    """Decay（LINE_ACCEPTED 24h → LOST_LINE_SILENT）在 Reactivation（3 天 min_silent）前触发。

    预期协同：一旦 decay 成 LOST，reactivation 不会再看到它（stage 不在白名单）。
    """

    def test_lost_line_silent_not_picked_up_by_reactivator(self, env):
        from src.contacts.models import STAGE_LINE_ACCEPTED, STAGE_LOST_LINE_SILENT
        store, gw, _, _, reactivator = env
        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_d1")
        past = int(time.time()) - 25 * 3600     # 25h before
        with store._lock:
            store._conn.execute(
                "UPDATE journeys SET funnel_stage=?, intimacy_score=60.0, updated_at=? "
                "WHERE journey_id=?",
                (STAGE_LINE_ACCEPTED, past, ctx.journey.journey_id))
            store._conn.commit()
        # 先跑 decay
        apply_silence_decay(store)
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == STAGE_LOST_LINE_SILENT
        # reactivator 白名单不含 LOST_* → 不选
        assert reactivator.list_candidates() == []


class TestW3_3B5_MergePreservesIntimacy:
    """W3-3B.5：合并必须保留 messenger 侧已经积累的 intimacy 历史。

    回归保护：之前 messenger 聊了 5 天积累的 intimacy_score 不能因为 LINE
    合并而清零；下次回话时 reunion 信号才有依据。
    """

    def test_token_merge_preserves_messenger_intimacy(self, env):
        """Token 合并：messenger 已积累的 intimacy 在合并后仍在新 journey 上。"""
        store, gw, intim, _, _ = env
        # Step 1: messenger 侧多日聊天 → intimacy 起来
        m_ctx = _seed_multi_day_chat(
            store, gw, fb_id="fb_dora", days=5, msgs_per_day=4,
        )
        bd_before = intim.refresh_journey_intimacy(m_ctx.journey.journey_id)
        intimacy_before = bd_before.score
        assert intimacy_before > 20, "5 天 4 条对话应该积累出 intimacy>20"
        messenger_contact_id = m_ctx.contact.contact_id

        # Step 2: 签发 token + 推 HANDOFF_SENT
        tok = gw.issue_handoff(messenger_ci_id=m_ctx.channel_identity.channel_identity_id)
        assert tok and tok.token
        gw.on_handoff_sent(
            messenger_ci_id=m_ctx.channel_identity.channel_identity_id, token=tok.token,
        )

        # Step 3: LINE 侧用 token 合并
        outcome = gw.on_line_first_text(
            account_id="a", external_id="line_dora",
            text=f"嗨 {tok.token}", display_name="Dora",
        )
        assert outcome.merged is True
        assert outcome.via == "token"
        # 合并后的 contact_id 应该等于 messenger 侧（不是新建的 LINE 那个）
        assert outcome.contact_id == messenger_contact_id

        # Step 4：合并后的 journey 仍带着 intimacy
        new_journey = store.get_journey_by_contact(messenger_contact_id)
        assert new_journey is not None
        assert new_journey.intimacy_score == intimacy_before, (
            f"合并不能清零 intimacy: before={intimacy_before} after={new_journey.intimacy_score}"
        )

    def test_signal_merge_via_approve_preserves_intimacy(self, env):
        """Heuristic 合并 + 运营 approve：同样不能丢 intimacy。"""
        from src.contacts.merge import MergeService
        store, gw, intim, _, _ = env
        merge_svc = MergeService(store)
        # messenger 侧积累 intimacy
        m_ctx = _seed_multi_day_chat(
            store, gw, fb_id="fb_evan", days=5, msgs_per_day=4,
        )
        # 在 Contact 上写完整画像，以便后面 signal 匹配
        store.update_contact(
            m_ctx.contact.contact_id,
            primary_name="Evan", language_hint="en", timezone_hint="Asia/Tokyo",
        )
        bd_before = intim.refresh_journey_intimacy(m_ctx.journey.journey_id)
        intimacy_before = bd_before.score
        messenger_contact_id = m_ctx.contact.contact_id

        # 签发 token（让候选池非空）
        gw.issue_handoff(messenger_ci_id=m_ctx.channel_identity.channel_identity_id)

        # LINE 侧来一条没带 token 但信号匹配的消息 → 入 review
        outcome = gw.on_line_first_text(
            account_id="a", external_id="line_evan",
            text="hello",
            display_name="Evan",
            language_hint="en", timezone_hint="Asia/Tokyo",
        )
        # 信号都满，可能 auto_merge 或 manual_review
        # 不管走哪条路径，最终 contact 必须是 messenger 那个 + intimacy 保留
        if outcome.merged:
            final_contact = outcome.contact_id
        else:
            assert outcome.review_id, "应该入 review 队列"
            ok = merge_svc.approve_review(outcome.review_id, resolved_by="ops_test")
            assert ok
            final_contact = messenger_contact_id

        assert final_contact == messenger_contact_id
        final_journey = store.get_journey_by_contact(messenger_contact_id)
        assert final_journey.intimacy_score == intimacy_before, (
            f"合并清零 intimacy 是严重 regression: before={intimacy_before} "
            f"after={final_journey.intimacy_score}"
        )

    def test_merge_preserves_history_replay_consistency(self, env):
        """W3-3D.5：合并后 30 天历史重放 = 合并前两 journey 历史的并集。

        关键回归保护：``relink_channel_identity`` 把老 journey 的事件搬到新
        journey，保证 ``compute_intimacy_from_events(now=past_ts)`` 对任意历史时间
        点都能产出与合并前一致的快照。
        """
        from src.skills.intimacy_engine import IntimacyEngine
        store, gw, intim, _, _ = env

        # 造 messenger 侧 journey + 5 天内多次互动
        m_ctx = _seed_multi_day_chat(
            store, gw, fb_id="fb_zoe", days=5, msgs_per_day=3,
        )
        m_jid = m_ctx.journey.journey_id

        # 取合并前的历史快照（每天一个点，最近 7 天）
        import time as _t
        now = int(_t.time())
        day_secs = 86400
        today_end = (now // day_secs) * day_secs + day_secs - 1
        m_events_before = store.list_events(m_jid, limit=500)
        before_snapshots = []
        for i in range(6, -1, -1):
            day_ts = today_end - i * day_secs
            bd = IntimacyEngine.compute_intimacy_from_events(
                m_events_before, now=day_ts,
            )
            before_snapshots.append((day_ts, bd.score))

        # 触发 token merge
        tok = gw.issue_handoff(messenger_ci_id=m_ctx.channel_identity.channel_identity_id)
        gw.on_handoff_sent(
            messenger_ci_id=m_ctx.channel_identity.channel_identity_id, token=tok.token,
        )
        outcome = gw.on_line_first_text(
            account_id="a", external_id="line_zoe",
            text=f"嗨 {tok.token}", display_name="Zoe",
        )
        assert outcome.merged

        # 合并后从新 journey 取事件，做相同时间点的快照
        new_journey = store.get_journey_by_contact(outcome.contact_id)
        new_events = store.list_events(new_journey.journey_id, limit=500)
        for day_ts, before_score in before_snapshots:
            # 跳过合并发生那一天（合并事件本身会引入 channel_identity_merged 等
            # 非 msg_in/msg_out 事件，不影响 score 计算；但若那天是「今天」则因为
            # token_issued / handoff_sent 后还有微弱时间偏移可能 score 微变）
            if day_ts >= today_end - day_secs:
                continue
            bd_after = IntimacyEngine.compute_intimacy_from_events(
                new_events, now=day_ts,
            )
            assert abs(bd_after.score - before_score) < 0.5, (
                f"合并后历史重放不一致: day_ts={day_ts} "
                f"before={before_score} after={bd_after.score}"
            )

    def test_merge_preserves_journey_events_for_future_reunion(self, env):
        """合并后老 journey 的 events 应迁到新 journey（reunion 判定依赖事件历史）。"""
        store, gw, intim, _, _ = env
        m_ctx = _seed_multi_day_chat(
            store, gw, fb_id="fb_fred", days=3, msgs_per_day=3,
        )
        msg_events_before = len(store.list_events(m_ctx.journey.journey_id, limit=500))
        assert msg_events_before > 0

        tok = gw.issue_handoff(messenger_ci_id=m_ctx.channel_identity.channel_identity_id)
        gw.on_handoff_sent(
            messenger_ci_id=m_ctx.channel_identity.channel_identity_id, token=tok.token,
        )
        events_after_handoff = len(store.list_events(m_ctx.journey.journey_id, limit=500))

        outcome = gw.on_line_first_text(
            account_id="a", external_id="line_fred",
            text=f"嗨 {tok.token}", display_name="Fred",
        )
        assert outcome.merged

        # 合并后 messenger 侧 journey 必须包含合并事件 + 原有事件
        new_journey = store.get_journey_by_contact(outcome.contact_id)
        all_events = store.list_events(new_journey.journey_id, limit=500)
        # 至少应有：原有事件 + 合并相关事件
        assert len(all_events) >= events_after_handoff
        types = {e["event_type"] for e in all_events}
        assert "channel_identity_merged" in types, "合并必须留事件痕迹"


class TestW3S5_IntimacyRecencyDecay:
    def test_inactive_contact_intimacy_decays_over_time(self, env):
        """验证 recency 半衰期：14 天前的对话 intimacy 贡献减半。"""
        store, gw, intim, _, _ = env
        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        jid = ctx.journey.journey_id
        now = int(time.time())

        # 把 5 条 msg_in 写成"14 天前"
        with store._lock:
            for i in range(5):
                store._conn.execute(
                    "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
                    "VALUES (?, ?, '', 'msg_in', '{}', ?)",
                    (uuid.uuid4().hex, jid, now - 14 * 86400 - i * 60),
                )
            store._conn.commit()

        bd_old = intim.compute_intimacy(jid, now=now)
        # 和"刚刚发 5 条"对照
        ctx2 = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_2")
        jid2 = ctx2.journey.journey_id
        with store._lock:
            for i in range(5):
                store._conn.execute(
                    "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
                    "VALUES (?, ?, '', 'msg_in', '{}', ?)",
                    (uuid.uuid4().hex, jid2, now - i * 60),
                )
            store._conn.commit()
        bd_new = intim.compute_intimacy(jid2, now=now)

        # 14 天前 recency ≈ 0.5；刚刚 ≈ 1.0
        assert bd_new.score > bd_old.score
        assert bd_new.contributions["recency"] > bd_old.contributions["recency"]
