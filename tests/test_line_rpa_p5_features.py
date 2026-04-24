"""P5 阶段：stale_check / TTL / 审计日志 / 时间轴 / 多类告警单元测试。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.integrations.line_rpa.state_store import LineRpaStateStore


@pytest.fixture
def store(tmp_path: Path) -> LineRpaStateStore:
    return LineRpaStateStore(tmp_path / "line.db", max_runs_kept=200)


# ── P5-1 ────────────────────────────────────────
def test_compute_peer_hash_stable(store: LineRpaStateStore):
    h1 = store.compute_peer_hash("你好")
    h2 = store.compute_peer_hash("你好")
    h3 = store.compute_peer_hash("你好 ")  # 尾空格 strip 保持一致
    assert h1 == h2 == h3
    assert store.compute_peer_hash("") == ""
    assert h1 != store.compute_peer_hash("您好")
    assert len(h1) == 16


def test_insert_pending_with_peer_hash(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="c1", chat_name="Alice",
        peer_text="今天天气如何", draft_reply="晴",
    )
    row = store.get_pending(pid)
    assert row is not None
    assert row["peer_hash"] == store.compute_peer_hash("今天天气如何")


def test_cancel_pending_with_reason_audits(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="c1", chat_name="Bob", peer_text="在吗", draft_reply="在",
    )
    row = store.cancel_pending_with_reason(pid, reason="stale_peer", by="auto:stale")
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["last_error"] == "stale_peer"
    audits = store.list_audit(target_type="pending")
    assert any(a["action"] == "auto_cancel" and a["target_id"] == pid for a in audits)


def test_sweep_stale_pending_ttl(store: LineRpaStateStore):
    # 手动插入两条 + 把其中一条 ts 回拨超过 TTL
    pid_old = store.insert_pending(
        chat_key="c1", chat_name="Old", peer_text="a", draft_reply="b",
    )
    store._conn.execute(  # noqa: SLF001
        "UPDATE line_rpa_pending SET ts=? WHERE id=?",
        (time.time() - 10 * 3600, pid_old),
    )
    store._conn.commit()  # noqa: SLF001
    pid_new = store.insert_pending(
        chat_key="c2", chat_name="Fresh", peer_text="x", draft_reply="y",
    )
    # TTL = 1h：只有 old 过期
    expired = store.sweep_stale_pending(ttl_sec=3600)
    assert pid_old in expired
    assert pid_new not in expired
    old = store.get_pending(pid_old)
    new = store.get_pending(pid_new)
    assert old and old["status"] == "cancelled" and old["last_error"] == "ttl_expired"
    assert new and new["status"] == "pending"
    # 审计应有 auto_cancel
    audits = store.list_audit(target_type="pending")
    assert any(a["action"] == "auto_cancel" and a["target_id"] == pid_old for a in audits)


def test_sweep_stale_pending_disabled_when_ttl_zero(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="c", chat_name="X", peer_text="a", draft_reply="b",
    )
    store._conn.execute(  # noqa: SLF001
        "UPDATE line_rpa_pending SET ts=? WHERE id=?",
        (time.time() - 48 * 3600, pid),
    )
    store._conn.commit()  # noqa: SLF001
    assert store.sweep_stale_pending(ttl_sec=0) == []
    assert store.get_pending(pid)["status"] == "pending"


# ── P5-5：审计日志 ─────────────────────────────
def test_resolve_pending_emits_audit(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="c1", chat_name="Carol", peer_text="hi", draft_reply="draft",
    )
    store.resolve_pending(pid, action="edit_approve", final_reply="edited reply", by="alice")
    audits = store.list_audit(target_type="pending")
    assert len(audits) == 1
    a = audits[0]
    assert a["action"] == "edit_approve"
    assert a["actor"] == "alice"
    assert a["after_status"] == "approved"
    assert "edited reply" in a["note"]


def test_ack_alert_emits_audit(store: LineRpaStateStore):
    aid = store.insert_alert(kind="x", severity="warning", message="m", detail={})
    store.ack_alert(aid, by="bob")
    audits = store.list_audit(target_type="alert")
    assert any(a["action"] == "ack" and a["target_id"] == aid for a in audits)
    # 再次 ack 同一条不应产生新审计
    store.ack_alert(aid, by="bob")
    audits2 = store.list_audit(target_type="alert")
    assert len(audits2) == len(audits)


def test_ack_all_alerts_emits_audit(store: LineRpaStateStore):
    store.insert_alert(kind="x", severity="warning", message="a", detail={})
    store.insert_alert(
        kind="y", severity="warning", message="b", detail={},
        dedup_window_sec=0,
    )
    n = store.ack_all_alerts(by="root")
    assert n >= 1
    audits = store.list_audit(target_type="alert")
    assert any(a["action"] == "ack_all" for a in audits)


def test_list_audit_filter_by_type(store: LineRpaStateStore):
    pid = store.insert_pending(
        chat_key="c", chat_name="N", peer_text="a", draft_reply="b",
    )
    store.resolve_pending(pid, action="approve", by="u")
    aid = store.insert_alert(kind="k", severity="warning", message="m", detail={})
    store.ack_alert(aid, by="u")
    only_pending = store.list_audit(target_type="pending")
    only_alert = store.list_audit(target_type="alert")
    assert all(a["target_type"] == "pending" for a in only_pending)
    assert all(a["target_type"] == "alert" for a in only_alert)
    all_ = store.list_audit()
    assert len(all_) == len(only_pending) + len(only_alert)


# ── P5-3：时间轴 ───────────────────────────────
def test_timeline_merges_three_sources(store: LineRpaStateStore):
    # runs
    store.record_run(
        chat_key="c1", ok=True, step="sent",
        peer_text="hi", reply_text="re", reader_path="",
        total_ms=123.0,
    )
    # pending 入队 + 立即 reject
    pid = store.insert_pending(
        chat_key="c", chat_name="Z", peer_text="p", draft_reply="d",
    )
    store.resolve_pending(pid, action="reject", by="alice")
    # alert
    store.insert_alert(kind="send_fail_streak", severity="warning",
                       message="m", detail={})

    items = store.timeline(minutes=60)
    types = {i["type"] for i in items}
    assert types == {"run", "pending", "alert"}
    # pending 应同时有 created + rejected 两条
    pending_kinds = {i["kind"] for i in items if i["type"] == "pending"}
    assert "pending_created" in pending_kinds
    assert "pending_rejected" in pending_kinds
    # 排序：按 ts 倒序
    ts_list = [i["ts"] for i in items]
    assert ts_list == sorted(ts_list, reverse=True)


def test_timeline_respects_window(store: LineRpaStateStore):
    old_alert = store.insert_alert(
        kind="old", severity="warning", message="old", detail={},
    )
    store._conn.execute(  # noqa: SLF001
        "UPDATE line_rpa_alerts SET ts=? WHERE id=?",
        (time.time() - 2 * 3600, old_alert),
    )
    store._conn.commit()  # noqa: SLF001
    store.insert_alert(
        kind="fresh", severity="warning", message="fresh", detail={},
        dedup_window_sec=0,
    )
    items = store.timeline(minutes=60)
    kinds = {i["kind"] for i in items if i["type"] == "alert"}
    assert "fresh" in kinds
    assert "old" not in kinds


# ── P5-2：多类告警（state_store 侧校验 insert_alert 可用；service 侧用 service 测试） ──
def test_insert_alert_dedup_by_kind_window(store: LineRpaStateStore):
    a1 = store.insert_alert(
        kind="send_fail_streak", severity="warning",
        message="m1", detail={"streak": 3}, dedup_window_sec=60,
    )
    a2 = store.insert_alert(
        kind="send_fail_streak", severity="warning",
        message="m2", detail={"streak": 4}, dedup_window_sec=60,
    )
    assert a1 > 0
    # 窗口内同 kind 的应被去重（返回 0 或同一 id，具体看实现，但不应产出独立新告警）
    items = store.list_alerts(only_unacked=True, limit=20)
    streak_items = [i for i in items if i["kind"] == "send_fail_streak"]
    assert len(streak_items) == 1
    # 不同 kind 不受影响
    a3 = store.insert_alert(
        kind="adb_lost", severity="critical",
        message="no adb", detail={}, dedup_window_sec=60,
    )
    assert a3 > 0
    assert len(store.list_alerts(only_unacked=True, limit=20)) == 2
    _ = a2  # 标记使用
