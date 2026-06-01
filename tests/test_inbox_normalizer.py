"""src/inbox/normalizer.py 纯函数单测（Phase A3）。"""

from __future__ import annotations

from src.inbox.normalizer import (
    SEND_MODES,
    candidate_messages_from_source,
    conv_id,
    message_obj,
    normalize_chat,
)


def test_conv_id_format():
    assert conv_id("line", "acc1", "u123") == "line:acc1:u123"


def test_message_obj_basic_fields():
    m = message_obj(text="hello", ts=100, direction="in", message_id="m1")
    assert m["text"] == "hello"
    assert m["original_text"] == "hello"
    assert m["direction"] == "in"
    assert m["message_id"] == "m1"
    assert m["ts"] == 100
    assert "language" in m and "translation" in m


def test_message_obj_direction_normalized():
    assert message_obj(text="x", direction="weird")["direction"] == "in"
    assert message_obj(text="x", direction="out")["direction"] == "out"


def test_message_obj_chinese_is_identity_translation():
    m = message_obj(text="你好")
    assert m["language"] == "zh"
    assert m["translation"]["ok"] is True
    assert m["translation"]["provider"] == "identity"


def test_normalize_chat_shape():
    c = normalize_chat(
        platform="line", platform_name="LINE", account_id="a1",
        account_label="A1", chat_key="u1", name="Alice",
        last_msg="hi", last_ts=5, unread=2, source={"k": "v"},
    )
    assert c["platform"] == "line"
    assert c["conversation_id"] == "line:a1:u1"
    assert c["unread"] == 2
    assert c["send_modes"] == SEND_MODES
    assert c["last_message"]["text"] == "hi"
    assert len(c["messages"]) == 1


def test_normalize_chat_empty_last_msg_no_messages():
    c = normalize_chat(
        platform="tg", platform_name="Telegram", account_id="d",
        account_label="d", chat_key="c", name="n", last_msg="",
    )
    assert c["messages"] == []


def test_candidate_messages_filters_empty_and_maps_direction():
    src = {"messages": [
        {"text": "hello", "is_self": False, "ts": 1, "id": "1"},
        {"text": "", "is_self": True},
        {"raw": "reply", "is_self": True, "ts": 2},
    ]}
    out = candidate_messages_from_source(src)
    assert len(out) == 2
    assert out[0]["text"] == "hello" and out[0]["direction"] == "in"
    assert out[1]["text"] == "reply" and out[1]["direction"] == "out"


def test_candidate_messages_none_when_no_list():
    assert candidate_messages_from_source({"foo": "bar"}) == []
