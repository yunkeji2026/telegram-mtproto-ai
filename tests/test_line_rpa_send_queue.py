"""P28: LINE send-manual queue — state_store unit tests + routes integration tests."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from src.integrations.line_rpa.state_store import LineRpaStateStore


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path: Path) -> LineRpaStateStore:
    return LineRpaStateStore(tmp_path / "line.db", max_runs_kept=100)


# ── state_store unit tests ────────────────────────────────────────────────────

def test_enqueue_returns_id(store: LineRpaStateStore):
    item_id = store.enqueue_send(
        chat_key="line_rpa:Alice", peer_name="Alice", text="hello", created_by="admin",
    )
    assert isinstance(item_id, int)
    assert item_id > 0


def test_list_send_queue_pending_only(store: LineRpaStateStore):
    store.enqueue_send(chat_key="k1", peer_name="A", text="msg1")
    store.enqueue_send(chat_key="k2", peer_name="B", text="msg2")
    items = store.list_send_queue(limit=10, include_done=False)
    assert len(items) == 2
    assert all(it["status"] == "queued" for it in items)


def test_get_send_queue_item(store: LineRpaStateStore):
    item_id = store.enqueue_send(chat_key="k1", peer_name="P", text="hi")
    item = store.get_send_queue_item(item_id)
    assert item is not None
    assert item["id"] == item_id
    assert item["chat_key"] == "k1"
    assert item["text"] == "hi"


def test_get_send_queue_item_not_found(store: LineRpaStateStore):
    assert store.get_send_queue_item(99999) is None


def test_cancel_queued_item(store: LineRpaStateStore):
    item_id = store.enqueue_send(chat_key="k1", peer_name="P", text="cancel me")
    ok = store.cancel_send_queue_item(item_id)
    assert ok is True
    item = store.get_send_queue_item(item_id)
    assert item["status"] == "cancelled"


def test_cancel_nonexistent_returns_false(store: LineRpaStateStore):
    assert store.cancel_send_queue_item(999) is False


def test_pop_send_queue_item_fifo(store: LineRpaStateStore):
    id1 = store.enqueue_send(chat_key="k1", peer_name="A", text="first")
    id2 = store.enqueue_send(chat_key="k2", peer_name="B", text="second")
    item = store.pop_send_queue_item()
    assert item is not None
    assert item["id"] == id1
    assert item["status"] == "queued"  # row returned before re-read
    # After pop: status in DB should be 'processing'
    fetched = store.get_send_queue_item(id1)
    assert fetched["status"] == "processing"


def test_pop_returns_none_when_empty(store: LineRpaStateStore):
    assert store.pop_send_queue_item() is None


def test_mark_send_queue_item_sent(store: LineRpaStateStore):
    item_id = store.enqueue_send(chat_key="k", peer_name="P", text="txt")
    store.pop_send_queue_item()
    store.mark_send_queue_item(item_id, "sent")
    item = store.get_send_queue_item(item_id)
    assert item["status"] == "sent"
    assert item["sent_at"] > 0


def test_mark_send_queue_item_failed_with_error(store: LineRpaStateStore):
    item_id = store.enqueue_send(chat_key="k", peer_name="P", text="txt")
    store.pop_send_queue_item()
    store.mark_send_queue_item(item_id, "failed", error="some error")
    item = store.get_send_queue_item(item_id)
    assert item["status"] == "failed"
    assert item["error"] == "some error"


def test_list_includes_done_when_flag_set(store: LineRpaStateStore):
    id1 = store.enqueue_send(chat_key="k1", peer_name="P", text="a")
    id2 = store.enqueue_send(chat_key="k2", peer_name="Q", text="b")
    store.pop_send_queue_item()
    store.mark_send_queue_item(id1, "sent")
    pending = store.list_send_queue(include_done=False)
    all_items = store.list_send_queue(include_done=True)
    assert len(pending) == 1
    assert len(all_items) == 2


def test_recovery_resets_processing_to_queued(tmp_path: Path):
    db = tmp_path / "line.db"
    s1 = LineRpaStateStore(db, max_runs_kept=100)
    item_id = s1.enqueue_send(chat_key="k", peer_name="P", text="recover me")
    s1.pop_send_queue_item()
    assert s1.get_send_queue_item(item_id)["status"] == "processing"
    # Simulate process restart: new store instance triggers recovery
    s2 = LineRpaStateStore(db, max_runs_kept=100)
    assert s2.get_send_queue_item(item_id)["status"] == "queued"


def test_cancel_processing_item_fails(store: LineRpaStateStore):
    item_id = store.enqueue_send(chat_key="k", peer_name="P", text="txt")
    store.pop_send_queue_item()
    ok = store.cancel_send_queue_item(item_id)
    assert ok is False  # processing state cannot be cancelled


# ── routes integration tests ──────────────────────────────────────────────────

@pytest.fixture
def mock_service(tmp_path: Path):
    """Return a real-state service mock with actual state_store."""
    state = LineRpaStateStore(tmp_path / "line.db", max_runs_kept=100)
    svc = MagicMock()
    svc.enqueue_send.side_effect = lambda **kw: state.enqueue_send(**kw)
    svc.list_send_queue.side_effect = lambda **kw: state.list_send_queue(**kw)
    svc.get_send_queue_item.side_effect = lambda item_id: state.get_send_queue_item(item_id)
    svc.cancel_send_queue_item.side_effect = lambda item_id: state.cancel_send_queue_item(item_id)
    return svc, state


def _build_app(mock_svc):
    """Build minimal FastAPI app with LINE send-manual routes for testing."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.line_rpa_routes import register_line_rpa_routes

    app = FastAPI()
    app.state.line_rpa_service = mock_svc

    cm = MagicMock()
    cm.config = {}

    def api_auth(request):
        pass

    def page_auth(request):
        pass

    register_line_rpa_routes(
        app,
        page_auth=page_auth,
        api_auth=api_auth,
        templates=MagicMock(),
        config_manager=cm,
        audit_store=None,
    )
    return TestClient(app)


def test_route_send_manual_enqueue(mock_service):
    svc, state = mock_service
    client = _build_app(svc)
    r = client.post(
        "/api/line-rpa/send-manual",
        json={"chat_key": "line_rpa:Alice", "peer_name": "Alice", "text": "hello"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert isinstance(data["item_id"], int)
    assert data["item_id"] > 0


def test_route_send_manual_missing_chat_key(mock_service):
    svc, _ = mock_service
    client = _build_app(svc)
    r = client.post("/api/line-rpa/send-manual", json={"text": "hi"})
    assert r.status_code == 400


def test_route_send_manual_missing_text(mock_service):
    svc, _ = mock_service
    client = _build_app(svc)
    r = client.post("/api/line-rpa/send-manual", json={"chat_key": "k"})
    assert r.status_code == 400


def test_route_list_send_queue(mock_service):
    svc, state = mock_service
    state.enqueue_send(chat_key="k1", peer_name="P", text="msg1")
    state.enqueue_send(chat_key="k2", peer_name="Q", text="msg2")
    client = _build_app(svc)
    r = client.get("/api/line-rpa/send-queue")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    assert len(data["items"]) == 2


def test_route_get_single_item(mock_service):
    svc, state = mock_service
    item_id = state.enqueue_send(chat_key="k1", peer_name="P", text="hello")
    client = _build_app(svc)
    r = client.get(f"/api/line-rpa/send-queue/{item_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == item_id
    assert data["chat_key"] == "k1"


def test_route_get_item_not_found(mock_service):
    svc, _ = mock_service
    client = _build_app(svc)
    r = client.get("/api/line-rpa/send-queue/99999")
    assert r.status_code == 404


def test_route_cancel_item(mock_service):
    svc, state = mock_service
    item_id = state.enqueue_send(chat_key="k1", peer_name="P", text="cancel")
    client = _build_app(svc)
    r = client.post(f"/api/line-rpa/send-queue/{item_id}/cancel")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert state.get_send_queue_item(item_id)["status"] == "cancelled"


def test_route_cancel_nonexistent(mock_service):
    svc, _ = mock_service
    client = _build_app(svc)
    r = client.post("/api/line-rpa/send-queue/99999/cancel")
    assert r.status_code == 409
