"""autosend_helpers.autosend_voice 抽取回归测试。

重点验证：函数可导入、可调用、正确使用 assistant 捕获（config），
voice 未启用时早退 False（不触发合成/发送）。
"""
import asyncio
from types import SimpleNamespace

import pytest

from src.inbox.autosend_helpers import autosend_image, autosend_voice


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
