"""多平台 deferred 队列运营可观测端点契约。

覆盖：未启用（store 缺）→ enabled:false；启用后 → stats/recent/senders；
pending_by_reason 暴露护栏分布；reply_text 不外泄（只回 reply_len）。
"""
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.integrations.shared.deferred_outbox import DeferredOutboxStore
from src.web.routes.deferred_outbox_routes import register_deferred_outbox_routes

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()


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
