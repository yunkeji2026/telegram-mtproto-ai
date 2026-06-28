"""全自动真发开闸前校准单测（纯函数）。"""

from __future__ import annotations

from src.companion.delivery_calibration import (
    delivery_calibration, summarize_automation_modes,
)


# ── 档位分布 ───────────────────────────────────────────────────────────────

def test_summarize_modes_counts():
    modes = {"c1": "auto_ai", "c2": "auto_ai", "c3": "review",
             "c4": "manual", "c5": "weird"}
    d = summarize_automation_modes(modes)
    assert d["auto_ai"] == 2
    assert d["total_with_setting"] == 5
    assert d["by_mode"]["auto_ai"] == 2 and d["by_mode"]["review"] == 1
    assert d["by_mode"]["other"] == 1          # 未知档归 other


def test_summarize_modes_empty():
    d = summarize_automation_modes(None)
    assert d["auto_ai"] == 0 and d["total_with_setting"] == 0


# ── verdict: inactive（主开关关，安全默认）─────────────────────────────────

def test_verdict_inactive_no_autoai():
    rep = delivery_calibration({}, {})
    assert rep["verdict"] == "inactive"
    assert rep["will_send_now"] is False
    assert rep["switches"] == {"worker": False, "deliver": False, "send_gate": False}
    assert "灰度" in rep["recommendation"]


def test_verdict_inactive_with_autoai_ready():
    # deliver 关，但已有 auto_ai 会话 → 提示"可灰度开"
    rep = delivery_calibration({}, {"c1": "auto_ai"})
    assert rep["verdict"] == "inactive"
    assert "1 个 auto_ai" in rep["recommendation"]


# ── verdict: misconfigured（deliver 开但发不出）──────────────────────────────

def test_verdict_misconfigured_deliver_on_no_autoai():
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}}}
    rep = delivery_calibration(cfg, {"c1": "review"})
    assert rep["verdict"] == "misconfigured"
    assert rep["will_send_now"] is False
    assert any("无 auto_ai 会话" in w for w in rep["warnings"])


def test_verdict_misconfigured_worker_off():
    cfg = {"inbox": {"l2_autosend": {"enabled": False, "deliver": True}}}
    rep = delivery_calibration(cfg, {"c1": "auto_ai"})
    assert rep["verdict"] == "misconfigured"
    assert any("worker 未启用" in w for w in rep["warnings"])


# ── verdict: effective（真发生效）+ send-gate 裸奔告警 ──────────────────────

def test_verdict_effective_but_gate_off():
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}}}
    rep = delivery_calibration(cfg, {"c1": "auto_ai", "c2": "auto_ai"})
    assert rep["verdict"] == "effective"
    assert rep["will_send_now"] is True
    assert any("安全闸" in w for w in rep["warnings"])
    assert "安全闸未开" in rep["recommendation"]


def test_verdict_effective_with_gate_on():
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}},
           "companion_send_gate": {"enabled": True}}
    rep = delivery_calibration(cfg, {"c1": "auto_ai"})
    assert rep["verdict"] == "effective"
    assert rep["switches"]["send_gate"] is True
    assert not any("安全闸" in w for w in rep["warnings"])


# ── 近期审计计数透传 ───────────────────────────────────────────────────────

def test_recent_counts_passthrough():
    rep = delivery_calibration({}, {}, recent_autosend=12, recent_autosend_failed=3)
    assert rep["recent"] == {"autosend": 12, "autosend_failed": 3}
    rep2 = delivery_calibration({}, {})
    assert rep2["recent"] == {"autosend": None, "autosend_failed": None}
