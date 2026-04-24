"""W4-Runner：验证 Messenger / LINE runner 的 ContactHooks 接入点。

实际 runner 需要 adb 设备才能跑完整循环；本文件只测接入契约：
  - `set_contact_hooks()` 存在并能原子设置 hook
  - `None` hooks 时所有 emit 静默跳过
  - 装上 hooks 后，`_emit_contact_message` / service 广播能调到对应方法
  - Noop hooks 永远不会让 runner 崩溃
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Fakes ───────────────────────────────────────────────
class _RecordingHooks:
    """把 hook 调用全记录成 (method, kwargs) 列表，便于断言。"""

    def __init__(self) -> None:
        self.calls: list = []

    def on_peer_seen(self, **kw): self.calls.append(("on_peer_seen", kw)); return None
    def on_message(self, **kw): self.calls.append(("on_message", kw)); return None
    def issue_handoff_for_messenger(self, **kw):
        self.calls.append(("issue_handoff_for_messenger", kw)); return None
    def on_handoff_sent(self, **kw): self.calls.append(("on_handoff_sent", kw)); return None
    def on_line_first_text(self, **kw):
        self.calls.append(("on_line_first_text", kw)); return None


# ── LINE runner 接入契约 ──────────────────────────────
class TestLineRunnerEmit:
    """LineRpaRunner._emit_contact_message 的行为矩阵。"""

    @pytest.fixture
    def runner(self):
        from src.integrations.line_rpa.runner import LineRpaRunner
        cm = SimpleNamespace(config_path=str(Path("config/config.yaml")))
        sm = MagicMock()
        cfg = {"account_id": "linacc1", "default_reply_lang": "zh"}
        return LineRpaRunner(
            config_manager=cm, skill_manager=sm,
            line_rpa_cfg=cfg, state_store=None,
        )

    def test_none_hooks_is_silent_noop(self, runner):
        # 默认就是 None，什么都不应抛
        runner._emit_contact_message(
            chat_key="c1", direction="in", text="hi", trace_id="t1")
        runner._emit_contact_message(
            chat_key="c1", direction="out", text="hi", trace_id="t1")

    def test_empty_chat_key_is_silent_noop(self, runner):
        h = _RecordingHooks()
        runner.set_contact_hooks(h)
        runner._emit_contact_message(
            chat_key="", direction="in", text="hi")
        assert h.calls == [], "空 chat_key 不应该触发 hook"

    def test_inbound_goes_to_on_line_first_text(self, runner):
        h = _RecordingHooks()
        runner.set_contact_hooks(h)
        runner._emit_contact_message(
            chat_key="line_peer_A", direction="in",
            text="hi 我加你了哈 abc123", trace_id="req-1",
        )
        assert len(h.calls) == 1
        name, kw = h.calls[0]
        assert name == "on_line_first_text"
        assert kw["account_id"] == "linacc1"
        assert kw["external_id"] == "line_peer_A"
        assert kw["text"] == "hi 我加你了哈 abc123"
        assert kw["display_name"] == "line_peer_A"
        assert kw["language_hint"] == "zh"
        assert kw["trace_id"] == "req-1"

    def test_outbound_goes_to_on_message_with_truncated_preview(self, runner):
        h = _RecordingHooks()
        runner.set_contact_hooks(h)
        long_text = "x" * 500
        runner._emit_contact_message(
            chat_key="line_peer_A", direction="out",
            text=long_text, trace_id="req-2",
        )
        assert len(h.calls) == 1
        name, kw = h.calls[0]
        assert name == "on_message"
        assert kw["channel"] == "line"
        assert kw["direction"] == "out"
        assert kw["external_id"] == "line_peer_A"
        # 超过 120 字符必须被截
        assert len(kw["text_preview"]) == 120

    def test_account_id_defaults_when_not_configured(self):
        from src.integrations.line_rpa.runner import LineRpaRunner
        cm = SimpleNamespace(config_path="config/config.yaml")
        # 不提供 account_id → 应 fallback 到 "default"
        runner = LineRpaRunner(
            config_manager=cm, skill_manager=MagicMock(),
            line_rpa_cfg={}, state_store=None,
        )
        h = _RecordingHooks()
        runner.set_contact_hooks(h)
        runner._emit_contact_message(
            chat_key="c1", direction="in", text="hi")
        assert h.calls[0][1]["account_id"] == "default"

    def test_set_contact_hooks_can_be_cleared(self, runner):
        h = _RecordingHooks()
        runner.set_contact_hooks(h)
        runner.set_contact_hooks(None)
        runner._emit_contact_message(
            chat_key="c1", direction="in", text="hi")
        assert h.calls == []

    def test_hook_exception_does_not_propagate(self, runner):
        """runner 保护原则：hook 崩也不能让消息处理流程崩。"""
        class _Boom:
            def on_line_first_text(self, **kw): raise RuntimeError("boom")
            def on_message(self, **kw): raise RuntimeError("boom")
            def on_peer_seen(self, **kw): raise RuntimeError("boom")
            def on_handoff_sent(self, **kw): pass
            def issue_handoff_for_messenger(self, **kw): return None

        runner.set_contact_hooks(_Boom())
        # 两次 emit 都不该抛
        runner._emit_contact_message(
            chat_key="c1", direction="in", text="hi")
        runner._emit_contact_message(
            chat_key="c1", direction="out", text="hi")


# ── Messenger runner 接入契约 ────────────────────────
class TestMessengerRunnerHooks:
    """MessengerRpaRunner 侧的 set_contact_hooks 存在且能存取。"""

    def test_set_contact_hooks_stores_reference(self):
        from src.integrations.messenger_rpa.runner import MessengerRpaRunner
        cm = SimpleNamespace(config_path="config/config.yaml")
        # runner init 需要 state_store，给个 mock 即可
        runner = MessengerRpaRunner(
            config_manager=cm, skill_manager=MagicMock(),
            messenger_rpa_cfg={}, state_store=MagicMock(),
        )
        assert runner._contact_hooks is None
        h = _RecordingHooks()
        runner.set_contact_hooks(h)
        assert runner._contact_hooks is h
        runner.set_contact_hooks(None)
        assert runner._contact_hooks is None


# ── Service 广播 ────────────────────────────────────
class TestLineServiceSetHooks:
    def test_service_set_hooks_propagates_to_runner(self, monkeypatch):
        from src.integrations.line_rpa.service import LineRpaService
        # service 构造要 config_manager / skill_manager / state_store...
        # 简化：直接构造 service 后替换 _runner 为 mock
        cm = SimpleNamespace(
            config_path=str(Path("config/config.yaml")),
            config={},
            get=lambda k, d=None: d,
        )
        # 绕过真 service.__init__，直接手工组装最小对象
        svc = object.__new__(LineRpaService)
        svc._runner = MagicMock()
        svc._contact_hooks = None
        # 调 set_contact_hooks
        h = _RecordingHooks()
        svc.set_contact_hooks(h)
        assert svc._contact_hooks is h
        svc._runner.set_contact_hooks.assert_called_once_with(h)


class TestMessengerServiceSetHooks:
    def test_service_set_hooks_propagates_to_all_runners(self):
        from src.integrations.messenger_rpa.service import MessengerRpaService
        svc = object.__new__(MessengerRpaService)
        r1 = MagicMock()
        r2 = MagicMock()
        svc._runners = {"accA": r1, "accB": r2}
        svc._contact_hooks = None

        h = _RecordingHooks()
        svc.set_contact_hooks(h)
        assert svc._contact_hooks is h
        r1.set_contact_hooks.assert_called_once_with(h)
        r2.set_contact_hooks.assert_called_once_with(h)

    def test_runner_created_after_set_inherits_hooks(self):
        """_get_or_create_runner 创建新 runner 时会继承 service 上的 hooks。"""
        # 我们只测 "_get_or_create_runner 里会调 runner.set_contact_hooks(self._contact_hooks)"
        # 的契约——通过读 service.py 里该分支是否存在来快速保证
        import src.integrations.messenger_rpa.service as mod
        src = mod.__loader__.get_source(mod.__name__)
        assert "set_contact_hooks(_hooks)" in src, \
            "_get_or_create_runner 必须把 service 上已有的 hooks 同步给新 runner"


# ── W4-Hooks-Flag：按 channel 独立开关 ─────────────────
class TestRpaHooksChannelFlag:
    """contacts.rpa_hooks.{messenger,line} 能独立关闭某路 hook 接入。"""

    @pytest.fixture
    def cfg_dir(self):
        from pathlib import Path as _P
        return _P(__file__).resolve().parent.parent / "config"

    def test_default_missing_config_both_enabled(self, tmp_path, cfg_dir):
        from src.contacts import bootstrap_contacts_subsystem
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(cfg_dir / "handoff_scripts.yaml"),
                "compliance_path": str(cfg_dir / "handoff_compliance.yaml"),
            }}, cfg_dir)
        try:
            assert sub.is_rpa_hook_enabled("messenger") is True
            assert sub.is_rpa_hook_enabled("line") is True
        finally:
            sub.close()

    def test_explicit_disable_messenger(self, tmp_path, cfg_dir):
        from src.contacts import bootstrap_contacts_subsystem
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(cfg_dir / "handoff_scripts.yaml"),
                "compliance_path": str(cfg_dir / "handoff_compliance.yaml"),
                "rpa_hooks": {"messenger": False, "line": True},
            }}, cfg_dir)
        try:
            assert sub.is_rpa_hook_enabled("messenger") is False
            assert sub.is_rpa_hook_enabled("line") is True
        finally:
            sub.close()

    def test_both_disabled(self, tmp_path, cfg_dir):
        from src.contacts import bootstrap_contacts_subsystem
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(cfg_dir / "handoff_scripts.yaml"),
                "compliance_path": str(cfg_dir / "handoff_compliance.yaml"),
                "rpa_hooks": {"messenger": False, "line": False},
            }}, cfg_dir)
        try:
            assert sub.is_rpa_hook_enabled("messenger") is False
            assert sub.is_rpa_hook_enabled("line") is False
        finally:
            sub.close()

    def test_unknown_channel_defaults_to_enabled(self, tmp_path, cfg_dir):
        """未在 config 出现的 channel 名字默认 true（向前兼容）。"""
        from src.contacts import bootstrap_contacts_subsystem
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(cfg_dir / "handoff_scripts.yaml"),
                "compliance_path": str(cfg_dir / "handoff_compliance.yaml"),
                "rpa_hooks": {"line": False},   # 只配了 line
            }}, cfg_dir)
        try:
            assert sub.is_rpa_hook_enabled("messenger") is True
            assert sub.is_rpa_hook_enabled("line") is False
            assert sub.is_rpa_hook_enabled("wechat") is True   # 未来 channel
        finally:
            sub.close()
