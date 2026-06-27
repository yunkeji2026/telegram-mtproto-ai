"""桌面/自包含可启动门控单测。

覆盖 main.py 的 _telegram_configured / _is_desktop_mode：
打包态用 config.example.yaml 自播种（telegram 为占位）时，必须判定为「未配置」→
后端跳过协议客户端初始化，「纯收件箱/网页翻译」形态也能开机。
"""
import os

import pytest

import main


class TestTelegramConfigured:
    def test_placeholder_example_is_not_configured(self):
        # config.example.yaml 的占位值
        cfg = {
            "api_id": "YOUR_API_ID",
            "api_hash": "YOUR_API_HASH",
            "phone_number": "+8612345678900",
        }
        assert main._telegram_configured(cfg) is False

    def test_missing_fields_not_configured(self):
        assert main._telegram_configured({"api_id": "123"}) is False
        assert main._telegram_configured({}) is False
        assert main._telegram_configured(None) is False

    def test_blank_fields_not_configured(self):
        cfg = {"api_id": "  ", "api_hash": "", "phone_number": "+86138"}
        assert main._telegram_configured(cfg) is False

    def test_real_creds_configured(self):
        cfg = {
            "api_id": "1234567",
            "api_hash": "abcdef1234567890abcdef1234567890",
            "phone_number": "+8613800000000",
        }
        assert main._telegram_configured(cfg) is True


class TestDesktopMode:
    def test_env_flag_enables(self, monkeypatch):
        monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
        assert main._is_desktop_mode({}) is True

    @pytest.mark.parametrize("val", ["true", "YES", "On", "1"])
    def test_env_truthy_variants(self, monkeypatch, val):
        monkeypatch.setenv("AITR_DESKTOP_MODE", val)
        assert main._is_desktop_mode({}) is True

    def test_env_absent_and_config_off(self, monkeypatch):
        monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
        assert main._is_desktop_mode({}) is False
        assert main._is_desktop_mode({"app": {"desktop_mode": False}}) is False

    def test_config_flag_enables(self, monkeypatch):
        monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
        assert main._is_desktop_mode({"app": {"desktop_mode": True}}) is True

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
        assert main._is_desktop_mode({"app": {"desktop_mode": False}}) is True
