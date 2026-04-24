"""W2 端到端集成测试：模拟 RPA 完整调用链。

覆盖场景：
  S1. Messenger 多轮对话 → 引流 → LINE 首条带 token → 自动合并
  S2. 话术里没带 token（用户忘了）→ 依靠信号融合 → 自动合并
  S3. 信号不够 → 进 manual_review → 运营批准 → 完成合并
  S4. 两个候选都像 → 降级 review（歧义保护）
  S5. token 过期 → 不能用 → 走信号路径
  S6. token 已被别人用 → 不能用 → 走信号路径
  S7. LINE 陌生人首条（完全无候选）→ 保持孤岛

设计：直接用 GatewayContactHooks 作为 RPA 代理——如果 hooks 没被正确实现，
E2E 会最先失败，给下阶段真接入兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts import (
    ContactStore, HandoffTokenService, MergeService, ContactGateway,
    GatewayContactHooks,
)
from src.contacts.models import (
    CHANNEL_LINE, CHANNEL_MESSENGER,
    STAGE_ENGAGED, STAGE_HANDOFF_READY, STAGE_HANDOFF_SENT,
    STAGE_LINE_ENGAGED, STAGE_INITIAL,
)


@pytest.fixture
def env(tmp_path):
    store = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(store, ttl_seconds=72 * 3600)
    merge = MergeService(store)
    gw = ContactGateway(store, handoff, merge)
    hooks = GatewayContactHooks(gw)
    yield store, handoff, merge, gw, hooks
    store.close()


def _simulate_messenger_session(hooks, *, account_id, fb_id, name, turns=3):
    """模拟 RPA 在 Messenger 上跟某人聊 N 轮。"""
    trace = f"trace-{fb_id}"
    for i in range(turns):
        hooks.on_message(
            channel=CHANNEL_MESSENGER, account_id=account_id, external_id=fb_id,
            direction="in", text_preview=f"你好 {i}", display_name=name, trace_id=trace,
        )
        hooks.on_message(
            channel=CHANNEL_MESSENGER, account_id=account_id, external_id=fb_id,
            direction="out", text_preview=f"嗯嗯 {i}", trace_id=trace,
        )


class TestScenarioS1_HappyTokenPath:
    def test_messenger_engaged_handoff_line_merged(self, env):
        store, _, _, gw, hooks = env
        # 1. Messenger 聊 3 轮
        _simulate_messenger_session(
            hooks, account_id="acc-A", fb_id="fb_alice", name="Alice")
        # 2. 业务层决定引流：签 token
        token = hooks.issue_handoff_for_messenger(
            account_id="acc-A", external_id="fb_alice")
        assert token is not None
        # 3. RPA 用 token 发话术 → 成功发送
        hooks.on_handoff_sent(
            account_id="acc-A", external_id="fb_alice", token=token)

        # 此时 Messenger 侧 Journey 状态：
        ci = store.get_ci_by_external(CHANNEL_MESSENGER, "acc-A", "fb_alice")
        j = store.get_journey_by_contact(ci.contact_id)
        assert j.funnel_stage == STAGE_HANDOFF_SENT

        # 4. LINE 侧：她加好友 → scanner 识别（此处省略）→ 运营批准 → runner 通过
        # 5. 她发首条带 token
        out = hooks.on_line_first_text(
            account_id="acc-A", external_id="line_alice_xx",
            text=f"加上啦 {token} 是我",
            display_name="Alice", language_hint="", timezone_hint="",
        )
        assert out.merged is True
        assert out.via == "token"

        # 6. 断言：Contact 只有一个，且 Journey 已到 LINE_ENGAGED
        cis = store.list_channel_identities_of(out.contact_id)
        channels = {c.channel for c in cis}
        assert channels == {CHANNEL_MESSENGER, CHANNEL_LINE}
        j2 = store.get_journey_by_contact(out.contact_id)
        assert j2.funnel_stage == STAGE_LINE_ENGAGED

        # 7. Funnel 节点都有事件
        events = store.list_events(j2.journey_id, limit=100)
        types = {e["event_type"] for e in events}
        for expected in [
            "contact_created", "msg_in", "msg_out", "stage_change",
            "token_issued", "handoff_sent", "line_first_reply",
            "channel_identity_merged",
        ]:
            assert expected in types, f"missing event: {expected}"


class TestScenarioS2_SignalPath:
    def test_no_token_signals_match_auto_merge(self, env):
        store, _, _, gw, hooks = env
        _simulate_messenger_session(
            hooks, account_id="acc-A", fb_id="fb_alice", name="Alice")
        # 把 contact 的 language/tz 设好（通常 RPA 会推断）
        ci = store.get_ci_by_external(CHANNEL_MESSENGER, "acc-A", "fb_alice")
        store.update_contact(ci.contact_id, language_hint="zh",
                              timezone_hint="Asia/Shanghai")
        token = hooks.issue_handoff_for_messenger(
            account_id="acc-A", external_id="fb_alice")
        hooks.on_handoff_sent(account_id="acc-A", external_id="fb_alice", token=token)

        # LINE 侧首条**没带 token**，但所有信号一致
        out = hooks.on_line_first_text(
            account_id="acc-A", external_id="line_alice_xx",
            text="我加你了",
            display_name="Alice", language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        assert out.merged is True
        assert out.via == "heuristic"
        assert out.confidence >= 0.90


class TestScenarioS3_ReviewPath:
    def test_medium_confidence_enters_review_then_approved(self, env):
        store, _, merge, gw, hooks = env
        _simulate_messenger_session(
            hooks, account_id="acc-A", fb_id="fb_alice", name="Alice Liu")
        ci = store.get_ci_by_external(CHANNEL_MESSENGER, "acc-A", "fb_alice")
        store.update_contact(ci.contact_id, language_hint="zh",
                              timezone_hint="Asia/Shanghai")
        hooks.issue_handoff_for_messenger(
            account_id="acc-A", external_id="fb_alice")

        # LINE 首条 无 token，名字半像、tz 不同 → 中置信
        out = hooks.on_line_first_text(
            account_id="acc-A", external_id="line_1",
            text="嗨",
            display_name="Alice L.", language_hint="zh", timezone_hint="Asia/Tokyo",
        )
        assert out.merged is False
        assert out.review_id
        # 运营在 Web 上批准
        assert merge.approve_review(out.review_id, resolved_by="admin_qa") is True
        # LINE ci 已迁到 Messenger 的 Contact
        target_ci = store.get_ci_by_external(CHANNEL_LINE, "acc-A", "line_1")
        assert target_ci.contact_id == ci.contact_id


class TestScenarioS4_AmbiguityProtection:
    def test_two_similar_candidates_forced_to_review(self, env):
        store, _, _, gw, hooks = env
        # 两个都叫 Alice 都是 zh Shanghai
        for fb in ("fb_a1", "fb_a2"):
            _simulate_messenger_session(
                hooks, account_id="acc-A", fb_id=fb, name="Alice")
            ci = store.get_ci_by_external(CHANNEL_MESSENGER, "acc-A", fb)
            store.update_contact(ci.contact_id, language_hint="zh",
                                  timezone_hint="Asia/Shanghai")
            hooks.issue_handoff_for_messenger(account_id="acc-A", external_id=fb)

        out = hooks.on_line_first_text(
            account_id="acc-A", external_id="line_mystery",
            text="嗨", display_name="Alice",
            language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        assert out.merged is False
        assert out.review_id
        assert out.decision is not None
        assert "ambiguous_top2" in out.decision.reason


class TestScenarioS5_ExpiredToken:
    def test_expired_token_drops_candidate_from_pool(self, env):
        """token 过期 = 引流窗口结束。这个 messenger 从候选池消失是有意设计。

        业务意图：过期窗口还强行匹配容易误合并——宁可让用户走"陌生人"流程。
        """
        store, handoff, _, gw, hooks = env
        _simulate_messenger_session(
            hooks, account_id="acc-A", fb_id="fb_alice", name="Alice")
        ci = store.get_ci_by_external(CHANNEL_MESSENGER, "acc-A", "fb_alice")
        store.update_contact(ci.contact_id, language_hint="zh",
                              timezone_hint="Asia/Shanghai")
        token = hooks.issue_handoff_for_messenger(
            account_id="acc-A", external_id="fb_alice")
        # 把 token expires_at 改到过去
        with store._lock:
            store._conn.execute(
                "UPDATE handoff_tokens SET expires_at=0 WHERE token=?", (token,))
            store._conn.commit()

        # LINE 首条带过期 token → 候选池为空 → keep_isolated
        out = hooks.on_line_first_text(
            account_id="acc-A", external_id="line_xx",
            text=f"加你了 {token}",
            display_name="Alice", language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        assert out.merged is False
        assert out.via == "none"
        assert out.reason == "no_candidates"
        # LINE 侧仍然是孤岛 Contact
        assert store.get_contact(out.contact_id) is not None


class TestScenarioS6_TokenAlreadyConsumed:
    def test_consumed_token_cannot_be_reused(self, env):
        store, handoff, _, gw, hooks = env
        _simulate_messenger_session(
            hooks, account_id="acc-A", fb_id="fb_alice", name="Alice")
        token = hooks.issue_handoff_for_messenger(
            account_id="acc-A", external_id="fb_alice")
        # 第一个 LINE 用户用掉
        out1 = hooks.on_line_first_text(
            account_id="acc-A", external_id="line_first",
            text=f"嗨 {token}", display_name="Alice",
        )
        assert out1.merged is True
        # 第二个人冒充同样的 token → consume 会抛 TokenAlreadyConsumed → try_consume 吞掉
        out2 = hooks.on_line_first_text(
            account_id="acc-A", external_id="line_imposter",
            text=f"也是我 {token}", display_name="Stranger",
        )
        assert out2.merged is False or out2.via != "token"


class TestScenarioS7_PureStranger:
    def test_stranger_no_signals_keeps_isolated(self, env):
        store, _, _, gw, hooks = env
        # 没有任何 messenger 候选
        out = hooks.on_line_first_text(
            account_id="acc-A", external_id="line_random",
            text="hi who dis",
            display_name="RandomGuy",
        )
        assert out.merged is False
        assert out.via == "none"
        # LINE 孤岛 Contact 仍在
        assert store.get_contact(out.contact_id) is not None


class TestScenarioS8_FullFunnelCounts:
    def test_multiple_users_funnel_stats(self, env):
        """模拟一天：3 个用户走完整漏斗，观察 Journey 状态分布。"""
        store, _, _, gw, hooks = env
        names = ["Alice", "Bob", "Cindy"]
        # 全部进入 Messenger 聊天
        for i, name in enumerate(names):
            _simulate_messenger_session(
                hooks, account_id="acc-A", fb_id=f"fb_{i}", name=name)

        # Alice + Bob 发 handoff，Cindy 不发
        tok_a = hooks.issue_handoff_for_messenger(
            account_id="acc-A", external_id="fb_0")
        tok_b = hooks.issue_handoff_for_messenger(
            account_id="acc-A", external_id="fb_1")
        hooks.on_handoff_sent(account_id="acc-A", external_id="fb_0", token=tok_a)
        hooks.on_handoff_sent(account_id="acc-A", external_id="fb_1", token=tok_b)

        # Alice 加 LINE 并回带 token；Bob 没加 LINE
        hooks.on_line_first_text(
            account_id="acc-A", external_id="line_alice",
            text=f"加你了 {tok_a}", display_name="Alice",
        )

        # 统计 Journey 分布
        stages = {}
        for i in range(3):
            ci = store.get_ci_by_external(CHANNEL_MESSENGER, "acc-A", f"fb_{i}")
            j = store.get_journey_by_contact(ci.contact_id)
            stages.setdefault(j.funnel_stage, 0)
            stages[j.funnel_stage] += 1

        # Alice: LINE_ENGAGED, Bob: HANDOFF_SENT, Cindy: ENGAGED
        assert stages.get(STAGE_LINE_ENGAGED, 0) == 1
        assert stages.get(STAGE_HANDOFF_SENT, 0) == 1
        assert stages.get(STAGE_ENGAGED, 0) == 1


class TestTraceIdEndToEnd:
    def test_trace_id_flows_across_platforms(self, env):
        store, _, _, gw, hooks = env
        trace_m = "trace-messenger-abc"
        trace_l = "trace-line-xyz"
        # Messenger 端用 trace_m
        hooks.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            display_name="Alice", trace_id=trace_m,
        )
        hooks.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi", trace_id=trace_m,
        )
        token = hooks.issue_handoff_for_messenger(
            account_id="a", external_id="fb_1", trace_id=trace_m,
        )
        hooks.on_handoff_sent(
            account_id="a", external_id="fb_1", token=token, trace_id=trace_m,
        )
        # LINE 端用 trace_l
        hooks.on_line_first_text(
            account_id="a", external_id="line_1",
            text=f"hi {token}", display_name="Alice", trace_id=trace_l,
        )
        ci = store.get_ci_by_external(CHANNEL_MESSENGER, "a", "fb_1")
        j = store.get_journey_by_contact(ci.contact_id)
        events = store.list_events(j.journey_id, limit=100)
        traces = {e["trace_id"] for e in events if e["trace_id"]}
        assert trace_m in traces
        assert trace_l in traces
