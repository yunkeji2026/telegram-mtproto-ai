"""P4-5：告警闭环 state_store CRUD 单元测试。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.integrations.line_rpa.state_store import LineRpaStateStore


@pytest.fixture
def store(tmp_path: Path) -> LineRpaStateStore:
    return LineRpaStateStore(tmp_path / "line.db")


def test_insert_alert_and_list(store: LineRpaStateStore):
    aid = store.insert_alert(
        kind="possibly_missed", severity="warn",
        message="连续 3 次疑似漏读",
        detail={"notif_count": 5, "main_unread": 0, "streak": 3},
    )
    assert aid and aid > 0
    items = store.list_alerts()
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "possibly_missed"
    assert it["message"] == "连续 3 次疑似漏读"
    assert it["detail"]["notif_count"] == 5
    assert it["acknowledged_at"] == 0


def test_insert_alert_dedup_within_window(store: LineRpaStateStore):
    aid1 = store.insert_alert(
        kind="possibly_missed", message="m1", dedup_window_sec=300.0,
    )
    aid2 = store.insert_alert(
        kind="possibly_missed", message="m2", dedup_window_sec=300.0,
    )
    assert aid1 and aid1 > 0
    assert aid2 is None  # 去重命中
    items = store.list_alerts()
    assert len(items) == 1
    assert items[0]["message"] == "m1"


def test_insert_alert_dedup_after_ack(store: LineRpaStateStore):
    aid1 = store.insert_alert(kind="k1", message="a", dedup_window_sec=300.0)
    assert aid1 is not None
    store.ack_alert(aid1, by="tester")
    # ack 后同 kind 可以再插
    aid2 = store.insert_alert(kind="k1", message="b", dedup_window_sec=300.0)
    assert aid2 and aid2 > aid1


def test_insert_alert_different_kinds_not_dedup(store: LineRpaStateStore):
    aid1 = store.insert_alert(kind="k1", message="a")
    aid2 = store.insert_alert(kind="k2", message="b")
    assert aid1 and aid2
    assert aid1 != aid2


def test_list_alerts_only_unacked(store: LineRpaStateStore):
    a1 = store.insert_alert(kind="k1", message="1", dedup_window_sec=0)
    a2 = store.insert_alert(kind="k1", message="2", dedup_window_sec=0)
    a3 = store.insert_alert(kind="k1", message="3", dedup_window_sec=0)
    store.ack_alert(a2, by="tester")
    unacked = store.list_alerts(only_unacked=True)
    assert {a["id"] for a in unacked} == {a1, a3}
    all_ = store.list_alerts(only_unacked=False)
    assert {a["id"] for a in all_} == {a1, a2, a3}


def test_ack_all_alerts(store: LineRpaStateStore):
    for i in range(4):
        store.insert_alert(kind="k", message=str(i), dedup_window_sec=0)
    assert store.alerts_count_unacked() == 4
    acked = store.ack_all_alerts(by="admin")
    assert acked == 4
    assert store.alerts_count_unacked() == 0


def test_ack_unknown_alert(store: LineRpaStateStore):
    assert store.ack_alert(99999) is None


def test_ack_does_not_double_set(store: LineRpaStateStore):
    aid = store.insert_alert(kind="k", message="m")
    r1 = store.ack_alert(aid, by="u1")
    first_at = r1["acknowledged_at"]
    time.sleep(0.01)
    r2 = store.ack_alert(aid, by="u2")
    # 第二次 ack 应该不更新 acknowledged_at（WHERE acknowledged_at=0 过滤）
    assert r2["acknowledged_at"] == first_at
    assert r2["acknowledged_by"] == "u1"


def test_dedup_window_zero_allows_multiple(store: LineRpaStateStore):
    a1 = store.insert_alert(kind="k", message="1", dedup_window_sec=0)
    a2 = store.insert_alert(kind="k", message="2", dedup_window_sec=0)
    assert a1 and a2 and a1 != a2
