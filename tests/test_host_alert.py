"""host_alert 主机告警工具单测（不真正弹窗：HOST_ALERT_SILENT 静默）。"""
import os

import pytest

from src.utils import host_alert


@pytest.fixture(autouse=True)
def _silent(monkeypatch):
    monkeypatch.setenv("HOST_ALERT_SILENT", "1")
    # 每个用例清空去抖状态，避免相互污染
    host_alert._last_alert.clear()
    yield
    host_alert._last_alert.clear()


class TestLooksLikeKeyFailure:
    def test_status_code_401_403(self):
        class E(Exception):
            status_code = 401
        assert host_alert.looks_like_key_failure(E()) is True

        class E2(Exception):
            status_code = 403
        assert host_alert.looks_like_key_failure(E2()) is True

    def test_response_status_code(self):
        class Resp:
            status_code = 401

        class E(Exception):
            response = Resp()
        assert host_alert.looks_like_key_failure(E()) is True

    @pytest.mark.parametrize("msg", [
        "Error: Unauthorized",
        "invalid api key provided",
        "Incorrect API key",
        "insufficient quota",
        "余额不足，请充值",
        "API key 已失效",
        "HTTP 403 Forbidden",
    ])
    def test_message_markers(self, msg):
        assert host_alert.looks_like_key_failure(Exception(msg)) is True

    @pytest.mark.parametrize("msg", [
        "timeout",
        "connection reset",
        "500 internal server error",
        "model not found",
    ])
    def test_non_key_failures(self, msg):
        assert host_alert.looks_like_key_failure(Exception(msg)) is False


class TestNotifyHost:
    def test_first_alert_fires(self):
        assert host_alert.notify_host("t", "m", key="k1") is True

    def test_debounce_within_cooldown(self):
        assert host_alert.notify_host("t", "m", key="k2", cooldown_sec=1800) is True
        # 冷却窗内第二次应被抑制
        assert host_alert.notify_host("t", "m", key="k2", cooldown_sec=1800) is False

    def test_different_keys_independent(self):
        assert host_alert.notify_host("t", "m", key="a") is True
        assert host_alert.notify_host("t", "m", key="b") is True

    def test_cooldown_zero_allows_repeat(self):
        assert host_alert.notify_host("t", "m", key="k3", cooldown_sec=0) is True
        assert host_alert.notify_host("t", "m", key="k3", cooldown_sec=0) is True

    def test_never_raises_on_bad_input(self):
        # 不抛异常即通过
        host_alert.notify_host(None, None, key=None)  # type: ignore

    def test_notify_key_failure_debounced_by_provider(self):
        assert host_alert.notify_key_failure("dashscope", "401") is True
        assert host_alert.notify_key_failure("dashscope", "401") is False
        assert host_alert.notify_key_failure("zhipu", "403") is True
