"""Tests for multi-message reply feature.

Covers:
- ui_hierarchy.pick_new_incoming_messages
- multi_msg_handler.analyze_multi_msg
"""

from __future__ import annotations

import asyncio
import json
from typing import List
from unittest.mock import AsyncMock

import pytest

from src.integrations.whatsapp_rpa.ui_hierarchy import (
    IncomingMessage,
    pick_new_incoming_messages,
)
from src.integrations.whatsapp_rpa.multi_msg_handler import (
    MsgGroup,
    MultiMsgAnalysis,
    analyze_multi_msg,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_xml(*msgs: tuple) -> bytes:
    """Build minimal WhatsApp chat XML. Each item is (text, bottom_y)."""
    nodes = []
    for text, by in msgs:
        top = by - 40
        nodes.append(
            f"<node resource-id='com.whatsapp:id/message_text' class='TextView'"
            f" text='{text}' bounds='[50,{top}][500,{by}]'/>"
        )
    return (
        "<hierarchy><node class='android.view.ViewGroup'>"
        + "".join(nodes)
        + "</node></hierarchy>"
    ).encode()


def _make_msgs(texts: List[str]) -> List[IncomingMessage]:
    return [IncomingMessage(text=t, cx=275, cy=100 * (i + 1), bottom_y=100 * (i + 1))
            for i, t in enumerate(texts)]


def _ai_client(response: dict):
    """Return a mock ai_client whose .chat() returns a JSON string."""
    m = AsyncMock()
    m.chat = AsyncMock(return_value=json.dumps(response))
    return m


# ── pick_new_incoming_messages ─────────────────────────────────────────────────

class TestPickNewIncomingMessages:
    def test_known_last_returns_new_only(self):
        xml = _make_xml(("你在哪", 640), ("好呀今晚", 740), ("还有上次那件事", 860))
        result = pick_new_incoming_messages(xml, last_peer_text="你在哪", screen_width=1080)
        assert len(result) == 2
        assert result[0].text == "好呀今晚"
        assert result[1].text == "还有上次那件事"

    def test_unknown_last_returns_one(self):
        xml = _make_xml(("你在哪", 640), ("好呀今晚", 740))
        result = pick_new_incoming_messages(xml, last_peer_text="", screen_width=1080)
        assert len(result) == 1
        assert result[0].text == "好呀今晚"

    def test_max_count_respected(self):
        xml = _make_xml(("消息一", 200), ("消息二", 300), ("消息三", 400), ("消息四", 500), ("消息五", 600))
        result = pick_new_incoming_messages(xml, last_peer_text="消息一", screen_width=1080, max_count=2)
        assert len(result) == 2
        assert result[-1].text == "消息五"

    def test_ascending_order(self):
        xml = _make_xml(("第一条消息", 300), ("第二条消息", 500), ("第三条消息", 700))
        result = pick_new_incoming_messages(xml, last_peer_text="", screen_width=1080)
        assert result[-1].text == "第三条消息"

    def test_empty_xml(self):
        result = pick_new_incoming_messages(b"<hierarchy/>", screen_width=1080)
        assert result == []

    def test_outgoing_excluded(self):
        xml = (
            b"<hierarchy><node class='android.view.ViewGroup'>"
            b"<node resource-id='com.whatsapp:id/message_text' class='TextView'"
            b" text='incoming' bounds='[50,600][500,640]'/>"
            b"<node resource-id='com.whatsapp:id/message_text' class='TextView'"
            b" text='outgoing_mine' bounds='[600,700][1050,740]'/>"
            b"</node></hierarchy>"
        )
        result = pick_new_incoming_messages(xml, screen_width=1080)
        assert all(m.text != "outgoing_mine" for m in result)

    def test_timestamp_excluded(self):
        xml = _make_xml(("上午8:12", 400), ("真正消息", 600))
        result = pick_new_incoming_messages(xml, screen_width=1080)
        assert all(m.text != "上午8:12" for m in result)

    def test_cx_cy_populated(self):
        xml = _make_xml(("你好世界", 600))
        result = pick_new_incoming_messages(xml, screen_width=1080)
        assert len(result) == 1
        assert result[0].cx == (50 + 500) // 2  # 275
        assert result[0].cy == (560 + 600) // 2  # 580

    def test_single_new_msg(self):
        xml = _make_xml(("old", 400), ("new1", 600))
        result = pick_new_incoming_messages(xml, last_peer_text="old", screen_width=1080)
        assert len(result) == 1
        assert result[0].text == "new1"

    def test_all_replied_returns_last(self):
        """last_peer_text is the newest → no newer messages exist → return last 1 safely."""
        xml = _make_xml(("只有这条", 600))
        result = pick_new_incoming_messages(xml, last_peer_text="只有这条", screen_width=1080)
        assert len(result) == 1


# ── analyze_multi_msg ──────────────────────────────────────────────────────────

class TestAnalyzeMultiMsg:
    async def test_single_msg_is_combined(self):
        msgs = _make_msgs(["你好"])
        result = await analyze_multi_msg(msgs, _ai_client({"mode": "combined"}))
        assert result.mode == "combined"
        assert len(result.groups) == 1
        assert result.groups[0].reply_to.text == "你好"

    async def test_casual_mode(self):
        msgs = _make_msgs(["哈哈", "😊"])
        ai = _ai_client({"mode": "casual"})
        result = await analyze_multi_msg(msgs, ai)
        assert result.mode == "casual"
        assert result.groups[0].reply_to.text == "😊"

    async def test_combined_mode(self):
        msgs = _make_msgs(["我想问", "你们的价格"])
        ai = _ai_client({"mode": "combined", "topic": "询价"})
        result = await analyze_multi_msg(msgs, ai)
        assert result.mode == "combined"
        assert len(result.groups) == 1
        assert "我想问" in result.groups[0].combined_text
        assert "你们的价格" in result.groups[0].combined_text

    async def test_multi_intent_mode(self):
        msgs = _make_msgs(["你今天有空吗", "我想吃饭", "还有上次那件事怎么了"])
        ai = _ai_client({
            "mode": "multi_intent",
            "groups": [
                {"indices": [0, 1], "topic": "约今晚吃饭"},
                {"indices": [2], "topic": "跟进上次事项"},
            ]
        })
        result = await analyze_multi_msg(msgs, ai)
        assert result.mode == "multi_intent"
        assert len(result.groups) == 2
        assert result.groups[0].topic == "约今晚吃饭"
        assert result.groups[1].topic == "跟进上次事项"
        assert result.groups[1].reply_to.text == "还有上次那件事怎么了"

    async def test_multi_intent_combined_text(self):
        msgs = _make_msgs(["问题A", "补充A", "问题B"])
        ai = _ai_client({
            "mode": "multi_intent",
            "groups": [
                {"indices": [0, 1], "topic": "A"},
                {"indices": [2], "topic": "B"},
            ]
        })
        result = await analyze_multi_msg(msgs, ai)
        grp0 = result.groups[0]
        assert "问题A" in grp0.combined_text
        assert "补充A" in grp0.combined_text
        assert grp0.reply_to.text == "补充A"

    async def test_fallback_on_bad_json(self):
        ai = AsyncMock()
        ai.chat = AsyncMock(return_value="not json at all")
        msgs = _make_msgs(["msg1", "msg2"])
        result = await analyze_multi_msg(msgs, ai)
        assert result.mode == "combined"

    async def test_fallback_on_timeout(self):
        async def _slow(*a, **kw):
            await asyncio.sleep(20)
            return "{}"
        ai = AsyncMock()
        ai.chat = _slow
        msgs = _make_msgs(["a", "b"])
        result = await analyze_multi_msg(msgs, ai)
        assert result.mode == "combined"

    async def test_empty_msgs(self):
        result = await analyze_multi_msg([], _ai_client({}))
        assert result.mode == "combined"
        assert result.groups == []

    async def test_multi_intent_out_of_range_indices(self):
        msgs = _make_msgs(["only"])
        ai = _ai_client({
            "mode": "multi_intent",
            "groups": [{"indices": [99], "topic": "ghost"}]
        })
        result = await analyze_multi_msg(msgs, ai)
        assert result.mode == "combined"

    async def test_ai_client_receives_all_texts(self):
        msgs = _make_msgs(["hello", "world"])
        captured = {}
        async def _capture(prompt, **kw):
            captured["prompt"] = prompt
            return json.dumps({"mode": "combined"})
        ai = AsyncMock()
        ai.chat = _capture
        await analyze_multi_msg(msgs, ai)
        assert "hello" in captured["prompt"]
        assert "world" in captured["prompt"]
