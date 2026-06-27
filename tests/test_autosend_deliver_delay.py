"""AutosendWorker 投递拟人延迟（Phase 4）单测。

锁定：
  - deliver_delay 未配置 → 0（向后兼容，不延迟）
  - 配置后 _pick_deliver_delay 落在 [min, max]
  - _tick 投递时「先延迟后发送」，且 sleep 可注入（不真等）
"""

from __future__ import annotations

import pytest

from src.inbox.autosend_worker import AutosendWorker


class _FakeSvc:
    """最小草稿服务：恒返回一条 L2 待发草稿，resolve 恒成功。"""

    def __init__(self):
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [{
            "draft_id": "d1", "autopilot_level": "L2",
            "final_text": "您好呀~", "platform": "telegram",
            "account_id": "a1", "chat_key": "c1", "conversation_id": "x1",
        }]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


def test_no_delay_by_default():
    w = AutosendWorker(draft_service=_FakeSvc())
    assert w._pick_deliver_delay() == 0.0


def test_pick_delay_within_range():
    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {"min_sec": 1.0, "max_sec": 2.0}},
    )
    for _ in range(20):
        d = w._pick_deliver_delay()
        assert 1.0 <= d <= 2.0


def test_invalid_range_yields_zero():
    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {"min_sec": 5.0, "max_sec": 1.0}},
    )
    assert w._pick_deliver_delay() == 0.0


@pytest.mark.asyncio
async def test_tick_delays_before_send():
    events = []

    async def _fake_sleep(d):
        events.append(("sleep", d))

    async def _send_cb(platform, account_id, chat_key, text):
        events.append(("send", text))
        return {"ok": True}

    svc = _FakeSvc()
    w = AutosendWorker(
        draft_service=svc,
        config={"deliver_delay": {"min_sec": 0.5, "max_sec": 0.5}},
        send_callback=_send_cb,
        sleep=_fake_sleep,
    )
    await w._tick()
    # 先 sleep 再 send，且 sleep 时长在配置区间
    assert ("sleep", 0.5) in events
    assert ("send", "您好呀~") in events
    assert events.index(("sleep", 0.5)) < events.index(("send", "您好呀~"))
    assert w.total_delivered == 1


@pytest.mark.asyncio
async def test_tick_no_sleep_when_unconfigured():
    events = []

    async def _fake_sleep(d):
        events.append(("sleep", d))

    async def _send_cb(platform, account_id, chat_key, text):
        events.append(("send", text))
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        sleep=_fake_sleep,
    )
    await w._tick()
    assert not any(e[0] == "sleep" for e in events)
    assert ("send", "您好呀~") in events
