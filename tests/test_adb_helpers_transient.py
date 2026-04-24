"""adb_helpers.adb_stderr_looks_transient 单测。"""

from __future__ import annotations

from src.integrations.line_rpa import adb_helpers as adb


def test_adb_stderr_looks_transient_positive() -> None:
    assert adb.adb_stderr_looks_transient("error: device offline") is True
    assert adb.adb_stderr_looks_transient("ADB: unauthorized") is True
    assert adb.adb_stderr_looks_transient("no devices/emulators found") is True


def test_adb_stderr_looks_transient_negative() -> None:
    assert adb.adb_stderr_looks_transient("") is False
    assert adb.adb_stderr_looks_transient("permission denied") is False
