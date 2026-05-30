"""P28: Messenger send-manual queue — state_store unit tests + routes integration tests."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from src.integrations.messenger_rpa.state_store import (
    MessengerRpaStateStore,
    default_state_db_path,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path: Path) -> MessengerRpaStateStore:
    return MessengerRpaStateStore(tmp_path / "msgr.db", max_runs_kept=100)


# ── state_store unit tests ────────────────────────────────────────────────────

def test_enqueue_returns_id(store: MessengerRpaStateStore):
    item_id = store.enqueue_send(
        chat_key="messenger_rpa:Alice", peer_name="Alice", text="hello",
    )
    assert isinstance(item_id, int) and item_id > 0


def test_list_pending_only(store: MessengerRpaStateStore):
    store.enqueue_send(chat_key="k1", peer_name="A", text="m1")
    store.enqueue_send(chat_key="k2", peer_name="B", text="m2")
    items = store.list_send_queue(include_done=False)
    assert len(items) == 2
    assert all(it["status"] == "queued" for it in items)


def test_get_item(store: MessengerRpaStateStore):
    item_id = store.enqueue_send(chat_key="k1", peer_name="P", text="hi")
    item = store.get_send_queue_item(item_id)
    assert item is not None
    assert item["id"] == item_id
    assert item["text"] == "hi"


def test_get_missing_returns_none(store: MessengerRpaStateStore):
    assert store.get_send_queue_item(99999) is None


def test_cancel_queued(store: MessengerRpaStateStore):
    item_id = store.enqueue_send(chat_key="k", peer_name="P", text="cancel")
    assert store.cancel_send_queue_item(item_id) is True
    assert store.get_send_queue_item(item_id)["status"] == "cancelled"


def test_cancel_nonexistent(store: MessengerRpaStateStore):
    assert store.cancel_send_queue_item(9999) is False


def test_pop_fifo_and_marks_processing(store: MessengerRpaStateStore):
    id1 = store.enqueue_send(chat_key="k1", peer_name="A", text="first")
    id2 = store.enqueue_send(chat_key="k2", peer_name="B", text="second")
    item = store.pop_send_queue_item()
    assert item["id"] == id1
    assert store.get_send_queue_item(id1)["status"] == "processing"
    assert store.get_send_queue_item(id2)["status"] == "queued"


def test_pop_empty_returns_none(store: MessengerRpaStateStore):
    assert store.pop_send_queue_item() is None


def test_mark_sent(store: MessengerRpaStateStore):
    item_id = store.enqueue_send(chat_key="k", peer_name="P", text="t")
    store.pop_send_queue_item()
    store.mark_send_queue_item(item_id, "sent")
    item = store.get_send_queue_item(item_id)
    assert item["status"] == "sent"
    assert item["sent_at"] > 0


def test_mark_failed_with_error(store: MessengerRpaStateStore):
    item_id = store.enqueue_send(chat_key="k", peer_name="P", text="t")
    store.pop_send_queue_item()
    store.mark_send_queue_item(item_id, "failed", error="adb error")
    item = store.get_send_queue_item(item_id)
    assert item["status"] == "failed"
    assert item["error"] == "adb error"


def test_include_done_flag(store: MessengerRpaStateStore):
    id1 = store.enqueue_send(chat_key="k1", peer_name="P", text="a")
    id2 = store.enqueue_send(chat_key="k2", peer_name="Q", text="b")
    store.pop_send_queue_item()
    store.mark_send_queue_item(id1, "sent")
    assert len(store.list_send_queue(include_done=False)) == 1
    assert len(store.list_send_queue(include_done=True)) == 2


def test_recovery_resets_processing(tmp_path: Path):
    db = tmp_path / "msgr.db"
    s1 = MessengerRpaStateStore(db, max_runs_kept=100)
    item_id = s1.enqueue_send(chat_key="k", peer_name="P", text="recover")
    s1.pop_send_queue_item()
    assert s1.get_send_queue_item(item_id)["status"] == "processing"
    s2 = MessengerRpaStateStore(db, max_runs_kept=100)
    assert s2.get_send_queue_item(item_id)["status"] == "queued"


def test_cancel_processing_fails(store: MessengerRpaStateStore):
    item_id = store.enqueue_send(chat_key="k", peer_name="P", text="t")
    store.pop_send_queue_item()
    assert store.cancel_send_queue_item(item_id) is False


# ── routes integration tests ──────────────────────────────────────────────────

@pytest.fixture
def mock_service(tmp_path: Path):
    state = MessengerRpaStateStore(tmp_path / "msgr.db", max_runs_kept=100)
    svc = MagicMock()
    svc.enqueue_send.side_effect = lambda **kw: state.enqueue_send(**kw)
    svc.list_send_queue.side_effect = lambda **kw: state.list_send_queue(**kw)
    svc.get_send_queue_item.side_effect = lambda item_id: state.get_send_queue_item(item_id)
    svc.cancel_send_queue_item.side_effect = lambda item_id: state.cancel_send_queue_item(item_id)
    return svc, state


def _build_app(mock_svc):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.messenger_rpa_routes import register_messenger_rpa_routes

    app = FastAPI()
    app.state.messenger_rpa_service = mock_svc
    app.state.messenger_rpa_state_store = None

    cm = MagicMock()
    cm.config = {}

    def api_auth(request):
        pass

    def page_auth(request):
        pass

    register_messenger_rpa_routes(
        app,
        page_auth=page_auth,
        api_auth=api_auth,
        templates=MagicMock(),
        config_manager=cm,
        audit_store=None,
    )
    return TestClient(app)


def test_route_enqueue(mock_service):
    svc, _ = mock_service
    client = _build_app(svc)
    r = client.post(
        "/api/messenger-rpa/send-manual",
        json={"chat_key": "msgr:Alice", "peer_name": "Alice", "text": "hello"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["item_id"] > 0


def test_route_enqueue_missing_chat_key(mock_service):
    svc, _ = mock_service
    client = _build_app(svc)
    r = client.post("/api/messenger-rpa/send-manual", json={"text": "hi"})
    assert r.status_code == 400


def test_route_list(mock_service):
    svc, state = mock_service
    state.enqueue_send(chat_key="k1", peer_name="P", text="m1")
    state.enqueue_send(chat_key="k2", peer_name="Q", text="m2")
    client = _build_app(svc)
    r = client.get("/api/messenger-rpa/send-queue")
    assert r.status_code == 200
    assert r.json()["count"] == 2


def test_route_get_item(mock_service):
    svc, state = mock_service
    item_id = state.enqueue_send(chat_key="k", peer_name="P", text="hi")
    client = _build_app(svc)
    r = client.get(f"/api/messenger-rpa/send-queue/{item_id}")
    assert r.status_code == 200
    assert r.json()["id"] == item_id


def test_route_get_item_404(mock_service):
    svc, _ = mock_service
    client = _build_app(svc)
    r = client.get("/api/messenger-rpa/send-queue/99999")
    assert r.status_code == 404


def test_route_cancel(mock_service):
    svc, state = mock_service
    item_id = state.enqueue_send(chat_key="k", peer_name="P", text="cancel me")
    client = _build_app(svc)
    r = client.post(f"/api/messenger-rpa/send-queue/{item_id}/cancel")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert state.get_send_queue_item(item_id)["status"] == "cancelled"


def test_route_cancel_nonexistent_409(mock_service):
    svc, _ = mock_service
    client = _build_app(svc)
    r = client.post("/api/messenger-rpa/send-queue/99999/cancel")
    assert r.status_code == 409
