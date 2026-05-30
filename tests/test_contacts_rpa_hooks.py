"""rpa_hooks — 吞异常 + NoopContactHooks 行为验证。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts import (
    ContactGateway,
    ContactStore,
    HandoffTokenService,
    MergeService,
    GatewayContactHooks,
    NoopContactHooks,
    ContactHooks,
)
from src.contacts.models import CHANNEL_MESSENGER, CHANNEL_LINE


@pytest.fixture
def hooks(tmp_path):
    store = ContactStore(db_path=tmp_path / "contacts.db")
    gw = ContactGateway(store, HandoffTokenService(store, ttl_seconds=3600), MergeService(store))
    h = GatewayContactHooks(gw)
    yield h, gw, store
    store.close()


class TestGatewayBacked:
    def test_peer_seen(self, hooks):
        h, gw, store = hooks
        ctx = h.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        assert ctx is not None
        assert ctx.is_new is True

    def test_on_message_routes_to_gateway(self, hooks):
        h, gw, store = hooks
        ctx = h.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi",
        )
        assert ctx is not None
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == "ENGAGED"

    def test_issue_handoff_by_external_id(self, hooks):
        h, gw, store = hooks
        h.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        tok = h.issue_handoff_for_messenger(account_id="a", external_id="fb_1")
        assert tok and len(tok) == 6

    def test_issue_handoff_unknown_peer_returns_none(self, hooks):
        h, _, _ = hooks
        assert h.issue_handoff_for_messenger(account_id="a", external_id="ghost") is None

    def test_issue_handoff_for_line_peer_returns_none(self, hooks):
        """Gateway 内部会因 channel 错误抛 ValueError，hook 吞掉返回 None。"""
        h, _, _ = hooks
        h.on_peer_seen(channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        # ci 存在，但 get_ci_by_external 查 messenger 找不到 → 返回 None
        assert h.issue_handoff_for_messenger(account_id="a", external_id="line_1") is None

    def test_line_first_text_full_merge(self, hooks):
        h, gw, store = hooks
        # 建 messenger ci 并签发 token
        h.on_message(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
                     direction="in", text_preview="hi")
        tok = h.issue_handoff_for_messenger(account_id="a", external_id="fb_1")
        h.on_handoff_sent(account_id="a", external_id="fb_1", token=tok)
        # LINE 端收首条
        out = h.on_line_first_text(
            account_id="a", external_id="line_1",
            text=f"嗨 {tok} 我是新来的", display_name="",
        )
        assert out is not None
        assert out.merged is True
        assert out.via == "token"


class TestSwallowExceptions:
    def test_peer_seen_bad_channel_returns_none(self, hooks):
        h, _, _ = hooks
        # 内部 ensure_channel_identity 会 raise ValueError；hook 吞掉返回 None
        assert h.on_peer_seen(channel="twitter", account_id="a", external_id="x") is None

    def test_bad_direction_returns_none(self, hooks):
        h, _, _ = hooks
        assert h.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="sideways", text_preview="",
        ) is None  # gateway 内部返回 None，hook 透传


class TestNoopContactHooks:
    def test_all_methods_return_none(self):
        n = NoopContactHooks()
        assert n.on_peer_seen(channel="x", account_id="a", external_id="b") is None
        assert n.on_message(channel="x", account_id="a", external_id="b",
                            direction="in") is None
        assert n.issue_handoff_for_messenger(account_id="a", external_id="b") is None
        assert n.on_handoff_sent(account_id="a", external_id="b", token="t") is None
        assert n.on_line_first_text(account_id="a", external_id="b", text="") is None
        # W3-3A.1
        assert n.get_journey_intimacy(channel="x", account_id="a", external_id="b") is None


class TestGetJourneyIntimacy:
    """W3-3A.1：runner 通过 hooks 查询 IntimacyEngine 写到 journey 的 score。"""

    def test_unknown_peer_returns_none(self, hooks):
        h, _, _ = hooks
        assert h.get_journey_intimacy(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="ghost",
        ) is None

    def test_returns_score_after_message(self, hooks):
        h, gw, store = hooks
        # 建 ci + journey
        ctx = h.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi",
        )
        assert ctx is not None
        # 直接改 journey.intimacy_score 模拟 IntimacyEngine 写入
        store.update_journey(ctx.journey.journey_id, intimacy_score=42.5)
        score = h.get_journey_intimacy(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
        )
        assert score == 42.5
        assert isinstance(score, float)

    def test_zero_score_returned_as_zero_not_none(self, hooks):
        """新 journey intimacy_score=0.0 必须返回 0.0，runner 才能区分「无数据」与「0 分」。"""
        h, gw, store = hooks
        h.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi",
        )
        score = h.get_journey_intimacy(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
        )
        assert score == 0.0

    def test_swallows_errors(self, hooks):
        """错误 channel → gateway raise ValueError → hook 吞掉返回 None。"""
        h, _, _ = hooks
        assert h.get_journey_intimacy(
            channel="twitter", account_id="a", external_id="x",
        ) is None


class TestProtocolConformance:
    def test_gateway_backed_is_contact_hooks(self, hooks):
        h, _, _ = hooks
        assert isinstance(h, ContactHooks)

    def test_noop_is_contact_hooks(self):
        assert isinstance(NoopContactHooks(), ContactHooks)
