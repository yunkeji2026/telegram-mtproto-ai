"""陪伴能力「分阶段开启」开闸护栏 + overlay 写入单测。"""

from __future__ import annotations

import yaml

from src.companion.capability_toggle import check_toggle
from src.utils.config_manager import ConfigManager


# ── 护栏：拒绝路径 ─────────────────────────────────────────────────────────

def test_unknown_key_rejected():
    r = check_toggle({}, {}, "no_such_cap", "enabled", True)
    assert r["allowed"] is False and "未知能力" in r["reason"]


def test_invalid_field_rejected():
    r = check_toggle({}, {}, "persona_guard", "bogus", True)
    assert r["allowed"] is False and "未知字段" in r["reason"]


def test_dry_run_unsupported_rejected():
    # persona_guard 无 dry_run 档
    r = check_toggle({"companion": {"enabled": True}}, {}, "persona_guard", "dry_run", True)
    assert r["allowed"] is False and "无此开关档" in r["reason"]


def test_enable_child_blocked_when_parent_off():
    # companion.enabled 关 → 开 proactive_topic 被拒
    r = check_toggle({"companion": {"enabled": False}}, {}, "proactive_topic", "enabled", True)
    assert r["allowed"] is False and "父总开关" in r["reason"]


# ── 护栏：主开关「全自动真发」双重 opt-in ──────────────────────────────────

def test_master_deliver_blocked_no_worker():
    cfg = {"inbox": {"l2_autosend": {"enabled": False, "deliver": False}}}
    r = check_toggle(cfg, {"c1": "auto_ai"}, "l2_autosend_deliver", "enabled", True)
    assert r["allowed"] is False and "worker" in r["reason"]


def test_master_deliver_blocked_no_autoai():
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": False}}}
    r = check_toggle(cfg, {"c1": "review"}, "l2_autosend_deliver", "enabled", True)
    assert r["allowed"] is False and "auto_ai" in r["reason"]


def test_master_deliver_allowed_with_warn_when_gate_off():
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": False}}}
    r = check_toggle(cfg, {"c1": "auto_ai"}, "l2_autosend_deliver", "enabled", True)
    assert r["allowed"] is True and r["warn"] is True
    assert "安全闸" in r["reason"]
    assert r["flag_path"] == "inbox.l2_autosend.deliver"


def test_master_deliver_allowed_clean_when_gate_on():
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": False}},
           "companion_send_gate": {"enabled": True}}
    r = check_toggle(cfg, {"c1": "auto_ai"}, "l2_autosend_deliver", "enabled", True)
    assert r["allowed"] is True and r["warn"] is False


# ── 护栏：关闭方向永远放行（关安全防护带 warn）────────────────────────────

def test_disable_safeguard_allowed_with_warn():
    r = check_toggle({}, {}, "companion_send_gate", "enabled", False)
    assert r["allowed"] is True and r["warn"] is True and "护栏" in r["reason"]


def test_disable_deliver_always_allowed():
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}}}
    r = check_toggle(cfg, {}, "l2_autosend_deliver", "enabled", False)
    assert r["allowed"] is True and r["warn"] is False


def test_enable_tier0_safeguard_when_parent_on():
    r = check_toggle({"companion": {"enabled": True}}, {}, "persona_guard", "enabled", True)
    assert r["allowed"] is True and r["warn"] is False


# ── overlay 写入：set_overlay_flag 落 config.local.yaml 且即时生效 ──────────

def test_set_overlay_flag_writes_and_merges(tmp_path):
    cm = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    cm.config = {"companion": {"enabled": True}}

    ok, _ = cm.set_overlay_flag("companion.proactive_topic.enabled", True)
    assert ok is True
    # 即时生效（深合并进 self.config）
    assert cm.config["companion"]["proactive_topic"]["enabled"] is True
    # 落盘 overlay
    overlay_file = tmp_path / "config.local.yaml"
    assert overlay_file.exists()
    data = yaml.safe_load(overlay_file.read_text(encoding="utf-8"))
    assert data["companion"]["proactive_topic"]["enabled"] is True

    # 第二次写不同键 → 合并保留首个
    cm.set_overlay_flag("inbox.l2_autosend.deliver", True)
    data2 = yaml.safe_load(overlay_file.read_text(encoding="utf-8"))
    assert data2["companion"]["proactive_topic"]["enabled"] is True
    assert data2["inbox"]["l2_autosend"]["deliver"] is True


def test_set_overlay_flag_empty_path_rejected(tmp_path):
    cm = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    ok, msg = cm.set_overlay_flag("", True)
    assert ok is False and "空" in msg
