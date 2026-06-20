"""质量趋势时序持久化单测。

覆盖：record_snapshot 扁平化落库 + recent 升序/窗口过滤 + prune 保留期 +
QualityTrendSnapshotter.snapshot_once 取值落库 + 空 overview 不写。
"""
from datetime import datetime

from src.monitoring.quality_trend_store import (
    QualityTrendSnapshotter,
    QualityTrendStore,
)

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()


def _overview(care_skip=0, care_like=0, re_skip=0, blacklist=0):
    return {
        "window_sec": 86400,
        "care": {"skipped": care_skip, "dry_run": 1,
                 "feedback": {"like": care_like, "dislike": 0}},
        "reactivation": {"scheduled": 3, "skipped": re_skip, "dry_run": 2,
                         "feedback": {"like": 0, "dislike": 1}},
        "disliked_blacklist_size": blacklist,
    }


def test_record_and_flatten():
    s = QualityTrendStore(":memory:")
    rid = s.record_snapshot(_overview(care_skip=5, care_like=2, blacklist=7), now=NOW)
    assert rid > 0
    pts = s.recent()
    assert len(pts) == 1
    p = pts[0]
    assert p["care_skipped"] == 5
    assert p["care_like"] == 2
    assert p["re_scheduled"] == 3
    assert p["re_dislike"] == 1
    assert p["blacklist_size"] == 7
    # 趋势线只回标量，不外发原始 payload
    assert "payload" not in p


def test_empty_overview_not_recorded():
    s = QualityTrendStore(":memory:")
    assert s.record_snapshot({}, now=NOW) == 0
    assert s.count() == 0


def test_recent_ascending_and_window():
    s = QualityTrendStore(":memory:")
    s.record_snapshot(_overview(care_skip=1), now=NOW - 100000)  # 窗口外
    s.record_snapshot(_overview(care_skip=2), now=NOW - 10)
    s.record_snapshot(_overview(care_skip=3), now=NOW - 5)
    pts = s.recent(since_ts=NOW - 3600)
    # 只取窗口内 2 条，且按时间升序
    assert [p["care_skipped"] for p in pts] == [2, 3]


def test_prune_removes_old():
    s = QualityTrendStore(":memory:")
    s.record_snapshot(_overview(care_skip=1), now=NOW - 100000)
    s.record_snapshot(_overview(care_skip=2), now=NOW - 10)
    removed = s.prune(older_than_sec=3600, now=NOW)
    assert removed == 1
    assert s.count() == 1


def test_latest_includes_payload():
    s = QualityTrendStore(":memory:")
    s.record_snapshot(_overview(care_skip=9), now=NOW)
    latest = s.latest()
    assert latest is not None
    assert latest["care_skipped"] == 9
    assert isinstance(latest["payload"], dict)
    assert latest["payload"]["care"]["skipped"] == 9


def test_snapshotter_snapshot_once():
    s = QualityTrendStore(":memory:")
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        return _overview(care_skip=calls["n"])

    snap = QualityTrendSnapshotter(store=s, overview_fn=_fn, interval_sec=60)
    assert snap.snapshot_once(now=NOW) > 0
    assert snap.snapshot_once(now=NOW + 1) > 0
    pts = s.recent()
    assert [p["care_skipped"] for p in pts] == [1, 2]


def test_snapshotter_handles_overview_fn_error():
    s = QualityTrendStore(":memory:")

    def _boom():
        raise RuntimeError("nope")

    snap = QualityTrendSnapshotter(store=s, overview_fn=_boom, interval_sec=60)
    assert snap.snapshot_once(now=NOW) == 0
    assert s.count() == 0
