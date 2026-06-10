"""P61-5：触达效果回流（回复率统计）契约测试。"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes

HOUR = 3600.0
DAY = 86400.0


def _conv(store, cid):
    store.ingest_batch(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a",
        chat_key=cid.split(":")[-1], display_name=cid,
    ), [])


def _msg(store, cid, direction, ts, pid):
    store.ingest_batch(
        InboxConversation(conversation_id=cid, platform="telegram",
                          account_id="a", chat_key=cid.split(":")[-1]),
        [InboxMessage(conversation_id=cid, platform_msg_id=pid, direction=direction,
                      text="x", ts=ts)],
    )


def test_response_counts_inbound_after_outreach():
    store = InboxStore(":memory:")
    base = 1_000_000.0
    _conv(store, "telegram:a:1")
    _conv(store, "telegram:a:2")
    # 两条都发送
    store.record_outreach("telegram:a:1", batch_id="b1", status="sent", ts=base)
    store.record_outreach("telegram:a:2", batch_id="b1", status="sent", ts=base)
    # conv1 触达后 2 小时回复；conv2 无回复
    _msg(store, "telegram:a:1", "in", base + 2 * HOUR, "m1")
    stats = store.outreach_response_stats("b1", response_window_days=7)
    assert stats["sent"] == 2
    assert stats["responded"] == 1
    assert stats["response_rate"] == 0.5
    assert stats["avg_response_minutes"] == 120.0


def test_response_ignores_reply_before_outreach():
    store = InboxStore(":memory:")
    base = 1_000_000.0
    _conv(store, "telegram:a:1")
    store.record_outreach("telegram:a:1", batch_id="b1", status="sent", ts=base)
    # 触达前就有的入站消息不算回复
    _msg(store, "telegram:a:1", "in", base - HOUR, "m0")
    stats = store.outreach_response_stats("b1")
    assert stats["responded"] == 0


def test_response_window_excludes_late_reply():
    store = InboxStore(":memory:")
    base = 1_000_000.0
    _conv(store, "telegram:a:1")
    store.record_outreach("telegram:a:1", batch_id="b1", status="sent", ts=base)
    # 10 天后才回复，窗口 7 天 → 不计入
    _msg(store, "telegram:a:1", "in", base + 10 * DAY, "m1")
    assert store.outreach_response_stats("b1", response_window_days=7)["responded"] == 0
    # 不限窗（0）→ 计入
    assert store.outreach_response_stats("b1", response_window_days=0)["responded"] == 1


def test_response_excludes_failed_sends():
    store = InboxStore(":memory:")
    base = 1_000_000.0
    _conv(store, "telegram:a:1")
    store.record_outreach("telegram:a:1", batch_id="b1", status="failed", ts=base)
    _msg(store, "telegram:a:1", "in", base + HOUR, "m1")
    # 失败的触达不计入 sent 分母
    assert store.outreach_response_stats("b1")["sent"] == 0


def test_response_outbound_does_not_count():
    store = InboxStore(":memory:")
    base = 1_000_000.0
    _conv(store, "telegram:a:1")
    store.record_outreach("telegram:a:1", batch_id="b1", status="sent", ts=base)
    # 触达后只有我方出站消息，不算对方回复
    _msg(store, "telegram:a:1", "out", base + HOUR, "m1")
    assert store.outreach_response_stats("b1")["responded"] == 0


# ── 端点 ──────────────────────────────────────────────────────────────────
class _Templates:
    def TemplateResponse(self, *a, **k):
        raise AssertionError("not used")


class FakeCM:
    def __init__(self, cfg):
        self.config = cfg


def test_batch_endpoint_includes_response():
    store = InboxStore(":memory:")
    base = 1_000_000.0
    _conv(store, "telegram:a:1")
    store.record_outreach("telegram:a:1", batch_id="bx", status="sent", ts=base)
    _msg(store, "telegram:a:1", "in", base + HOUR, "m1")

    app = FastAPI()

    def _auth(request: Request):
        return True

    register_unified_inbox_routes(app, page_auth=_auth, api_auth=_auth, templates=_Templates())
    app.state.config_manager = FakeCM({"outreach": {"response_window_days": 7}})
    app.state.inbox_store = store
    client = TestClient(app)

    d = client.get("/api/unified-inbox/outreach/batch?batch_id=bx").json()
    assert d["ok"] is True
    assert d["by_status"].get("sent") == 1
    assert d["response"]["responded"] == 1
    assert d["response"]["response_rate"] == 1.0
