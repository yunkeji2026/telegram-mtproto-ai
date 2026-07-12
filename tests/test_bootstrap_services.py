# -*- coding: utf-8 -*-
"""bootstrap.services 抽取回归测试（Stage 3）。

setup_contacts_subsystem 主体 113 行、多子系统装配,完整行为需大量 mock;
此处守护抽取最易回归处:可导入性 + enabled/disabled 两条主路的 self->assistant 改名正确。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.bootstrap.services import (
    setup_contacts_subsystem,
    setup_device_management,
    setup_rpa_services,
    setup_telegram_clients,
)


def _assistant():
    a = MagicMock()
    a.config.config_path = "C:/x/config.yaml"
    return a


def test_contacts_disabled_sets_none():
    a = _assistant()
    with patch("src.contacts.bootstrap_contacts_subsystem", return_value=None):
        setup_contacts_subsystem(a)
    assert a.contacts is None


def test_contacts_enabled_starts_background():
    a = _assistant()
    contacts = MagicMock()
    with patch("src.contacts.bootstrap_contacts_subsystem", return_value=contacts):
        setup_contacts_subsystem(a)
    assert a.contacts is contacts
    contacts.start_background_tasks.assert_called_once()


def test_device_management_all_disabled():
    a = MagicMock()
    a.config.config = {}
    a.device_coordinator_service = None
    setup_device_management(a)  # 三块全跳过,不抛异常


def test_device_coordinator_enabled_builds():
    a = MagicMock()
    a.config.config = {
        "device_coordinator": {"enabled": True},
        "hotplug_watcher": {"enabled": False},
    }
    a.device_coordinator_service = None
    with patch(
        "src.integrations.shared.device_service.DeviceCoordinatorService",
        return_value="COORD",
    ):
        setup_device_management(a)
    assert a.device_coordinator_service == "COORD"


def test_rpa_all_disabled():
    a = MagicMock()
    a.config.get_line_rpa_config.return_value = {}
    a.config.get_messenger_rpa_config.return_value = {}
    a.config.config = {}
    setup_rpa_services(a)  # 三块全跳过,不抛异常


def test_line_rpa_enabled_single_account():
    a = MagicMock()
    a.line_rpa_services = []
    a.config.get_line_rpa_config.return_value = {"enabled": True}
    a.config.get_messenger_rpa_config.return_value = {}
    a.config.config = {}
    with patch(
        "src.integrations.line_rpa.service.LineRpaService",
        return_value="LINE_SVC",
    ):
        setup_rpa_services(a)
    assert a.line_rpa_service == "LINE_SVC"
    assert "LINE_SVC" in a.line_rpa_services


# --- Telegram 专项: 覆盖桌面 smoke 盲区(真实 client 初始化分支) ---

def test_telegram_desktop_skips_client():
    a = MagicMock()
    a.config.config = {}
    with patch("src.bootstrap.env_probe._is_desktop_mode", return_value=True), \
         patch("src.client.telegram_account_registry.TelegramAccountRegistry.from_config",
               return_value=None):
        asyncio.run(setup_telegram_clients(a))
    assert a.telegram_client is None
    assert a.telegram_clients == []


def test_telegram_real_init_single_account():
    a = MagicMock()
    a.config.config = {"telegram": {}}
    tc = MagicMock()
    tc.initialize = AsyncMock()
    with patch("src.bootstrap.env_probe._is_desktop_mode", return_value=False), \
         patch("src.bootstrap.env_probe._telegram_configured", return_value=True), \
         patch("src.client.telegram_client.TelegramClient", return_value=tc), \
         patch("src.client.telegram_account_registry.TelegramAccountRegistry.from_config",
               return_value=None):
        asyncio.run(setup_telegram_clients(a))
    assert a.telegram_client is tc
    assert a.telegram_clients == [tc]
    tc.initialize.assert_awaited_once()
