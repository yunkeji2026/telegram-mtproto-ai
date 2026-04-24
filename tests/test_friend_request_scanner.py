"""friend_request_scanner 单元测试（mock vision callable）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.integrations.line_rpa.friend_request_scanner import (
    parse_friend_requests,
    scan_friend_requests,
    FriendRequest,
)


def _run(coro):
    """环境里 pytest-asyncio 不可用，用 asyncio.run 跑协程。"""
    return asyncio.run(coro)


class TestParse:
    def test_plain_json_array(self):
        txt = '[{"display_name":"小明","hint_text":"你好"},{"display_name":"Alice","hint_text":""}]'
        out = parse_friend_requests(txt)
        assert len(out) == 2
        assert out[0].display_name == "小明"
        assert out[0].hint_text == "你好"
        assert out[1].hint_text == ""

    def test_markdown_wrapped(self):
        txt = '```json\n[{"display_name":"Alice","hint_text":""}]\n```'
        out = parse_friend_requests(txt)
        assert len(out) == 1
        assert out[0].display_name == "Alice"

    def test_leading_text_then_array(self):
        txt = '以下是识别结果：[{"display_name":"Alice","hint_text":"hi"}]'
        out = parse_friend_requests(txt)
        assert len(out) == 1
        assert out[0].display_name == "Alice"

    def test_empty_array(self):
        assert parse_friend_requests("[]") == []

    def test_empty_string(self):
        assert parse_friend_requests("") == []

    def test_not_json_at_all(self):
        assert parse_friend_requests("这个页面不是好友申请") == []

    def test_invalid_entry_dropped(self):
        txt = '[{"display_name":"","hint_text":"x"},{"display_name":"Valid","hint_text":""}]'
        out = parse_friend_requests(txt)
        assert len(out) == 1
        assert out[0].display_name == "Valid"

    def test_placeholder_names_rejected(self):
        txt = '[{"display_name":"未知用户","hint_text":""},{"display_name":"unknown"},{"display_name":"Zoe"}]'
        out = parse_friend_requests(txt)
        names = [r.display_name for r in out]
        assert names == ["Zoe"]

    def test_non_string_hint_coerced(self):
        txt = '[{"display_name":"X","hint_text":123}]'
        out = parse_friend_requests(txt)
        assert out[0].hint_text == "123"

    def test_non_dict_entries_skipped(self):
        txt = '["just a string", {"display_name":"Y","hint_text":""}, null]'
        out = parse_friend_requests(txt)
        assert len(out) == 1
        assert out[0].display_name == "Y"


class TestScan:
    def test_scan_happy(self):
        async def fake_vision(image_path, prompt=None):
            assert "LINE" in prompt
            return '[{"display_name":"Alice","hint_text":"hi"}]'

        out = _run(scan_friend_requests("dummy.png", fake_vision))
        assert len(out) == 1
        assert out[0].display_name == "Alice"

    def test_scan_vision_returns_none(self):
        async def fake_vision(image_path, prompt=None):
            return None

        out = _run(scan_friend_requests("dummy.png", fake_vision))
        assert out == []

    def test_scan_vision_raises(self):
        async def fake_vision(image_path, prompt=None):
            raise RuntimeError("network down")

        # 不应抛，返回 []
        out = _run(scan_friend_requests("dummy.png", fake_vision))
        assert out == []

    def test_prompt_override(self):
        captured = {}

        async def fake_vision(image_path, prompt=None):
            captured["prompt"] = prompt
            return "[]"

        _run(scan_friend_requests("dummy.png", fake_vision,
                                   prompt_override="custom prompt text"))
        assert captured["prompt"] == "custom prompt text"
