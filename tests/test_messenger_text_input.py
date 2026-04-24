"""messenger_rpa.text_input 单测。"""
from __future__ import annotations

from src.integrations.line_rpa.adb_helpers import AdbResult
from src.integrations.messenger_rpa import text_input as ti


def test_inject_text_retries_transient_clipboard(monkeypatch) -> None:
    monkeypatch.setattr(ti.adb, "is_adbkeyboard_installed", lambda s, **kw: False)
    calls = {"n": 0}

    def fake_clipboard(serial: str, text: str) -> AdbResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return AdbResult("", "error: device offline\n", 1)
        return AdbResult("", "", 0)

    monkeypatch.setattr(ti.adb, "clipboard_paste", fake_clipboard)
    waits = {"n": 0}

    def fake_run_adb(args, **kwargs):
        if args and args[0] == "wait-for-device":
            waits["n"] += 1
            return AdbResult("", "", 0)
        return AdbResult("", "", 0)

    monkeypatch.setattr(ti.adb, "run_adb", fake_run_adb)

    r = ti.inject_text(
        "SERIAL",
        "你好",
        use_adb_keyboard=True,
        allow_clipboard_fallback=True,
        allow_input_text_fallback_for_ascii=True,
        device_transient_retries=3,
    )
    assert r.ok is True
    assert r.path == "clipboard_paste"
    assert calls["n"] == 2
    assert waits["n"] >= 1
