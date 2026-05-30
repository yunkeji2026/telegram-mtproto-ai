"""设备统计聚合器测试 — device_stats.py"""
from __future__ import annotations

import time

import pytest

from src.integrations.shared.device_stats import (
    DeviceStatsRegistry,
    PlatformStats,
    StatBucket,
    BUCKET_SEC,
    MAX_BUCKETS,
)


# ── PlatformStats 基础 ─────────────────────────────────────────────────

class TestPlatformStats:

    def test_record_single(self):
        ps = PlatformStats(platform_type="line", account_id="line_1")
        now = time.time()
        ps.record(ok=True, is_reply=True, elapsed_ms=150.0, now=now)

        assert len(ps.buckets) == 1
        b = ps.buckets[0]
        assert b.runs == 1
        assert b.ok_count == 1
        assert b.fail_count == 0
        assert b.replies == 1
        assert b.total_ms == 150.0

    def test_record_failure(self):
        ps = PlatformStats(platform_type="messenger", account_id="msg_1")
        now = time.time()
        ps.record(ok=False, is_reply=False, elapsed_ms=80.0, now=now)
        ps.record(ok=True, is_reply=False, elapsed_ms=120.0, now=now)

        b = ps.buckets[0]
        assert b.runs == 2
        assert b.ok_count == 1
        assert b.fail_count == 1

    def test_record_multiple_buckets(self):
        ps = PlatformStats(platform_type="whatsapp", account_id="wa_1")
        now = time.time()
        # 两个不同时间窗口
        t1 = now
        t2 = now + BUCKET_SEC + 1  # 下一个 bucket
        ps.record(ok=True, is_reply=True, elapsed_ms=100.0, now=t1)
        ps.record(ok=True, is_reply=False, elapsed_ms=200.0, now=t2)

        assert len(ps.buckets) == 2

    def test_bucket_trimming(self):
        """超过 MAX_BUCKETS 数量的旧 bucket 应被清理。"""
        ps = PlatformStats(platform_type="line", account_id="line_1")
        base = time.time() - (MAX_BUCKETS + 10) * BUCKET_SEC
        for i in range(MAX_BUCKETS + 10):
            ps.record(ok=True, is_reply=False, elapsed_ms=50.0,
                     now=base + i * BUCKET_SEC)
        assert len(ps.buckets) <= MAX_BUCKETS

    def test_summary_rates(self):
        ps = PlatformStats(platform_type="line", account_id="line_1")
        now = time.time()
        # 8 成功 2 失败，其中 3 个回复
        for i in range(8):
            ps.record(ok=True, is_reply=(i < 3), elapsed_ms=100.0, now=now)
        for i in range(2):
            ps.record(ok=False, is_reply=False, elapsed_ms=200.0, now=now)

        s = ps.summary(hours=24.0)
        assert s["total_runs"] == 10
        assert s["total_ok"] == 8
        assert s["total_fail"] == 2
        assert s["total_replies"] == 3
        assert s["success_rate_pct"] == 80.0
        assert s["reply_rate_pct"] == 30.0

    def test_summary_empty(self):
        ps = PlatformStats(platform_type="line")
        s = ps.summary()
        assert s["total_runs"] == 0
        assert s["success_rate_pct"] == 0.0

    def test_timeseries(self):
        ps = PlatformStats(platform_type="messenger", account_id="msg_1")
        now = time.time()
        ps.record(ok=True, is_reply=True, elapsed_ms=150.0, now=now)
        ts = ps.timeseries(hours=1.0)
        assert len(ts) == 1
        assert ts[0]["runs"] == 1
        assert ts[0]["replies"] == 1

    def test_circuit_open_recording(self):
        ps = PlatformStats(platform_type="whatsapp", account_id="wa_1")
        now = time.time()
        ps.record_circuit_open(30.0, now=now)
        ps.record_circuit_open(60.0, now=now)
        s = ps.summary()
        assert s["circuit_open_sec"] == 90.0


# ── DeviceStatsRegistry ────────────────────────────────────────────────

class TestDeviceStatsRegistry:

    def test_record_and_summary(self):
        reg = DeviceStatsRegistry()
        now = time.time()
        reg.record("SER1", "line", "line_1", ok=True, is_reply=True, elapsed_ms=100.0)
        reg.record("SER1", "line", "line_1", ok=False, is_reply=False, elapsed_ms=200.0)
        reg.record("SER1", "messenger", "msg_1", ok=True, is_reply=True, elapsed_ms=80.0)

        s = reg.device_summary("SER1")
        assert s["total_runs"] == 3
        assert s["total_replies"] == 2
        assert len(s["platforms"]) == 2

    def test_all_summaries(self):
        reg = DeviceStatsRegistry()
        reg.record("SER1", "line", "l1", ok=True, is_reply=False, elapsed_ms=50.0)
        reg.record("SER2", "messenger", "m1", ok=True, is_reply=True, elapsed_ms=60.0)

        summaries = reg.all_summaries()
        assert len(summaries) == 2
        serials = {s["serial"] for s in summaries}
        assert serials == {"SER1", "SER2"}

    def test_device_timeseries(self):
        reg = DeviceStatsRegistry()
        reg.record("SER1", "line", "l1", ok=True, is_reply=False, elapsed_ms=50.0)
        ts = reg.device_timeseries("SER1", hours=1.0)
        assert "line" in ts["platforms"]
        assert len(ts["platforms"]["line"]) == 1

    def test_unknown_device_summary(self):
        reg = DeviceStatsRegistry()
        s = reg.device_summary("UNKNOWN")
        assert s["total_runs"] == 0
        assert s["platforms"] == []
