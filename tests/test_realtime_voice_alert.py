"""实时语音退化告警纯函数门禁（B 线）。"""

from src.utils.realtime_voice_alert import (
    DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS,
    calibrate_realtime_voice_alert,
    evaluate_realtime_voice_alert,
    sanitize_realtime_voice_alert_thresholds,
)


def test_insufficient_samples_silent():
    res = evaluate_realtime_voice_alert({"attempts": 1, "health_ok": 1, "health_fail": 0})
    assert res["light"] == "green"
    assert res["problems"] == []


def test_health_fail_red():
    stats = {
        "attempts": 0,
        "health_ok": 1,
        "health_fail": 4,
        "health_ok_rate": 0.2,
    }
    res = evaluate_realtime_voice_alert(stats)
    assert res["light"] == "red"
    assert any(p["id"] == "health_ok_rate_low" and p["status"] == "fail" for p in res["problems"])


def test_connect_warn_and_host_unreachable_fail():
    stats = {
        "attempts": 4,
        "connected": 2,
        "connect_rate": 0.5,
        "health_ok": 10,
        "health_fail": 0,
        "health_ok_rate": 1.0,
        "by_end_reason": {"host_unreachable": 2},
    }
    res = evaluate_realtime_voice_alert(stats)
    ids = {p["id"] for p in res["problems"]}
    assert "host_unreachable_spike" in ids
    assert "connect_rate_low" not in ids  # 50% == warn threshold, not below


def test_connect_rate_fail():
    stats = {
        "attempts": 5,
        "connected": 1,
        "connect_rate": 0.2,
        "health_ok": 5,
        "health_fail": 0,
        "health_ok_rate": 1.0,
        "by_end_reason": {},
    }
    res = evaluate_realtime_voice_alert(stats)
    cr = next(p for p in res["problems"] if p["id"] == "connect_rate_low")
    assert cr["status"] == "fail"
    assert res["light"] == "red"


def test_sanitize_thresholds():
    raw = {
        "min_attempts": "5",
        "health_ok_rate_warn": 1.5,
        "connect_rate_warn": 0.4,
        "bogus": 99,
    }
    out = sanitize_realtime_voice_alert_thresholds(raw)
    assert out["min_attempts"] == 5
    assert out["connect_rate_warn"] == 0.4
    assert "health_ok_rate_warn" not in out
    assert "bogus" not in out


def test_defaults_align_readiness():
    assert DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS["min_attempts"] == 3
    assert DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS["health_ok_rate_warn"] == 0.80


def test_calibrate_current_snapshot():
    stats = {
        "attempts": 5,
        "connected": 1,
        "connect_rate": 0.2,
        "health_ok": 1,
        "health_fail": 4,
        "health_ok_rate": 0.2,
        "by_end_reason": {"host_unreachable": 3},
    }
    out = calibrate_realtime_voice_alert(stats)
    assert out["evaluated_windows"] == 1
    assert out["current_light"] == "red"
    assert out["alerts"] == 1
    assert out["by_signal"]["health_ok_rate_low"] == 1
    assert out["margins"]["connect_rate"]["warn_margin"] < 0


def test_calibrate_daily_replay_dedup():
    daily = [
        {"day": "2026-07-01", "attempts": 5, "connected": 1, "connect_rate": 0.2,
         "health_ok": 5, "health_fail": 0, "health_ok_rate": 1.0, "by_end_reason": {}},
        {"day": "2026-07-02", "attempts": 5, "connected": 1, "connect_rate": 0.2,
         "health_ok": 5, "health_fail": 0, "health_ok_rate": 1.0, "by_end_reason": {}},
    ]
    out = calibrate_realtime_voice_alert({}, daily=daily)
    assert out["evaluated_windows"] == 2
    assert out["alerts"] == 1  # 相同签名去抖
    assert out["days_in_alert"] == 2
