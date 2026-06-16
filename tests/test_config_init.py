"""P0-2 场景预设脚手架测试：预设质量守卫 + scaffold 逻辑 + 覆盖解析。"""

from pathlib import Path

import pytest
import yaml

from src.utils.config_check import check_config, has_errors
from src.utils.config_init import (
    apply_overrides,
    describe_preset,
    list_presets,
    load_preset,
    parse_set_args,
    scaffold_config,
)

# 填好这两类凭证后，任何预设都应 check 通过（0 错误）
_FILL = {
    "ai.api_key": "sk-real-key",
    "telegram.api_id": "1234567",
    "telegram.api_hash": "abcdef1234567890abcdef1234567890",
}


def _repo_presets_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "presets"


def test_presets_exist():
    names = list_presets()
    assert names, "config/presets/ 下应至少有一个预设"
    assert {"ecommerce", "payment", "outreach"} <= set(names)


@pytest.mark.parametrize("name", list_presets() or ["__none__"])
def test_each_preset_is_valid_yaml_with_domain(name):
    if name == "__none__":
        pytest.skip("无预设")
    data = load_preset(name)
    assert "domain" in data, f"{name} 缺 domain"
    assert "ai" in data and "skills" in data, f"{name} 缺 ai/skills"


@pytest.mark.parametrize("name", list_presets() or ["__none__"])
def test_each_preset_clean_after_filling_credentials(name):
    """质量守卫：填好 AI+TG 凭证后，预设跑 check_config 必须 0 错误。"""
    if name == "__none__":
        pytest.skip("无预设")
    data = load_preset(name)
    apply_overrides(data, dict(_FILL))
    issues = check_config(data, config_path=_repo_presets_dir() / f"{name}.yaml")
    errs = [str(i) for i in issues if i.severity == "error"]
    assert not errs, f"{name} 填凭证后仍有错误: {errs}"


@pytest.mark.parametrize("name", list_presets() or ["__none__"])
def test_each_preset_domain_exists(name):
    """质量守卫：预设引用的 domain 必须是 domains/ 下真实存在的域包。"""
    if name == "__none__":
        pytest.skip("无预设")
    data = load_preset(name)
    domain = data.get("domain")
    domains_dir = Path(__file__).resolve().parent.parent / "domains" / str(domain)
    assert domains_dir.is_dir(), f"{name} 引用了不存在的 domain: {domain}"


def test_describe_preset_returns_scenario_line():
    desc = describe_preset("ecommerce")
    assert "场景预设" in desc


# ── apply_overrides / parse_set_args ──────────────────────────────────────

def test_apply_overrides_nested_creates_path():
    cfg = {}
    apply_overrides(cfg, {"ai.api_key": "x", "a.b.c": 1})
    assert cfg["ai"]["api_key"] == "x"
    assert cfg["a"]["b"]["c"] == 1


def test_apply_overrides_overwrites_non_dict():
    cfg = {"ai": "scalar"}
    apply_overrides(cfg, {"ai.key": "v"})
    assert cfg["ai"]["key"] == "v"


def test_parse_set_args():
    assert parse_set_args(["ai.api_key=sk-1", "x.y=z", "bad"]) == {
        "ai.api_key": "sk-1", "x.y": "z"}
    assert parse_set_args(None) == {}


# ── scaffold_config ───────────────────────────────────────────────────────

def test_scaffold_writes_file_and_runs_check(tmp_path):
    dest = tmp_path / "config.yaml"
    ok, msg, issues = scaffold_config(
        "ecommerce", dest, overrides=dict(_FILL))
    assert ok and dest.exists(), msg
    loaded = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert loaded["ai"]["api_key"] == "sk-real-key"
    assert not has_errors(issues)


def test_scaffold_refuses_existing_without_force(tmp_path):
    dest = tmp_path / "config.yaml"
    dest.write_text("existing: 1", encoding="utf-8")
    ok, msg, issues = scaffold_config("payment", dest)
    assert not ok
    assert "已存在" in msg
    assert dest.read_text(encoding="utf-8") == "existing: 1"


def test_scaffold_force_overwrites(tmp_path):
    dest = tmp_path / "config.yaml"
    dest.write_text("existing: 1", encoding="utf-8")
    ok, _, _ = scaffold_config("payment", dest, overrides=dict(_FILL), force=True)
    assert ok
    loaded = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert loaded["domain"] == "payment"


def test_scaffold_unknown_preset_returns_error(tmp_path):
    ok, msg, issues = scaffold_config("nope", tmp_path / "config.yaml")
    assert not ok and not issues
    assert "不存在" in msg


def test_scaffold_outreach_has_contacts_enabled(tmp_path):
    dest = tmp_path / "config.yaml"
    scaffold_config("outreach", dest, overrides=dict(_FILL))
    loaded = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert loaded["contacts"]["enabled"] is True
    assert loaded["web_chat"]["handoff"]["enabled"] is True
