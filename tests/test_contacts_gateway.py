"""ContactGateway 集成测试（走完 token / signal / 孤岛 三条合并路径）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.contacts.gateway import ContactGateway, new_trace_id
from src.contacts.models import (
    CHANNEL_LINE,
    CHANNEL_MESSENGER,
    STAGE_ENGAGED,
    STAGE_HANDOFF_READY,
    STAGE_HANDOFF_SENT,
    STAGE_INITIAL,
    STAGE_LINE_ENGAGED,
)


@pytest.fixture
def wiring(tmp_path):
    store = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(store, ttl_seconds=3600)
    merge = MergeService(store)
    gw = ContactGateway(store, handoff, merge)
    yield store, handoff, merge, gw
    store.close()


class TestPeerSeen:
    def test_first_time_creates_everything(self, wiring):
        _, _, _, gw = wiring
        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="acc-A", external_id="fb_1",
            display_name="Alice",
        )
        assert ctx.is_new is True
        assert ctx.contact.primary_name == "Alice"
        assert ctx.channel_identity.channel == CHANNEL_MESSENGER
        assert ctx.journey.funnel_stage == STAGE_INITIAL

    def test_second_time_idempotent(self, wiring):
        _, _, _, gw = wiring
        a = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="x", external_id="y")
        b = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="x", external_id="y")
        assert a.is_new is True
        assert b.is_new is False
        assert a.contact.contact_id == b.contact.contact_id


class TestOnMessage:
    def test_msg_in_pushes_to_engaged(self, wiring):
        store, _, _, gw = wiring
        ctx = gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="你好",
        )
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == STAGE_ENGAGED
        # 落了事件：contact_created（ensure 时）+ msg_in + stage_change
        events = store.list_events(j.journey_id)
        types = {e["event_type"] for e in events}
        assert {"contact_created", "msg_in", "stage_change"}.issubset(types)

    def test_msg_out_does_not_push_to_engaged(self, wiring):
        store, _, _, gw = wiring
        ctx = gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="out", text_preview="hi",
        )
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == STAGE_INITIAL  # 只有 msg_in 才触发

    def test_bad_direction_returns_none(self, wiring):
        _, _, _, gw = wiring
        assert gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="upward", text_preview="x",
        ) is None


class TestStoryCompletion:
    """剧情收场镜像：落 story_complete 事件，但不动 intimacy 事实源。"""

    def test_appends_story_complete_event(self, wiring):
        store, _, _, gw = wiring
        eid = gw.record_story_completion(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            scenario_id="coffee_date", ending="warm", intimacy_bonus=4.0,
            title="初次约会",
        )
        assert eid
        j = store.get_journey_by_contact(
            store.get_ci_by_external(CHANNEL_MESSENGER, "a", "fb_1").contact_id)
        evs = [e for e in store.list_events(j.journey_id)
               if e["event_type"] == "story_complete"]
        assert len(evs) == 1
        pl = evs[0]["payload"]
        assert pl["scenario_id"] == "coffee_date"
        assert pl["ending"] == "warm"
        assert pl["intimacy_bonus"] == 4.0
        assert pl["title"] == "初次约会"

    def test_does_not_touch_intimacy_score(self, wiring):
        store, _, _, gw = wiring
        # 先建 journey（intimacy_score 默认 0）
        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_2")
        before = store.get_journey(ctx.journey.journey_id).intimacy_score
        gw.record_story_completion(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_2",
            scenario_id="coffee_date", intimacy_bonus=9.0)
        after = store.get_journey(ctx.journey.journey_id).intimacy_score
        assert after == before  # 镜像不重算/不写 intimacy_score

    def test_negative_bonus_clamped_to_zero(self, wiring):
        store, _, _, gw = wiring
        gw.record_story_completion(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_3",
            scenario_id="x", intimacy_bonus=-5)
        j = store.get_journey_by_contact(
            store.get_ci_by_external(CHANNEL_MESSENGER, "a", "fb_3").contact_id)
        ev = [e for e in store.list_events(j.journey_id)
              if e["event_type"] == "story_complete"][0]
        assert ev["payload"]["intimacy_bonus"] == 0.0

    def test_long_text_preview_truncated(self, wiring):
        store, _, _, gw = wiring
        ctx = gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="x" * 300,
        )
        events = store.list_events(ctx.journey.journey_id)
        msg_in = [e for e in events if e["event_type"] == "msg_in"][0]
        assert len(msg_in["payload"]["preview"]) == 120


class TestIssueHandoff:
    def test_issue_moves_stage_to_handoff_ready(self, wiring):
        store, _, _, gw = wiring
        # 先 engage
        ctx = gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi",
        )
        tok = gw.issue_handoff(messenger_ci_id=ctx.channel_identity.channel_identity_id)
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == STAGE_HANDOFF_READY
        events = store.list_events(j.journey_id)
        assert any(e["event_type"] == "token_issued" for e in events)
        assert tok.issued_from_ci_id == ctx.channel_identity.channel_identity_id

    def test_issue_on_line_ci_rejected(self, wiring):
        store, _, _, gw = wiring
        ctx = gw.on_peer_seen(channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        with pytest.raises(ValueError):
            gw.issue_handoff(messenger_ci_id=ctx.channel_identity.channel_identity_id)

    def test_issue_unknown_ci_rejected(self, wiring):
        _, _, _, gw = wiring
        with pytest.raises(ValueError):
            gw.issue_handoff(messenger_ci_id="nonexistent")

    def test_handoff_sent_moves_stage(self, wiring):
        store, _, _, gw = wiring
        ctx = gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi",
        )
        tok = gw.issue_handoff(messenger_ci_id=ctx.channel_identity.channel_identity_id)
        gw.on_handoff_sent(
            messenger_ci_id=ctx.channel_identity.channel_identity_id, token=tok.token,
        )
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == STAGE_HANDOFF_SENT


class TestLineFirstText_Token:
    def test_token_path_merges_and_stage_moves(self, wiring):
        store, _, _, gw = wiring
        # 起势：Messenger 一侧有用户 + 签发 token + 发过引流
        msg_ctx = gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="acc", external_id="fb_1",
            direction="in", text_preview="hi",
            display_name="Alice",
        )
        tok = gw.issue_handoff(messenger_ci_id=msg_ctx.channel_identity.channel_identity_id)
        gw.on_handoff_sent(
            messenger_ci_id=msg_ctx.channel_identity.channel_identity_id, token=tok.token,
        )
        # LINE 一侧：对方加过来后发首条含 token
        outcome = gw.on_line_first_text(
            account_id="acc", external_id="line_xx", display_name="Alice",
            text=f"嗨我加上了 暗号 {tok.token}~",
        )
        assert outcome.merged is True
        assert outcome.via == "token"
        assert outcome.contact_id == msg_ctx.contact.contact_id
        assert outcome.token_candidates_seen >= 1

        # 合并后 Journey 进入 LINE_ENGAGED
        j = store.get_journey_by_contact(msg_ctx.contact.contact_id)
        assert j.funnel_stage == STAGE_LINE_ENGAGED
        # 合并事件落在 journey 上
        types = {e["event_type"] for e in store.list_events(j.journey_id)}
        assert "channel_identity_merged" in types
        assert "line_first_reply" in types

    def test_text_no_token_no_signal_keeps_isolated(self, wiring):
        store, _, _, gw = wiring
        outcome = gw.on_line_first_text(
            account_id="acc", external_id="line_stranger", display_name="Zoe",
            text="hi",
        )
        assert outcome.merged is False
        assert outcome.via == "none"
        # LINE 孤岛 Contact 仍然存在
        assert store.get_contact(outcome.contact_id) is not None


class TestLineFirstText_Signal:
    def test_signal_auto_merge(self, wiring):
        store, _, _, gw = wiring
        # Messenger 侧铺候选
        msg_ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="acc", external_id="fb_1",
            display_name="Alice", language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        gw.issue_handoff(messenger_ci_id=msg_ctx.channel_identity.channel_identity_id)
        # LINE 侧来人，无 token，但名/语言/时区全对
        outcome = gw.on_line_first_text(
            account_id="acc", external_id="line_1", display_name="Alice",
            language_hint="zh", timezone_hint="Asia/Shanghai",
            text="在吗",
        )
        assert outcome.merged is True
        assert outcome.via == "heuristic"
        assert outcome.contact_id == msg_ctx.contact.contact_id

    def test_signal_medium_enters_review(self, wiring):
        store, _, _, gw = wiring
        # 候选：全信息齐
        msg_ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="acc", external_id="fb_1",
            display_name="Alice Liu", language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        gw.issue_handoff(messenger_ci_id=msg_ctx.channel_identity.channel_identity_id)
        # LINE：名字半像，tz 不同 → 中置信
        outcome = gw.on_line_first_text(
            account_id="acc", external_id="line_1", display_name="Alice L.",
            language_hint="zh", timezone_hint="Asia/Tokyo",
            text="嗨",
        )
        assert outcome.merged is False
        assert outcome.via == "none"
        assert outcome.review_id
        assert outcome.reason == "manual_review"


class TestReplayProtection:
    def test_second_first_text_call_returns_replay_ignored(self, wiring):
        store, _, _, gw = wiring
        msg_ctx = gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi", display_name="Alice",
        )
        tok = gw.issue_handoff(messenger_ci_id=msg_ctx.channel_identity.channel_identity_id)
        gw.on_handoff_sent(
            messenger_ci_id=msg_ctx.channel_identity.channel_identity_id, token=tok.token)
        out1 = gw.on_line_first_text(
            account_id="a", external_id="line_1",
            text=f"加你了 {tok.token}", display_name="Alice",
        )
        assert out1.merged is True
        # 模拟 runner 误把下一条也当首条
        out2 = gw.on_line_first_text(
            account_id="a", external_id="line_1",
            text="我又来了", display_name="Alice",
        )
        assert out2.reason == "replay_ignored"
        assert out2.merged is True  # Contact 本来就已合并，状态保持
        # 事件表里只有一条 line_first_reply，但有额外的 msg_in(first_text_replay=true)
        j = store.get_journey_by_contact(out1.contact_id)
        events = store.list_events(j.journey_id, limit=100)
        first_replies = [e for e in events if e["event_type"] == "line_first_reply"]
        assert len(first_replies) == 1
        replay_events = [e for e in events
                         if e["event_type"] == "msg_in"
                         and e["payload"].get("first_text_replay")]
        assert len(replay_events) == 1


class TestStageTransitionGuard:
    def test_cannot_skip_from_initial_to_handoff_sent(self, wiring):
        store, _, _, gw = wiring
        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        # 直接 on_handoff_sent：Journey 仍在 INITIAL，前驱不合法，被 guard 拒
        gw.on_handoff_sent(
            messenger_ci_id=ctx.channel_identity.channel_identity_id, token="xxxxxx",
        )
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == STAGE_INITIAL
        # 但"handoff_sent"事件仍然落了（事件是事实记录，独立于 stage 变化）
        types = {e["event_type"] for e in store.list_events(j.journey_id)}
        assert "handoff_sent" in types
        # stage_change 不在（因为被 guard 拒了）
        stage_changes = [e for e in store.list_events(j.journey_id)
                         if e["event_type"] == "stage_change"]
        for sc in stage_changes:
            assert sc["payload"].get("to") != STAGE_HANDOFF_SENT


class TestTraceIdPropagation:
    def test_same_trace_flows_through(self, wiring):
        store, _, _, gw = wiring
        trace = new_trace_id()
        msg_ctx = gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi", trace_id=trace,
        )
        tok = gw.issue_handoff(
            messenger_ci_id=msg_ctx.channel_identity.channel_identity_id, trace_id=trace,
        )
        gw.on_handoff_sent(
            messenger_ci_id=msg_ctx.channel_identity.channel_identity_id,
            token=tok.token, trace_id=trace,
        )
        trace2 = new_trace_id()
        outcome = gw.on_line_first_text(
            account_id="a", external_id="line_1", display_name="",
            text=f"嗨 {tok.token}", trace_id=trace2,
        )
        assert outcome.merged is True
        # Messenger 和 LINE 的事件现在都在同一个合并后的 journey 里
        j = store.get_journey_by_contact(outcome.contact_id)
        events = store.list_events(j.journey_id, limit=100)
        trace_ids = {e["trace_id"] for e in events}
        assert trace in trace_ids
        assert trace2 in trace_ids
