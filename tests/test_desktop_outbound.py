"""桌面「受控出站队列」(D4) 单测。

锁定不变式：
  - enqueue 前必过闸门——闸门拦截则**根本不入队**（不可被旁路）
  - 空文本 / 缺主键直接拒
  - pull 认领 pending→claimed（attempts+1），按 id 升序，按账号隔离
  - ack claimed→sent/failed
  - 超时未 ack 的 claimed 被 pull 自动回收为 pending（防客户端崩溃卡死）
  - pending_count / summary 可观测
"""

from __future__ import annotations

from src.inbox.desktop_outbound import (
    DesktopOutboundQueue, corrections_to_export,
)


def _allow(platform, account_id, *, config=None, registry=None):
    return False, ""


def _block(platform, account_id, *, config=None, registry=None):
    return True, "kill_switch:global"


def _q() -> DesktopOutboundQueue:
    return DesktopOutboundQueue(":memory:")


# ── 闸门不变式 ──────────────────────────────────────────────────────
def test_enqueue_blocked_does_not_persist():
    q = _q()
    r = q.enqueue("instagram", "ig1", "c1", "你好", guard=_block)
    assert r["enqueued"] is False
    assert r["blocked"] == "kill_switch:global"
    # 被拦截 → 队列里一条都没有
    assert q.pending_count() == 0
    assert q.pull("instagram", "ig1") == []


def test_enqueue_allowed_persists_pending():
    q = _q()
    r = q.enqueue("instagram", "ig1", "c1", "你好", guard=_allow)
    assert r["enqueued"] is True
    assert r["status"] == "pending"
    assert r["id"] > 0
    assert q.pending_count("instagram", "ig1") == 1


def test_default_guard_allows_without_killswitch():
    # 不注入 guard → 走默认 send_blocked；无 kill-switch/gate 时应放行
    q = _q()
    r = q.enqueue("instagram", "ig1", "c1", "hello")
    assert r["enqueued"] is True


def test_empty_text_and_missing_key_rejected():
    q = _q()
    assert q.enqueue("instagram", "ig1", "c1", "   ", guard=_allow)["blocked"] == "empty_text"
    assert q.enqueue("", "ig1", "c1", "hi", guard=_allow)["blocked"] == "missing_key"
    assert q.enqueue("instagram", "", "c1", "hi", guard=_allow)["blocked"] == "missing_key"
    assert q.enqueue("instagram", "ig1", "", "hi", guard=_allow)["blocked"] == "missing_key"
    assert q.pending_count() == 0


# ── 生命周期 ────────────────────────────────────────────────────────
def test_pull_claims_in_order_and_increments_attempts():
    q = _q()
    q.enqueue("instagram", "ig1", "c1", "一", guard=_allow)
    q.enqueue("instagram", "ig1", "c1", "二", guard=_allow)
    items = q.pull("instagram", "ig1", limit=10)
    assert [it["text"] for it in items] == ["一", "二"]
    assert all(it["attempts"] == 1 for it in items)
    # 已认领 → 再 pull 取不到（仍算 pending_count，因 claimed 计入在途）
    assert q.pull("instagram", "ig1") == []
    assert q.pending_count("instagram", "ig1") == 2


def test_pull_isolated_by_account():
    q = _q()
    q.enqueue("instagram", "ig1", "c1", "给1", guard=_allow)
    q.enqueue("instagram", "ig2", "c1", "给2", guard=_allow)
    items = q.pull("instagram", "ig1")
    assert [it["text"] for it in items] == ["给1"]


def test_ack_marks_sent_and_clears_pending():
    q = _q()
    q.enqueue("instagram", "ig1", "c1", "你好", guard=_allow)
    item = q.pull("instagram", "ig1")[0]
    assert q.ack(item["id"], ok=True) is True
    assert q.pending_count("instagram", "ig1") == 0
    assert q.summary().get("sent") == 1


def test_ack_failed_records_reason():
    q = _q()
    q.enqueue("instagram", "ig1", "c1", "你好", guard=_allow)
    item = q.pull("instagram", "ig1")[0]
    assert q.ack(item["id"], ok=False, error="composer 失配") is True
    assert q.summary().get("failed") == 1
    assert q.pending_count("instagram", "ig1") == 0


def test_ack_unknown_id_returns_false():
    q = _q()
    assert q.ack(999999, ok=True) is False


# ── 超时回收 ────────────────────────────────────────────────────────
def test_stale_claimed_is_reclaimed_on_pull():
    q = _q()
    q.enqueue("instagram", "ig1", "c1", "你好", guard=_allow, now=1000.0)
    # 在 t=1000 认领
    q.pull("instagram", "ig1", now=1000.0)
    assert q.pull("instagram", "ig1", now=1000.0) == []  # 已 claimed 取不到
    # 远超回收阈值后再 pull → 自动回收为 pending 并重新认领
    again = q.pull("instagram", "ig1", now=1000.0 + 10_000.0)
    assert len(again) == 1
    assert again[0]["attempts"] == 2  # 第二次认领


def test_pull_filters_by_chat_key():
    q = _q()
    q.enqueue("instagram", "ig1", "cA", "给A", guard=_allow)
    q.enqueue("instagram", "ig1", "cB", "给B", guard=_allow)
    # 只拉当前打开会话 cA → 只认领 cA，cB 留队列（不发错聊天、不丢）
    got = q.pull("instagram", "ig1", chat_key="cA")
    assert [it["text"] for it in got] == ["给A"]
    assert q.pending_count("instagram", "ig1") == 2  # cB 仍在途/待发
    # 切到 cB 才认领 cB
    got2 = q.pull("instagram", "ig1", chat_key="cB")
    assert [it["text"] for it in got2] == ["给B"]


def test_pull_without_chat_key_takes_all():
    q = _q()
    q.enqueue("instagram", "ig1", "cA", "给A", guard=_allow)
    q.enqueue("instagram", "ig1", "cB", "给B", guard=_allow)
    got = q.pull("instagram", "ig1")  # 不限会话 → 全取
    assert {it["text"] for it in got} == {"给A", "给B"}


def test_summary_counts_by_status():
    q = _q()
    q.enqueue("instagram", "ig1", "c1", "a", guard=_allow)
    q.enqueue("instagram", "ig1", "c1", "b", guard=_allow)
    q.pull("instagram", "ig1")  # 两条 claimed
    s = q.summary()
    assert s.get("claimed") == 2
    assert s.get("total") == 2


# ── P2 人审介入：held / 拦截 / 改写 / 放行 / 暂停 / 重试 ───────────────────────
def test_enqueue_hold_lands_held_not_pulled():
    q = _q()
    r = q.enqueue("instagram", "ig1", "c1", "待审", guard=_allow, hold=True)
    assert r["enqueued"] is True and r["status"] == "held"
    # held 不被 pull 认领（不会自动发）
    assert q.pull("instagram", "ig1") == []
    assert q.summary().get("held") == 1


def test_hold_blocked_still_not_enqueued():
    # 受控不变式：即便 hold，闸门拦截仍根本不入队
    q = _q()
    r = q.enqueue("instagram", "ig1", "c1", "x", guard=_block, hold=True)
    assert r["enqueued"] is False
    assert q.summary().get("total", 0) == 0


def test_release_held_then_pullable():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "待审", guard=_allow, hold=True)["id"]
    assert q.release(rid) is True
    got = q.pull("instagram", "ig1")
    assert [it["text"] for it in got] == ["待审"]


def test_hold_pending_then_not_pulled():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "暂停我", guard=_allow)["id"]
    assert q.hold(rid) is True
    assert q.pull("instagram", "ig1") == []


def test_cancel_intercepts_pending_and_held():
    q = _q()
    p = q.enqueue("instagram", "ig1", "c1", "拦我", guard=_allow)["id"]
    h = q.enqueue("instagram", "ig1", "c1", "拦审", guard=_allow, hold=True)["id"]
    assert q.cancel(p) is True and q.cancel(h) is True
    assert q.pull("instagram", "ig1") == []
    s = q.summary()
    assert s.get("cancelled") == 2 and s.get("pending", 0) == 0


def test_cancel_claimed_or_sent_not_allowed():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "已认领", guard=_allow)["id"]
    q.pull("instagram", "ig1")  # → claimed
    assert q.cancel(rid) is False  # 飞行中不可拦截


def test_edit_pending_and_held_only():
    q = _q()
    p = q.enqueue("instagram", "ig1", "c1", "原文", guard=_allow)["id"]
    assert q.edit(p, "改写后") is True
    assert q.pull("instagram", "ig1")[0]["text"] == "改写后"
    # 空文本拒
    h = q.enqueue("instagram", "ig1", "c1", "原文2", guard=_allow, hold=True)["id"]
    assert q.edit(h, "   ") is False


def test_edit_claimed_rejected():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "原文", guard=_allow)["id"]
    q.pull("instagram", "ig1")  # → claimed
    assert q.edit(rid, "想改飞行中") is False


def test_retry_failed_back_to_pending():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "会失败", guard=_allow)["id"]
    q.pull("instagram", "ig1")
    q.ack(rid, ok=False, error="composer 失配")
    assert q.summary().get("failed") == 1
    assert q.retry(rid) is True
    got = q.pull("instagram", "ig1")
    assert [it["text"] for it in got] == ["会失败"]


def test_transition_unknown_id_returns_false():
    q = _q()
    assert q.hold(999) is False and q.release(999) is False
    assert q.cancel(999) is False and q.retry(999) is False
    assert q.edit(999, "x") is False


# ── P3 待审队列可观测：review_list / intercept_rate ───────────────────────────
def test_review_list_held_fifo():
    q = _q()
    a = q.enqueue("instagram", "ig1", "c1", "先到", guard=_allow, hold=True)["id"]
    b = q.enqueue("instagram", "ig2", "c2", "后到", guard=_allow, hold=True)["id"]
    # 非 held 不应出现在 review_list
    q.enqueue("instagram", "ig1", "c1", "自动发", guard=_allow)
    rev = q.review_list()
    assert [it["id"] for it in rev] == [a, b]  # FIFO（id 升序）
    assert all(it["status"] == "held" for it in rev)


def test_intercept_rate_windowed():
    q = _q()
    # 3 已发 + 1 失败 + 1 拦截 → 拦截率 1/5
    for i in range(3):
        rid = q.enqueue("instagram", "ig1", "c1", "s%d" % i, guard=_allow)["id"]
        q.pull("instagram", "ig1")
        q.ack(rid, ok=True)
    fid = q.enqueue("instagram", "ig1", "c1", "f", guard=_allow)["id"]
    q.pull("instagram", "ig1")
    q.ack(fid, ok=False, error="x")
    cid = q.enqueue("instagram", "ig1", "c1", "c", guard=_allow)["id"]
    q.cancel(cid)
    rate, sample = q.intercept_rate()
    assert sample == 5
    assert abs(rate - 0.2) < 1e-9


def test_intercept_rate_no_sample():
    q = _q()
    q.enqueue("instagram", "ig1", "c1", "待审", guard=_allow, hold=True)  # held 不计入分母
    rate, sample = q.intercept_rate()
    assert sample == 0 and rate == 0.0


# ── P4.3 人审 SLA：最久待审等待秒数 ───────────────────────────────────────────
def test_review_oldest_age_none_when_empty():
    q = _q()
    assert q.review_oldest_age() == 0.0
    # 只有非 held 也应为 0
    q.enqueue("instagram", "ig1", "c1", "自动发", guard=_allow)
    assert q.review_oldest_age() == 0.0


def test_review_oldest_age_uses_oldest_held():
    q = _q()
    q.enqueue("instagram", "ig1", "c1", "先", guard=_allow, hold=True, now=1000.0)
    q.enqueue("instagram", "ig1", "c1", "后", guard=_allow, hold=True, now=1200.0)
    # 以最久的那条（t=1000）计龄
    assert q.review_oldest_age(now=1300.0) == 300.0


def test_review_oldest_age_excludes_released():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "审", guard=_allow, hold=True, now=1000.0)["id"]
    q.release(rid)  # held→pending → 不再计入待审
    assert q.review_oldest_age(now=2000.0) == 0.0


# ── P4.2 人审纠正留痕（AI 失误样本集）─────────────────────────────────────────
def test_edit_records_before_after():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "原始回复", guard=_allow)["id"]
    assert q.edit(rid, "更好的回复") is True
    rows = q.corrections()
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "edit"
    assert r["orig_text"] == "原始回复" and r["new_text"] == "更好的回复"
    assert r["source"] == "human"  # 无 AI 候选 → 纯人改
    assert q.corrections_summary() == {"edit": 1, "total": 1, "ai_assisted": 0}


def test_edit_no_change_no_sample():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "一样", guard=_allow)["id"]
    assert q.edit(rid, "一样") is True  # 文本未变 → 仍成功但不留样本
    assert q.corrections_summary().get("total", 0) == 0


def test_cancel_with_reason_records_sample():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "答非所问", guard=_allow)["id"]
    assert q.cancel(rid, reason="答非所问，应转人工") is True
    rows = q.corrections()
    assert len(rows) == 1 and rows[0]["kind"] == "cancel"
    assert rows[0]["orig_text"] == "答非所问"
    assert rows[0]["reason"] == "答非所问，应转人工"


def test_cancel_without_reason_no_sample():
    # 批量/无理由拦截不污染失误数据集
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "冗余", guard=_allow)["id"]
    assert q.cancel(rid) is True
    assert q.corrections_summary().get("total", 0) == 0


def test_corrections_summary_mixed():
    q = _q()
    e = q.enqueue("instagram", "ig1", "c1", "改我", guard=_allow)["id"]
    q.edit(e, "改后")
    c = q.enqueue("instagram", "ig1", "c1", "拦我", guard=_allow)["id"]
    q.cancel(c, reason="错的")
    s = q.corrections_summary()
    assert s == {"edit": 1, "cancel": 1, "total": 2, "ai_assisted": 0}


def test_clear_resets_corrections():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "x", guard=_allow)["id"]
    q.edit(rid, "y")
    q.clear()
    assert q.corrections() == []
    assert q.corrections_summary() == {"total": 0, "ai_assisted": 0}


# ── P4.4 AI 候选三元组留痕（原草稿→AI候选→人定稿 + source 标注）─────────────────
def test_edit_with_ai_suggestion_adopted():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "原草稿", guard=_allow)["id"]
    # AI 候选被原样采纳
    assert q.edit(rid, "AI候选", ai_suggestion="AI候选", source="ai_adopted") is True
    r = q.corrections()[0]
    assert r["orig_text"] == "原草稿"
    assert r["ai_suggestion"] == "AI候选" and r["new_text"] == "AI候选"
    assert r["source"] == "ai_adopted"
    assert q.corrections_summary()["ai_assisted"] == 1


def test_edit_with_ai_suggestion_edited():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "原草稿", guard=_allow)["id"]
    # AI 候选被人微调后定稿
    assert q.edit(rid, "AI候选+人改", ai_suggestion="AI候选", source="ai_edited") is True
    r = q.corrections()[0]
    assert r["ai_suggestion"] == "AI候选" and r["new_text"] == "AI候选+人改"
    assert r["source"] == "ai_edited"
    assert q.corrections_summary()["ai_assisted"] == 1


def test_corrections_summary_ai_mix():
    q = _q()
    a = q.enqueue("instagram", "ig1", "c1", "d1", guard=_allow)["id"]
    q.edit(a, "纯人改")  # human
    b = q.enqueue("instagram", "ig1", "c1", "d2", guard=_allow)["id"]
    q.edit(b, "采纳", ai_suggestion="采纳", source="ai_adopted")
    s = q.corrections_summary()
    assert s["edit"] == 2 and s["total"] == 2 and s["ai_assisted"] == 1


# ── P5 数据资产导出：过滤 + 偏好对变换 + 去重 ──────────────────────────────────
def test_corrections_filter_by_kind_and_source():
    q = _q()
    a = q.enqueue("instagram", "ig1", "c1", "d1", guard=_allow)["id"]
    q.edit(a, "采纳", ai_suggestion="采纳", source="ai_adopted")
    b = q.enqueue("instagram", "ig1", "c1", "d2", guard=_allow)["id"]
    q.cancel(b, reason="错")
    assert [r["kind"] for r in q.corrections(kind="edit")] == ["edit"]
    assert [r["kind"] for r in q.corrections(kind="cancel")] == ["cancel"]
    assert [r["source"] for r in q.corrections(source="ai_adopted")] == ["ai_adopted"]


def test_corrections_filter_since():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "c1", "d", guard=_allow)["id"]
    q.edit(rid, "改")
    # 未来时间戳 → 过滤掉全部
    assert q.corrections(since=time_far_future()) == []
    # 远古时间戳 → 全部保留
    assert len(q.corrections(since=0.0)) == 1


def time_far_future() -> float:
    import time as _t
    return _t.time() + 86400.0


def test_corrections_to_export_shape():
    items = [{
        "kind": "edit", "source": "ai_edited", "platform": "instagram",
        "orig_text": "原", "new_text": "定稿", "ai_suggestion": "候选",
        "reason": "", "created_at": 123.0,
    }]
    out = corrections_to_export(items)
    assert out == [{
        "kind": "edit", "source": "ai_edited", "platform": "instagram",
        "rejected": "原", "chosen": "定稿", "ai_suggestion": "候选",
        "reason": "", "ts": 123.0,
    }]


def test_corrections_to_export_dedup():
    items = [
        {"kind": "edit", "orig_text": "a", "new_text": "b", "reason": ""},
        {"kind": "edit", "orig_text": "a", "new_text": "b", "reason": ""},  # dup
        {"kind": "edit", "orig_text": "a", "new_text": "c", "reason": ""},
    ]
    assert len(corrections_to_export(items)) == 2
    assert len(corrections_to_export(items, dedup=False)) == 3


def test_corrections_to_export_empty_safe():
    assert corrections_to_export([]) == []
    assert corrections_to_export(None) == []


# ── P7 结构化拦截理由聚类 ─────────────────────────────────────────────────────
def test_reason_breakdown_groups_cancel_reasons():
    q = _q()
    for code in ("off_topic", "off_topic", "factual"):
        rid = q.enqueue("instagram", "ig1", "c1", "x", guard=_allow)["id"]
        q.cancel(rid, reason=code)
    brk = q.corrections_reason_breakdown()
    assert brk == {"off_topic": 2, "factual": 1}


def test_reason_breakdown_excludes_empty_and_edits():
    q = _q()
    # 无理由拦截 → 不计入
    a = q.enqueue("instagram", "ig1", "c1", "x", guard=_allow)["id"]
    q.cancel(a)
    # 改写样本（有 new_text 无 reason）→ 不计入
    b = q.enqueue("instagram", "ig1", "c1", "y", guard=_allow)["id"]
    q.edit(b, "z")
    assert q.corrections_reason_breakdown() == {}


# ── P4.1 单条取用（AI 重写助手取命令上下文）────────────────────────────────────
def test_get_returns_command_fields():
    q = _q()
    rid = q.enqueue("instagram", "ig1", "ck9", "待发", guard=_allow,
                    conversation_id="instagram:ig1:ck9")["id"]
    cmd = q.get(rid)
    assert cmd is not None
    assert cmd["platform"] == "instagram" and cmd["account_id"] == "ig1"
    assert cmd["chat_key"] == "ck9" and cmd["text"] == "待发"
    assert cmd["conversation_id"] == "instagram:ig1:ck9"


def test_get_unknown_returns_none():
    q = _q()
    assert q.get(123456) is None
