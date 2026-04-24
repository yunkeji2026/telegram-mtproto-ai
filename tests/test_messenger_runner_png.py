"""messenger_rpa.runner 中与截图相关的纯函数单测。"""
from __future__ import annotations

from src.integrations.messenger_rpa.runner import _messenger_png_screencap_ok


def test_messenger_png_screencap_ok_rejects_empty() -> None:
    assert _messenger_png_screencap_ok(b"") is False
    assert _messenger_png_screencap_ok(b"error: device not found\n") is False


def test_messenger_png_screencap_ok_accepts_minimal_png() -> None:
    body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 250
    assert _messenger_png_screencap_ok(body) is True
