# -*- coding: utf-8 -*-
"""bootstrap.services 抽取回归测试（Stage 3）。

setup_contacts_subsystem 主体 113 行、多子系统装配,完整行为需大量 mock;
此处守护抽取最易回归处:可导入性 + enabled/disabled 两条主路的 self->assistant 改名正确。"""
from unittest.mock import MagicMock, patch

from src.bootstrap.services import (
    setup_contacts_subsystem,
    setup_device_management,
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
