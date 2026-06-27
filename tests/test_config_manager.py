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


class TestEnvConfigPathOverride:
    """打包/自包含部署：AITR_CONFIG_PATH / AITR_DATA_DIR 环境覆盖 + 自播种。"""

    def test_aitr_config_path_overrides_default(self, tmp_path, monkeypatch):
        target = tmp_path / "writable" / "config" / "config.yaml"
        target.parent.mkdir(parents=True)
        target.write_text(yaml.dump({
            "telegram": {"api_id": "1", "api_hash": "a", "phone_number": "+1"},
            "ai": {"api_key": "k"}, "skills": {"enabled": []},
        }, allow_unicode=True), encoding="utf-8")
        monkeypatch.setenv("AITR_CONFIG_PATH", str(target))
        mgr = ConfigManager()  # 无参 → 走默认解析 → 命中 env
        assert mgr.config_path == target

    def test_aitr_data_dir_derives_config(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
        monkeypatch.setenv("AITR_DATA_DIR", str(data_dir))
        mgr = ConfigManager()
        assert mgr.config_path == data_dir / "config" / "config.yaml"

    def test_seeds_from_bundled_example_when_missing(self, tmp_path, monkeypatch):
        # 内置 example 一定存在于仓库 config/，自播种应把它拷到可写目标
        target = tmp_path / "writable" / "config" / "config.yaml"
        assert not target.exists()
        monkeypatch.setenv("AITR_CONFIG_PATH", str(target))
        mgr = ConfigManager()
        assert mgr.config_path == target
        assert target.exists(), "自播种应已创建 config.yaml"
        # 内容来自 example（含 telegram/ai 等关键节）
        seeded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        assert isinstance(seeded, dict) and seeded, "播种内容应为非空 dict"

    def test_existing_target_not_overwritten(self, tmp_path, monkeypatch):
        target = tmp_path / "writable" / "config" / "config.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("telegram: {api_id: KEEP}\n", encoding="utf-8")
        monkeypatch.setenv("AITR_CONFIG_PATH", str(target))
        ConfigManager()
        assert "KEEP" in target.read_text(encoding="utf-8"), "已存在的 config 不应被播种覆盖"

    def test_no_env_uses_repo_default(self, monkeypatch):
        monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
        monkeypatch.delenv("AITR_DATA_DIR", raising=False)
        mgr = ConfigManager()
        # 仓库默认路径（开发态行为不变）
        assert mgr.config_path.name in ("config.yaml", "config.example.yaml")
        assert "config" in mgr.config_path.parts


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


class TestWebAdminEnvOverride:
    """AITR_WEB_* / AITR_DESKTOP_MODE 覆盖 web_admin（桌面壳 serve↔talk 强一致）。"""

    def _make(self, tmp_path, web_admin=None):
        cfg = {
            "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
            "ai": {"api_key": "test"},
            "skills": {"enabled": []},
        }
        if web_admin is not None:
            cfg["web_admin"] = web_admin
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        return p

    def test_env_overrides_host_port_token(self, tmp_path, monkeypatch):
        p = self._make(tmp_path, {"enabled": True, "host": "0.0.0.0",
                                  "port": 18787, "auth_token": "OLD"})
        monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
        monkeypatch.setenv("AITR_WEB_HOST", "127.0.0.1")
        monkeypatch.setenv("AITR_WEB_PORT", "18799")
        monkeypatch.setenv("AITR_WEB_TOKEN", "admin")
        mgr = ConfigManager(str(p))
        asyncio.run(mgr.load())
        web = mgr.config["web_admin"]
        assert web["host"] == "127.0.0.1"
        assert web["port"] == 18799 and isinstance(web["port"], int)
        assert web["auth_token"] == "admin"

    def test_invalid_port_ignored(self, tmp_path, monkeypatch):
        p = self._make(tmp_path, {"enabled": True, "port": 18787})
        monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
        monkeypatch.delenv("AITR_WEB_HOST", raising=False)
        monkeypatch.delenv("AITR_WEB_TOKEN", raising=False)
        monkeypatch.setenv("AITR_WEB_PORT", "not-a-number")
        mgr = ConfigManager(str(p))
        asyncio.run(mgr.load())
        assert mgr.config["web_admin"]["port"] == 18787

    def test_desktop_mode_forces_web_enabled(self, tmp_path, monkeypatch):
        # web_admin.enabled 缺省/为假，桌面模式必须强制开启（否则桌面壳全无路由）
        p = self._make(tmp_path, {"enabled": False})
        for k in ("AITR_WEB_HOST", "AITR_WEB_PORT", "AITR_WEB_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
        mgr = ConfigManager(str(p))
        asyncio.run(mgr.load())
        assert mgr.config["web_admin"]["enabled"] is True

    def test_desktop_mode_creates_web_admin_when_missing(self, tmp_path, monkeypatch):
        p = self._make(tmp_path, None)
        for k in ("AITR_WEB_HOST", "AITR_WEB_PORT", "AITR_WEB_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
        mgr = ConfigManager(str(p))
        asyncio.run(mgr.load())
        assert mgr.config.get("web_admin", {}).get("enabled") is True

    def test_no_env_leaves_web_admin_untouched(self, tmp_path, monkeypatch):
        p = self._make(tmp_path, {"enabled": True, "port": 18787, "auth_token": "KEEP"})
        for k in ("AITR_DESKTOP_MODE", "AITR_WEB_HOST", "AITR_WEB_PORT", "AITR_WEB_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        mgr = ConfigManager(str(p))
        asyncio.run(mgr.load())
        web = mgr.config["web_admin"]
        assert web["port"] == 18787 and web["auth_token"] == "KEEP"
