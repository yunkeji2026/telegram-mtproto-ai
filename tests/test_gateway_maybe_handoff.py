"""Gateway.maybe_issue_handoff 集成测试：readiness + cap + script + compliance + token。"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts import (
    ContactStore, HandoffTokenService, MergeService, ContactGateway,
)
from src.contacts.models import (
    CHANNEL_MESSENGER, STAGE_ENGAGED, STAGE_HANDOFF_READY,
)
from src.skills.intimacy_engine import IntimacyEngine
from src.skills.handoff_readiness import HandoffReadinessScorer
from src.skills.handoff_renderer import HandoffRenderer
from src.skills.handoff_compliance import HandoffComplianceChecker
from src.skills.account_limiter import AccountLimiter


CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture
def env(tmp_path):
    store = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(store, ttl_seconds=3600)
    merge = MergeService(store)
    intim = IntimacyEngine(store)
    scorer = HandoffReadinessScorer(store, intim, turn_saturation=3, open_threshold=70.0)
    renderer = HandoffRenderer(CONFIG / "handoff_scripts.yaml")
    compliance = HandoffComplianceChecker(config_path=CONFIG / "handoff_compliance.yaml")
    limiter = AccountLimiter(store, daily_cap=3)
    gw = ContactGateway(
        store, handoff, merge,
        renderer=renderer, limiter=limiter, compliance=compliance,
        readiness_scorer=scorer,
        line_id_provider=lambda acc: f"@line_{acc}",
    )
    yield store, gw, scorer, limiter, compliance, renderer
    store.close()


def _seed_warm_chat(store, gw, fb_id="fb_1"):
    """造一个 5 天 * 每天 4 次交互的 journey → intimacy 高。"""
    ctx = gw.on_peer_seen(
        channel=CHANNEL_MESSENGER, account_id="acc-A", external_id=fb_id,
        display_name="Alice", language_hint="zh", timezone_hint="Asia/Shanghai",
    )
    store.update_contact(ctx.contact.contact_id,
                          primary_name="Alice",
                          language_hint="zh", timezone_hint="Asia/Shanghai")
    jid = ctx.journey.journey_id
    now = int(time.time())
    with store._lock:
        for d in range(5):
            for i in range(4):
                for et in ("msg_in", "msg_out"):
                    store._conn.execute(
                        "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
                        "VALUES (?, ?, '', ?, '{}', ?)",
                        (uuid.uuid4().hex, jid, et, now - d * 86400 - i * 60),
                    )
        store._conn.execute(
            "UPDATE journeys SET funnel_stage='ENGAGED', updated_at=? WHERE journey_id=?",
            (now, jid))
        store._conn.commit()
    return ctx


class TestHappyPath:
    def test_all_checks_pass_returns_rendered_text(self, env):
        store, gw, _, _, _, _ = env
        ctx = _seed_warm_chat(store, gw)
        result = gw.maybe_issue_handoff(
            messenger_ci_id=ctx.channel_identity.channel_identity_id,
            latest_in_text="好啦 我去睡啦 晚安",
        )
        assert result.success is True
        assert result.token
        # token 和 line_id 在文本里
        assert result.token in result.text
        assert "@line_acc-A" in result.text
        assert result.script_id.startswith("zh_")
        assert result.readiness_score >= 70
        assert result.remaining_today >= 0
        # Journey 推到 HANDOFF_READY
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == STAGE_HANDOFF_READY


class TestReadinessBlocks:
    def test_no_goodbye_blocks(self, env):
        store, gw, _, _, _, _ = env
        ctx = _seed_warm_chat(store, gw)
        result = gw.maybe_issue_handoff(
            messenger_ci_id=ctx.channel_identity.channel_identity_id,
            latest_in_text="你今天干嘛了",    # 没 goodbye
        )
        assert result.success is False
        assert result.reason == "not_ready"
        assert "readiness" in result.details


class TestCapBlocks:
    def test_cap_exhausted(self, env):
        store, gw, _, limiter, _, _ = env
        ctx = _seed_warm_chat(store, gw)
        # 用光 3 次
        for _ in range(3):
            r = gw.maybe_issue_handoff(
                messenger_ci_id=ctx.channel_identity.channel_identity_id,
                latest_in_text="我去睡啦 晚安",
            )
            # 每次成功后 remaining_today 会递减
            assert r.success
        # 第 4 次 → cap 耗尽
        r4 = gw.maybe_issue_handoff(
            messenger_ci_id=ctx.channel_identity.channel_identity_id,
            latest_in_text="再一次 晚安",
        )
        assert r4.success is False
        assert r4.reason == "account_cap_exceeded"


class TestComplianceBlocks:
    def test_token_revoked_on_compliance_block(self, tmp_path):
        """模拟：用自定义 compliance 拒掉一切——token 应被撤销。"""
        store = ContactStore(db_path=tmp_path / "contacts.db")
        handoff = HandoffTokenService(store, ttl_seconds=3600)
        merge = MergeService(store)
        intim = IntimacyEngine(store)
        scorer = HandoffReadinessScorer(store, intim, turn_saturation=3, open_threshold=70.0)
        renderer = HandoffRenderer(CONFIG / "handoff_scripts.yaml")
        # 用一个极端严苛的 compliance（整个话术池都会触发）
        compliance = HandoffComplianceChecker(
            blocked_keywords=["line"],   # 话术里一定会出现 "LINE" 字样
            min_length=1, max_length=10000,
        )
        limiter = AccountLimiter(store, daily_cap=10)
        gw = ContactGateway(
            store, handoff, merge,
            renderer=renderer, limiter=limiter, compliance=compliance,
            readiness_scorer=scorer,
            line_id_provider=lambda acc: f"@line_{acc}",
        )
        ctx = _seed_warm_chat(store, gw, fb_id="fb_x")
        result = gw.maybe_issue_handoff(
            messenger_ci_id=ctx.channel_identity.channel_identity_id,
            latest_in_text="晚安 睡啦",
        )
        assert result.success is False
        assert result.reason == "compliance_blocked"
        # 签过的 token 已被撤销（不会被误消费）
        active = store.list_active_tokens_issued_from(
            ctx.channel_identity.channel_identity_id)
        assert active == []
        store.close()


class TestWithoutOptionalServices:
    def test_no_renderer_fails_gracefully(self, tmp_path):
        store = ContactStore(db_path=tmp_path / "contacts.db")
        handoff = HandoffTokenService(store, ttl_seconds=3600)
        merge = MergeService(store)
        gw = ContactGateway(store, handoff, merge)   # 全默认，无可选服务
        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        result = gw.maybe_issue_handoff(
            messenger_ci_id=ctx.channel_identity.channel_identity_id,
            latest_in_text="",
        )
        assert result.success is False
        assert result.reason == "no_renderer"
        store.close()


class TestBadInput:
    def test_unknown_ci(self, env):
        _, gw, _, _, _, _ = env
        r = gw.maybe_issue_handoff(messenger_ci_id="nonexistent",
                                     latest_in_text="晚安")
        assert r.success is False
        assert r.reason == "bad_messenger_ci"


class TestDryRun:
    """预览模式：必须不落任何副作用。"""

    def test_dry_run_renders_without_side_effects(self, env):
        store, gw, _, limiter, _, _ = env
        ctx = _seed_warm_chat(store, gw)
        ci_id = ctx.channel_identity.channel_identity_id
        acc_id = ctx.channel_identity.account_id

        before_remaining = limiter.remaining_for(acc_id)
        before_tokens = store.list_active_tokens_issued_from(ci_id)
        before_stage = store.get_journey(ctx.journey.journey_id).funnel_stage

        r = gw.maybe_issue_handoff(
            messenger_ci_id=ci_id,
            latest_in_text="好啦 我去睡啦 晚安",
            dry_run=True,
        )

        # 成功路径：有渲染文本 + 占位 token
        assert r.success is True
        assert r.reason == "dry_run_ok"
        assert r.token == "dry_rn"
        assert "dry_rn" in r.text
        assert "@line_acc-A" in r.text
        assert r.details.get("dry_run") is True

        # 关键：三个副作用都没发生
        assert limiter.remaining_for(acc_id) == before_remaining, "cap 不该被扣"
        assert store.list_active_tokens_issued_from(ci_id) == before_tokens, \
            "不该签真 token"
        assert store.get_journey(ctx.journey.journey_id).funnel_stage == before_stage, \
            "Journey stage 不该被推进"

    def test_dry_run_cap_exceeded_returns_early(self, env):
        """cap 用尽时 dry_run 只查不扣，返回 account_cap_exceeded。"""
        store, gw, _, limiter, _, _ = env
        ctx = _seed_warm_chat(store, gw)
        ci_id = ctx.channel_identity.channel_identity_id
        acc_id = ctx.channel_identity.account_id

        # 真实消费把 cap 用光（fixture daily_cap=3）
        for _ in range(3):
            r = gw.maybe_issue_handoff(
                messenger_ci_id=ci_id, latest_in_text="我去睡啦 晚安")
            assert r.success

        assert limiter.remaining_for(acc_id) == 0

        # dry_run 应直接拒掉，不再往后走
        r = gw.maybe_issue_handoff(
            messenger_ci_id=ci_id,
            latest_in_text="再一次 晚安",
            dry_run=True,
        )
        assert r.success is False
        assert r.reason == "account_cap_exceeded"
        assert r.remaining_today == 0
        assert r.text == ""
        assert r.token == ""

    def test_dry_run_compliance_block_no_revoke_needed(self, tmp_path):
        """dry_run + compliance 拒绝：不需要撤 token（压根没签真 token）。"""
        store = ContactStore(db_path=tmp_path / "contacts.db")
        handoff = HandoffTokenService(store, ttl_seconds=3600)
        merge = MergeService(store)
        intim = IntimacyEngine(store)
        scorer = HandoffReadinessScorer(store, intim, turn_saturation=3,
                                          open_threshold=70.0)
        renderer = HandoffRenderer(CONFIG / "handoff_scripts.yaml")
        compliance = HandoffComplianceChecker(
            blocked_keywords=["line"], min_length=1, max_length=10000,
        )
        limiter = AccountLimiter(store, daily_cap=10)
        gw = ContactGateway(
            store, handoff, merge,
            renderer=renderer, limiter=limiter, compliance=compliance,
            readiness_scorer=scorer,
            line_id_provider=lambda acc: f"@line_{acc}",
        )
        ctx = _seed_warm_chat(store, gw, fb_id="fb_dry")
        ci_id = ctx.channel_identity.channel_identity_id
        acc_id = ctx.channel_identity.account_id

        before_remaining = limiter.remaining_for(acc_id)

        r = gw.maybe_issue_handoff(
            messenger_ci_id=ci_id,
            latest_in_text="晚安 睡啦",
            dry_run=True,
        )
        assert r.success is False
        assert r.reason == "compliance_blocked"
        # cap 没扣 + 没有活 token（dry_run 根本没签）
        assert limiter.remaining_for(acc_id) == before_remaining
        assert store.list_active_tokens_issued_from(ci_id) == []
        store.close()

    def test_dry_run_then_real_both_succeed(self, env):
        """同一会话：预览一下，再真发——两次都应成功，且真发那次正常扣 cap / 推 stage。"""
        store, gw, _, limiter, _, _ = env
        ctx = _seed_warm_chat(store, gw)
        ci_id = ctx.channel_identity.channel_identity_id
        acc_id = ctx.channel_identity.account_id
        before_remaining = limiter.remaining_for(acc_id)

        preview = gw.maybe_issue_handoff(
            messenger_ci_id=ci_id, latest_in_text="晚安 睡啦", dry_run=True)
        assert preview.success and preview.token == "dry_rn"

        real = gw.maybe_issue_handoff(
            messenger_ci_id=ci_id, latest_in_text="晚安 睡啦")
        assert real.success and real.token != "dry_rn"
        # 真 token 落在活跃表里（dry_rn 永远不会）
        active = {t.token for t in store.list_active_tokens_issued_from(ci_id)}
        assert real.token in active
        assert "dry_rn" not in active

        # 只有"真发"扣了一次
        assert limiter.remaining_for(acc_id) == before_remaining - 1
        # stage 被真发推到 HANDOFF_READY
        assert store.get_journey(ctx.journey.journey_id).funnel_stage \
            == STAGE_HANDOFF_READY


class TestCapRefundOnComplianceBlock:
    """合规拒绝时 cap 必须退回，不能"白扣"。"""
    def test_refund_after_compliance_block(self, tmp_path):
        store = ContactStore(db_path=tmp_path / "contacts.db")
        handoff = HandoffTokenService(store, ttl_seconds=3600)
        merge = MergeService(store)
        intim = IntimacyEngine(store)
        scorer = HandoffReadinessScorer(store, intim, turn_saturation=3, open_threshold=70.0)
        renderer = HandoffRenderer(CONFIG / "handoff_scripts.yaml")
        compliance = HandoffComplianceChecker(
            blocked_keywords=["line"], min_length=1, max_length=10000,
        )
        limiter = AccountLimiter(store, daily_cap=3)
        gw = ContactGateway(
            store, handoff, merge,
            renderer=renderer, limiter=limiter, compliance=compliance,
            readiness_scorer=scorer,
            line_id_provider=lambda acc: f"@line_{acc}",
        )
        ctx = _seed_warm_chat(store, gw, fb_id="fb_y")
        assert limiter.remaining_for("acc-A") == 3
        result = gw.maybe_issue_handoff(
            messenger_ci_id=ctx.channel_identity.channel_identity_id,
            latest_in_text="晚安",
        )
        assert result.success is False
        assert result.reason == "compliance_blocked"
        # refund 生效：配额没被白扣
        assert limiter.remaining_for("acc-A") == 3
        store.close()
