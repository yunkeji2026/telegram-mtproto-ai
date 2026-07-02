"""实时语音开闸校准纯函数单测。"""

from __future__ import annotations

from src.companion.realtime_voice_calibration import realtime_voice_calibration


def test_inactive_when_disabled():
    cal = realtime_voice_calibration(
        {"realtime_voice": {"enabled": False, "base_url": "http://127.0.0.1:7860"}},
        ref_summary={"persona_count": 0, "with_reference": 0, "worst_grade": "none"})
    assert cal["verdict"] == "inactive"


def test_warming_when_engine_not_loaded():
    cal = realtime_voice_calibration(
        {"realtime_voice": {"enabled": True, "base_url": "http://127.0.0.1:7860"}},
        ref_summary={"persona_count": 1, "with_reference": 1, "worst_grade": "green"},
        engine_loaded=False, memory_store=True)
    assert cal["verdict"] == "warming"
    assert "启动引擎" in cal["recommendation"]


def test_ready_with_green_ref_and_engine():
    cal = realtime_voice_calibration(
        {"realtime_voice": {"enabled": True, "base_url": "http://127.0.0.1:7860"}},
        ref_summary={"persona_count": 2, "with_reference": 2, "worst_grade": "green"},
        engine_loaded=True, memory_store=True)
    assert cal["verdict"] == "ready"
    assert cal["chain"]["opener_enabled"] is True
    assert cal["trial_url"] == "/ops/voice-call"


def test_trial_builtin_no_reference():
    cal = realtime_voice_calibration(
        {"realtime_voice": {"enabled": True, "base_url": "http://127.0.0.1:7860"}},
        ref_summary={"persona_count": 3, "with_reference": 0, "worst_grade": "none"},
        engine_loaded=True)
    assert cal["verdict"] == "trial_builtin"
    assert any("参考音" in w for w in cal["warnings"])
