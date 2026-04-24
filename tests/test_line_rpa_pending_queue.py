"""P4-3：审核队列 state_store CRUD + find_chat_row_by_name 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.integrations.line_rpa.chat_list_scanner import find_chat_row_by_name
from src.integrations.line_rpa.state_store import LineRpaStateStore


@pytest.fixture
def store(tmp_path: Path) -> LineRpaStateStore:
    db = tmp_path / "line.db"
    return LineRpaStateStore(db, max_runs_kept=200)


def test_insert_and_list_pending(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="line_rpa:name:Alice",
        chat_name="Alice",
        peer_text="你好",
        draft_reply="Hi Alice",
    )
    assert pid > 0
    items = store.list_pending(status="pending")
    assert len(items) == 1
    it = items[0]
    assert it["chat_name"] == "Alice"
    assert it["draft_reply"] == "Hi Alice"
    assert it["final_reply"] == "Hi Alice"  # 初始 final = draft
    assert it["status"] == "pending"
    assert it["send_attempts"] == 0


def test_resolve_pending_approve(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="c1", chat_name="Bob", peer_text="在吗", draft_reply="在的",
    )
    row = store.resolve_pending(pid, action="approve", by="admin")
    assert row is not None
    assert row["status"] == "approved"
    assert row["final_reply"] == "在的"
    assert row["resolved_by"] == "admin"
    assert row["resolved_at"] > 0


def test_resolve_pending_edit_approve_changes_text(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="c1", chat_name="Bob", peer_text="x", draft_reply="原稿",
    )
    row = store.resolve_pending(
        pid, action="edit_approve", final_reply="改后文本", by="me",
    )
    assert row is not None
    assert row["status"] == "approved"
    assert row["final_reply"] == "改后文本"


def test_resolve_pending_reject_then_can_still_cancel(store: LineRpaStateStore):
    # 拒绝后仍允许改状态（cancel）以匹配 resolve_pending 的语义：
    # "pending|rejected" 都还能被进一步变更
    pid = store.insert_pending(
        chat_key="c1", chat_name="Bob", peer_text="x", draft_reply="draft",
    )
    r1 = store.resolve_pending(pid, action="reject")
    assert r1 is not None and r1["status"] == "rejected"
    r2 = store.resolve_pending(pid, action="cancel")
    assert r2 is not None and r2["status"] == "cancelled"


def test_resolve_pending_sent_is_frozen(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="c1", chat_name="Bob", peer_text="x", draft_reply="d",
    )
    store.resolve_pending(pid, action="approve")
    store.mark_pending_sent(pid)
    row = store.get_pending(pid)
    assert row is not None and row["status"] == "sent"
    # 已发的不允许再改；我们的实现返回原行
    row2 = store.resolve_pending(pid, action="cancel")
    assert row2 is not None and row2["status"] == "sent"  # 不变


def test_resolve_unknown_action_returns_none(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="c1", chat_name="Bob", peer_text="x", draft_reply="d",
    )
    assert store.resolve_pending(pid, action="explode") is None


def test_resolve_pending_missing_id(store: LineRpaStateStore):
    assert store.resolve_pending(999999, action="approve") is None


def test_pending_stats_counts(store: LineRpaStateStore):
    ids = [
        store.insert_pending(chat_key=f"c{i}", chat_name=f"N{i}", peer_text="x", draft_reply="d")
        for i in range(5)
    ]
    store.resolve_pending(ids[0], action="approve")
    store.resolve_pending(ids[1], action="reject")
    store.resolve_pending(ids[2], action="approve")
    store.mark_pending_sent(ids[2])
    stats = store.pending_stats()
    assert stats["pending"] == 2
    assert stats["approved"] == 1
    assert stats["rejected"] == 1
    assert stats["sent"] == 1


def test_mark_pending_sent_with_error(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="c1", chat_name="Bob", peer_text="x", draft_reply="d",
    )
    store.resolve_pending(pid, action="approve")
    store.mark_pending_sent(pid, error="chat_not_found")
    row = store.get_pending(pid)
    assert row["status"] == "error"
    assert row["last_error"] == "chat_not_found"
    assert row["send_attempts"] == 1


# ── find_chat_row_by_name ─────────────────────────────────

def _list_xml(rows):
    """rows = [(name, top, bottom)]"""
    nodes = []
    for i, (name, t, b) in enumerate(rows):
        nodes.append(
            f'<node index="{i}" text="{name}" resource-id="jp.naver.line.android:id/chat_name" '
            f'class="android.widget.TextView" bounds="[60,{t}][500,{b}]"/>'
        )
    inner = "\n".join(nodes)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<hierarchy rotation="0">'
        f'<node class="android.widget.FrameLayout" bounds="[0,0][1080,2340]">{inner}</node>'
        "</hierarchy>"
    ).encode("utf-8")


def test_find_chat_row_by_name_exact_match():
    xml = _list_xml([("Alice", 300, 380), ("Bob", 500, 580), ("Charlie", 700, 780)])
    row = find_chat_row_by_name(xml, "Bob")
    assert row is not None
    assert row.name == "Bob"
    assert row.source == "name_match"
    assert 500 <= row.tap_y <= 580


def test_find_chat_row_by_name_prefix_match():
    xml = _list_xml([("Alice 王", 300, 380), ("Bob 李", 500, 580)])
    row = find_chat_row_by_name(xml, "Alice")
    assert row is not None
    assert row.name.startswith("Alice")


def test_find_chat_row_by_name_no_match_returns_none():
    xml = _list_xml([("Alice", 300, 380)])
    assert find_chat_row_by_name(xml, "Zoe") is None


def test_find_chat_row_by_name_exact_beats_prefix():
    xml = _list_xml([("Alice 同名扩展", 300, 380), ("Alice", 500, 580)])
    row = find_chat_row_by_name(xml, "Alice")
    # 精确匹配分数 100 > 前缀 70，应选第二行
    assert row is not None
    assert row.name == "Alice"
    assert 500 <= row.tap_y <= 580


def test_find_chat_row_by_name_empty_target():
    xml = _list_xml([("Alice", 300, 380)])
    assert find_chat_row_by_name(xml, "") is None


def test_find_chat_row_by_name_broken_xml():
    assert find_chat_row_by_name(b"not xml", "Anything") is None
