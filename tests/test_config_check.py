"""P0-1 配置自检测试：规则驱动校验器 + 报告渲染 + 退出码语义。"""

from pathlib import Path

import pytest

from src.utils.config_check import (
    Issue,
    check_config,
    format_report,
    has_errors,
)


def _ok_base() -> dict:
    """一份「足够干净」的最小配置，单独跑应无 error。"""
    return {
        "ai": {
            "provider": "openai_compatible",
            "api_key": "sk-real-key",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "max_tokens": 1024,
            "timeout": 30,
            "temperature": 0.7,
        },
    }


def _paths(issues):
    return {i.path for i in issues}


def _by_path(issues, path):
    return [i for i in issues if i.path == path]


# ── AI 段 ────────────────────────────────────────────────────────────────

def test_clean_config_has_no_errors():
    issues = check_config(_ok_base())
    assert not has_errors(issues), [str(i) for i in issues if i.severity == "error"]


def test_ai_provider_deepseek_footgun_is_error():
    cfg = _ok_base()
    cfg["ai"]["provider"] = "deepseek"
    issues = check_config(cfg)
    hit = _by_path(issues, "ai.provider")
    assert hit and hit[0].severity == "error"
    assert "gemini" in hit[0].message


def test_openai_compatible_requires_base_url():
    cfg = _ok_base()
    cfg["ai"]["base_url"] = ""
    issues = check_config(cfg)
    hit = _by_path(issues, "ai.base_url")
    assert hit and hit[0].severity == "error"


def test_ai_api_key_placeholder_is_warn_not_error():
    cfg = _ok_base()
    cfg["ai"]["api_key"] = "YOUR_API_KEY"
    issues = check_config(cfg)
    hit = _by_path(issues, "ai.api_key")
    assert hit and hit[0].severity == "warn"
    assert not has_errors(issues)


def test_missing_ai_section_is_error():
    issues = check_config({})
    assert any(i.path == "ai" and i.severity == "error" for i in issues)


def test_ai_max_tokens_non_positive_warns():
    cfg = _ok_base()
    cfg["ai"]["max_tokens"] = 0
    issues = check_config(cfg)
    assert _by_path(issues, "ai.max_tokens")


def test_ai_temperature_out_of_range_warns():
    cfg = _ok_base()
    cfg["ai"]["temperature"] = 5
    issues = check_config(cfg)
    assert _by_path(issues, "ai.temperature")


# ── 渠道 enabled 缺必填 ────────────────────────────────────────────────────

def test_line_enabled_missing_secret_errors():
    cfg = _ok_base()
    cfg["line"] = {"enabled": True, "channel_secret": "", "channel_access_token": ""}
    issues = check_config(cfg)
    assert _by_path(issues, "line.channel_secret")[0].severity == "error"
    assert _by_path(issues, "line.channel_access_token")[0].severity == "error"


def test_line_disabled_skips_checks():
    cfg = _ok_base()
    cfg["line"] = {"enabled": False, "channel_secret": ""}
    issues = check_config(cfg)
    assert "line.channel_secret" not in _paths(issues)


def test_web_chat_handoff_requires_contacts():
    cfg = _ok_base()
    cfg["web_chat"] = {"enabled": True, "token_secret": "x",
                       "handoff": {"enabled": True}}
    cfg["contacts"] = {"enabled": False}
    issues = check_config(cfg)
    hit = _by_path(issues, "web_chat.handoff.enabled")
    assert hit and hit[0].severity == "error"


def test_web_chat_handoff_ok_when_contacts_enabled():
    cfg = _ok_base()
    cfg["web_chat"] = {"enabled": True, "token_secret": "x",
                       "handoff": {"enabled": True}}
    cfg["contacts"] = {"enabled": True}
    issues = check_config(cfg)
    assert "web_chat.handoff.enabled" not in _paths(issues)


def test_messenger_rpa_enabled_without_account_warns():
    cfg = _ok_base()
    cfg["messenger_rpa"] = {"enabled": True, "adb_serial": "", "accounts": []}
    issues = check_config(cfg)
    assert _by_path(issues, "messenger_rpa")[0].severity == "warn"


def test_whatsapp_rpa_enabled_without_accounts_warns():
    cfg = _ok_base()
    cfg["whatsapp_rpa"] = {"enabled": True, "accounts": []}
    issues = check_config(cfg)
    assert _by_path(issues, "whatsapp_rpa.accounts")


def test_webhook_enabled_without_urls_warns():
    cfg = _ok_base()
    cfg["webhook"] = {"enabled": True, "webhooks": []}
    issues = check_config(cfg)
    assert _by_path(issues, "webhook.webhooks")


# ── 翻译引擎 / 数值一致性 ──────────────────────────────────────────────────

def test_translation_deepl_in_order_without_key_warns():
    cfg = _ok_base()
    cfg["translation"] = {"engines": {"order": ["deepl", "ai"], "deepl": {"api_key": ""}}}
    issues = check_config(cfg)
    assert _by_path(issues, "translation.engines.deepl.api_key")


def test_inbox_sla_warn_ge_crit_warns():
    cfg = _ok_base()
    cfg["inbox"] = {"sla_warn_sec": 7200, "sla_crit_sec": 1800}
    issues = check_config(cfg)
    assert _by_path(issues, "inbox.sla_crit_sec")


def test_workspace_auto_claim_without_auto_assign_warns():
    cfg = _ok_base()
    cfg["workspace"] = {"auto_assign": {"enabled": False,
                                        "auto_claim": {"enabled": True}}}
    issues = check_config(cfg)
    assert _by_path(issues, "workspace.auto_assign.auto_claim.enabled")


# ── N 线 协议登录 / 编排器 / 统一运行时 ──────────────────────────────────────

def _tg_creds():
    return {"api_id": 123456, "api_hash": "abcdef0123456789",
            "phone_number": "+10000000000"}


def test_protocol_login_absent_no_warn():
    issues = check_config(_ok_base())
    assert "platform_login.telegram.companion_runtime" not in _paths(issues)
    assert "platform_login.telegram.protocol_enabled" not in _paths(issues)


def test_companion_runtime_without_protocol_warns():
    cfg = _ok_base()
    cfg["telegram"] = _tg_creds()
    cfg["platform_login"] = {"orchestrator_enabled": True,
                             "telegram": {"protocol_enabled": False,
                                          "companion_runtime": True}}
    issues = check_config(cfg)
    hit = _by_path(issues, "platform_login.telegram.companion_runtime")
    assert hit and any("protocol_enabled" in i.message for i in hit)


def test_companion_runtime_without_orchestrator_warns():
    cfg = _ok_base()
    cfg["telegram"] = _tg_creds()
    cfg["platform_login"] = {"orchestrator_enabled": False,
                             "telegram": {"protocol_enabled": True,
                                          "companion_runtime": True}}
    issues = check_config(cfg)
    hit = _by_path(issues, "platform_login.telegram.companion_runtime")
    assert hit and any("orchestrator_enabled" in i.message for i in hit)


def test_protocol_enabled_without_creds_warns():
    cfg = _ok_base()  # 无 telegram 凭证
    cfg["platform_login"] = {"telegram": {"protocol_enabled": True}}
    issues = check_config(cfg)
    assert _by_path(issues, "platform_login.telegram.protocol_enabled")


def test_companion_fully_wired_no_protocol_login_warn():
    cfg = _ok_base()
    cfg["telegram"] = _tg_creds()
    cfg["platform_login"] = {"orchestrator_enabled": True,
                             "telegram": {"protocol_enabled": True,
                                          "companion_runtime": True}}
    issues = check_config(cfg)
    assert "platform_login.telegram.companion_runtime" not in _paths(issues)
    assert "platform_login.telegram.protocol_enabled" not in _paths(issues)


def test_send_gate_start_above_target_warns():
    cfg = _ok_base()
    cfg["companion_send_gate"] = {"enabled": True, "warmup_start_cap": 20,
                                  "target_cap": 15}
    issues = check_config(cfg)
    assert _by_path(issues, "companion_send_gate.warmup_start_cap")


def test_send_gate_disabled_skips_checks():
    cfg = _ok_base()
    cfg["companion_send_gate"] = {"enabled": False, "warmup_start_cap": 20,
                                  "target_cap": 15}
    issues = check_config(cfg)
    assert "companion_send_gate.warmup_start_cap" not in _paths(issues)


# ── 跨字段 / 文件存在性 ────────────────────────────────────────────────────

def test_contacts_missing_script_file_warns(tmp_path):
    cfg = _ok_base()
    cfg["contacts"] = {"enabled": True, "scripts_path": "nope.yaml",
                       "compliance_path": "also_nope.yaml"}
    fake_config = tmp_path / "config.yaml"
    issues = check_config(cfg, config_path=fake_config)
    assert _by_path(issues, "contacts.scripts_path")


def test_contacts_existing_script_file_ok(tmp_path):
    (tmp_path / "scripts.yaml").write_text("x: 1", encoding="utf-8")
    (tmp_path / "comp.yaml").write_text("x: 1", encoding="utf-8")
    cfg = _ok_base()
    cfg["contacts"] = {"enabled": True, "scripts_path": "scripts.yaml",
                       "compliance_path": "comp.yaml"}
    issues = check_config(cfg, config_path=tmp_path / "config.yaml")
    assert "contacts.scripts_path" not in _paths(issues)


# ── 报告渲染 / 退出码 ──────────────────────────────────────────────────────

def test_non_dict_config_is_error():
    issues = check_config("not a dict")  # type: ignore[arg-type]
    assert has_errors(issues)


def test_format_report_lists_enabled_subsystems():
    cfg = _ok_base()
    cfg["line"] = {"enabled": True, "channel_secret": "s", "channel_access_token": "t"}
    issues = check_config(cfg)
    report = format_report(issues, config=cfg)
    assert "已启用子系统" in report
    assert "line" in report


def test_format_report_clean_says_no_problem():
    report = format_report(check_config(_ok_base()), config=_ok_base())
    assert "未发现问题" in report or "汇总" in report


def test_issues_sorted_errors_first():
    cfg = _ok_base()
    cfg["ai"]["provider"] = "deepseek"          # error
    cfg["ai"]["api_key"] = "YOUR_API_KEY"       # warn
    issues = check_config(cfg)
    severities = [i.severity for i in issues]
    assert severities == sorted(
        severities, key=lambda s: {"error": 0, "warn": 1, "info": 2}[s])


def test_telegram_phone_only_not_treated_as_configured():
    """只剩 example 假手机号、凭证仍占位 → 视为「未配置 TG」(info)，不报 error。"""
    cfg = _ok_base()
    cfg["telegram"] = {"api_id": "YOUR_API_ID", "api_hash": "YOUR_API_HASH",
                       "phone_number": "+8612345678900"}
    issues = check_config(cfg)
    assert not has_errors(issues)
    assert any(i.path == "telegram" and i.severity == "info" for i in issues)


def test_telegram_partial_creds_errors():
    """填了 api_id 但 api_hash 仍占位 → 视为打算用 TG，报缺失。"""
    cfg = _ok_base()
    cfg["telegram"] = {"api_id": "1234567", "api_hash": "YOUR_API_HASH",
                       "phone_number": "+8612345678900"}
    issues = check_config(cfg)
    assert _by_path(issues, "telegram.api_hash")[0].severity == "error"


def test_example_config_check_runs_without_crash():
    """对仓库真实 config.example.yaml 跑自检不应崩（占位符会产出 warn/info）。"""
    import yaml
    root = Path(__file__).resolve().parent.parent
    example = root / "config" / "config.example.yaml"
    if not example.exists():
        pytest.skip("config.example.yaml 不存在")
    with open(example, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    issues = check_config(cfg, config_path=example)
    assert isinstance(issues, list)
    assert all(isinstance(i, Issue) for i in issues)
