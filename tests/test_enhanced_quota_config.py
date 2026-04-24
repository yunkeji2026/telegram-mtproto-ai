"""EnhancedQuotaConfigSkill 模板/通道/审计/回滚 测试"""

import asyncio
import os
import shutil
import tempfile
import yaml
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.config_manager import ConfigManager
from src.skills.skill_manager import EnhancedQuotaConfigSkill


@pytest.fixture
def config_dir(tmp_path):
    cfg = {
        "telegram": {
            "api_id": "111", "api_hash": "abc", "phone_number": "+1",
            "quota_config_commands": {"enabled": True, "allowed_user_ids": [12345]},
        },
        "ai": {"api_key": "test"},
        "skills": {"enabled": [], "cooldown": {"global": 5, "per_content": 30}},
    }
    tpl = {"greeting": ["hello", "hi"], "farewell": "goodbye"}
    rates = {
        "channels": {
            "ep": {"display_name": "EP通道", "fee_rate": "0.5%", "status": "正常",
                   "limits": {"default": "100-20000"}, "names": ["EP"]},
            "jc": {"display_name": "JC通道", "fee_rate": "0.8%", "status": "正常",
                   "limits": {"default": "200-50000"}, "names": ["JC"]},
        }
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(yaml.dump(tpl, allow_unicode=True), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(yaml.dump(rates, allow_unicode=True), encoding="utf-8")
    return tmp_path


@pytest.fixture
def skill(config_dir):
    cm = ConfigManager(str(config_dir / "config.yaml"))
    asyncio.get_event_loop().run_until_complete(cm.load())
    ai = MagicMock()
    return EnhancedQuotaConfigSkill(cm, ai)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestPermission:
    def test_disabled_returns_none(self, skill):
        skill.config.config["telegram"]["quota_config_commands"]["enabled"] = False
        result = run(skill.execute("列出话术", "12345", {}))
        assert result is None

    def test_no_permission_returns_none(self, skill):
        result = run(skill.execute("列出话术", "99999", {}))
        assert result is None


class TestTemplateManagement:
    def test_list_templates(self, skill):
        result = run(skill.execute("列出话术", "12345", {}))
        assert "greeting" in result
        assert "farewell" in result

    def test_view_template(self, skill):
        result = run(skill.execute("查看话术 greeting", "12345", {}))
        assert "hello" in result
        assert "hi" in result

    def test_update_template(self, skill):
        result = run(skill.execute("更新话术 greeting 新的问候语内容", "12345", {}))
        assert "已更新" in result

    def test_add_template(self, skill):
        result = run(skill.execute("添加话术 greeting 第三条问候", "12345", {}))
        assert "已添加" in result

    def test_delete_template(self, skill):
        result = run(skill.execute("删除话术 greeting 2", "12345", {}))
        assert "已删除" in result

    def test_nonexistent_template(self, skill):
        result = run(skill.execute("查看话术 nonexist", "12345", {}))
        assert "不存在" in result


class TestChannelManagement:
    def test_list_channels(self, skill):
        result = run(skill.execute("列出通道", "12345", {}))
        assert "EP通道" in result
        assert "JC通道" in result

    def test_view_channel(self, skill):
        result = run(skill.execute("查看通道 ep", "12345", {}))
        assert "EP通道" in result
        assert "0.5%" in result

    def test_update_rate(self, skill):
        result = run(skill.execute("更新费率 ep 1.0%", "12345", {}))
        assert "已更新" in result
        assert "1.0%" in result

    def test_update_status(self, skill):
        result = run(skill.execute("启用通道 ep", "12345", {}))
        assert "正常" in result

    def test_disable_channel(self, skill):
        result = run(skill.execute("禁用通道 jc", "12345", {}))
        assert "暂停" in result

    def test_natural_language_rate(self, skill):
        result = run(skill.execute("jc费率改成2.5%", "12345", {}))
        assert "已更新" in result or "JC通道" in result


class TestConfigView:
    def test_summary(self, skill):
        result = run(skill.execute("查看配置", "12345", {}))
        assert "话术模板" in result
        assert "通道" in result


class TestAuditAndRollback:
    def test_audit_log_persists(self, skill, config_dir):
        run(skill.execute("更新费率 ep 9.9%", "12345", {}))
        db_file = config_dir / "audit.db"
        assert db_file.exists()
        entry = skill._audit.last_entry()
        assert entry is not None
        assert entry["action"] == "update_rate"
        assert entry["new_val"] == "9.9%"

    def test_snapshot_created(self, skill, config_dir):
        run(skill.execute("更新费率 ep 8.8%", "12345", {}))
        snaps = list((config_dir / "snapshots").glob("exchange_rates_*.yaml"))
        assert len(snaps) >= 1

    def test_show_audit_log(self, skill):
        run(skill.execute("更新费率 ep 7.7%", "12345", {}))
        result = run(skill.execute("查看操作记录", "12345", {}))
        assert "update_rate" in result

    def test_list_snapshots(self, skill):
        run(skill.execute("更新话术 greeting 测试内容xx", "12345", {}))
        result = run(skill.execute("列出快照", "12345", {}))
        assert "templates_" in result

    def test_undo_last(self, skill, config_dir):
        run(skill.execute("更新费率 ep 6.6%", "12345", {}))
        result = run(skill.execute("撤销上次", "12345", {}))
        assert "已回滚" in result
        data = yaml.safe_load((config_dir / "exchange_rates.yaml").read_text(encoding="utf-8"))
        assert data["channels"]["ep"]["fee_rate"] != "6.6%"


class TestBatchModification:
    def test_batch_set_preview(self, skill):
        ctx = {"user_context": {}}
        result = run(skill.execute("所有通道费率统一改成3.0%", "12345", ctx))
        assert "批量修改预览" in result
        assert "3.0%" in result
        assert "batch_pending" in ctx["user_context"]

    def test_batch_confirm(self, skill, config_dir):
        ctx = {"user_context": {}}
        run(skill.execute("所有通道费率统一改成3.0%", "12345", ctx))
        result = run(skill.execute("确认批量修改", "12345", ctx))
        assert "已批量更新" in result
        data = yaml.safe_load((config_dir / "exchange_rates.yaml").read_text(encoding="utf-8"))
        assert data["channels"]["ep"]["fee_rate"] == "3.0%"
        assert data["channels"]["jc"]["fee_rate"] == "3.0%"

    def test_batch_delta_preview(self, skill):
        ctx = {"user_context": {}}
        result = run(skill.execute("所有通道费率降低0.1%", "12345", ctx))
        assert "批量修改预览" in result
        assert "batch_pending" in ctx["user_context"]

    def test_batch_no_confirm_clears(self, skill):
        ctx = {"user_context": {}}
        run(skill.execute("所有通道费率统一改成9.9%", "12345", ctx))
        assert "batch_pending" in ctx["user_context"]
        result = run(skill.execute("列出通道", "12345", ctx))
        assert "batch_pending" in ctx["user_context"]
