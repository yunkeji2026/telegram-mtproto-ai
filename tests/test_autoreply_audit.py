"""Phase 4 自动回复审计存储 + 决策副作用（审计/转人工）单测。"""

from __future__ import annotations

from src.integrations import protocol_autoreply as pa
from src.integrations.protocol_autoreply_audit import AutoReplyAudit


def _audit(tmp_path):
    return AutoReplyAudit(tmp_path / "ar_audit.db")


def _payload(text="在吗"):
    return {"platform": "telegram", "account_id": "tg1",
            "chat_key": "123", "text": text, "direction": "in"}


def test_record_and_recent(tmp_path):
    a = _audit(tmp_path)
    a.record(platform="telegram", account_id="tg1", chat_key="1",
             inbound="hi", reply="你好", risk="low",
             decision="sent", reason="ok")
    a.record(platform="telegram", account_id="tg1", chat_key="2",
             inbound="支付", reply="", risk="high",
             decision="skipped", reason="high_risk")
    items = a.recent(limit=10)
    assert len(items) == 2
    # 最新在前
    assert items[0]["reason"] == "high_risk"
    assert items[1]["reply"] == "你好"


async def test_record_publishes_to_subscriber(tmp_path):
    """Phase 10：record() 应即时把事件推给 SSE 订阅者（零轮询）。"""
    import asyncio

    from src.integrations import protocol_autoreply_audit as ara

    a = _audit(tmp_path)
    q = ara.subscribe()
    try:
        assert ara.subscriber_count() >= 1
        rid = a.record(platform="telegram", account_id="tg1", chat_key="1",
                       inbound="hi", reply="你好", decision="sent", reason="ok")
        row = await asyncio.wait_for(q.get(), timeout=2.0)
        assert row["id"] == rid
        assert row["decision"] == "sent"
        assert row["platform"] == "telegram"
        assert row["reply"] == "你好"
    finally:
        ara.unsubscribe(q)
    assert all(item[1] is not q for item in ara._subscribers)


def test_recent_filters_by_account(tmp_path):
    a = _audit(tmp_path)
    a.record(platform="telegram", account_id="tg1", reason="ok", decision="sent")
    a.record(platform="telegram", account_id="tg2", reason="ok", decision="sent")
    assert len(a.recent(account_id="tg1")) == 1
    assert len(a.recent(platform="telegram")) == 2


def test_stats_counts(tmp_path):
    a = _audit(tmp_path)
    for _ in range(3):
        a.record(platform="telegram", account_id="tg1",
                 decision="sent", reason="ok", ts=1000.0)
    a.record(platform="telegram", account_id="tg1",
             decision="skipped", reason="high_risk", ts=1000.0)
    s = a.stats(since_ts=0)
    assert s["sent"] == 3
    assert s["skipped"] == 1
    assert s["by_reason"]["ok"] == 3
    assert s["by_reason"]["high_risk"] == 1


def test_record_decision_audit_only_meaningful(tmp_path):
    a = _audit(tmp_path)
    # 噪声原因不记
    assert pa.record_decision_audit(a, _payload(), {"reason": "cooldown"}) is False
    assert pa.record_decision_audit(a, _payload(), {"reason": "disabled"}) is False
    # 有意义的原因记
    assert pa.record_decision_audit(
        a, _payload(), {"reason": "ok", "decision": "sent",
                        "text": "你好", "risk": "low"}) is True
    assert pa.record_decision_audit(
        a, _payload(), {"reason": "high_risk", "decision": "skipped",
                        "text": "x", "risk": "high"}) is True
    items = a.recent()
    assert len(items) == 2
    assert items[0]["conversation_id"] == "telegram:tg1:123"


def test_needs_handoff():
    assert pa.needs_handoff({"reason": "high_risk"}) is True
    assert pa.needs_handoff({"reason": "empty_reply"}) is True
    assert pa.needs_handoff({"reason": "send_error"}) is True
    assert pa.needs_handoff({"reason": "ok"}) is False
    assert pa.needs_handoff({"reason": "cooldown"}) is False


class _FakeStore:
    def __init__(self):
        self._tags = {}

    def get_conv_tags(self, cid):
        return self._tags.get(cid, [])

    def set_conv_tags(self, cid, tags):
        self._tags[cid] = list(tags)


def test_tag_needs_human_idempotent():
    store = _FakeStore()
    assert pa.tag_needs_human(store, _payload()) is True
    assert store.get_conv_tags("telegram:tg1:123") == [pa.HANDOFF_TAG]
    # 再次打不重复
    assert pa.tag_needs_human(store, _payload()) is False
    assert store.get_conv_tags("telegram:tg1:123") == [pa.HANDOFF_TAG]


def test_tag_needs_human_none_store():
    assert pa.tag_needs_human(None, _payload()) is False
