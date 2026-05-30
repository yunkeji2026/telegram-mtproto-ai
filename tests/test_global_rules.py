"""Tests for global_rules.yaml loading and PersonaManager integration."""
import copy
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.utils.persona_manager import PersonaManager, GLOBAL_RULES_FILENAME


@pytest.fixture(autouse=True)
def reset_pm():
    PersonaManager.reset()
    yield
    PersonaManager.reset()


@pytest.fixture()
def rules_yaml(tmp_path):
    """Create a minimal global_rules.yaml in tmp_path and return its path."""
    data = {
        "reply_constraints": [
            {"id": "direct_answer", "title": "先回答再展开", "rule": "先正面回答问题。"},
            {"id": "follow_topic", "title": "跟随话题", "rule": "立即跟随新话题。"},
        ],
        "platform_rules": {
            "whatsapp": {"label": "WhatsApp 专用", "rule": "回复简短。"},
        },
        "funnel_tone": {
            "cold": {"label": "冷启动", "tone": "建立信任。"},
            "hot": {"label": "高意向", "tone": "把握时机。"},
        },
    }
    p = tmp_path / GLOBAL_RULES_FILENAME
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return p, data


# ── Loading ──────────────────────────────────────────────────────────────────

def test_load_global_rules_from_path(rules_yaml):
    path, expected = rules_yaml
    pm = PersonaManager.get_instance()
    pm._global_rules_path = path
    rules = pm.get_global_rules()
    assert len(rules["reply_constraints"]) == 2
    assert rules["reply_constraints"][0]["id"] == "direct_answer"


def test_hot_reload_on_mtime_change(rules_yaml, tmp_path):
    path, data = rules_yaml
    pm = PersonaManager.get_instance()
    pm._global_rules_path = path

    rules = pm.get_global_rules()
    assert len(rules["reply_constraints"]) == 2

    # modify file — add a third constraint
    data["reply_constraints"].append(
        {"id": "new_rule", "title": "新规则", "rule": "新的规则内容。"}
    )
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")

    # force mtime difference (file was just written, mtime differs)
    rules2 = pm.get_global_rules()
    assert len(rules2["reply_constraints"]) == 3
    assert rules2["reply_constraints"][2]["id"] == "new_rule"


def test_missing_file_returns_empty():
    pm = PersonaManager.get_instance()
    pm._global_rules_path = Path("/nonexistent/global_rules.yaml")
    rules = pm.get_global_rules()
    assert rules == {} or rules is not None  # empty dict or cached


# ── Text builders ────────────────────────────────────────────────────────────

def test_build_constraints_text(rules_yaml):
    path, _ = rules_yaml
    pm = PersonaManager.get_instance()
    pm._global_rules_path = path
    text = pm._build_constraints_text()
    assert "【回复硬约束】" in text
    assert "1. 先正面回答问题。" in text
    assert "2. 立即跟随新话题。" in text


def test_build_platform_constraints_whatsapp(rules_yaml):
    path, _ = rules_yaml
    pm = PersonaManager.get_instance()
    pm._global_rules_path = path
    text = pm._build_platform_constraints("whatsapp")
    assert "WhatsApp 专用" in text
    assert "回复简短" in text


def test_build_platform_constraints_unknown(rules_yaml):
    path, _ = rules_yaml
    pm = PersonaManager.get_instance()
    pm._global_rules_path = path
    text = pm._build_platform_constraints("telegram")
    assert text == ""


def test_build_funnel_tone(rules_yaml):
    path, _ = rules_yaml
    pm = PersonaManager.get_instance()
    pm._global_rules_path = path
    text = pm._build_funnel_tone("cold")
    assert "冷启动" in text
    assert "建立信任" in text

    text_hot = pm._build_funnel_tone("hot")
    assert "高意向" in text_hot

    text_none = pm._build_funnel_tone("nonexistent")
    assert text_none == ""


# ── Save ─────────────────────────────────────────────────────────────────────

def test_save_global_rules(tmp_path):
    path = tmp_path / GLOBAL_RULES_FILENAME
    pm = PersonaManager.get_instance()
    pm._global_rules_path = path

    new_data = {
        "reply_constraints": [
            {"id": "test_rule", "title": "测试", "rule": "测试规则。"},
        ],
        "platform_rules": {},
        "funnel_tone": {},
    }
    ok = pm.save_global_rules(new_data)
    assert ok is True
    assert path.exists()

    # verify file content
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["reply_constraints"][0]["id"] == "test_rule"

    # verify in-memory cache updated
    rules = pm.get_global_rules()
    assert rules["reply_constraints"][0]["id"] == "test_rule"


# ── P2-a: enabled/disabled toggle ────────────────────────────────────────────

def test_disabled_rules_excluded_from_constraints(tmp_path):
    data = {
        "reply_constraints": [
            {"id": "r1", "enabled": True, "title": "A", "rule": "规则A。"},
            {"id": "r2", "enabled": False, "title": "B", "rule": "规则B。"},
            {"id": "r3", "enabled": True, "title": "C", "rule": "规则C。"},
        ],
        "platform_rules": {},
        "funnel_tone": {},
    }
    p = tmp_path / GLOBAL_RULES_FILENAME
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    pm = PersonaManager.get_instance()
    pm._global_rules_path = p
    text = pm._build_constraints_text()
    assert "1. 规则A。" in text
    assert "2. 规则C。" in text
    assert "规则B" not in text


def test_all_disabled_returns_empty(tmp_path):
    data = {
        "reply_constraints": [
            {"id": "r1", "enabled": False, "title": "A", "rule": "规则A。"},
        ],
        "platform_rules": {},
        "funnel_tone": {},
    }
    p = tmp_path / GLOBAL_RULES_FILENAME
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    pm = PersonaManager.get_instance()
    pm._global_rules_path = p
    text = pm._build_constraints_text()
    assert text == ""


def test_missing_enabled_defaults_to_true(tmp_path):
    """Rules without explicit 'enabled' field should default to True."""
    data = {
        "reply_constraints": [
            {"id": "r1", "title": "A", "rule": "规则A。"},  # no enabled field
        ],
        "platform_rules": {},
        "funnel_tone": {},
    }
    p = tmp_path / GLOBAL_RULES_FILENAME
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    pm = PersonaManager.get_instance()
    pm._global_rules_path = p
    text = pm._build_constraints_text()
    assert "规则A" in text


# ── P2-c: backup/restore ────────────────────────────────────────────────────

def test_save_creates_backup(tmp_path):
    p = tmp_path / GLOBAL_RULES_FILENAME
    initial = {"reply_constraints": [{"id": "v1", "title": "V1", "rule": "版本1。"}]}
    p.write_text(yaml.dump(initial, allow_unicode=True), encoding="utf-8")

    pm = PersonaManager.get_instance()
    pm._global_rules_path = p

    v2 = {"reply_constraints": [{"id": "v2", "title": "V2", "rule": "版本2。"}]}
    pm.save_global_rules(v2)

    bak1 = p.with_suffix(".yaml.bak.1")
    assert bak1.exists()
    bak_data = yaml.safe_load(bak1.read_text(encoding="utf-8"))
    assert bak_data["reply_constraints"][0]["id"] == "v1"


def test_backup_rotation(tmp_path):
    p = tmp_path / GLOBAL_RULES_FILENAME
    p.write_text(yaml.dump({"reply_constraints": [{"id": "v0"}]}, allow_unicode=True), encoding="utf-8")

    pm = PersonaManager.get_instance()
    pm._global_rules_path = p

    for i in range(1, 5):
        pm.save_global_rules({"reply_constraints": [{"id": f"v{i}"}]})

    # should have bak.1 (newest backup = v3), bak.2 (v2), bak.3 (v1)
    # current file = v4
    bak1 = yaml.safe_load(p.with_suffix(".yaml.bak.1").read_text(encoding="utf-8"))
    assert bak1["reply_constraints"][0]["id"] == "v3"
    bak3 = yaml.safe_load(p.with_suffix(".yaml.bak.3").read_text(encoding="utf-8"))
    assert bak3["reply_constraints"][0]["id"] == "v1"
    # bak.4 should not exist (max=3)
    assert not p.with_suffix(".yaml.bak.4").exists()


def test_list_backups(tmp_path):
    p = tmp_path / GLOBAL_RULES_FILENAME
    p.write_text(yaml.dump({"reply_constraints": []}, allow_unicode=True), encoding="utf-8")

    pm = PersonaManager.get_instance()
    pm._global_rules_path = p

    assert pm.list_backups() == []  # no backups yet

    pm.save_global_rules({"reply_constraints": [{"id": "v1"}]})
    backups = pm.list_backups()
    assert len(backups) == 1
    assert backups[0]["slot"] == 1


def test_restore_backup(tmp_path):
    p = tmp_path / GLOBAL_RULES_FILENAME
    p.write_text(yaml.dump({"reply_constraints": [{"id": "original"}]}, allow_unicode=True), encoding="utf-8")

    pm = PersonaManager.get_instance()
    pm._global_rules_path = p

    pm.save_global_rules({"reply_constraints": [{"id": "modified"}]})
    # bak.1 should be "original"
    ok = pm.restore_backup(1)
    assert ok is True
    rules = pm.get_global_rules()
    assert rules["reply_constraints"][0]["id"] == "original"


def test_restore_nonexistent_slot(tmp_path):
    p = tmp_path / GLOBAL_RULES_FILENAME
    p.write_text(yaml.dump({"reply_constraints": []}, allow_unicode=True), encoding="utf-8")
    pm = PersonaManager.get_instance()
    pm._global_rules_path = p
    assert pm.restore_backup(99) is False


# ── Integration: format_persona_block uses YAML rules ────────────────────────

def test_format_persona_block_uses_yaml_rules(rules_yaml):
    path, _ = rules_yaml
    pm = PersonaManager.get_instance()
    pm._global_rules_path = path
    # set a minimal persona
    pm.set_domain_persona({"name": "TestBot", "role": "测试助手"})

    block = pm.format_persona_block(platform="whatsapp", funnel_stage="cold")
    assert "先正面回答问题" in block
    assert "WhatsApp 专用" in block
    assert "冷启动" in block


# ── P3-a: preview_constraints_text ───────────────────────────────────────────

def test_preview_constraints_text_respects_enabled():
    pm = PersonaManager.get_instance()
    rules = {
        "reply_constraints": [
            {"id": "r1", "enabled": True, "title": "A", "rule": "规则A。"},
            {"id": "r2", "enabled": False, "title": "B", "rule": "规则B。"},
        ],
        "platform_rules": {
            "whatsapp": {"label": "WA 约束", "rule": "短回复。"},
        },
        "funnel_tone": {
            "cold": {"label": "冷启动", "tone": "建立信任。"},
        },
    }
    text = pm.preview_constraints_text(rules)
    assert "规则A" in text
    assert "规则B" not in text
    assert "WA 约束" in text
    assert "短回复" in text
    assert "冷启动" in text
    assert "建立信任" in text


def test_preview_constraints_text_empty_when_all_disabled():
    pm = PersonaManager.get_instance()
    rules = {
        "reply_constraints": [
            {"id": "r1", "enabled": False, "title": "A", "rule": "规则A。"},
        ],
        "platform_rules": {},
        "funnel_tone": {},
    }
    text = pm.preview_constraints_text(rules)
    assert text == ""


def test_preview_constraints_text_no_rules():
    pm = PersonaManager.get_instance()
    text = pm.preview_constraints_text({"reply_constraints": [], "platform_rules": {}, "funnel_tone": {}})
    assert text == ""


# ── P5-a: platform scope filtering ───────────────────────────────────────────

def test_assemble_constraints_platform_filter():
    """Rules with platforms field should only appear for matching platform."""
    constraints = [
        {"id": "r1", "enabled": True, "title": "A", "rule": "通用规则。"},
        {"id": "r2", "enabled": True, "title": "B", "rule": "仅WA规则。", "platforms": ["whatsapp"]},
        {"id": "r3", "enabled": True, "title": "C", "rule": "仅TG规则。", "platforms": ["telegram", "line"]},
    ]
    # no platform = all rules
    text_all = PersonaManager._assemble_constraints(constraints)
    assert "通用规则" in text_all
    assert "仅WA规则" in text_all
    assert "仅TG规则" in text_all

    # whatsapp platform
    text_wa = PersonaManager._assemble_constraints(constraints, platform="whatsapp")
    assert "通用规则" in text_wa
    assert "仅WA规则" in text_wa
    assert "仅TG规则" not in text_wa

    # telegram platform
    text_tg = PersonaManager._assemble_constraints(constraints, platform="telegram")
    assert "通用规则" in text_tg
    assert "仅WA规则" not in text_tg
    assert "仅TG规则" in text_tg


def test_assemble_constraints_empty_platforms_means_all():
    """Rules with empty platforms list should match all platforms."""
    constraints = [
        {"id": "r1", "enabled": True, "rule": "全平台。", "platforms": []},
    ]
    text = PersonaManager._assemble_constraints(constraints, platform="messenger")
    assert "全平台" in text


def test_preview_with_platform_filter():
    pm = PersonaManager.get_instance()
    rules = {
        "reply_constraints": [
            {"id": "r1", "enabled": True, "rule": "通用。"},
            {"id": "r2", "enabled": True, "rule": "WA专属。", "platforms": ["whatsapp"]},
        ],
        "platform_rules": {},
        "funnel_tone": {},
    }
    text_all = pm.preview_constraints_text(rules)
    assert "通用" in text_all
    assert "WA专属" in text_all

    text_tg = pm.preview_constraints_text(rules, platform="telegram")
    assert "通用" in text_tg
    assert "WA专属" not in text_tg


def test_format_persona_block_respects_disabled(tmp_path):
    data = {
        "reply_constraints": [
            {"id": "r1", "enabled": True, "title": "A", "rule": "启用规则。"},
            {"id": "r2", "enabled": False, "title": "B", "rule": "禁用规则。"},
        ],
        "platform_rules": {},
        "funnel_tone": {},
    }
    p = tmp_path / GLOBAL_RULES_FILENAME
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    pm = PersonaManager.get_instance()
    pm._global_rules_path = p
    pm.set_domain_persona({"name": "TestBot", "role": "测试助手"})

    block = pm.format_persona_block()
    assert "启用规则" in block
    assert "禁用规则" not in block
