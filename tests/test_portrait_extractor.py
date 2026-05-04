"""Phase 1 — PortraitExtractor 单元测试。"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.contacts.portrait_extractor import PortraitExtractor, render_block


class FakeJourney:
    def __init__(self, journey_id: str = "j1", snapshot: str = "", refreshed_at: int = 0) -> None:
        self.journey_id = journey_id
        self.context_snapshot_json = snapshot
        self.snapshot_refreshed_at = refreshed_at


# ── should_refresh ────────────────────────────────────────────


def test_should_refresh_no_snapshot_with_enough_inbound_returns_true():
    """W3-D1.2 改：无 snapshot 时也要 inbound >= min_for_initial（默认 2）"""
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": int(time.time())} for _ in range(3)
    ]
    ai = MagicMock()
    ext = PortraitExtractor(store, ai, min_for_initial=2)
    j = FakeJourney(snapshot="")
    assert ext.should_refresh(j) is True


def test_should_refresh_no_snapshot_with_too_few_inbound_returns_false():
    """W3-D1.2 新：无 snapshot + inbound < min_for_initial → 不抽（避免白调 LLM）"""
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": int(time.time())},  # 仅 1 条
    ]
    ai = MagicMock()
    ext = PortraitExtractor(store, ai, min_for_initial=2)
    j = FakeJourney(snapshot="")
    assert ext.should_refresh(j) is False


def test_should_refresh_old_snapshot_returns_true():
    store = MagicMock()
    ai = MagicMock()
    ext = PortraitExtractor(store, ai, refresh_after_hours=1.0)
    j = FakeJourney(snapshot='{"x":1}', refreshed_at=int(time.time()) - 3600 * 5)
    assert ext.should_refresh(j) is True


def test_should_refresh_recent_with_few_inbound_returns_false():
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": int(time.time())} for _ in range(2)
    ]
    ai = MagicMock()
    ext = PortraitExtractor(store, ai, refresh_every_n_inbound=5, refresh_after_hours=24)
    j = FakeJourney(snapshot='{"x":1}', refreshed_at=int(time.time()) - 60)
    assert ext.should_refresh(j) is False


def test_should_refresh_recent_with_enough_inbound_returns_true():
    refreshed = int(time.time()) - 60
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": refreshed + 10 + i} for i in range(5)
    ]
    ai = MagicMock()
    ext = PortraitExtractor(store, ai, refresh_every_n_inbound=5, refresh_after_hours=24)
    j = FakeJourney(snapshot='{"x":1}', refreshed_at=refreshed)
    assert ext.should_refresh(j) is True


def test_should_refresh_old_inbound_doesnt_trigger():
    """新增入站消息时间戳 ≤ 上次抽时间 → 不算新增，不触发。"""
    refreshed = int(time.time())
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": refreshed - 100} for _ in range(10)
    ]
    ai = MagicMock()
    ext = PortraitExtractor(store, ai, refresh_every_n_inbound=5, refresh_after_hours=24)
    j = FakeJourney(snapshot='{"x":1}', refreshed_at=refreshed)
    assert ext.should_refresh(j) is False


# ── collect_recent_inbound ─────────────────────────────────────


def test_collect_recent_inbound_filters_only_msg_in_and_orders_asc():
    store = MagicMock()
    # store.list_events 返 DESC（newest 最前）
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": 100, "payload_json": '{"text_preview":"newest"}'},
        {"event_type": "msg_out", "ts": 99, "payload_json": '{"text_preview":"bot reply"}'},
        {"event_type": "msg_in", "ts": 98, "payload_json": {"text_preview": "older"}},
    ]
    ai = MagicMock()
    ext = PortraitExtractor(store, ai)
    j = FakeJourney()
    msgs = ext.collect_recent_inbound(j)
    # asc: 旧 → 新；只含 msg_in
    assert msgs == ["older", "newest"]


def test_collect_handles_dict_or_json_payload():
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": 200, "payload_json": {"text_preview": "dict-form"}},
        {"event_type": "msg_in", "ts": 100, "payload_json": '{"text_preview":"json-form"}'},
    ]
    ai = MagicMock()
    ext = PortraitExtractor(store, ai)
    msgs = ext.collect_recent_inbound(FakeJourney())
    assert msgs == ["json-form", "dict-form"]


def test_collect_skips_empty_text_and_caps_at_max():
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": 1000 - i, "payload_json": {"text_preview": f"m{i}"}}
        for i in range(20)
    ] + [
        {"event_type": "msg_in", "ts": 0, "payload_json": {"text_preview": ""}},
    ]
    ai = MagicMock()
    ext = PortraitExtractor(store, ai, max_inbound_messages_for_extract=5)
    msgs = ext.collect_recent_inbound(FakeJourney())
    assert len(msgs) == 5
    # 应取 ts 最新的 5 条（m0..m4），asc 排序后是 m4 → m0
    assert msgs[0] == "m4"
    assert msgs[-1] == "m0"


# ── extract_and_persist ────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_and_persist_calls_ai_and_writes_snapshot():
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": 1000 + i, "payload_json": {"text_preview": f"msg {i}"}}
        for i in range(5)
    ]
    ai = MagicMock()
    ai.chat = AsyncMock(return_value=(
        '{"language":"ja","tone":"casual_friendly",'
        '"interests":["旅行"],"recent_topics":["週末の予定"],'
        '"key_facts":["日本在住"],"intimacy_signal":"warming"}'
    ))
    ext = PortraitExtractor(store, ai)
    j = FakeJourney(journey_id="j-test")

    result = await ext.extract_and_persist(journey=j, display_name="さとう たかひろ")

    assert result is not None
    assert result["language"] == "ja"
    assert result["tone"] == "casual_friendly"
    assert "_extracted_at" in result
    assert result["_msg_count"] == 5

    # update_journey 被调用且参数对
    store.update_journey.assert_called_once()
    args, kwargs = store.update_journey.call_args
    assert args == ("j-test",)
    assert "context_snapshot_json" in kwargs
    assert "snapshot_refreshed_at" in kwargs
    saved = json.loads(kwargs["context_snapshot_json"])
    assert saved["language"] == "ja"
    assert saved["intimacy_signal"] == "warming"


@pytest.mark.asyncio
async def test_extract_and_persist_handles_too_few_messages():
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": 1, "payload_json": {"text_preview": "only one"}},
    ]
    ai = MagicMock()
    ai.chat = AsyncMock()
    ext = PortraitExtractor(store, ai)
    j = FakeJourney()
    result = await ext.extract_and_persist(journey=j, display_name="X")
    assert result is None
    ai.chat.assert_not_called()
    store.update_journey.assert_not_called()


@pytest.mark.asyncio
async def test_extract_and_persist_handles_invalid_json():
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": i, "payload_json": {"text_preview": f"m{i}"}}
        for i in range(5)
    ]
    ai = MagicMock()
    ai.chat = AsyncMock(return_value="this is not json")
    ext = PortraitExtractor(store, ai)
    result = await ext.extract_and_persist(journey=FakeJourney())
    assert result is None
    store.update_journey.assert_not_called()


@pytest.mark.asyncio
async def test_extract_and_persist_strips_markdown_fence():
    """LLM 偶尔会包 ```json 围栏，extractor 应能容错。"""
    store = MagicMock()
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": i, "payload_json": {"text_preview": f"m{i}"}}
        for i in range(5)
    ]
    ai = MagicMock()
    ai.chat = AsyncMock(return_value='```json\n{"language":"en","tone":"curt"}\n```')
    ext = PortraitExtractor(store, ai)
    result = await ext.extract_and_persist(journey=FakeJourney())
    assert result is not None
    assert result["language"] == "en"


# ── render_block ───────────────────────────────────────────────


def test_render_block_full():
    snap = json.dumps({
        "language": "ja", "tone": "casual_friendly",
        "interests": ["旅行", "料理"], "recent_topics": ["週末の予定"],
        "key_facts": ["日本在住"], "intimacy_signal": "warming",
    }, ensure_ascii=False)
    block = render_block(snap)
    assert "对话伙伴画像" in block
    assert "ja" in block
    assert "casual_friendly" in block
    assert "旅行" in block
    assert "日本在住" in block
    assert "warming" in block


def test_render_block_empty_or_invalid_returns_empty():
    assert render_block("") == ""
    assert render_block("not json") == ""


def test_render_block_skips_all_unknown_fields():
    snap = json.dumps({
        "language": "unknown", "tone": "unknown",
        "interests": [], "recent_topics": [], "key_facts": ["unknown"],
        "intimacy_signal": "unknown",
    })
    assert render_block(snap) == ""


def test_render_block_partial():
    snap = json.dumps({"language": "ja", "tone": "unknown", "interests": [], "key_facts": []})
    block = render_block(snap)
    assert "ja" in block
    assert "unknown" not in block
