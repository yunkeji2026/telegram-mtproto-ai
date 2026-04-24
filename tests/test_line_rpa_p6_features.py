"""P6: vision 结构化读屏 + 列表 vision 扫描 + ime 告警 辅助函数测试。"""

from __future__ import annotations

import json

from src.integrations.line_rpa.chat_list_scanner import parse_unread_rows_vision
from src.integrations.line_rpa.runner import _parse_vision_msg, _vision_msg_to_peer_text


def test_parse_vision_msg_json() -> None:
    raw = json.dumps({
        "role": "peer",
        "kind": "sticker",
        "content": "",
        "desc": "棕熊欢呼",
    })
    v = _parse_vision_msg(raw)
    assert v["role"] == "peer"
    assert v["kind"] == "sticker"
    assert v["desc"] == "棕熊欢呼"


def test_vision_msg_to_peer_text_sticker() -> None:
    peer = _vision_msg_to_peer_text({
        "role": "peer",
        "kind": "sticker",
        "content": "",
        "desc": "棕熊撒彩纸",
    })
    assert peer == "[LINE贴图] 棕熊撒彩纸"


def test_vision_msg_to_peer_text_none_on_self() -> None:
    assert _vision_msg_to_peer_text({"role": "self", "kind": "text", "content": "hi", "desc": ""}) is None


def test_parse_unread_rows_vision_no_api_key() -> None:
    rows, dbg = parse_unread_rows_vision(
        b"\x89PNG\r\n\x1a\n",  # invalid png but not reached if no key
        vision_cfg={},
        global_vision_cfg={},
    )
    assert rows == []
    assert "no_api_key" in dbg
