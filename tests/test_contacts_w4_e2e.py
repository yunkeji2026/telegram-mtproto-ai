"""W4 E2E：用 bootstrap 真实装配，跑"Messenger 引流 → LINE 合并"整链。

和 W3 不同点：
  - 走 `bootstrap_contacts_subsystem`（生产路径），不直接 new 各组件
  - 主打 `maybe_issue_handoff`（集成 readiness/cap/render/token/compliance 的对外入口）
  - 两轮批判：
      · Round 1（资源边界）：cap 跨账号独立、dry_run 不污染真发
      · Round 2（前置失败不泄漏）：no_script/compliance 拒时 cap 保持正确
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts import bootstrap_contacts_subsystem
from src.contacts.models import (
    CHANNEL_MESSENGER,
    STAGE_ENGAGED,
    STAGE_HANDOFF_READY,
    STAGE_HANDOFF_SENT,
    STAGE_LINE_ENGAGED,
)

CFG_DIR = Path(__file__).resolve().parent.parent / "config"


# ── helpers ─────────────────────────────────────────────
def _base_cfg(tmp_db: Path, **overrides) -> dict:
    cfg = {
        "contacts": {
            "enabled": True,
            "db_path": str(tmp_db),
            "daily_cap": 3,
            "token_ttl_hours": 24,
            "readiness_threshold": 70,
            "turn_saturation": 3,
            "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
            "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
            "default_line_id": "@w4_default",
            "line_ids_by_account": {"acc-A": "@w4_A", "acc-B": "@w4_B"},
        }
    }
    cfg["contacts"].update(overrides)
    return cfg


def _backfill_warm(store, journey_id: str, days: int = 5, rounds: int = 4) -> None:
    """写入模拟聊天事件，让 readiness 能过 70 分。"""
    now = int(time.time())
    with store._lock:
        for d in range(days):
            for i in range(rounds):
                for et in ("msg_in", "msg_out"):
                    store._conn.execute(
                        "INSERT INTO journey_events "
                        "(event_id, journey_id, trace_id, event_type, payload_json, ts) "
                        "VALUES (?, ?, '', ?, '{}', ?)",
                        (uuid.uuid4().hex, journey_id, et,
                         now - d * 86400 - i * 60),
                    )
        store._conn.execute(
            "UPDATE journeys SET funnel_stage=?, updated_at=? WHERE journey_id=?",
            (STAGE_ENGAGED, now, journey_id))
        store._conn.commit()


@pytest.fixture
def sub(tmp_path):
    """标准 3/天 cap、真话术、真合规的 subsystem——cleanup 自动关 store。"""
    s = bootstrap_contacts_subsystem(_base_cfg(tmp_path / "c.db"), CFG_DIR)
    assert s is not None, "bootstrap failed"
    yield s
    s.close()


# ─────────────────────────────────────────────────────────
class TestW4S1_FullFlowViaBootstrap:
    """主干：bootstrap 装配的系统能把 Messenger 聊天一路带到 LINE 合并。"""

    def test_messenger_to_line_merged(self, sub):
        gw = sub.gateway
        store = sub.store

        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="acc-A",
            external_id="fb_alice", display_name="Alice",
            language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        store.update_contact(ctx.contact.contact_id,
                              primary_name="Alice",
                              language_hint="zh", timezone_hint="Asia/Shanghai")
        jid = ctx.journey.journey_id

        # 几次消息 + 补历史 → readiness 开得起来
        gw.on_message(channel=CHANNEL_MESSENGER, account_id="acc-A",
                       external_id="fb_alice", direction="in", text_preview="你好")
        _backfill_warm(store, jid)

        # 预览：dry_run
        preview = gw.maybe_issue_handoff(
            messenger_ci_id=ctx.channel_identity.channel_identity_id,
            latest_in_text="好啦 晚安 睡啦",
            dry_run=True,
        )
        assert preview.success and preview.token == "dry_rn"

        # 真发
        real = gw.maybe_issue_handoff(
            messenger_ci_id=ctx.channel_identity.channel_identity_id,
            latest_in_text="好啦 晚安 睡啦",
        )
        assert real.success, f"unexpected failure: {real.reason}"
        assert real.token != "dry_rn"
        # 文本里同时带 token 和 line_id
        assert real.token in real.text
        assert "@w4_A" in real.text
        # Journey 推到 HANDOFF_READY
        assert store.get_journey(jid).funnel_stage == STAGE_HANDOFF_READY

        gw.on_handoff_sent(
            messenger_ci_id=ctx.channel_identity.channel_identity_id,
            token=real.token,
        )
        assert store.get_journey(jid).funnel_stage == STAGE_HANDOFF_SENT

        # LINE 侧首条带 token → 自动合并
        outcome = gw.on_line_first_text(
            account_id="acc-A", external_id="line_alice",
            text=f"我加上啦 {real.token}", display_name="Alice Chan",
        )
        assert outcome.merged is True
        assert outcome.via == "token"
        j = store.get_journey_by_contact(outcome.contact_id)
        assert j.funnel_stage == STAGE_LINE_ENGAGED

        # 最终视图：一个 Contact，两个 channel
        cis = store.list_channel_identities_of(outcome.contact_id)
        channels = {ci.channel for ci in cis}
        assert channels == {"messenger", "line"}


# ─────────────────────────────────────────────────────────
class TestW4Round1_ResourceBoundaries:
    """第一轮批判：资源相关——cap 隔离 + dry_run 不污染真发。"""

    def test_cap_is_per_account_not_global(self, sub):
        """账号 A 用光 cap 不影响账号 B——限额按账号独立。"""
        gw = sub.gateway
        store = sub.store
        # 两个账号各自的 contact
        ctx_a = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="acc-A",
            external_id="fb_boundA", display_name="X")
        ctx_b = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="acc-B",
            external_id="fb_boundB", display_name="Y")
        _backfill_warm(store, ctx_a.journey.journey_id)
        _backfill_warm(store, ctx_b.journey.journey_id)

        # 账号 A 耗尽（cap=3）
        for _ in range(3):
            r = gw.maybe_issue_handoff(
                messenger_ci_id=ctx_a.channel_identity.channel_identity_id,
                latest_in_text="晚安")
            assert r.success
        r_a_over = gw.maybe_issue_handoff(
            messenger_ci_id=ctx_a.channel_identity.channel_identity_id,
            latest_in_text="晚安")
        assert r_a_over.success is False
        assert r_a_over.reason == "account_cap_exceeded"

        # 账号 B 不受影响
        r_b = gw.maybe_issue_handoff(
            messenger_ci_id=ctx_b.channel_identity.channel_identity_id,
            latest_in_text="晚安")
        assert r_b.success, f"account B should still be allowed: {r_b.reason}"
        assert r_b.remaining_today == 2

    def test_dry_run_chain_then_real_cap_drops_by_one(self, sub):
        """N 次 dry_run 后做一次真发——cap 只应减 1。"""
        gw = sub.gateway
        store = sub.store
        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="acc-A",
            external_id="fb_dry_chain")
        _backfill_warm(store, ctx.journey.journey_id)
        limiter = sub.limiter
        before = limiter.remaining_for("acc-A")

        # 5 次预览
        for _ in range(5):
            p = gw.maybe_issue_handoff(
                messenger_ci_id=ctx.channel_identity.channel_identity_id,
                latest_in_text="晚安", dry_run=True)
            assert p.success
        # 真发一次
        r = gw.maybe_issue_handoff(
            messenger_ci_id=ctx.channel_identity.channel_identity_id,
            latest_in_text="晚安")
        assert r.success
        after = limiter.remaining_for("acc-A")
        assert after == before - 1, \
            f"cap 应只被真发消耗一次：before={before}, after={after}"


# ─────────────────────────────────────────────────────────
class TestW4Round2_PreReserveChecksDontLeakCap:
    """第二轮批判：前置失败（no_script / 没有 renderer）不能扣 cap。

    这是最近 reorder 的保护性测试——把 renderer/script 预检放在 cap reserve 之前。
    """

    def test_no_script_does_not_consume_cap(self, tmp_path):
        """拿个没有 'furious' tone 条目的渲染器 → script=None → cap 不该动。"""
        sub = bootstrap_contacts_subsystem(
            _base_cfg(tmp_path / "c.db"), CFG_DIR)
        assert sub is not None
        try:
            gw = sub.gateway
            store = sub.store
            ctx = gw.on_peer_seen(
                channel=CHANNEL_MESSENGER, account_id="acc-A",
                external_id="fb_ns")
            _backfill_warm(store, ctx.journey.journey_id)

            before = sub.limiter.remaining_for("acc-A")
            r = gw.maybe_issue_handoff(
                messenger_ci_id=ctx.channel_identity.channel_identity_id,
                latest_in_text="晚安",
                tone="__tone_that_does_not_exist__",
                language_override="klingon",     # 脚本池里不可能有
            )
            assert r.success is False
            assert r.reason == "no_script"
            after = sub.limiter.remaining_for("acc-A")
            assert after == before, f"no_script 不该扣 cap：before={before}, after={after}"
        finally:
            sub.close()

    def test_no_renderer_does_not_consume_cap(self, tmp_path):
        """renderer 不存在时直接拒，不进 cap 流程。"""
        # 用 bootstrap 后手动摘掉 renderer，模拟 renderer 初始化失败场景
        sub = bootstrap_contacts_subsystem(
            _base_cfg(tmp_path / "c.db"), CFG_DIR)
        assert sub is not None
        try:
            gw = sub.gateway
            store = sub.store
            # 摘掉 renderer（测试隔离）
            gw._renderer = None

            ctx = gw.on_peer_seen(
                channel=CHANNEL_MESSENGER, account_id="acc-A",
                external_id="fb_nr")
            _backfill_warm(store, ctx.journey.journey_id)

            before = sub.limiter.remaining_for("acc-A")
            r = gw.maybe_issue_handoff(
                messenger_ci_id=ctx.channel_identity.channel_identity_id,
                latest_in_text="晚安")
            assert r.success is False
            assert r.reason == "no_renderer"
            after = sub.limiter.remaining_for("acc-A")
            assert after == before, f"no_renderer 不该扣 cap: before={before}, after={after}"
        finally:
            sub.close()


# ─────────────────────────────────────────────────────────
class TestW4Round2_PostReserveFailuresRefund:
    """第二轮批判：reserve 之后的失败（真运行时）要 refund。"""

    def test_compliance_block_refunds_cap(self, tmp_path):
        """compliance 把话术全拒了 → cap 必须退回。"""
        # 覆盖 compliance：屏蔽 "line" 字样，话术池 100% 命中
        from src.skills.handoff_compliance import HandoffComplianceChecker
        sub = bootstrap_contacts_subsystem(
            _base_cfg(tmp_path / "c.db"), CFG_DIR)
        assert sub is not None
        try:
            # 热替换一个严格 compliance
            sub.compliance = HandoffComplianceChecker(
                blocked_keywords=["line"], min_length=1, max_length=10000)
            sub.gateway._compliance = sub.compliance

            store = sub.store
            gw = sub.gateway
            ctx = gw.on_peer_seen(
                channel=CHANNEL_MESSENGER, account_id="acc-A",
                external_id="fb_refund")
            _backfill_warm(store, ctx.journey.journey_id)

            before = sub.limiter.remaining_for("acc-A")
            r = gw.maybe_issue_handoff(
                messenger_ci_id=ctx.channel_identity.channel_identity_id,
                latest_in_text="晚安")
            assert r.success is False
            assert r.reason == "compliance_blocked"

            after = sub.limiter.remaining_for("acc-A")
            assert after == before, \
                f"合规拒应 refund cap: before={before}, after={after}"
            # 而且 token 被撤销，不在活跃列表
            active = store.list_active_tokens_issued_from(
                ctx.channel_identity.channel_identity_id)
            assert active == []
        finally:
            sub.close()


# ─────────────────────────────────────────────────────────
class TestW4S_FeatureFlagOff:
    """关闭 flag 时整个子系统不启动——防回归。"""

    def test_disabled_returns_none(self, tmp_path):
        cfg = _base_cfg(tmp_path / "c.db", enabled=False)
        assert bootstrap_contacts_subsystem(cfg, CFG_DIR) is None
