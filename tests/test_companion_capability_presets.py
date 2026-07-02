"""一键预设档 + 快照/回滚纯计划层单测。"""

from __future__ import annotations

from src.companion.capability_presets import (
    PRESETS, build_preset_plan, capture_snapshot, snapshot_to_plan,
)


def _find(plan, key, field):
    return next((it for it in plan if it["key"] == key and it["field"] == field), None)


# ── 预设计划 ───────────────────────────────────────────────────────────────

def test_unknown_preset_returns_none():
    assert build_preset_plan("nope") is None


def test_three_presets_exist():
    assert set(PRESETS) == {"safe_default", "dry_run_trial", "full_auto"}


def test_safe_default_kills_outbound_keeps_safeguards():
    plan = build_preset_plan("safe_default")
    assert _find(plan, "l2_autosend_deliver", "enabled")["value"] is False
    assert _find(plan, "voice_autosend", "enabled")["value"] is False
    assert _find(plan, "realtime_voice", "enabled")["value"] is False
    assert _find(plan, "proactive_topic", "enabled")["value"] is False
    # 安全栈开
    assert _find(plan, "persona_guard", "enabled")["value"] is True
    assert _find(plan, "companion_send_gate", "enabled")["value"] is True


def test_dry_run_trial_proactive_is_dry_run():
    plan = build_preset_plan("dry_run_trial")
    assert _find(plan, "proactive_topic", "enabled")["value"] is True
    assert _find(plan, "proactive_topic", "dry_run")["value"] is True
    assert _find(plan, "l2_autosend_worker", "enabled")["value"] is True
    assert _find(plan, "l2_autosend_deliver", "enabled")["value"] is False  # 不真发


def test_full_auto_enables_everything_real():
    plan = build_preset_plan("full_auto")
    assert _find(plan, "l2_autosend_deliver", "enabled")["value"] is True
    assert _find(plan, "proactive_topic", "dry_run")["value"] is False      # 真发
    assert _find(plan, "voice_autosend", "enabled")["value"] is True
    assert _find(plan, "realtime_voice", "enabled")["value"] is False       # 实时通话仍独立 opt-in


def test_full_auto_order_gate_and_worker_before_deliver():
    """send_gate / worker 必须排在真发主开关之前（开 deliver 时已无裸奔 warn）。"""
    plan = build_preset_plan("full_auto")
    keys = [(it["key"], it["field"]) for it in plan]
    i_deliver = keys.index(("l2_autosend_deliver", "enabled"))
    i_gate = keys.index(("companion_send_gate", "enabled"))
    i_worker = keys.index(("l2_autosend_worker", "enabled"))
    assert i_gate < i_deliver and i_worker < i_deliver


# ── 快照 / 回滚 ────────────────────────────────────────────────────────────

def test_capture_snapshot_shape():
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}},
           "companion": {"enabled": True, "proactive_topic": {"enabled": True, "dry_run": True}}}
    snap = capture_snapshot(cfg)
    assert snap["l2_autosend_deliver"]["enabled"] is True
    assert snap["proactive_topic"]["enabled"] is True
    assert snap["proactive_topic"]["dry_run"] is True
    # 无 dry_run 档的能力快照不含 dry_run 键
    assert "dry_run" not in snap["persona_guard"]


def test_snapshot_roundtrip_to_plan():
    cfg = {"inbox": {"l2_autosend": {"deliver": True}},
           "companion": {"proactive_topic": {"enabled": True, "dry_run": True}}}
    snap = capture_snapshot(cfg)
    plan = snapshot_to_plan(snap)
    assert _find(plan, "l2_autosend_deliver", "enabled")["value"] is True
    assert _find(plan, "proactive_topic", "dry_run")["value"] is True


def test_snapshot_to_plan_ignores_unknown_keys():
    plan = snapshot_to_plan({"bogus_cap": {"enabled": True}})
    assert plan == []
