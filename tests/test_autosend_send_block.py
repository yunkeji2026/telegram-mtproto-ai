"""AutosendWorker 会话级发送封禁（永久性投递错误）单测。

锁定：投递遇「永久性」硬错误（如群聊无发言权 CHAT_WRITE_FORBIDDEN）时——
  - 该会话进冷却黑名单；
  - 冷却窗口内的新 pending 草稿被直接取消（不 resolve/投递），total_skipped_blocked 增长；
  - cooldown=0 时机制关闭（回旧行为：每条都试）。
瞬时错误（超时等）不触发封禁。
"""

from __future__ import annotations

import pytest

from src.inbox.autosend_worker import AutosendWorker, _is_permanent_send_error


class _FakeStore:
    def __init__(self):
        self.cancelled = []  # (draft_id, status, decided_by)

    def update_draft_status(self, draft_id, *, status, final_text="", decided_by=""):
        self.cancelled.append((draft_id, status, decided_by))
        return True


class _FakeSvc:
    """每次 list_drafts 都返回同一会话的一条新 pending L2 草稿（模拟持续入站）。"""

    def __init__(self):
        self._store = _FakeStore()
        self.failures = []
        self._seq = 0

    def list_drafts(self, status="pending", limit=200):
        self._seq += 1
        return [{
            "draft_id": f"d{self._seq}", "autopilot_level": "L2",
            "final_text": "在群里发个消息~", "platform": "telegram",
            "account_id": "tg1", "chat_key": "c1",
            "conversation_id": "telegram:tg1:-100999",
        }]

    def resolve_with_audit(self, draft_id, action, by=""):
        return {"ok": True}

    def record_autosend_failure(self, draft_id, *, conversation_id="", reason=""):
        self.failures.append((draft_id, reason))


# ── 分类器 ──────────────────────────────────────────────────────────

def test_permanent_error_classifier():
    assert _is_permanent_send_error(
        "Telegram says: [403 CHAT_WRITE_FORBIDDEN] - You don't have rights to send messages") is True
    assert _is_permanent_send_error("USER_IS_BLOCKED") is True
    assert _is_permanent_send_error("peer_id_invalid") is True  # 大小写无关
    # 瞬时错误不算永久
    assert _is_permanent_send_error("ReadTimeout: connection timed out") is False
    assert _is_permanent_send_error("FLOOD_WAIT_30") is False
    assert _is_permanent_send_error("") is False


# ── 会话封禁闭环 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_permanent_failure_blocks_conversation():
    async def _cb(platform, account_id, chat_key, text):
        raise RuntimeError("Telegram says: [403 CHAT_WRITE_FORBIDDEN] - "
                           "You don't have rights to send messages in this chat")

    svc = _FakeSvc()
    w = AutosendWorker(draft_service=svc, send_callback=_cb,
                       config={"send_block_cooldown_sec": 3600})

    # 第一轮：投递→永久失败→会话进封禁
    await w._tick()
    assert w.total_deliver_errors == 1
    assert w.total_skipped_blocked == 0
    assert w._conv_send_blocked("telegram:tg1:-100999") is True

    # 第二轮：同会话新草稿被跳过取消，不再投递
    await w._tick()
    assert w.total_skipped_blocked == 1
    assert w.total_deliver_errors == 1  # 未再尝试投递
    assert svc._store.cancelled and svc._store.cancelled[0][1] == "cancelled"
    assert svc._store.cancelled[0][2] == "send_blocked"

    snap = w.status_snapshot()
    assert snap["total_skipped_blocked"] == 1
    assert snap["blocked_conversations"] == 1


@pytest.mark.asyncio
async def test_transient_failure_does_not_block():
    async def _cb(platform, account_id, chat_key, text):
        raise RuntimeError("ReadTimeout: connection timed out")

    svc = _FakeSvc()
    w = AutosendWorker(draft_service=svc, send_callback=_cb,
                       config={"send_block_cooldown_sec": 3600})

    await w._tick()
    await w._tick()
    # 两轮都尝试投递（瞬时错误不封禁），无跳过取消
    assert w.total_deliver_errors == 2
    assert w.total_skipped_blocked == 0
    assert w._conv_send_blocked("telegram:tg1:-100999") is False


@pytest.mark.asyncio
async def test_cooldown_zero_disables_block():
    async def _cb(platform, account_id, chat_key, text):
        raise RuntimeError("CHAT_WRITE_FORBIDDEN")

    svc = _FakeSvc()
    w = AutosendWorker(draft_service=svc, send_callback=_cb,
                       config={"send_block_cooldown_sec": 0})

    await w._tick()
    await w._tick()
    # 关闭机制 → 每轮都试都失败，从不跳过
    assert w.total_deliver_errors == 2
    assert w.total_skipped_blocked == 0
