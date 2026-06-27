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

from src.inbox.desktop_outbound import DesktopOutboundQueue


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
