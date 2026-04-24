"""MessengerRpaStateStore.enqueue_approval —— allow_empty_reply 契约回归。

覆盖点：
- 正常入队（reply_text 非空）→ 返回 id、行 status=pending、reply_text 正确
- 默认拒绝空 reply_text（防止 auto-reply 路径意外写入空回复）
- 默认拒绝纯空白 reply_text（strip 后为空同样兜住）
- allow_empty_reply=True 合法放行（escalation 分支专用，等人工 Suggest More）
- allow_empty_reply=True 下 chat_key 校验仍然生效（两个 guard 独立）
- extra_json 携带的 escalation 元数据可经 list_approvals / get_approval 读回
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore


@pytest.fixture
def store(tmp_path: Path) -> MessengerRpaStateStore:
    return MessengerRpaStateStore(tmp_path / "msg.db")


def test_enqueue_approval_happy_path(store: MessengerRpaStateStore) -> None:
    aid = store.enqueue_approval(
        chat_key="ck:alice",
        chat_name="Alice",
        peer_text="hi",
        peer_kind="text",
        reply_text="hello",
    )
    assert aid > 0
    items = store.list_approvals(status="pending")
    assert len(items) == 1
    row = items[0]
    assert row["chat_key"] == "ck:alice"
    assert row["reply_text"] == "hello"
    assert row["status"] == "pending"


def test_enqueue_approval_rejects_empty_reply_by_default(
    store: MessengerRpaStateStore,
) -> None:
    with pytest.raises(ValueError, match="reply_text"):
        store.enqueue_approval(
            chat_key="ck:bob",
            chat_name="Bob",
            peer_text="hi",
            peer_kind="text",
            reply_text="",
        )
    assert store.list_approvals(status="pending") == []


def test_enqueue_approval_rejects_whitespace_reply_by_default(
    store: MessengerRpaStateStore,
) -> None:
    with pytest.raises(ValueError, match="reply_text"):
        store.enqueue_approval(
            chat_key="ck:bob",
            chat_name="Bob",
            peer_text="hi",
            peer_kind="text",
            reply_text="   \n\t",
        )


def test_enqueue_approval_allow_empty_reply_opt_in(
    store: MessengerRpaStateStore,
) -> None:
    """escalation 分支调用：reply_text="" 合法入队，等人工 Suggest More。"""
    aid = store.enqueue_approval(
        chat_key="ck:esc",
        chat_name="EscChat",
        peer_text="urgent question",
        peer_kind="text",
        reply_text="",
        allow_empty_reply=True,
        extra={
            "escalation": True,
            "escalation_reason": "keyword:人工",
            "escalation_message": "chat handed off",
        },
        run_id="r1",
    )
    assert aid > 0

    row = store.get_approval(aid)
    assert row is not None
    assert row["reply_text"] == ""
    assert row["status"] == "pending"
    assert row["run_id"] == "r1"
    extra = json.loads(row["extra_json"])
    assert extra["escalation"] is True
    assert extra["escalation_reason"] == "keyword:人工"


def test_enqueue_approval_allow_empty_still_requires_chat_key(
    store: MessengerRpaStateStore,
) -> None:
    """allow_empty_reply 只放松 reply_text 校验，chat_key 必填不变。"""
    with pytest.raises(ValueError, match="chat_key"):
        store.enqueue_approval(
            chat_key="   ",
            chat_name="",
            peer_text="hi",
            peer_kind="text",
            reply_text="",
            allow_empty_reply=True,
        )


def test_enqueue_approval_default_and_optin_coexist(
    store: MessengerRpaStateStore,
) -> None:
    """同一 store 上默认严格 + opt-in 放行两路可共存，不互相污染。"""
    normal_id = store.enqueue_approval(
        chat_key="ck:n", chat_name="N",
        peer_text="x", peer_kind="text", reply_text="draft",
    )
    esc_id = store.enqueue_approval(
        chat_key="ck:e", chat_name="E",
        peer_text="x", peer_kind="text", reply_text="",
        allow_empty_reply=True,
    )
    assert normal_id != esc_id

    with pytest.raises(ValueError):
        store.enqueue_approval(
            chat_key="ck:n2", chat_name="N2",
            peer_text="x", peer_kind="text", reply_text="",
        )

    pending = store.list_approvals(status="pending")
    assert {r["id"] for r in pending} == {normal_id, esc_id}
