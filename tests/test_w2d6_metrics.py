"""W2-D6.1/D6.3/D6.5：metrics 与 reactivation 回复率归因测试。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.contacts.store import ContactStore
from src.monitoring.metrics_store import MetricsStore


# ── D6.5 _bucket_counts ────────────────────────────────

def test_bucket_counts_basic():
    now = time.time()
    items = [
        now - 60,         # 最近一桶
        now - 60,
        now - 60 * 6,     # 第二桶
        now - 60 * 70,    # 太老（> 60 min）
    ]
    buckets = MetricsStore._bucket_counts(items, n_buckets=12, bucket_sec=300)
    assert sum(buckets) == 3  # 第 4 个被 cutoff 排除
    assert buckets[-1] == 2   # 最近 5min 桶里 2 条
    assert len(buckets) == 12


def test_bucket_counts_tuples():
    """支持 (ts, ...) 元组形式"""
    now = time.time()
    items = [(now - 30, "a"), (now - 200, "b")]
    buckets = MetricsStore._bucket_counts(items, n_buckets=12, bucket_sec=60)
    assert sum(buckets) == 2


def test_bucket_counts_empty():
    assert MetricsStore._bucket_counts([], n_buckets=12) == [0] * 12


def test_bucket_counts_future_ts_ignored():
    now = time.time()
    items = [now + 3600]  # 未来时间，应忽略
    assert sum(MetricsStore._bucket_counts(items)) == 0


# ── D6.3 pacing 分布 ──────────────────────────────────

def test_pacing_delay_stats():
    ms = MetricsStore()
    # 注入 100 个延迟样本
    for d in [1, 5, 10, 20, 30, 60, 120, 180]:
        ms.record_pacing_delay(d)
    stats = ms._pacing_delay_stats()
    assert stats["count"] == 8
    assert stats["max_sec"] == 180
    assert 1 <= stats["p50_sec"] <= 180
    assert stats["p95_sec"] >= stats["p50_sec"]


def test_pacing_delay_empty():
    ms = MetricsStore()
    s = ms._pacing_delay_stats()
    assert s["count"] == 0


def test_pacing_delay_negative_ignored():
    ms = MetricsStore()
    ms.record_pacing_delay(-5)
    stats = ms._pacing_delay_stats()
    # 负数 max(0, ...) 后存为 0，count=1
    assert stats["count"] == 1
    assert stats["max_sec"] == 0


# ── D6.1 reactivation 回复率归因 ────────────────────

@pytest.fixture
def store(tmp_path: Path):
    return ContactStore(tmp_path / "contacts.db")


def test_reactivation_response_stats_no_data(store):
    s = store.compute_reactivation_response_stats()
    assert s["sent"] == 0
    assert s["responded"] == 0
    assert s["response_rate_pct"] == 0


def test_reactivation_response_stats_sent_no_reply(store):
    """主动发了但对方没回 → 0%"""
    store.append_event(journey_id="j1", event_type="reactivation_sent")
    s = store.compute_reactivation_response_stats()
    assert s["sent"] == 1
    assert s["responded"] == 0
    assert s["response_rate_pct"] == 0


def _insert_event(store, jid, etype, ts):
    """直接 SQL 注入指定 ts 的事件（绕过 _now() 整秒粒度）。"""
    import json as _json
    from src.contacts.store import new_id as _new_id
    with store._lock:
        store._conn.execute(
            "INSERT INTO journey_events (event_id, journey_id, trace_id, "
            "event_type, payload_json, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (_new_id(), jid, "", etype, "{}", int(ts)),
        )
        store._conn.commit()


def test_reactivation_response_stats_with_reply(store):
    """主动发了对方回了 → 100%"""
    now = int(time.time())
    _insert_event(store, "j1", "reactivation_sent", now - 100)
    _insert_event(store, "j1", "msg_in", now - 50)  # 50 秒后回
    s = store.compute_reactivation_response_stats()
    assert s["sent"] == 1
    assert s["responded"] == 1
    assert s["response_rate_pct"] == 100.0


def test_reactivation_response_only_msg_before_react_not_counted(store):
    """对方在 reactivation_sent 之前的 msg_in 不算回应"""
    now = int(time.time())
    _insert_event(store, "j1", "msg_in", now - 200)  # 之前
    _insert_event(store, "j1", "reactivation_sent", now - 100)
    s = store.compute_reactivation_response_stats()
    assert s["sent"] == 1
    assert s["responded"] == 0


def test_reactivation_response_partial(store):
    """3 条主动，2 条收到回复 → 66.7%"""
    now = int(time.time())
    for jid in ("j1", "j2", "j3"):
        _insert_event(store, jid, "reactivation_sent", now - 100)
    _insert_event(store, "j1", "msg_in", now - 50)
    _insert_event(store, "j2", "msg_in", now - 30)
    # j3 没回
    s = store.compute_reactivation_response_stats()
    assert s["sent"] == 3
    assert s["responded"] == 2
    assert s["response_rate_pct"] == round(200.0 / 3, 1)


def test_reactivation_response_outside_window_not_counted(store):
    """response_window 之外的回复不算"""
    # 老的 reactivation（10 天前 ts），通过直接 SQL 注入
    import json as _json
    old_ts = int(time.time()) - 10 * 86400
    with store._lock:
        store._conn.execute(
            "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
            "VALUES ('e1', 'j1', '', 'reactivation_sent', '{}', ?)",
            (old_ts,),
        )
        store._conn.commit()
    # 默认 window=24h，老的不进 sent 里
    s = store.compute_reactivation_response_stats(window_sec=86400)
    assert s["sent"] == 0


# ── D6.2 feedback 计数 ────────────────────────────

def test_feedback_recording():
    ms = MetricsStore()
    ms.record_reactivation_feedback("like", sample_ts=time.time())
    ms.record_reactivation_feedback("dislike", sample_ts=time.time())
    snap = ms.snapshot()
    fb = snap["reactivation"]["feedback_1h"]
    assert fb["like"] == 1
    assert fb["dislike"] == 1
