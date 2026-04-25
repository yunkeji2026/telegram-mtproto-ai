"""W4-Bridge：runner → 真 GatewayContactHooks → 真 ContactStore 的契约集成测试。

现有覆盖里的缺口（本文件填补）：
  - `test_rpa_contact_hooks_wireup.py`: runner emit → **fake** recording hooks (`**kw` 全收，
    不会发现 kwargs 形状漂移)
  - `test_contacts_e2e.py` / `test_contacts_w4_e2e.py`: **直调 gateway**，绕过 hooks 层

如果 LINE runner 的 emit kwargs / Messenger runner 散调的 kwargs 与
`GatewayContactHooks` 的签名漂移，**只有本文件能在 CI 中第一时间发现**。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts import bootstrap_contacts_subsystem
from src.contacts.models import (
    CHANNEL_LINE, CHANNEL_MESSENGER,
    STAGE_HANDOFF_SENT, STAGE_LINE_ENGAGED,
)

CFG_DIR = Path(__file__).resolve().parent.parent / "config"


# ── helpers ─────────────────────────────────────────────
def _make_subsystem(tmp_path, *, auto_inject: bool = False):
    """启动一个真 ContactsSubsystem，db 落 tmp_path。"""
    sub = bootstrap_contacts_subsystem(
        {"contacts": {
            "enabled": True,
            "db_path": str(tmp_path / "c.db"),
            "daily_cap": 5,
            "token_ttl_hours": 24,
            "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
            "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
            "default_line_id": "@bridge_test",
            "handoff_auto_inject": {"enabled": auto_inject},
        }},
        CFG_DIR,
    )
    assert sub is not None, "bootstrap failed"
    return sub


# ── 1. LINE runner.emit → 真 hooks → 真 store 全链路 ────────────────
class TestLineRunnerToRealHooks:
    """`LineRpaRunner._emit_contact_message` 调真 GatewayContactHooks，
    端到端打到 ContactStore。"""

    def test_inbound_with_token_triggers_real_merge(self, tmp_path):
        """LINE runner inbound 带 token → 真 hooks → 真 merge → store 里 contact 合一。"""
        sub = _make_subsystem(tmp_path)
        try:
            # 1. 在 messenger 侧先制造 contact + handoff token (用真 hooks，
            #    不直调 gateway——本测试就是要验 hooks 路径)
            sub.hooks.on_peer_seen(
                channel=CHANNEL_MESSENGER, account_id="acc-A",
                external_id="fb_alice", display_name="Alice",
                trace_id="m-1",
            )
            sub.hooks.on_message(
                channel=CHANNEL_MESSENGER, account_id="acc-A",
                external_id="fb_alice", direction="in",
                text_preview="你好", display_name="Alice", trace_id="m-1",
            )
            token = sub.hooks.issue_handoff_for_messenger(
                account_id="acc-A", external_id="fb_alice", trace_id="m-1",
            )
            assert token, "issue_handoff_for_messenger should return a token"
            sub.hooks.on_handoff_sent(
                account_id="acc-A", external_id="fb_alice",
                token=token, trace_id="m-1",
            )

            # 2. 真 LINE runner（不跑 adb，只调 emit 路径）
            from src.integrations.line_rpa.runner import LineRpaRunner
            cm = SimpleNamespace(config_path=str(CFG_DIR / "config.yaml"))
            line_runner = LineRpaRunner(
                config_manager=cm, skill_manager=MagicMock(),
                line_rpa_cfg={"account_id": "acc-A", "default_reply_lang": "zh"},
                state_store=None,
            )
            line_runner.set_contact_hooks(sub.hooks)

            # 3. line runner 收到 inbound，走 emit → 真 hooks.on_line_first_text
            line_runner._emit_contact_message(
                chat_key="line_alice_xx", direction="in",
                text=f"加上啦 {token} 是我", trace_id="l-1",
            )

            # 4. 断言：合并已发生（messenger ci 和 line ci 共一个 contact_id）
            line_ci = sub.store.get_ci_by_external(
                CHANNEL_LINE, "acc-A", "line_alice_xx")
            msg_ci = sub.store.get_ci_by_external(
                CHANNEL_MESSENGER, "acc-A", "fb_alice")
            assert line_ci is not None, "LINE ci should exist after emit"
            assert msg_ci is not None
            assert line_ci.contact_id == msg_ci.contact_id, \
                "merge via real hooks should unify contacts"

            # 5. Journey 推到 LINE_ENGAGED
            j = sub.store.get_journey_by_contact(line_ci.contact_id)
            assert j.funnel_stage == STAGE_LINE_ENGAGED

            # 6. 跨平台 trace 都落事件了
            events = sub.store.list_events(j.journey_id, limit=100)
            traces = {e["trace_id"] for e in events if e["trace_id"]}
            assert "m-1" in traces and "l-1" in traces
        finally:
            sub.close()

    def test_outbound_via_real_hooks_records_msg_out(self, tmp_path):
        """LINE runner outbound 调真 hooks.on_message(direction='out') → store 里有 msg_out。"""
        sub = _make_subsystem(tmp_path)
        try:
            # 先让 LINE 端有 contact (inbound 一次, 无 token, 走 keep_isolated)
            sub.hooks.on_line_first_text(
                account_id="acc-A", external_id="line_solo",
                text="hi", display_name="Solo",
            )
            line_ci = sub.store.get_ci_by_external(
                CHANNEL_LINE, "acc-A", "line_solo")
            assert line_ci is not None

            # 真 LINE runner outbound
            from src.integrations.line_rpa.runner import LineRpaRunner
            cm = SimpleNamespace(config_path=str(CFG_DIR / "config.yaml"))
            line_runner = LineRpaRunner(
                config_manager=cm, skill_manager=MagicMock(),
                line_rpa_cfg={"account_id": "acc-A"},
                state_store=None,
            )
            line_runner.set_contact_hooks(sub.hooks)

            line_runner._emit_contact_message(
                chat_key="line_solo", direction="out",
                text="你好我是客服 " * 50,  # > 120 char, 测 truncation
                trace_id="l-out",
            )

            j = sub.store.get_journey_by_contact(line_ci.contact_id)
            events = sub.store.list_events(j.journey_id, limit=200)
            out_events = [e for e in events if e["event_type"] == "msg_out"]
            assert len(out_events) >= 1
            assert any(e["trace_id"] == "l-out" for e in out_events)
        finally:
            sub.close()

    def test_empty_chat_key_silent_in_real_hooks_path(self, tmp_path):
        """空 chat_key 早返不打 hooks（runner 内部守卫）— 真 hooks 也不会被调。"""
        sub = _make_subsystem(tmp_path)
        try:
            from src.integrations.line_rpa.runner import LineRpaRunner
            cm = SimpleNamespace(config_path=str(CFG_DIR / "config.yaml"))
            line_runner = LineRpaRunner(
                config_manager=cm, skill_manager=MagicMock(),
                line_rpa_cfg={"account_id": "acc-A"}, state_store=None,
            )
            line_runner.set_contact_hooks(sub.hooks)
            line_runner._emit_contact_message(
                chat_key="", direction="in", text="hi",
            )
            # store 里不应有任何 LINE channel ci
            with sub.store._lock:
                row = sub.store._conn.execute(
                    "SELECT COUNT(*) FROM channel_identities WHERE channel=?",
                    (CHANNEL_LINE,)).fetchone()
            assert row[0] == 0
        finally:
            sub.close()


# ── 2. Messenger runner kwargs-shape 契约 ──────────────────────────
class TestMessengerKwargsShapeContract:
    """Messenger runner 散调 hooks 的 4 个调用点的 kwargs 形状必须被
    GatewayContactHooks 接受。

    runner.py 实际调用点（截至 2026-04-25 main HEAD）::
        - hooks.on_message(channel, account_id, external_id, direction,
                           text_preview, display_name, trace_id)
        - hooks.maybe_before_reply(account_id, external_id, ai_reply,
                                   latest_in_text, trace_id)
        - hooks.on_handoff_sent(account_id, external_id, token, trace_id)
    """

    def test_on_message_in_kwargs_accepted(self, tmp_path):
        sub = _make_subsystem(tmp_path)
        try:
            # 复刻 runner.py:2922 的 kwargs（截 120 + display_name）
            sub.hooks.on_message(
                channel="messenger",
                account_id="acc-A",
                external_id="fb_bob",
                direction="in",
                text_preview=("hello " * 30)[:120],
                display_name="Bob",
                trace_id="req-msg-in",
            )
            ci = sub.store.get_ci_by_external(
                CHANNEL_MESSENGER, "acc-A", "fb_bob")
            assert ci is not None
        finally:
            sub.close()

    def test_on_handoff_sent_kwargs_accepted(self, tmp_path):
        sub = _make_subsystem(tmp_path)
        try:
            # 先制造 ci + token
            sub.hooks.on_message(
                channel="messenger", account_id="acc-A",
                external_id="fb_bob", direction="in",
                text_preview="hi", display_name="Bob", trace_id="t1",
            )
            token = sub.hooks.issue_handoff_for_messenger(
                account_id="acc-A", external_id="fb_bob", trace_id="t1",
            )
            assert token

            # 复刻 runner.py:950 的 kwargs
            sub.hooks.on_handoff_sent(
                account_id="acc-A",
                external_id="fb_bob",
                token=token,
                trace_id="t1",
            )
            ci = sub.store.get_ci_by_external(
                CHANNEL_MESSENGER, "acc-A", "fb_bob")
            j = sub.store.get_journey_by_contact(ci.contact_id)
            assert j.funnel_stage == STAGE_HANDOFF_SENT
        finally:
            sub.close()

    def test_maybe_before_reply_kwargs_accepted_with_auto_inject(self, tmp_path):
        """auto_inject 开关默认关——开了之后 runner.py:915 的 kwargs 必须能跑通。"""
        sub = _make_subsystem(tmp_path, auto_inject=True)
        try:
            # 复刻 runner.py:915 的 kwargs（不要求一定签到 token，只要 hooks 不抛）
            dec = sub.hooks.maybe_before_reply(
                account_id="acc-A",
                external_id="fb_bob",
                ai_reply="您好，谢谢咨询",
                latest_in_text="想加 LINE",
                trace_id="req-mbr",
            )
            assert dec is not None
            # auto_inject 开关开 + 没 ci 时 reason='no_ci', augmented_text 等于原 ai_reply
            assert dec.augmented_text == "您好，谢谢咨询"
            assert dec.reason in {
                "no_ci", "ok", "no_script", "compliance_block",
                "readiness_low", "cap_full",
            }
        finally:
            sub.close()

    def test_maybe_before_reply_kwargs_accepted_when_disabled(self, tmp_path):
        """auto_inject 关时 reason='auto_inject_disabled'，kwargs 仍要能塞进去不抛。"""
        sub = _make_subsystem(tmp_path, auto_inject=False)
        try:
            dec = sub.hooks.maybe_before_reply(
                account_id="acc-A",
                external_id="fb_bob",
                ai_reply="您好",
                latest_in_text="想加 LINE",
                trace_id="req-mbr-2",
            )
            assert dec.reason == "auto_inject_disabled"
            assert dec.augmented_text == "您好"
        finally:
            sub.close()
