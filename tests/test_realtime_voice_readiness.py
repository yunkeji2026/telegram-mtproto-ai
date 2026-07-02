"""实时语音配置就绪度单测（开闸 preflight）。"""

from __future__ import annotations

from src.companion.realtime_voice_readiness import (
    check_realtime_voice_readiness,
    realtime_voice_host_configured,
)


def test_host_configured_when_base_url_set():
    assert realtime_voice_host_configured({"realtime_voice": {"base_url": "http://127.0.0.1:7860"}})


def test_host_not_configured_when_base_url_empty():
    assert not realtime_voice_host_configured({"realtime_voice": {"base_url": "  "}})


def test_enabled_without_host_is_error():
    r = check_realtime_voice_readiness({"realtime_voice": {"enabled": True, "base_url": ""}})
    assert r["severity"] == "error" and not r["ready"]


def test_enabled_with_host_ready():
    r = check_realtime_voice_readiness(
        {"realtime_voice": {"enabled": True, "base_url": "http://127.0.0.1:7860"}})
    assert r["ready"] is True and r["warn_public"] is True  # 无 access_token


def test_disabled_reports_ok():
    r = check_realtime_voice_readiness({"realtime_voice": {"enabled": False}})
    assert r["severity"] == "ok" and not r["ready"]
