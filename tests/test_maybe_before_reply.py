"""W4-Handoff-Auto-Inject：`maybe_before_reply` 钩子行为验证。"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts import (
    bootstrap_contacts_subsystem,
    BeforeReplyDecision,
    ContactGateway,
    ContactStore,
    GatewayContactHooks,
    HandoffTokenService,
    MergeService,
    NoopContactHooks,
)
from src.contacts.models import (
    CHANNEL_MESSENGER, STAGE_ENGAGED, STAGE_HANDOFF_READY, STAGE_HANDOFF_SENT,
)
from src.skills.account_limiter import AccountLimiter
from src.skills.handoff_compliance import HandoffComplianceChecker
from src.skills.handoff_readiness import HandoffReadinessScorer
from src.skills.handoff_renderer import HandoffRenderer
from src.skills.intimacy_engine import IntimacyEngine


CFG_DIR = Path(__file__).resolve().parent.parent / "config"


# ── fixture ─────────────────────────────────────────────
@pytest.fixture
def full_gw(tmp_path):
    """gateway with 全套可选服务，供 maybe_issue_handoff 成功路径使用。"""
    store = ContactStore(db_path=tmp_path / "c.db")
    handoff = HandoffTokenService(store, ttl_seconds=3600)
    merge = MergeService(store)
    intim = IntimacyEngine(store)
    scorer = HandoffReadinessScorer(store, intim, turn_saturation=3, open_threshold=70.0)
    renderer = HandoffRenderer(CFG_DIR / "handoff_scripts.yaml")
    compliance = HandoffComplianceChecker(config_path=CFG_DIR / "handoff_compliance.yaml")
    limiter = AccountLimiter(store, daily_cap=3)
    gw = ContactGateway(
        store, handoff, merge,
        renderer=renderer, limiter=limiter, compliance=compliance,
        readiness_scorer=scorer,
        line_id_provider=lambda acc: f"@line_{acc}",
    )
    yield store, gw
    store.close()


def _seed_warm(store, gw, fb_id="fb_a"):
    ctx = gw.on_peer_seen(
        channel=CHANNEL_MESSENGER, account_id="acc-A", external_id=fb_id,
        display_name="Alice", language_hint="zh", timezone_hint="Asia/Shanghai",
    )
    store.update_contact(ctx.contact.contact_id, primary_name="Alice",
                          language_hint="zh", timezone_hint="Asia/Shanghai")
    jid = ctx.journey.journey_id
    now = int(time.time())
    with store._lock:
        for d in range(5):
            for i in range(4):
                for et in ("msg_in", "msg_out"):
                    store._conn.execute(
                        "INSERT INTO journey_events "
                        "(event_id, journey_id, trace_id, event_type, payload_json, ts) "
                        "VALUES (?, ?, '', ?, '{}', ?)",
                        (uuid.uuid4().hex, jid, et,
                         now - d * 86400 - i * 60),
                    )
        store._conn.execute(
            "UPDATE journeys SET funnel_stage=?, updated_at=? WHERE journey_id=?",
            (STAGE_ENGAGED, now, jid))
        store._conn.commit()
    return ctx


# ── feature flag 关（默认）────────────────────────────
class TestFlagDisabled:
    def test_default_disabled_returns_original(self, full_gw):
        store, gw = full_gw
        hooks = GatewayContactHooks(gw)   # 默认 auto_inject_enabled=False
        _seed_warm(store, gw)
        dec = hooks.maybe_before_reply(
            account_id="acc-A", external_id="fb_a",
            ai_reply="好的哈~", latest_in_text="我去睡啦 晚安",
        )
        assert dec.augmented_text == "好的哈~"
        assert dec.token is None
        assert dec.reason == "auto_inject_disabled"

    def test_noop_hooks_returns_original(self):
        noop = NoopContactHooks()
        dec = noop.maybe_before_reply(
            account_id="a", external_id="b", ai_reply="hello")
        assert dec.augmented_text == "hello"
        assert dec.reason == "noop_hooks"
        assert dec.token is None


# ── 开关打开 + 各种决策路径 ────────────────────────
class TestFlagEnabledHappyPath:
    def test_all_checks_pass_augments(self, full_gw):
        store, gw = full_gw
        hooks = GatewayContactHooks(gw, auto_inject_enabled=True)
        ctx = _seed_warm(store, gw)
        ai = "好的哈~"
        dec = hooks.maybe_before_reply(
            account_id="acc-A", external_id="fb_a",
            ai_reply=ai, latest_in_text="我去睡啦 晚安",
        )
        assert dec.reason == "ok"
        assert dec.token and len(dec.token) > 0
        assert dec.script_id
        # 结构：AI 回复 + 分隔符 + 引流话术
        assert dec.augmented_text.startswith(ai)
        assert dec.token in dec.augmented_text
        assert "@line_acc-A" in dec.augmented_text
        # 分隔符默认是 "\n\n"
        assert "\n\n" in dec.augmented_text
        # Journey 已推到 HANDOFF_READY
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == STAGE_HANDOFF_READY

    def test_custom_separator_used(self, full_gw):
        store, gw = full_gw
        hooks = GatewayContactHooks(
            gw, auto_inject_enabled=True, inject_separator=" 👉 ")
        _seed_warm(store, gw)
        dec = hooks.maybe_before_reply(
            account_id="acc-A", external_id="fb_a",
            ai_reply="好的", latest_in_text="我去睡啦 晚安",
        )
        assert dec.reason == "ok"
        assert " 👉 " in dec.augmented_text

    def test_empty_ai_reply_uses_handoff_text_only(self, full_gw):
        store, gw = full_gw
        hooks = GatewayContactHooks(gw, auto_inject_enabled=True)
        _seed_warm(store, gw)
        dec = hooks.maybe_before_reply(
            account_id="acc-A", external_id="fb_a",
            ai_reply="", latest_in_text="晚安",
        )
        assert dec.reason == "ok"
        # 空 AI 回复时不带分隔符，直接拿话术
        assert not dec.augmented_text.startswith("\n\n")
        assert dec.token in dec.augmented_text


class TestFlagEnabledSkipPaths:
    def test_no_ci_returns_original(self, full_gw):
        store, gw = full_gw
        hooks = GatewayContactHooks(gw, auto_inject_enabled=True)
        dec = hooks.maybe_before_reply(
            account_id="acc-A", external_id="unknown_peer",
            ai_reply="hi", latest_in_text="晚安",
        )
        assert dec.reason == "no_ci"
        assert dec.augmented_text == "hi"
        assert dec.token is None

    def test_readiness_not_open_returns_original(self, full_gw):
        store, gw = full_gw
        hooks = GatewayContactHooks(gw, auto_inject_enabled=True)
        _seed_warm(store, gw)
        # 非 goodbye 文本 → readiness 不开窗
        dec = hooks.maybe_before_reply(
            account_id="acc-A", external_id="fb_a",
            ai_reply="嗯嗯", latest_in_text="你吃饭了没",
        )
        assert dec.reason == "not_ready"
        assert dec.augmented_text == "嗯嗯"
        assert dec.token is None

    def test_cap_exhausted_returns_original(self, full_gw):
        store, gw = full_gw
        hooks = GatewayContactHooks(gw, auto_inject_enabled=True)
        _seed_warm(store, gw)
        # 用掉 3 次
        for _ in range(3):
            d = hooks.maybe_before_reply(
                account_id="acc-A", external_id="fb_a",
                ai_reply="x", latest_in_text="晚安",
            )
            assert d.reason == "ok"
        # 第 4 次 cap 用尽
        dec = hooks.maybe_before_reply(
            account_id="acc-A", external_id="fb_a",
            ai_reply="x", latest_in_text="晚安",
        )
        assert dec.reason == "account_cap_exceeded"
        assert dec.augmented_text == "x"
        assert dec.token is None


class TestGatewayException:
    """gateway 抛任何异常，maybe_before_reply 必须静默降级。"""

    def test_gateway_exception_returns_original(self, full_gw):
        store, gw = full_gw
        hooks = GatewayContactHooks(gw, auto_inject_enabled=True)
        # 摘掉 renderer 让 maybe_issue_handoff 走到 no_renderer 分支（不是异常）
        # 所以这里单独 patch find_channel_identity 使它抛
        def boom(**_):
            raise RuntimeError("db went away")
        gw.find_channel_identity = boom    # type: ignore[assignment]
        dec = hooks.maybe_before_reply(
            account_id="acc-A", external_id="fb_a",
            ai_reply="hi", latest_in_text="晚安",
        )
        assert dec.reason == "hook_error"
        assert dec.augmented_text == "hi"


class TestOnHandoffSentWorkflow:
    """augmented → send 成功 → on_handoff_sent 推到 HANDOFF_SENT。"""

    def test_full_flow_advances_to_handoff_sent(self, full_gw):
        store, gw = full_gw
        hooks = GatewayContactHooks(gw, auto_inject_enabled=True)
        ctx = _seed_warm(store, gw)
        dec = hooks.maybe_before_reply(
            account_id="acc-A", external_id="fb_a",
            ai_reply="好的", latest_in_text="晚安",
        )
        assert dec.token
        # runner 发送成功后调 on_handoff_sent
        hooks.on_handoff_sent(
            account_id="acc-A", external_id="fb_a", token=dec.token,
        )
        j = store.get_journey(ctx.journey.journey_id)
        assert j.funnel_stage == STAGE_HANDOFF_SENT


# ── bootstrap 注入 ─────────────────────────────────
class TestBootstrapWiring:
    def test_bootstrap_reads_config_disabled_by_default(self, tmp_path):
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
                "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
            }},
            CFG_DIR,
        )
        try:
            assert sub.hooks._auto_inject_enabled is False
        finally:
            sub.close()

    def test_bootstrap_wires_auto_inject_when_enabled(self, tmp_path):
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
                "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
                "handoff_auto_inject": {"enabled": True, "separator": " | "},
            }},
            CFG_DIR,
        )
        try:
            assert sub.hooks._auto_inject_enabled is True
            assert sub.hooks._inject_sep == " | "
        finally:
            sub.close()
