"""多平台 deferred 队列运营可观测端点契约。

覆盖：未启用（store 缺）→ enabled:false；启用后 → stats/recent/senders；
pending_by_reason 暴露护栏分布；reply_text 不外泄（只回 reply_len）。
"""
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.integrations.shared.deferred_outbox import (
    DeferredDispatcher,
    DeferredOutboxStore,
)
from src.web.routes.deferred_outbox_routes import register_deferred_outbox_routes

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()


def _ops_client():
    """带真 store + 真 dispatcher（支持 pause/resume）的运营动作 client。"""
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_deferred_outbox_routes(app, api_auth=_auth)
    store = DeferredOutboxStore(":memory:")
    dispatcher = DeferredDispatcher(
        store=store, kill_switch_check=lambda p, a: (False, "", ""))
    app.state.deferred_outbox_store = store
    app.state.deferred_outbox_dispatcher = dispatcher
    return TestClient(app), store, dispatcher


def _client(with_store=True):
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_deferred_outbox_routes(app, api_auth=_auth)
    if with_store:
        store = DeferredOutboxStore(":memory:")
        app.state.deferred_outbox_store = store

        class _Disp:
            def registered_platforms(self):
                return ["line", "telegram"]
        app.state.deferred_outbox_dispatcher = _Disp()
        return TestClient(app), store
    return TestClient(app), None


def test_status_disabled_when_no_store():
    client, _ = _client(with_store=False)
    r = client.get("/api/deferred-outbox/status").json()
    assert r["ok"] is True and r["enabled"] is False


def test_status_reports_stats_and_senders():
    client, store = _client()
    store.enqueue(platform="telegram", account_id="a", chat_key="c1",
                  reply_text="secret text here", defer_until=NOW + 9999, now=NOW,
                  reason="no_sender")
    r = client.get("/api/deferred-outbox/status").json()
    assert r["enabled"] is True
    assert r["stats"]["by_status"].get("pending") == 1
    assert r["stats"]["pending_by_reason"].get("no_sender") == 1
    assert r["senders"] == ["line", "telegram"]
    assert len(r["recent"]) == 1
    # reply_text 不外泄，只回长度
    assert "reply_text" not in r["recent"][0]
    assert r["recent"][0]["reply_len"] == len("secret text here")
    assert r["recent"][0]["platform"] == "telegram"


def test_status_recent_limit_capped():
    client, store = _client()
    for i in range(10):
        store.enqueue(platform="telegram", account_id="a", chat_key=f"c{i}",
                      reply_text="x", defer_until=NOW + 1, now=NOW)
    r = client.get("/api/deferred-outbox/status?limit=3").json()
    assert len(r["recent"]) == 3


# ── 运营动作端点 ────────────────────────────────────────────────
def test_retry_failed_requeues():
    client, store, _ = _ops_client()
    rid = store.enqueue(platform="line", account_id="a", chat_key="c",
                        reply_text="x", defer_until=NOW, now=NOW)
    store.mark_failed(rid, "boom")
    r = client.post("/api/deferred-outbox/retry", json={"status": "failed"}).json()
    assert r["ok"] is True and r["requeued"] == 1
    assert store.count(status="pending") == 1
    assert store.count(status="failed") == 0


def test_retry_single_id():
    client, store, _ = _ops_client()
    rid = store.enqueue(platform="line", account_id="a", chat_key="c",
                        reply_text="x", defer_until=NOW, now=NOW)
    store.mark_expired(rid, "stale")
    r = client.post("/api/deferred-outbox/retry", json={"id": rid}).json()
    assert r["ok"] is True and r["requeued"] == 1


def test_retry_needs_id_or_status():
    client, _, _ = _ops_client()
    r = client.post("/api/deferred-outbox/retry", json={}).json()
    assert r["ok"] is False


def test_cancel_by_reason():
    client, store, _ = _ops_client()
    for i in range(3):
        rid = store.enqueue(platform="line", account_id="a", chat_key=f"c{i}",
                            reply_text="x", defer_until=NOW + 9999, now=NOW)
        store.push_until(rid, NOW + 9999, note="no_sender")
    r = client.post("/api/deferred-outbox/cancel", json={"reason": "no_sender"}).json()
    assert r["ok"] is True and r["cancelled"] == 3
    assert store.count(status="pending") == 0


def test_cancel_needs_filter():
    client, _, _ = _ops_client()
    r = client.post("/api/deferred-outbox/cancel", json={}).json()
    assert r["ok"] is False


def test_pause_resume_and_status_shows_paused():
    client, _, dispatcher = _ops_client()
    r = client.post("/api/deferred-outbox/pause", json={"platform": "line"}).json()
    assert r["ok"] is True and "line" in r["paused"]
    assert dispatcher.is_paused("line")
    st = client.get("/api/deferred-outbox/status").json()
    assert "line" in st["paused"]
    r2 = client.post("/api/deferred-outbox/resume", json={"platform": "line"}).json()
    assert "line" not in r2["paused"]


def test_ops_disabled_when_no_store():
    client, _ = _client(with_store=False)
    r = client.post("/api/deferred-outbox/retry", json={"status": "failed"}).json()
    assert r["ok"] is False and r["enabled"] is False
