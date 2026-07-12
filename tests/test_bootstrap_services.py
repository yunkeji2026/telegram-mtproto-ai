# -*- coding: utf-8 -*-
"""bootstrap.services 抽取回归测试（Stage 3）。

setup_contacts_subsystem 主体 113 行、多子系统装配,完整行为需大量 mock;
此处守护抽取最易回归处:可导入性 + enabled/disabled 两条主路的 self->assistant 改名正确。"""
from unittest.mock import MagicMock, patch

from src.bootstrap.services import setup_contacts_subsystem


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
