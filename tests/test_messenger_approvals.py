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


# ───────────────── list_approvals / get_approval ─────────────────


def _seed(store: MessengerRpaStateStore) -> dict:
    """建 3 条审批：normal pending / empty pending(escalation) / normal approved。"""
    ids = {}
    ids["n1"] = store.enqueue_approval(
        chat_key="ck:a", chat_name="A",
        peer_text="q1", peer_kind="text", reply_text="r1",
    )
    ids["esc"] = store.enqueue_approval(
        chat_key="ck:b", chat_name="B",
        peer_text="q2", peer_kind="text", reply_text="",
        allow_empty_reply=True,
        extra={"escalation": True},
    )
    ids["n2"] = store.enqueue_approval(
        chat_key="ck:a", chat_name="A",
        peer_text="q3", peer_kind="text", reply_text="r3",
    )
    # 把 n2 decided 掉，留 pending 只剩 n1 + esc
    assert store.decide_approval(ids["n2"], approve=True) is True
    return ids


def test_list_approvals_filter_status(store: MessengerRpaStateStore) -> None:
    ids = _seed(store)
    pending_ids = {r["id"] for r in store.list_approvals(status="pending")}
    assert pending_ids == {ids["n1"], ids["esc"]}
    approved_ids = {r["id"] for r in store.list_approvals(status="approved")}
    assert approved_ids == {ids["n2"]}


def test_list_approvals_filter_chat_key(store: MessengerRpaStateStore) -> None:
    ids = _seed(store)
    rows = store.list_approvals(chat_key="ck:a")
    assert {r["id"] for r in rows} == {ids["n1"], ids["n2"]}


def test_list_approvals_reply_text_empty_filter(
    store: MessengerRpaStateStore,
) -> None:
    """新增 reply_text_empty 过滤：True 仅 escalation 占位行，False 仅实体草稿。"""
    ids = _seed(store)
    empty_pending = store.list_approvals(
        status="pending", reply_text_empty=True
    )
    assert {r["id"] for r in empty_pending} == {ids["esc"]}

    nonempty_pending = store.list_approvals(
        status="pending", reply_text_empty=False
    )
    assert {r["id"] for r in nonempty_pending} == {ids["n1"]}

    all_pending = store.list_approvals(
        status="pending", reply_text_empty=None
    )
    assert {r["id"] for r in all_pending} == {ids["n1"], ids["esc"]}


def test_list_approvals_combined_filters(store: MessengerRpaStateStore) -> None:
    """chat_key + reply_text_empty 组合可精确定位单个 chat 的 escalation 占位行。"""
    ids = _seed(store)
    rows = store.list_approvals(chat_key="ck:b", reply_text_empty=True)
    assert {r["id"] for r in rows} == {ids["esc"]}
    rows_none = store.list_approvals(chat_key="ck:a", reply_text_empty=True)
    assert rows_none == []


# ───────────────── count_approvals ─────────────────


def test_count_approvals_empty_store(store: MessengerRpaStateStore) -> None:
    assert store.count_approvals() == 0
    assert store.count_approvals(status="pending") == 0
    assert store.count_approvals(reply_text_empty=True) == 0


def test_count_approvals_matches_list_approvals(
    store: MessengerRpaStateStore,
) -> None:
    """count 与 list 在相同过滤条件下数字一致（不依赖 limit）。"""
    _seed(store)  # 2 pending (1 empty + 1 nonempty) + 1 approved
    assert store.count_approvals() == 3
    assert store.count_approvals(status="pending") == 2
    assert store.count_approvals(status="approved") == 1
    assert store.count_approvals(status="rejected") == 0


def test_count_approvals_reply_text_empty_filter(
    store: MessengerRpaStateStore,
) -> None:
    """这是 /status pending_empty_count 监控字段的数据来源。"""
    _seed(store)
    assert store.count_approvals(
        status="pending", reply_text_empty=True
    ) == 1
    assert store.count_approvals(
        status="pending", reply_text_empty=False
    ) == 1
    assert store.count_approvals(
        status="pending", reply_text_empty=None
    ) == 2


def test_count_approvals_ignores_list_limit(
    store: MessengerRpaStateStore,
) -> None:
    """list_approvals 有 limit 截断；count 必须返回完整总数，不受 limit 影响。"""
    for i in range(60):
        store.enqueue_approval(
            chat_key=f"ck:{i}", chat_name=f"C{i}",
            peer_text="q", peer_kind="text", reply_text="r",
        )
    # list_approvals 默认 limit=50
    assert len(store.list_approvals(status="pending")) == 50
    # count 应返回全部 60
    assert store.count_approvals(status="pending") == 60


def test_count_approvals_combined_filters(
    store: MessengerRpaStateStore,
) -> None:
    ids = _seed(store)
    # ck:a 有 n1 (pending) + n2 (approved)
    assert store.count_approvals(chat_key="ck:a") == 2
    assert store.count_approvals(
        chat_key="ck:a", status="pending"
    ) == 1
    # ck:b 只有 1 个 escalation 占位行
    assert store.count_approvals(
        chat_key="ck:b", status="pending", reply_text_empty=True
    ) == 1
    del ids  # 仅用来建数据


def test_get_approval_existing_and_missing(
    store: MessengerRpaStateStore,
) -> None:
    aid = store.enqueue_approval(
        chat_key="ck:x", chat_name="X",
        peer_text="q", peer_kind="text", reply_text="r",
    )
    row = store.get_approval(aid)
    assert row is not None
    assert row["reply_text"] == "r"
    assert store.get_approval(999_999) is None


# ───────────────── update_approval_reply ─────────────────


def test_update_approval_reply_on_pending_succeeds(
    store: MessengerRpaStateStore,
) -> None:
    aid = store.enqueue_approval(
        chat_key="ck", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="",
        allow_empty_reply=True,
    )
    ok = store.update_approval_reply(aid, reply_text="human filled")
    assert ok is True
    row = store.get_approval(aid)
    assert row is not None
    assert row["reply_text"] == "human filled"
    assert row["status"] == "pending"  # 只改文案不改状态


def test_update_approval_reply_on_non_pending_noop(
    store: MessengerRpaStateStore,
) -> None:
    """approved/rejected 状态下 update_approval_reply 返回 False 不修改。"""
    aid = store.enqueue_approval(
        chat_key="ck", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="r",
    )
    assert store.decide_approval(aid, approve=True) is True
    ok = store.update_approval_reply(aid, reply_text="attempt overwrite")
    assert ok is False
    row = store.get_approval(aid)
    assert row["reply_text"] == "r"  # 原文案不变


def test_update_approval_reply_on_missing_id_noop(
    store: MessengerRpaStateStore,
) -> None:
    assert store.update_approval_reply(999_999, reply_text="x") is False


# ───────────────── decide_approval ─────────────────


def test_decide_approval_approve_and_reject(
    store: MessengerRpaStateStore,
) -> None:
    a1 = store.enqueue_approval(
        chat_key="c1", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="r1",
    )
    a2 = store.enqueue_approval(
        chat_key="c2", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="r2",
    )
    assert store.decide_approval(a1, approve=True, decided_by="admin") is True
    assert store.decide_approval(a2, approve=False, decision_note="spam") is True

    r1 = store.get_approval(a1)
    assert r1["status"] == "approved"
    assert r1["decided_by"] == "admin"
    assert r1["decided_at"] > 0

    r2 = store.get_approval(a2)
    assert r2["status"] == "rejected"
    assert r2["decision_note"] == "spam"


def test_decide_approval_override_applies_only_on_approve(
    store: MessengerRpaStateStore,
) -> None:
    """reply_text_override：approve 时覆盖，reject 时忽略保留原文。"""
    a1 = store.enqueue_approval(
        chat_key="c1", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="orig",
    )
    a2 = store.enqueue_approval(
        chat_key="c2", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="orig",
    )
    store.decide_approval(a1, approve=True, reply_text_override="edited")
    store.decide_approval(a2, approve=False, reply_text_override="ignored")

    assert store.get_approval(a1)["reply_text"] == "edited"
    assert store.get_approval(a2)["reply_text"] == "orig"


def test_decide_approval_non_pending_returns_false(
    store: MessengerRpaStateStore,
) -> None:
    """已 decided 的 approval 不能再次 decide。"""
    aid = store.enqueue_approval(
        chat_key="c", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="r",
    )
    assert store.decide_approval(aid, approve=True) is True
    assert store.decide_approval(aid, approve=False) is False
    row = store.get_approval(aid)
    assert row["status"] == "approved"  # 状态未被二次覆盖


def test_decide_approval_missing_id_returns_false(
    store: MessengerRpaStateStore,
) -> None:
    assert store.decide_approval(999_999, approve=True) is False


# ───────────────── escalation 端到端流程 ─────────────────


def test_escalation_full_workflow_enqueue_update_decide(
    store: MessengerRpaStateStore,
) -> None:
    """PR #6 启用的 escalation 工作流：入队空行 → 人工填文案 → 批准发送。"""
    aid = store.enqueue_approval(
        chat_key="ck:esc", chat_name="EscChat",
        peer_text="urgent", peer_kind="text", reply_text="",
        allow_empty_reply=True,
        extra={"escalation": True, "escalation_reason": "keyword:人工"},
    )
    # 观测：escalation 占位行能被 reply_text_empty=True 过滤器定位
    placeholders = store.list_approvals(
        status="pending", reply_text_empty=True
    )
    assert [r["id"] for r in placeholders] == [aid]

    # 人工 Suggest More → update_approval_reply 回填
    assert store.update_approval_reply(aid, reply_text="人工回复草稿") is True

    # 回填后不再是空占位，但仍 pending
    assert store.list_approvals(
        status="pending", reply_text_empty=True
    ) == []
    assert len(store.list_approvals(
        status="pending", reply_text_empty=False
    )) == 1

    # 人工批准（可带 override 再改一次文案）
    assert store.decide_approval(
        aid, approve=True, decided_by="ops",
        reply_text_override="最终版本",
    ) is True

    final = store.get_approval(aid)
    assert final["status"] == "approved"
    assert final["reply_text"] == "最终版本"
    assert final["decided_by"] == "ops"
