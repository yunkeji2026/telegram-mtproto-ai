"""combined_vision._parse_combined：智谱截断 / 未闭合字符串时的 JSON 自愈。"""

from __future__ import annotations

import json

from src.integrations.messenger_rpa.combined_vision import (
    _parse_combined,
    is_outbound_or_draft_preview,
)


def test_parse_combined_valid_minimal() -> None:
    raw = '{"guard":{"type":"none","action":"none","title":"","confidence":"high"},"unread":[],"risk":{"hit":false}}'
    d = _parse_combined(raw)
    assert d is not None
    assert d["guard"]["type"] == "none"
    assert d.get("unread") == []


def test_parse_combined_truncated_preview_string() -> None:
    """模拟日志：preview 值未闭合引号 + JSON 被截断。"""
    raw = (
        '{"guard":{"type":"none","action":"none","title":"","confidence":"high"},'
        '"unread":[{"name":"Shuichi Ito","preview":"hello wor'
    )
    d = _parse_combined(raw)
    assert d is not None
    assert isinstance(d.get("unread"), list)


def test_parse_combined_balance_only() -> None:
    raw = '{"guard":{"type":"none","action":"none","title":"","confidence":"high"},"unread":['
    d = _parse_combined(raw)
    assert d is not None
    assert json.loads(json.dumps(d)).get("guard", {}).get("type") == "none"


def test_parse_combined_prefixed_noise() -> None:
    raw = 'Here is JSON:\n{"guard":{"type":"none","action":"none","title":"","confidence":"high"},"unread":[]}'
    d = _parse_combined(raw)
    assert d is not None
    assert d["unread"] == []


def test_inbox_json_truncated_repair() -> None:
    """inbox_scanner._parse_inbox_json 与 combined 共用 parse_vision_json_loose。"""
    from src.integrations.messenger_rpa.inbox_scanner import _parse_inbox_json

    raw = '{"unread":[{"name":"Test","preview":"x'
    d = _parse_inbox_json(raw)
    assert d is not None
    assert isinstance(d.get("unread"), list)


def test_inbox_json_unread_list_minimal() -> None:
    from src.integrations.messenger_rpa.inbox_scanner import _parse_inbox_json

    raw = json.dumps(
        {
            "unread": [
                {
                    "name": "A",
                    "preview": "hi",
                    "time": "1m",
                    "quality_hint": "friend",
                    "y_percent": 10.0,
                }
            ]
        }
    )
    d = _parse_inbox_json(raw)
    assert d is not None
    assert d["unread"][0].get("name") == "A"


def test_outbound_or_draft_preview_detection() -> None:
    assert is_outbound_or_draft_preview("You: hello")
    assert is_outbound_or_draft_preview("Draft: hello")
    assert is_outbound_or_draft_preview(" Me: hello")
    assert not is_outbound_or_draft_preview("hello from customer")
