# -*- coding: utf-8 -*-
"""Tests for src.integrations.shared.hotplug_watcher."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_registry():
    """Fake registry returning known devices."""
    reg = MagicMock()
    reg.get.return_value = {
        "serial": "TESTSERIAL123456",
        "label": "TST",
        "platform_messenger": "msg_tst",
        "platform_line": "line_tst",
        "platform_whatsapp": "",
    }
    return reg


@pytest.fixture
def mock_config_manager():
    cm = MagicMock()
    cm.config = {
        "line_rpa": {"enabled": True},
        "messenger_rpa": {"enabled": True},
    }
    cm.config_path = "config/config.yaml"
    return cm


@pytest.fixture
def mock_skill_manager():
    return MagicMock()


class TestHotPlugWatcherInit:
    def test_import(self):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher
        assert HotPlugWatcher is not None

    def test_construct(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher
        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
            static_serials={"EXISTING123"},
        )
        assert "EXISTING123" in w._static_serials
        assert w._coordinators == {}

    def test_construct_with_host_name(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher
        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
            host_name="\u4e3b\u63a7",
        )
        assert w.host_name == "\u4e3b\u63a7"

    def test_status_before_start(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher
        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
            host_name="W03",
        )
        st = w.status()
        assert st["running"] is False
        assert st["managed_devices"] == []
        assert st["host_name"] == "W03"
        assert st["unregistered_online"] == []


class TestHotPlugWatcherScanCycle:
    @pytest.mark.asyncio
    async def test_new_device_detected(self, mock_config_manager, mock_skill_manager, mock_registry):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher, _STABLE_THRESHOLD

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
        )
        w._registry = mock_registry

        fake_rows = [("TESTSERIAL123456", "device")]

        with patch("src.integrations.shared.hotplug_watcher.list_adb_device_rows", return_value=fake_rows):
            with patch.object(w, "_build_coordinator", return_value=None) as mock_build:
                # First scan: not stable yet
                await w._scan_cycle()
                assert w._seen_count["TESTSERIAL123456"] == 1
                mock_build.assert_not_called()

                # Second scan: reaches threshold
                await w._scan_cycle()
                assert w._seen_count["TESTSERIAL123456"] == _STABLE_THRESHOLD
                mock_build.assert_called_once()

    @pytest.mark.asyncio
    async def test_static_serial_ignored(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
            static_serials={"STATIC_DEVICE_123"},
        )

        fake_rows = [("STATIC_DEVICE_123", "device")]

        with patch("src.integrations.shared.hotplug_watcher.list_adb_device_rows", return_value=fake_rows):
            await w._scan_cycle()
            # Should not be tracked at all
            assert "STATIC_DEVICE_123" not in w._seen_count
            assert "STATIC_DEVICE_123" not in w._coordinators

    @pytest.mark.asyncio
    async def test_unregistered_device_ignored(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher, _STABLE_THRESHOLD

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
        )
        # Registry returns None for unknown serial
        w._registry = MagicMock()
        w._registry.get.return_value = None

        fake_rows = [("UNKNOWN_SERIAL_X", "device")]

        with patch("src.integrations.shared.hotplug_watcher.list_adb_device_rows", return_value=fake_rows):
            for _ in range(_STABLE_THRESHOLD):
                await w._scan_cycle()
            # Not added to coordinators
            assert "UNKNOWN_SERIAL_X" not in w._coordinators
            # But tracked as unregistered online
            assert "UNKNOWN_SERIAL_X" in w._unregistered_online

    @pytest.mark.asyncio
    async def test_host_name_filtering(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher, _STABLE_THRESHOLD

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
            host_name="W03",
        )
        # Device belongs to different group
        w._registry = MagicMock()
        w._registry.get.return_value = {
            "serial": "DEV_OTHER_GROUP",
            "label": "TST",
            "group_name": "\u4e3b\u63a7",
            "platform_messenger": "msg_tst",
            "platform_line": "",
            "platform_whatsapp": "",
        }

        fake_rows = [("DEV_OTHER_GROUP", "device")]

        with patch("src.integrations.shared.hotplug_watcher.list_adb_device_rows", return_value=fake_rows):
            with patch.object(w, "_build_coordinator", return_value=None) as mock_build:
                for _ in range(_STABLE_THRESHOLD + 1):
                    await w._scan_cycle()
                # Should not build coordinator for device from different group
                mock_build.assert_not_called()

    @pytest.mark.asyncio
    async def test_offline_detection(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
        )
        # Simulate a device already being managed
        mock_coord = MagicMock()

        async def _noop():
            pass

        mock_coord.stop = MagicMock(return_value=_noop())
        w._coordinators["GONE_DEVICE_123"] = mock_coord

        # Scan with empty device list
        with patch("src.integrations.shared.hotplug_watcher.list_adb_device_rows", return_value=[]):
            await w._scan_cycle()
            # First scan: just marks offline_since
            assert "GONE_DEVICE_123" in w._offline_since
            assert "GONE_DEVICE_123" in w._coordinators  # still there

    @pytest.mark.asyncio
    async def test_extract_platforms(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
        )
        dev_info = {
            "platform_messenger": "msg_abc",
            "platform_line": "line_abc",
            "platform_whatsapp": "",
        }
        platforms = w._extract_platforms(dev_info)
        assert len(platforms) == 2
        assert platforms[0] == {"type": "messenger", "account_id": "msg_abc"}
        assert platforms[1] == {"type": "line", "account_id": "line_abc"}


class TestHotPlugWatcherReload:
    @pytest.mark.asyncio
    async def test_reload_device_creates_coordinator(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
        )
        w._registry = MagicMock()
        w._registry.get.return_value = {
            "serial": "RELOAD_TEST_A",
            "label": "RTA",
            "group_name": "",
            "platform_messenger": "msg_rta",
            "platform_line": "",
            "platform_whatsapp": "wa_rta",
        }

        mock_coord = MagicMock()
        mock_coord.status.return_value = {"serial": "RELOAD_TEST_A", "label": "HP-RTA"}

        with patch.object(w, "_build_coordinator", return_value=mock_coord) as mock_build:
            result = await w.reload_device("RELOAD_TEST_A")
            assert result["ok"] is True
            assert result["action"] == "created"
            assert "messenger" in result["platforms"]
            assert "whatsapp" in result["platforms"]
            mock_build.assert_called_once()
            assert "RELOAD_TEST_A" in w._coordinators

    @pytest.mark.asyncio
    async def test_reload_device_rebuilds_existing(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
        )
        # Pre-existing coordinator
        old_coord = MagicMock()

        async def _noop():
            pass

        old_coord.stop = MagicMock(return_value=_noop())
        w._coordinators["REBUILD_SER"] = old_coord

        w._registry = MagicMock()
        w._registry.get.return_value = {
            "serial": "REBUILD_SER",
            "label": "RB",
            "group_name": "",
            "platform_messenger": "",
            "platform_line": "line_rb",
            "platform_whatsapp": "",
        }

        new_coord = MagicMock()
        with patch.object(w, "_build_coordinator", return_value=new_coord):
            result = await w.reload_device("REBUILD_SER")
            assert result["ok"] is True
            assert result["action"] == "rebuilt"
            assert "REBUILD_SER" in w._coordinators
            assert w._coordinators["REBUILD_SER"] is new_coord
            old_coord.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_static_device_rejected(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
            static_serials={"STATIC_123"},
        )
        result = await w.reload_device("STATIC_123")
        assert result["ok"] is False
        assert "statically managed" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_reload_no_platforms_removes_coordinator(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
        )
        # Pre-existing coordinator
        old_coord = MagicMock()

        async def _noop():
            pass

        old_coord.stop = MagicMock(return_value=_noop())
        w._coordinators["NOPLAT_SER"] = old_coord

        w._registry = MagicMock()
        w._registry.get.return_value = {
            "serial": "NOPLAT_SER",
            "label": "NP",
            "group_name": "",
            "platform_messenger": "",
            "platform_line": "",
            "platform_whatsapp": "",
        }

        result = await w.reload_device("NOPLAT_SER")
        assert result["ok"] is True
        assert result["action"] == "no_platforms"
        assert "NOPLAT_SER" not in w._coordinators


class TestHotPlugWatcherLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self, mock_config_manager, mock_skill_manager):
        from src.integrations.shared.hotplug_watcher import HotPlugWatcher

        w = HotPlugWatcher(
            config_manager=mock_config_manager,
            skill_manager=mock_skill_manager,
            scan_interval_sec=0.1,
        )

        with patch("src.integrations.shared.hotplug_watcher.list_adb_device_rows", return_value=[]):
            await w.start()
            assert w.status()["running"] is True
            await asyncio.sleep(0.15)
            await w.stop()
            assert w.status()["running"] is False
