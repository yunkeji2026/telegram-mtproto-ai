"""MergeService 单元测试。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import (
    MergeService,
    score_signals,
    decide,
    _name_match,
    _time_proximity,
)
from src.contacts.models import (
    CHANNEL_MESSENGER,
    CHANNEL_LINE,
    MergeSignals,
    DECISION_AUTO_MERGE,
    DECISION_MANUAL_REVIEW,
    DECISION_KEEP_ISOLATED,
)


@pytest.fixture
def store(tmp_path):
    s = ContactStore(db_path=tmp_path / "contacts.db")
    yield s
    s.close()


@pytest.fixture
def svc(store):
    return MergeService(store)


@pytest.fixture
def token_svc(store):
    return HandoffTokenService(store, ttl_seconds=3600)


class TestScoring:
    def test_all_zero(self):
        conf, bd = score_signals(MergeSignals())
        assert conf == 0.0

    def test_perfect_match(self):
        conf, bd = score_signals(MergeSignals(
            name_match=1.0, lang_match=1.0, tz_match=1.0,
            time_proximity=1.0, style_match=1.0,
        ))
        assert abs(conf - 1.0) < 1e-9

    def test_partial(self):
        conf, _ = score_signals(MergeSignals(name_match=1.0, lang_match=1.0))
        # 0.30 + 0.20
        assert abs(conf - 0.50) < 1e-9

    def test_clamps_bounds(self):
        # 手动构造超大信号（实际不会发生，验证 clamp 防呆）
        conf, _ = score_signals(MergeSignals(
            name_match=10, lang_match=10, tz_match=10,
            time_proximity=10, style_match=10,
        ))
        assert conf == 1.0

    def test_name_match_exact(self):
        assert _name_match("Alice", "alice") == 1.0

    def test_name_match_empty(self):
        assert _name_match("", "Alice") == 0.0

    def test_name_match_fuzzy(self):
        v = _name_match("Alice Liu", "Alice L.")
        assert 0.5 < v < 1.0

    def test_time_proximity(self):
        assert _time_proximity(0) == 1.0
        assert _time_proximity(72 * 3600) == 0.0
        mid = _time_proximity(36 * 3600)
        assert 0.49 < mid < 0.51

    def test_time_proximity_negative(self):
        assert _time_proximity(-100) == 0.0


class TestDecisionThresholds:
    def test_auto(self):
        d = decide(0.9)
        assert d.decision == DECISION_AUTO_MERGE

    def test_review(self):
        d = decide(0.7)
        assert d.decision == DECISION_MANUAL_REVIEW

    def test_isolated(self):
        d = decide(0.3)
        assert d.decision == DECISION_KEEP_ISOLATED

    def test_boundary_inclusive_auto(self):
        # 阈值从 0.85 上调至 0.90，让 MVP 更保守（只有所有信号全中才自动合并）
        assert decide(0.90).decision == DECISION_AUTO_MERGE
        assert decide(0.899).decision == DECISION_MANUAL_REVIEW

    def test_boundary_inclusive_review(self):
        assert decide(0.60).decision == DECISION_MANUAL_REVIEW


class TestTokenMerge:
    def test_apply_token_merge_success(self, store, svc, token_svc):
        m_contact, m_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            display_name="Alice",
        )
        l_contact, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1",
            display_name="Alice",
        )
        tok = token_svc.issue(m_ci.channel_identity_id)
        consumed = token_svc.consume(tok.token, consumed_by_ci_id=l_ci.channel_identity_id)
        ok = svc.apply_token_merge(consumed, l_ci.channel_identity_id, trace_id="tr-1")
        assert ok is True
        # 合并后 LINE ci 的 contact_id 变成 messenger 的
        l_ci_fresh = store.get_channel_identity(l_ci.channel_identity_id)
        assert l_ci_fresh.contact_id == m_contact.contact_id
        assert l_ci_fresh.linked_via == "token"


class TestSignalMerge:
    def _setup_candidate(self, store, token_svc, *, messenger_name="Alice",
                         lang="zh", tz="Asia/Shanghai"):
        c, ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_X",
            display_name=messenger_name, language_hint=lang, timezone_hint=tz,
        )
        # 刷 contact 的 lang/tz（ensure 已写入，但以防万一）
        store.update_contact(c.contact_id, primary_name=messenger_name,
                             language_hint=lang, timezone_hint=tz)
        tok = token_svc.issue(ci.channel_identity_id)
        return c, ci, tok

    def test_recent_candidates_excludes_consumed(self, store, svc, token_svc):
        c, ci, tok = self._setup_candidate(store, token_svc)
        l_contact, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        assert len(svc.recent_handoff_candidates()) == 1
        token_svc.consume(tok.token, consumed_by_ci_id=l_ci.channel_identity_id)
        assert svc.recent_handoff_candidates() == []

    def test_evaluate_picks_best_and_auto_merges(self, store, svc, token_svc):
        # 只造一个正确的候选（唯一匹配，runner-up 为 0）
        cb, cib, _ = self._setup_candidate(store, token_svc, messenger_name="Alice")
        # LINE 侧
        _, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        best, decision = svc.evaluate(
            line_ci=l_ci, line_display_name="Alice",
            line_lang="zh", line_tz="Asia/Shanghai",
        )
        assert best is not None
        assert best.messenger_ci.channel_identity_id == cib.channel_identity_id
        # Alice 完美匹配 + runner-up=0 → 差距 >0.10，通过歧义检查
        assert decision.confidence >= 0.90
        assert decision.decision == DECISION_AUTO_MERGE

    def test_evaluate_ambiguous_downgrades_to_review(self, store, svc, token_svc):
        """两个候选都叫 Alice 语言时区全同——系统分不清，必须进人工。"""
        # 候选 A
        self._setup_candidate(store, token_svc, messenger_name="Alice")
        # 候选 B，同名同语同 tz，不同 external_id
        c2, ci2, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_Y",
            display_name="Alice", language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        store.update_contact(c2.contact_id, primary_name="Alice",
                             language_hint="zh", timezone_hint="Asia/Shanghai")
        token_svc.issue(ci2.channel_identity_id)
        _, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_2")
        best, decision = svc.evaluate(
            line_ci=l_ci, line_display_name="Alice",
            line_lang="zh", line_tz="Asia/Shanghai",
        )
        # 两名候选完全一样 → confidence 相同 → margin=0 < 0.10 → 降级 review
        assert decision.decision == DECISION_MANUAL_REVIEW
        assert "ambiguous_top2" in decision.reason

    def test_apply_auto_merge_relinks(self, store, svc, token_svc):
        cb, cib, tokb = self._setup_candidate(store, token_svc, messenger_name="Alice")
        _, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        best, decision = svc.evaluate(
            line_ci=l_ci, line_display_name="Alice",
            line_lang="zh", line_tz="Asia/Shanghai",
        )
        result = svc.apply_signal_decision(
            line_ci_id=l_ci.channel_identity_id,
            best=best, decision=decision,
        )
        assert result == "merged"
        l_fresh = store.get_channel_identity(l_ci.channel_identity_id)
        assert l_fresh.contact_id == cb.contact_id
        assert l_fresh.linked_via == "heuristic"

    def test_medium_confidence_enqueues_review(self, store, svc, token_svc):
        # 名字对一半、语言对、tz 不对、time 中等
        c, ci, _ = self._setup_candidate(
            store, token_svc, messenger_name="Alice Liu",
            lang="zh", tz="Asia/Shanghai",
        )
        _, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        best, decision = svc.evaluate(
            line_ci=l_ci, line_display_name="Alice L.",
            line_lang="zh", line_tz="Asia/Tokyo",  # tz 不同
        )
        # 期望落在 review 区间
        assert 0.60 <= decision.confidence < 0.85
        assert decision.decision == DECISION_MANUAL_REVIEW
        result = svc.apply_signal_decision(
            line_ci_id=l_ci.channel_identity_id, best=best, decision=decision,
        )
        pending = store.list_pending_reviews()
        assert len(pending) == 1
        assert pending[0]["review_id"] == result

    def test_low_confidence_keeps_isolated(self, store, svc, token_svc):
        c, ci, _ = self._setup_candidate(store, token_svc, messenger_name="Bob",
                                          lang="en", tz="America/Los_Angeles")
        _, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        best, decision = svc.evaluate(
            line_ci=l_ci, line_display_name="Zoe",
            line_lang="zh", line_tz="Asia/Shanghai",
        )
        # 名字完全不对 + lang 不对 + tz 不对，confidence 应该很低
        assert decision.decision == DECISION_KEEP_ISOLATED
        result = svc.apply_signal_decision(
            line_ci_id=l_ci.channel_identity_id, best=best, decision=decision,
        )
        assert result is None

    def test_no_candidates(self, store, svc):
        _, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        best, decision = svc.evaluate(
            line_ci=l_ci, line_display_name="X", line_lang="zh", line_tz="",
        )
        assert best is None
        assert decision.decision == DECISION_KEEP_ISOLATED


class TestReviewActions:
    def test_approve_review_relinks(self, store, svc, token_svc):
        # 构造一个入队 review：中置信场景
        c_msg, ci_msg, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            display_name="Alice Liu", language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        store.update_contact(c_msg.contact_id, primary_name="Alice Liu",
                             language_hint="zh", timezone_hint="Asia/Shanghai")
        token_svc.issue(ci_msg.channel_identity_id)
        _, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        best, decision = svc.evaluate(
            line_ci=l_ci, line_display_name="Alice L.",
            line_lang="zh", line_tz="Asia/Tokyo",
        )
        rid = svc.apply_signal_decision(
            line_ci_id=l_ci.channel_identity_id, best=best, decision=decision,
        )
        assert rid is not None
        # 运营审批通过
        assert svc.approve_review(rid, resolved_by="admin") is True
        # LINE ci 已迁到 messenger 的 contact
        l_fresh = store.get_channel_identity(l_ci.channel_identity_id)
        assert l_fresh.contact_id == c_msg.contact_id
        assert l_fresh.linked_via == "manual"
        # review 已标记 resolved
        assert store.list_pending_reviews() == []

    def test_reject_review_does_not_merge(self, store, svc, token_svc):
        c_msg, ci_msg, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            display_name="Alice", language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        store.update_contact(c_msg.contact_id, primary_name="Alice",
                             language_hint="zh", timezone_hint="Asia/Shanghai")
        token_svc.issue(ci_msg.channel_identity_id)
        _, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        best, decision = svc.evaluate(
            line_ci=l_ci, line_display_name="Alice L.",
            line_lang="zh", line_tz="Asia/Tokyo",
        )
        rid = svc.apply_signal_decision(
            line_ci_id=l_ci.channel_identity_id, best=best, decision=decision,
        )
        original_contact_id = l_ci.contact_id
        assert svc.reject_review(rid, resolved_by="admin") is True
        # LINE ci 保持原 contact，没合并
        l_fresh = store.get_channel_identity(l_ci.channel_identity_id)
        assert l_fresh.contact_id == original_contact_id

    def test_approve_unknown_review_returns_false(self, svc):
        assert svc.approve_review("nonexistent") is False

    def test_approve_idempotent_when_already_merged(self, store, svc, token_svc):
        """模拟上次 relink 成功但 resolve 失败的场景：再次 approve 应能正常关闭 review。"""
        c_msg, ci_msg, _ = store.ensure_channel_identity(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            display_name="Alice Liu", language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        store.update_contact(c_msg.contact_id, primary_name="Alice Liu",
                             language_hint="zh", timezone_hint="Asia/Shanghai")
        token_svc.issue(ci_msg.channel_identity_id)
        _, l_ci, _ = store.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        best, decision = svc.evaluate(
            line_ci=l_ci, line_display_name="Alice L.",
            line_lang="zh", line_tz="Asia/Tokyo",
        )
        rid = svc.apply_signal_decision(
            line_ci_id=l_ci.channel_identity_id, best=best, decision=decision,
        )
        # 绕过 service，手动把 ci relink + 故意不 resolve review（模拟故障）
        store.relink_channel_identity(
            ci_id=l_ci.channel_identity_id,
            new_contact_id=c_msg.contact_id,
            linked_via="manual",
            attribution_confidence=0.8,
        )
        # review 仍在 pending
        assert len(store.list_pending_reviews()) == 1
        # 再次 approve：走幂等短路，把 review 关闭
        assert svc.approve_review(rid, resolved_by="admin") is True
        assert store.list_pending_reviews() == []
