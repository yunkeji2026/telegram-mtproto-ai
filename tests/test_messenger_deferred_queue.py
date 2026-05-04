"""W2-D1.1-1.4：deferred 队列 enqueue/drain/expire 测试。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore


@pytest.fixture
def store(tmp_path: Path):
    db = tmp_path / "state.db"
    return MessengerRpaStateStore(db, account_id="test_acc")


def test_enqueue_deferred_basic(store):
    until = time.time() + 60
    rid = store.enqueue_deferred(
        chat_key="test_acc:Alice",
        chat_name="Alice",
        peer_text="hey",
        peer_kind="text",
        reply_text="hi～",
        defer_until=until,
        defer_reason="rate_limit:quiet_hours",
    )
    assert rid > 0
    rows = store.list_approvals(status="deferred", limit=10)
    assert len(rows) == 1
    assert rows[0]["chat_key"] == "test_acc:Alice"
    assert rows[0]["reply_text"] == "hi～"
    assert rows[0]["deferred_until"] == pytest.approx(until, abs=0.01)
    assert rows[0]["defer_reason"].startswith("rate_limit:quiet_hours")


def test_enqueue_deferred_empty_reply_raises(store):
    with pytest.raises(ValueError):
        store.enqueue_deferred(
            chat_key="acc:Alice", chat_name="Alice",
            peer_text="hey", peer_kind="text", reply_text="   ",
            defer_until=time.time() + 60,
        )


def test_enqueue_deferred_supersedes_old_for_same_chat(store):
    """同 chat 第二次 enqueue 时，第一条自动 expired"""
    rid1 = store.enqueue_deferred(
        chat_key="acc:Alice", chat_name="Alice",
        peer_text="msg1", peer_kind="text", reply_text="reply1",
        defer_until=time.time() + 60,
    )
    rid2 = store.enqueue_deferred(
        chat_key="acc:Alice", chat_name="Alice",
        peer_text="msg2", peer_kind="text", reply_text="reply2",
        defer_until=time.time() + 120,
    )
    assert rid2 > rid1
    deferred = store.list_approvals(status="deferred", limit=10)
    assert len(deferred) == 1
    assert deferred[0]["id"] == rid2
    expired = store.list_approvals(status="expired", limit=10)
    assert len(expired) >= 1
    assert any(r["id"] == rid1 for r in expired)


def test_drain_due_deferred_only_returns_due(store):
    now = time.time()
    rid_past = store.enqueue_deferred(
        chat_key="acc:A", chat_name="A", peer_text="p",
        peer_kind="text", reply_text="r1",
        defer_until=now - 10,  # 已过期
    )
    rid_future = store.enqueue_deferred(
        chat_key="acc:B", chat_name="B", peer_text="p",
        peer_kind="text", reply_text="r2",
        defer_until=now + 600,  # 未来
    )
    due = store.drain_due_deferred(now_ts=now, limit=20)
    ids = [r["id"] for r in due]
    assert rid_past in ids
    assert rid_future not in ids


def test_drain_due_ordered_by_until(store):
    now = time.time()
    r1 = store.enqueue_deferred(
        chat_key="acc:A", chat_name="A", peer_text="p",
        peer_kind="text", reply_text="r1",
        defer_until=now - 5,
    )
    # 不同 chat 才会保留两条（同 chat 会被 supersede）
    r2 = store.enqueue_deferred(
        chat_key="acc:B", chat_name="B", peer_text="p",
        peer_kind="text", reply_text="r2",
        defer_until=now - 10,  # 更早过期
    )
    due = store.drain_due_deferred(now_ts=now)
    ids = [r["id"] for r in due]
    # 更早过期的排前面
    assert ids.index(r2) < ids.index(r1)


def test_mark_deferred_sent(store):
    rid = store.enqueue_deferred(
        chat_key="acc:A", chat_name="A", peer_text="p",
        peer_kind="text", reply_text="r1",
        defer_until=time.time() - 5,
    )
    store.mark_deferred_sent(rid)
    deferred_left = store.list_approvals(status="deferred", limit=10)
    assert len(deferred_left) == 0
    sent = store.list_approvals(status="sent", limit=10)
    assert any(r["id"] == rid for r in sent)


def test_mark_deferred_failed(store):
    rid = store.enqueue_deferred(
        chat_key="acc:A", chat_name="A", peer_text="p",
        peer_kind="text", reply_text="r1",
        defer_until=time.time() - 5,
    )
    store.mark_deferred_failed(rid, "device_unreachable")
    failed = store.list_approvals(status="failed", limit=10)
    assert any(r["id"] == rid and "device_unreachable" in r["send_error"] for r in failed)


def test_expire_deferred_for_chat(store):
    rid = store.enqueue_deferred(
        chat_key="acc:Alice", chat_name="Alice",
        peer_text="p", peer_kind="text", reply_text="r1",
        defer_until=time.time() + 600,
    )
    n = store.expire_deferred_for_chat("acc:Alice", reason="superseded_by_new_send")
    assert n == 1
    deferred_left = store.list_approvals(status="deferred", limit=10)
    assert all(r["chat_key"] != "acc:Alice" for r in deferred_left)


def test_expire_other_chats_unaffected(store):
    rid_alice = store.enqueue_deferred(
        chat_key="acc:Alice", chat_name="Alice", peer_text="p",
        peer_kind="text", reply_text="r1",
        defer_until=time.time() + 600,
    )
    rid_bob = store.enqueue_deferred(
        chat_key="acc:Bob", chat_name="Bob", peer_text="p",
        peer_kind="text", reply_text="r2",
        defer_until=time.time() + 600,
    )
    store.expire_deferred_for_chat("acc:Alice")
    deferred = store.list_approvals(status="deferred", limit=10)
    chat_keys = [r["chat_key"] for r in deferred]
    assert "acc:Bob" in chat_keys
    assert "acc:Alice" not in chat_keys


def test_drain_stale_deferred_auto_expire(store):
    """W2-D1 v5：created_at 距 now > staleness_sec 的 deferred 自动 expired"""
    now = time.time()
    # 这条 created_at 是 8 小时前，应该被 stale 化
    rid_old = store.enqueue_deferred(
        chat_key="acc:Alice", chat_name="Alice", peer_text="msg",
        peer_kind="text", reply_text="reply_old",
        defer_until=now - 5,
    )
    # 手动改 created_at 让它过期（模拟存留 8 小时）
    import sqlite3
    c = sqlite3.connect(store._db_path)
    c.execute(
        "UPDATE messenger_rpa_approvals SET created_at=? WHERE id=?",
        (now - 8 * 3600, rid_old),
    )
    c.commit(); c.close()
    # 一条新鲜的（10 分钟前 created）应该被 drain 出来
    rid_fresh = store.enqueue_deferred(
        chat_key="acc:Bob", chat_name="Bob", peer_text="msg",
        peer_kind="text", reply_text="reply_fresh",
        defer_until=now - 5,
    )
    due = store.drain_due_deferred(now_ts=now, staleness_sec=6 * 3600)
    ids = [r["id"] for r in due]
    assert rid_old not in ids
    assert rid_fresh in ids
    # 老的应该是 expired
    expired = store.list_approvals(status="expired", limit=10)
    assert any(r["id"] == rid_old for r in expired)


def test_drain_stale_disabled_by_zero(store):
    """staleness_sec=0 关闭检查，老 deferred 也会被 drain"""
    now = time.time()
    rid = store.enqueue_deferred(
        chat_key="acc:A", chat_name="A", peer_text="m",
        peer_kind="text", reply_text="r",
        defer_until=now - 5,
    )
    import sqlite3
    c = sqlite3.connect(store._db_path)
    c.execute("UPDATE messenger_rpa_approvals SET created_at=? WHERE id=?",
              (now - 24 * 3600, rid))
    c.commit(); c.close()
    due = store.drain_due_deferred(now_ts=now, staleness_sec=0)
    ids = [r["id"] for r in due]
    assert rid in ids


def test_pending_approvals_unaffected_by_deferred_supersede(store):
    """pending 状态的 approval 不应被 deferred enqueue 覆盖"""
    pid = store.enqueue_approval(
        chat_key="acc:A", chat_name="A", peer_text="p",
        peer_kind="text", reply_text="manual_pending",
    )
    # 同 chat enqueue deferred
    did = store.enqueue_deferred(
        chat_key="acc:A", chat_name="A", peer_text="p",
        peer_kind="text", reply_text="auto_deferred",
        defer_until=time.time() + 60,
    )
    pending = store.list_approvals(status="pending", limit=10)
    deferred = store.list_approvals(status="deferred", limit=10)
    assert any(r["id"] == pid for r in pending)
    assert any(r["id"] == did for r in deferred)
