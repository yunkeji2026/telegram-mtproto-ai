"""陪伴能力就绪度聚合单测（纯函数，零 IO）。"""

from __future__ import annotations

from src.companion.capability_status import (
    CAPABILITIES, collect_capability_status, evaluate_capability,
)
from src.companion.capability_status import _dig


def _cap(key):
    return next(c for c in CAPABILITIES if c.key == key)


def _status(report, key):
    return next(c for c in report["capabilities"] if c["key"] == key)


# ── _dig ─────────────────────────────────────────────────────────────────

def test_dig_nested_and_missing():
    cfg = {"a": {"b": {"c": 1}}}
    assert _dig(cfg, "a.b.c") == 1
    assert _dig(cfg, "a.b.x", "d") == "d"
    assert _dig(cfg, "a.x.c", "d") == "d"
    assert _dig({}, "a.b", None) is None
    assert _dig({"a": 5}, "a.b", "d") == "d"   # 中途非 dict


# ── 空配置：全 feature off，safeguard off ───────────────────────────────────

def test_empty_config_all_off():
    rep = collect_capability_status({})
    assert rep["summary"]["total"] == len(CAPABILITIES)
    assert rep["summary"]["by_stage"]["off"] == len(CAPABILITIES)
    assert rep["summary"]["master_delivery_on"] is False
    # 提示层 safeguard + send-gate 都应在 safeguards_off 里
    for k in ("persona_guard", "empathy_strategy", "wellbeing", "companion_send_gate"):
        assert k in rep["summary"]["safeguards_off"]
        assert "建议开启" in _status(rep, k)["recommended"]


# ── tier0 安全栈开启 → active ───────────────────────────────────────────────

def test_tier0_safeguards_active():
    cfg = {"companion": {"enabled": True, "persona_guard": {"enabled": True},
                         "empathy_strategy": {"enabled": True},
                         "wellbeing": {"enabled": True}}}
    rep = collect_capability_status(cfg)
    for k in ("persona_guard", "empathy_strategy", "wellbeing"):
        s = _status(rep, k)
        assert s["stage"] == "active" and s["recommended"] == "运行中"


# ── 父开关 companion.enabled=false → companion.* blocked ─────────────────────

def test_parent_off_blocks_companion_caps():
    cfg = {"companion": {"enabled": False, "persona_guard": {"enabled": True}}}
    rep = collect_capability_status(cfg)
    s = _status(rep, "persona_guard")
    assert s["stage"] == "blocked"
    assert "父开关" in s["recommended"]
    # 非 companion.* 不受影响（companion_send_gate 顶层）
    assert _status(rep, "companion_send_gate")["stage"] == "off"


# ── dry_run / runtime-blocked / active 三态（主动话题）──────────────────────

def test_proactive_topic_dry_run():
    cfg = {"companion": {"enabled": True,
                         "proactive_topic": {"enabled": True, "dry_run": True}}}
    rep = collect_capability_status(cfg, runtime={"companion_proactive_preview": True})
    s = _status(rep, "proactive_topic")
    assert s["stage"] == "dry_run"
    assert "灰度" in s["recommended"]
    assert "proactive_topic" in rep["summary"]["dry_running"]


def test_proactive_topic_runtime_blocked():
    cfg = {"companion": {"enabled": True,
                         "proactive_topic": {"enabled": True, "dry_run": False}}}
    rep = collect_capability_status(cfg, runtime={"companion_proactive_preview": False})
    s = _status(rep, "proactive_topic")
    assert s["stage"] == "blocked"
    assert "未挂载" in s["recommended"]
    assert {"key": "proactive_topic", "why": s["recommended"]} in rep["summary"]["blocked"]


def test_proactive_topic_active():
    cfg = {"companion": {"enabled": True,
                         "proactive_topic": {"enabled": True, "dry_run": False}}}
    rep = collect_capability_status(cfg, runtime={"companion_proactive_preview": True})
    assert _status(rep, "proactive_topic")["stage"] == "active"


# ── runtime=None：不因运行时判 blocked，precondition ok=None ─────────────────

def test_runtime_unknown_not_blocked():
    cfg = {"companion": {"enabled": True,
                         "proactive_topic": {"enabled": True, "dry_run": False}}}
    rep = collect_capability_status(cfg)            # 不传 runtime
    s = _status(rep, "proactive_topic")
    assert s["stage"] == "active"                   # 未知不拦
    rt_pc = next(p for p in s["preconditions"] if "子系统" in p["name"])
    assert rt_pc["ok"] is None


# ── 主开关：全自动真发 ─────────────────────────────────────────────────────

def test_master_delivery_switch():
    s_off = _status(collect_capability_status({}), "l2_autosend_deliver")
    assert s_off["critical"] is True and s_off["risk"] == "high"
    assert "高风险" in s_off["recommended"]

    cfg = {"inbox": {"l2_autosend": {"deliver": True}}}
    rep = collect_capability_status(cfg, runtime={"autosend_worker": True})
    assert _status(rep, "l2_autosend_deliver")["stage"] == "active"
    assert rep["summary"]["master_delivery_on"] is True


# ── 阶梯视图：按 tier 分组 + 点亮计数 ───────────────────────────────────────

def test_ladder_structure_and_lit_count():
    cfg = {"companion": {"enabled": True, "persona_guard": {"enabled": True},
                         "empathy_strategy": {"enabled": True},
                         "wellbeing": {"enabled": True}}}
    rep = collect_capability_status(cfg)
    tier0 = next(t for t in rep["ladder"] if t["tier"] == 0)
    assert tier0["lit"] == tier0["total"] and tier0["complete"] is True
    tier2 = next(t for t in rep["ladder"] if t["tier"] == 2)
    assert tier2["complete"] is False
    # 阶梯按 tier 升序（分阶段开启顺序）
    tiers = [t["tier"] for t in rep["ladder"]]
    assert tiers == sorted(tiers)


# ── evaluate_capability 直测：safeguard off 文案 ────────────────────────────

def test_evaluate_safeguard_off_recommend():
    out = evaluate_capability(_cap("companion_send_gate"), {}, None)
    assert out["stage"] == "off" and out["kind"] == "safeguard"
    assert "安全闸" in out["recommended"]


def test_calibration_deeplinks():
    rep = collect_capability_status({})
    # 主开关带专属校准深链
    assert _status(rep, "l2_autosend_deliver")["calibration"] == \
        "/api/companion/capabilities/delivery-calibration"
    # 主动触达复用 proactive preview
    assert _status(rep, "proactive_topic")["calibration"] == \
        "/api/companion/proactive/preview"
    assert _status(rep, "realtime_voice")["calibration"] == \
        "/api/companion/capabilities/realtime-voice-calibration"
    # 无专属校准的能力为空串
    assert _status(rep, "persona_guard")["calibration"] == ""
