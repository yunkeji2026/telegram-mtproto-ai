"""ConfigManager 写入/缓存/热重载/回滚 测试"""

import asyncio
import os
import shutil
import tempfile
import yaml
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.config_manager import ConfigManager


@pytest.fixture
def config_dir(tmp_path):
    """创建临时配置目录，包含最小可用 config.yaml / templates.yaml / exchange_rates.yaml"""
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
    }
    tpl = {"greeting": ["hello", "hi"], "farewell": "goodbye"}
    rates = {
        "channels": {
            "ep": {"display_name": "EP通道", "fee_rate": "0.5%", "status": "正常",
                   "limits": {"default": "100-20000"}, "names": ["EP"]},
        }
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(yaml.dump(tpl, allow_unicode=True), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(yaml.dump(rates, allow_unicode=True), encoding="utf-8")
    return tmp_path


@pytest.fixture
def cm(config_dir):
    mgr = ConfigManager(str(config_dir / "config.yaml"))
    asyncio.run(mgr.load())
    return mgr


class TestSaveTemplates:
    def test_save_and_reload(self, cm, config_dir):
        data = cm.get_dynamic_templates_config()
        assert "greeting" in data
        data["greeting"][0] = "NEW_GREETING"
        ok, msg = cm.save_templates(data)
        assert ok
        cm.invalidate_templates_cache()
        reloaded = cm.get_dynamic_templates_config()
        assert reloaded["greeting"][0] == "NEW_GREETING"

    def test_save_validates_yaml(self, cm):
        ok, msg = cm.save_templates({"key": "value"})
        assert ok

    def test_no_file_returns_error(self, tmp_path):
        mgr = ConfigManager(str(tmp_path / "config.yaml"))
        ok, msg = mgr.save_templates({"a": 1})
        assert not ok
        assert "未找到" in msg


class TestSaveExchangeRates:
    def test_save_and_reload(self, cm):
        data = cm.get_exchange_rates_config()
        data["channels"]["ep"]["fee_rate"] = "1.0%"
        ok, _ = cm.save_exchange_rates(data)
        assert ok
        cm.invalidate_exchange_rates_cache()
        reloaded = cm.get_exchange_rates_config()
        assert reloaded["channels"]["ep"]["fee_rate"] == "1.0%"


class TestHotReload:
    def test_no_change_returns_false(self, cm):
        cm._last_hot_reload_check = 0
        assert cm.check_and_hot_reload() is False

    def test_mtime_change_triggers_reload(self, cm, config_dir):
        import time
        cm._last_hot_reload_check = 0
        cfg_path = config_dir / "config.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        data["skills"]["cooldown_test"] = 999
        cfg_path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
        time.sleep(0.1)
        os.utime(cfg_path, (time.time() + 10, time.time() + 10))
        cm._last_hot_reload_check = 0
        assert cm.check_and_hot_reload() is True
        assert cm.config.get("skills", {}).get("cooldown_test") == 999

    def test_protected_keys_preserved(self, cm, config_dir):
        import time
        cfg_path = config_dir / "config.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        data["telegram"]["api_id"] = "HACKED"
        cfg_path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
        time.sleep(0.1)
        os.utime(cfg_path, (time.time() + 20, time.time() + 20))
        cm._last_hot_reload_check = 0
        cm.check_and_hot_reload()
        assert cm.config["telegram"]["api_id"] == "111"

    def test_on_reload_callback_fires(self, cm, config_dir):
        import time
        fired = []
        cm.on_reload(lambda: fired.append(1))
        cfg_path = config_dir / "config.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        data["skills"]["marker"] = True
        cfg_path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
        time.sleep(0.1)
        os.utime(cfg_path, (time.time() + 30, time.time() + 30))
        cm._last_hot_reload_check = 0
        cm.check_and_hot_reload()
        assert len(fired) == 1
