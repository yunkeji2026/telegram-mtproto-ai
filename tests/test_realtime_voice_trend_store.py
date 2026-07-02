"""E 线：实时语音按日趋势落库（realtime_voice_trend_store）+ stats 旁路写入。"""

from __future__ import annotations

import time

import pytest

from src.ai.realtime_voice_stats import get_realtime_voice_stats
from src.ai.realtime_voice_trend_store import (
    RealtimeVoiceTrendStore,
    configure_realtime_voice_trend_store,
    get_realtime_voice_trend_store,
    record_realtime_voice_trend,
    reset_realtime_voice_trend_store,
)


def test_daily_zero_fills_and_rates():
    store = RealtimeVoiceTrendStore(":memory:")
    now = time.time()
    store.add(attempts=10, connected=8, health_ok=9, health_fail=1,
                host_unreachable=2, now=now)
    days = store.daily(days=7, now=now)
    assert len(days) == 7
    assert all(d["attempts"] == 0 for d in days[:-1])
    today = days[-1]
    assert today["attempts"] == 10
    assert today["connected"] == 8
    assert today["connect_rate"] == 0.8
    assert today["health_ok_rate"] == 0.9
    assert today["by_end_reason"]["host_unreachable"] == 2


def test_record_noop_until_configured():
    reset_realtime_voice_trend_store()
    record_realtime_voice_trend(attempts=5)
    assert get_realtime_voice_trend_store() is None
    store = configure_realtime_voice_trend_store(enabled=True, db_path=":memory:")
    assert store is not None
    record_realtime_voice_trend(attempts=3, connected=2, health_fail=1)
    today = store.daily(days=1)[-1]
    assert today["attempts"] == 3
    assert today["connected"] == 2
    assert today["health_fail"] == 1
    reset_realtime_voice_trend_store()


def test_stats_hooks_write_trend():
    reset_realtime_voice_trend_store()
    configure_realtime_voice_trend_store(enabled=True, db_path=":memory:")
    s = get_realtime_voice_stats()
    s.reset()
    s.attempt()
    s.connected()
    s.health_probe(True)
    s.ended("host_unreachable")
    today = get_realtime_voice_trend_store().daily(days=1)[-1]
    assert today["attempts"] == 1
    assert today["connected"] == 1
    assert today["health_ok"] == 1
    assert today["by_end_reason"]["host_unreachable"] == 1
    reset_realtime_voice_trend_store()
    s.reset()


def test_daily_for_calibrate_shape():
    store = RealtimeVoiceTrendStore(":memory:")
    now = time.time()
    store.add(attempts=5, connected=1, health_ok=1, health_fail=4, now=now)
    row = store.daily_for_calibrate(days=1, now=now)[-1]
    assert row["day"]
    assert row["connect_rate"] == 0.2
    assert "by_end_reason" in row


def test_calibrate_uses_daily_series():
    from src.utils.realtime_voice_alert import calibrate_realtime_voice_alert
    daily = [
        {"day": "2026-07-01", "attempts": 5, "connected": 1, "connect_rate": 0.2,
         "health_ok": 5, "health_fail": 0, "health_ok_rate": 1.0, "by_end_reason": {}},
        {"day": "2026-07-02", "attempts": 5, "connected": 5, "connect_rate": 1.0,
         "health_ok": 5, "health_fail": 0, "health_ok_rate": 1.0, "by_end_reason": {}},
    ]
    out = calibrate_realtime_voice_alert({}, daily=daily)
    assert out["evaluated_windows"] == 2
    assert out["points"][0]["light"] in ("yellow", "red")
    assert out["points"][1]["light"] == "green"


def test_sync_writes_missed_deltas(monkeypatch):
    from unittest.mock import patch
    from src.ai.realtime_voice_trend_store import sync_realtime_voice_trend_from_stats
    reset_realtime_voice_trend_store()
    store = configure_realtime_voice_trend_store(enabled=True, db_path=":memory:")
    assert store is not None
    sync_realtime_voice_trend_from_stats({})  # bootstrap
    s = get_realtime_voice_stats()
    s.reset()
    with patch("src.ai.realtime_voice_trend_store.record_realtime_voice_trend"):
        s.attempt()
        s.connected()
        s.health_probe(False)
        s.ended("host_unreachable")
    sync_realtime_voice_trend_from_stats(s.dump())
    today = store.daily(days=1)[-1]
    assert today["attempts"] == 1
    assert today["connected"] == 1
    assert today["health_fail"] == 1
    assert today["by_end_reason"]["host_unreachable"] == 1
    reset_realtime_voice_trend_store()
    s.reset()
