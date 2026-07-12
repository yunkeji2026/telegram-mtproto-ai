"""autosend_helpers.autosend_voice 抽取回归测试。

重点验证：函数可导入、可调用、正确使用 assistant 捕获（config），
voice 未启用时早退 False（不触发合成/发送）。
"""
import asyncio
from types import SimpleNamespace

import pytest

from src.inbox import autosend_helpers
from src.inbox.autosend_helpers import (
    autosend_image,
    autosend_voice,
    build_autosend_callbacks,
    build_autosend_translate_cb,
)


def _assistant(cfg: dict):
    return SimpleNamespace(config=SimpleNamespace(config=cfg))


def test_is_coroutine_function():
    assert asyncio.iscoroutinefunction(autosend_voice)


def test_disabled_empty_config_returns_false():
    # 空配置 → voice_autosend 未启用 → 早退 False（验证 assistant.config 捕获正确）
    r = asyncio.run(autosend_voice(_assistant({}), "telegram", "acct", "chat", "hi"))
    assert r is False


def test_disabled_explicit_returns_false():
    cfg = {"voice_autosend": {"enabled": False}}
    r = asyncio.run(autosend_voice(_assistant(cfg), "telegram", "acct", "chat", "hi"))
    assert r is False


def test_owns_media_false_returns_false(monkeypatch):
    # voice 启用，但账号不归编排器管理（owns_media=False）→ 早退 False。
    # 验证走到 orchestrator 分支的捕获链不 NameError。
    cfg = {"voice_autosend": {"enabled": True}}

    class _Orch:
        def owns_media(self, platform, account_id):
            return False

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

    r = asyncio.run(autosend_voice(_assistant(cfg), "telegram", "acct", "chat", "hi"))
    assert r is False


class TestAutosendImage:
    def test_is_coroutine_function(self):
        assert asyncio.iscoroutinefunction(autosend_image)

    def test_disabled_empty_config_returns_false(self):
        r = asyncio.run(autosend_image(_assistant({}), "telegram", "acct", "chat", "hi"))
        assert r is False

    def test_disabled_explicit_returns_false(self):
        cfg = {"image_autosend": {"enabled": False}}
        r = asyncio.run(autosend_image(_assistant(cfg), "telegram", "acct", "chat", "hi"))
        assert r is False

    def test_owns_media_false_returns_false(self, monkeypatch):
        cfg = {"image_autosend": {"enabled": True}}

        class _Orch:
            def owns_media(self, platform, account_id):
                return False

        import src.integrations.account_orchestrator as _ao
        monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
        r = asyncio.run(autosend_image(_assistant(cfg), "telegram", "acct", "chat", "hi"))
        assert r is False


def _assistant_full(cfg=None):
    return SimpleNamespace(
        config=SimpleNamespace(config=cfg or {}),
        logger=SimpleNamespace(debug=lambda *a, **k: None,
                               info=lambda *a, **k: None,
                               warning=lambda *a, **k: None),
        inbox_store=None, _web_loop=None,
    )


def _web_app():
    return SimpleNamespace(state=SimpleNamespace())


class TestBuildAutosendCallbacks:
    def test_deliver_disabled_send_cb_none(self):
        send_cb, _tr = build_autosend_callbacks(_assistant_full(), _web_app(), False)
        assert send_cb is None

    def test_deliver_enabled_send_cb_callable(self):
        send_cb, _tr = build_autosend_callbacks(_assistant_full(), _web_app(), True)
        assert asyncio.iscoroutinefunction(send_cb)

    def test_translate_cb_none_when_disabled(self):
        _s, tr = build_autosend_callbacks(_assistant_full(), _web_app(), False)
        assert tr is None  # 出站翻译默认未启用

    def test_send_cb_falls_through_to_text(self, monkeypatch):
        # image/voice 都返回 False → deliver 落到文本投递(_send_via)。
        # 验证 deliver 主体的捕获链(autosend_image/voice/_send_via/_send_shim/
        # _send_adapters/assistant/_make_coro)运行时不 NameError。
        async def _false(*a, **k):
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_image", _false)
        monkeypatch.setattr(autosend_helpers, "autosend_voice", _false)

        async def _fake_send_via(shim, platform, account_id, chat_key, text, adapters):
            return {"ok": True, "delivered_as": "text", "echo": text}
        import src.inbox.channel_adapters as _ca
        monkeypatch.setattr(_ca, "send_via_adapters", _fake_send_via)

        class _Orch:
            def owns(self, platform, account_id):
                return False
        import src.integrations.account_orchestrator as _ao
        monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

        send_cb, _tr = build_autosend_callbacks(_assistant_full(), _web_app(), True)
        res = asyncio.run(send_cb("telegram", "acct", "chat", "hi"))
        assert res.get("delivered_as") == "text" and res.get("echo") == "hi"
