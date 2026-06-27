"""AutosendWorker 投递成功判定 (D4 收口) 单测。

锁定：send_callback 返回「被闸门拦截 / 未送达」时**不得计为已送达**——
否则 kill-switch/send-gate 拦截、桌面出站被拒会被误计入 total_delivered 刷指标。
"""

from __future__ import annotations

import pytest

from src.inbox.autosend_worker import AutosendWorker


class _FakeSvc:
    def __init__(self):
        self.failures = []

    def list_drafts(self, status="pending", limit=200):
        return [{
            "draft_id": "d1", "autopilot_level": "L2",
            "final_text": "您好呀~", "platform": "instagram",
            "account_id": "ig1", "chat_key": "c1", "conversation_id": "x1",
        }]

    def resolve_with_audit(self, draft_id, action, by=""):
        return {"ok": True}

    def record_autosend_failure(self, draft_id, *, conversation_id="", reason=""):
        self.failures.append((draft_id, reason))


@pytest.mark.asyncio
async def test_blocked_result_counts_as_failure():
    async def _cb(platform, account_id, chat_key, text):
        return {"ok": False, "error": "blocked:kill_switch:global"}

    svc = _FakeSvc()
    w = AutosendWorker(draft_service=svc, send_callback=_cb)
    await w._tick()
    assert w.total_delivered == 0
    assert w.total_deliver_errors == 1
    assert svc.failures and "blocked" in svc.failures[0][1]


@pytest.mark.asyncio
async def test_delivered_false_counts_as_failure():
    async def _cb(platform, account_id, chat_key, text):
        # 编排器拦截形态：{delivered: False, blocked: ...}（无 ok 字段）
        return {"delivered": False, "blocked": "send_gate:warmup_cap"}

    svc = _FakeSvc()
    w = AutosendWorker(draft_service=svc, send_callback=_cb)
    await w._tick()
    assert w.total_delivered == 0
    assert w.total_deliver_errors == 1


@pytest.mark.asyncio
async def test_desktop_queued_counts_as_delivered():
    async def _cb(platform, account_id, chat_key, text):
        return {"ok": True, "delivered_as": "desktop_queued", "id": 7}

    svc = _FakeSvc()
    w = AutosendWorker(draft_service=svc, send_callback=_cb)
    await w._tick()
    assert w.total_delivered == 1
    assert w.total_deliver_errors == 0
