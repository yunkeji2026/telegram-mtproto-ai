"""能力档 × 信号 联动建议 + 一致性体检单测。"""

from __future__ import annotations

from src.companion.capability_advisor import (
    build_advice, build_recommendations, consistency_issues,
)


def _cap(key, stage, *, kind="feature", enabled=True, dry_run_supported=False,
         label=None, tier=2, recommended=""):
    return {"key": key, "label": label or key, "tier": tier, "stage": stage,
            "kind": kind, "enabled": enabled,
            "dry_run_supported": dry_run_supported, "recommended": recommended}


def _sig(key, verdict, advice="x"):
    return {"key": key, "verdict": verdict, "advice": advice}


# ── 联动建议 ───────────────────────────────────────────────────────────────

def test_dry_run_healthy_suggests_advance_with_target():
    caps = [_cap("proactive_topic", "dry_run", dry_run_supported=True)]
    sigs = {"proactive_topic": _sig("proactive_topic", "healthy", "好评达标")}
    recs = build_recommendations(caps, sigs)
    assert recs[0]["action"] == "advance"
    assert recs[0]["target"] == {"key": "proactive_topic", "field": "dry_run", "value": False}


def test_dry_run_caution_holds():
    caps = [_cap("proactive_topic", "dry_run", dry_run_supported=True)]
    sigs = {"proactive_topic": _sig("proactive_topic", "caution", "好评偏低")}
    recs = build_recommendations(caps, sigs)
    assert recs[0]["action"] == "hold" and "target" not in recs[0]


def test_active_failing_suggests_downgrade():
    # deliver 无 dry_run → 降档=关闭
    caps = [_cap("l2_autosend_deliver", "active", dry_run_supported=False)]
    sigs = {"l2_autosend_deliver": _sig("l2_autosend_deliver", "failing", "失败率高")}
    recs = build_recommendations(caps, sigs)
    assert recs[0]["action"] == "downgrade"
    assert recs[0]["target"] == {"key": "l2_autosend_deliver", "field": "enabled", "value": False}


def test_active_failing_with_dry_run_downgrades_to_dry_run():
    caps = [_cap("proactive_topic", "active", dry_run_supported=True)]
    sigs = {"proactive_topic": _sig("proactive_topic", "failing")}
    recs = build_recommendations(caps, sigs)
    assert recs[0]["target"]["field"] == "dry_run" and recs[0]["target"]["value"] is True


def test_blocked_suggests_fix_first():
    caps = [_cap("proactive_topic", "blocked", recommended="子系统未挂")]
    recs = build_recommendations(caps, {})
    assert recs[0]["action"] == "fix" and "子系统" in recs[0]["reason"]


def test_off_safeguard_suggests_enable():
    caps = [_cap("companion_send_gate", "off", kind="safeguard", enabled=False)]
    recs = build_recommendations(caps, {})
    assert recs[0]["action"] == "enable"
    assert recs[0]["target"]["value"] is True


def test_off_feature_not_nagged():
    caps = [_cap("voice_autosend", "off", enabled=False)]
    assert build_recommendations(caps, {}) == []


def test_recommendations_sorted_by_priority():
    caps = [_cap("proactive_topic", "dry_run", dry_run_supported=True),
            _cap("x_blocked", "blocked"),
            _cap("companion_send_gate", "off", kind="safeguard", enabled=False)]
    sigs = {"proactive_topic": _sig("proactive_topic", "healthy")}
    recs = build_recommendations(caps, sigs)
    assert [r["action"] for r in recs] == ["fix", "enable", "advance"]


# ── 一致性体检 ─────────────────────────────────────────────────────────────

def test_consistency_deliver_without_worker_is_error():
    caps = [_cap("l2_autosend_deliver", "active", enabled=True),
            _cap("l2_autosend_worker", "off", enabled=False),
            _cap("companion_send_gate", "active", kind="safeguard", enabled=True)]
    issues = consistency_issues(caps, auto_ai=3)
    assert any(i["severity"] == "error" and "worker" in i["message"] for i in issues)


def test_consistency_deliver_without_gate_warns():
    caps = [_cap("l2_autosend_deliver", "active", enabled=True),
            _cap("l2_autosend_worker", "active", enabled=True),
            _cap("companion_send_gate", "off", kind="safeguard", enabled=False)]
    issues = consistency_issues(caps, auto_ai=3)
    assert any(i["severity"] == "warn" and "安全闸" in i["message"] for i in issues)


def test_consistency_deliver_no_autoai_error():
    caps = [_cap("l2_autosend_deliver", "active", enabled=True),
            _cap("l2_autosend_worker", "active", enabled=True),
            _cap("companion_send_gate", "active", kind="safeguard", enabled=True)]
    issues = consistency_issues(caps, auto_ai=0)
    assert any("auto_ai" in i["message"] for i in issues)


def test_consistency_voice_without_deliver_warns():
    caps = [_cap("voice_autosend", "active", enabled=True),
            _cap("l2_autosend_deliver", "off", enabled=False)]
    issues = consistency_issues(caps)
    assert any("语音真发" in i["message"] for i in issues)


def test_consistency_blocked_surfaced():
    caps = [_cap("proactive_topic", "blocked", enabled=True, recommended="子系统未挂")]
    issues = consistency_issues(caps)
    assert any(i["keys"] == ["proactive_topic"] for i in issues)


# ── 合并 ───────────────────────────────────────────────────────────────────

def test_build_advice_shape_and_summary():
    status = {"capabilities": [
        _cap("l2_autosend_deliver", "active", enabled=True),
        _cap("l2_autosend_worker", "off", enabled=False),
        _cap("proactive_topic", "dry_run", dry_run_supported=True, enabled=True),
    ]}
    signals = {"signals": [_sig("proactive_topic", "healthy"),
                           _sig("l2_autosend_deliver", "healthy")]}
    out = build_advice(status, signals, auto_ai=0)
    assert "recommendations" in out and "consistency" in out
    assert out["summary"]["errors"] >= 1   # worker off + no auto_ai
    assert out["summary"]["advance"] == 1   # proactive dry_run healthy
