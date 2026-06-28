"""AutosendWorker × 出站翻译回调集成单测（增量8）。

锁定：
  - 无 translate_callback → 发原文（向后兼容，旧行为不变）
  - 有 translate_callback → 投递前文本被替换为译文，total_translated 自增
  - translate_callback 抛异常 → 回落发原文，不阻塞投递（仍 total_delivered）
  - 翻译在 send 之前发生（顺序锁定）
"""

from __future__ import annotations

import pytest

from src.inbox.autosend_worker import AutosendWorker


class _FakeSvc:
    def __init__(self):
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [{
            "draft_id": "d1", "autopilot_level": "L2",
            "final_text": "你好呀~", "platform": "telegram",
            "account_id": "a1", "chat_key": "c1", "conversation_id": "x1",
        }]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


@pytest.mark.asyncio
async def test_no_translate_callback_sends_original():
    sent = []

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(draft_service=_FakeSvc(), send_callback=_send_cb)
    await w._tick()
    assert sent == ["你好呀~"]
    assert w.total_translated == 0
    assert w.total_delivered == 1


@pytest.mark.asyncio
async def test_translate_callback_replaces_text():
    events = []

    async def _translate_cb(item):
        events.append(("translate", item["text"]))
        return "Hello~"

    async def _send_cb(p, a, c, text):
        events.append(("send", text))
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        translate_callback=_translate_cb,
    )
    await w._tick()
    assert ("send", "Hello~") in events
    assert events.index(("translate", "你好呀~")) < events.index(("send", "Hello~"))
    assert w.total_translated == 1
    assert w.total_delivered == 1


@pytest.mark.asyncio
async def test_translate_same_text_no_counter_bump():
    async def _translate_cb(item):
        return item["text"]  # 回落原文（同文）

    sent = []

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        translate_callback=_translate_cb,
    )
    await w._tick()
    assert sent == ["你好呀~"]
    assert w.total_translated == 0   # 译文==原文不计


@pytest.mark.asyncio
async def test_translate_exception_falls_back_to_original():
    sent = []

    async def _translate_cb(item):
        raise RuntimeError("translate boom")

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        translate_callback=_translate_cb,
    )
    await w._tick()
    assert sent == ["你好呀~"]        # 异常回落原文
    assert w.total_delivered == 1     # 投递未被阻塞


def test_status_snapshot_exposes_translate_fields():
    w = AutosendWorker(draft_service=_FakeSvc())
    snap = w.status_snapshot()
    assert snap["translate_enabled"] is False
    assert snap["total_translated"] == 0


class _EmptyDraftSvc:
    """L2 草稿正文为空（回填失败/竞态）——投递模式下必须被跳过，绝不标记已发。"""

    def __init__(self):
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [{
            "draft_id": "d_empty", "autopilot_level": "L2",
            "final_text": "", "draft_text": "", "platform": "telegram",
            "account_id": "a1", "chat_key": "c1", "conversation_id": "x1",
        }]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


@pytest.mark.asyncio
async def test_empty_draft_skipped_not_marked_sent():
    """投递模式：空正文 L2 草稿不 resolve、不投递、不计入 sent（防『只标记不真发』）。"""
    svc = _EmptyDraftSvc()
    sent = []

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(draft_service=svc, send_callback=_send_cb)
    await w._tick()
    assert svc.resolved == []          # 空草稿没有被 resolve（不会被标记 approved/已发）
    assert sent == []                  # 没有任何投递
    assert w.total_sent == 0
    assert w.total_delivered == 0


@pytest.mark.asyncio
async def test_empty_draft_still_resolved_when_no_delivery():
    """非投递模式（send_callback=None，旧『仅 DB 标记』行为）：保持向后兼容，仍 resolve。"""
    svc = _EmptyDraftSvc()
    w = AutosendWorker(draft_service=svc)  # 无 send_callback
    await w._tick()
    assert svc.resolved == ["d_empty"]
    assert w.total_sent == 1
