"""P8 新模块单元测试 — RateLimiter / QualityTracker / ConfigMigrator / ChannelHealth / PriorityQueue"""

import asyncio
import tempfile
import time
import yaml
import pytest
from pathlib import Path

# ── RateLimiter ───────────────────────────────────────────────

from src.utils.rate_limiter import RateLimiter, TokenBucket


class TestTokenBucket:
    def test_consume_within_capacity(self):
        b = TokenBucket(capacity=5, rate=1)
        for _ in range(5):
            assert b.consume() is True
        assert b.consume() is False

    def test_refill_over_time(self):
        b = TokenBucket(capacity=2, rate=100)
        b.consume()
        b.consume()
        assert b.consume() is False
        time.sleep(0.05)
        assert b.consume() is True


class TestRateLimiter:
    def test_disabled_by_default(self):
        rl = RateLimiter({})
        assert rl.enabled is False
        ok, _ = rl.allow("u1", 100)
        assert ok is True

    def test_enabled_and_limits(self):
        cfg = {"rate_limit": {
            "enabled": True,
            "global": {"capacity": 3, "rate_per_sec": 0},
            "per_user": {"capacity": 2, "rate_per_sec": 0},
            "per_chat": {"capacity": 5, "rate_per_sec": 0},
        }}
        rl = RateLimiter(cfg)
        assert rl.enabled is True
        assert rl.allow("u1", 1)[0] is True
        assert rl.allow("u1", 1)[0] is True
        ok, reason = rl.allow("u1", 1)
        assert ok is False
        assert reason == "user"

    def test_global_limit(self):
        cfg = {"rate_limit": {
            "enabled": True,
            "global": {"capacity": 2, "rate_per_sec": 0},
            "per_user": {"capacity": 100, "rate_per_sec": 0},
            "per_chat": {"capacity": 100, "rate_per_sec": 0},
        }}
        rl = RateLimiter(cfg)
        rl.allow("u1", 1)
        rl.allow("u2", 2)
        ok, reason = rl.allow("u3", 3)
        assert ok is False
        assert reason == "global"

    def test_stats(self):
        cfg = {"rate_limit": {"enabled": True,
                              "global": {"capacity": 100, "rate_per_sec": 10},
                              "per_user": {"capacity": 1, "rate_per_sec": 0},
                              "per_chat": {"capacity": 100, "rate_per_sec": 10}}}
        rl = RateLimiter(cfg)
        rl.allow("u1", 1)
        rl.allow("u1", 1)
        stats = rl.get_stats()
        assert stats["passed"] >= 1
        assert stats["blocked_user"] >= 1


# ── QualityTracker ────────────────────────────────────────────

from src.utils.quality_tracker import QualityTracker


class TestQualityTracker:
    def test_record_and_summary(self):
        qt = QualityTracker()
        qt.record_call(prompt_tokens=100, completion_tokens=50, elapsed_ms=200, reply="Hello world")
        s = qt.get_summary()
        assert s["total_calls"] == 1
        assert s["total_prompt_tokens"] == 100
        assert s["total_completion_tokens"] == 50

    def test_anomaly_detection_short(self):
        qt = QualityTracker({"ai_quality": {"min_reply_length": 10}})
        qt.record_call(reply="Hi")
        assert qt.get_summary()["total_anomalies"] == 1
        anomalies = qt.get_recent_anomalies()
        assert any(a["type"] == "too_short" for a in anomalies)

    def test_anomaly_detection_repeated(self):
        qt = QualityTracker()
        qt.record_call(reply="This is a repeated reply that is long enough")
        qt.record_call(reply="This is a repeated reply that is long enough")
        assert qt.get_summary()["total_anomalies"] >= 1

    def test_identity_leak_detection(self):
        qt = QualityTracker()
        qt.record_call(reply="作为一个AI语言模型，我不能做这件事")
        anomalies = qt.get_recent_anomalies()
        assert any(a["type"] == "identity_leak" for a in anomalies)

    def test_token_trend(self):
        qt = QualityTracker()
        for i in range(5):
            qt.record_call(prompt_tokens=50, completion_tokens=30, elapsed_ms=100)
        trend = qt.get_token_trend(10)
        assert len(trend) == 5


# ── ConfigMigrator ────────────────────────────────────────────

from src.utils.config_migrator import ConfigMigrator, CURRENT_VERSION


class TestConfigMigrator:
    def test_migrate_adds_missing_fields(self):
        tmpdir = Path(tempfile.mkdtemp())
        cfg_path = tmpdir / "config.yaml"
        cfg_path.write_text(yaml.dump({"telegram": {"api_id": "123"}}, allow_unicode=True))
        m = ConfigMigrator(cfg_path)
        ok, msg = m.check_and_migrate()
        assert ok is True
        assert "新增" in msg
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "rate_limit" in data
        assert "plugins" in data
        assert data["_config_version"] == CURRENT_VERSION

    def test_already_current_version(self):
        tmpdir = Path(tempfile.mkdtemp())
        cfg_path = tmpdir / "config.yaml"
        cfg_path.write_text(yaml.dump({"_config_version": CURRENT_VERSION}, allow_unicode=True))
        m = ConfigMigrator(cfg_path)
        ok, msg = m.check_and_migrate()
        assert ok is False
        assert "最新版本" in msg

    def test_backup_created(self):
        tmpdir = Path(tempfile.mkdtemp())
        cfg_path = tmpdir / "config.yaml"
        cfg_path.write_text(yaml.dump({"telegram": {}}, allow_unicode=True))
        m = ConfigMigrator(cfg_path)
        m.check_and_migrate()
        baks = list(tmpdir.glob("*.bak_*"))
        assert len(baks) >= 1


# ── ChannelHealth ─────────────────────────────────────────────

from src.utils.channel_health import compute_health_scores


class TestChannelHealth:
    def test_healthy_channel(self):
        channels = {
            "ep": {"display_name": "EP", "status": "active", "fee_rate": "3.5%",
                   "processing_time": "5min", "last_updated": time.strftime("%Y-%m-%d")},
        }
        scores = compute_health_scores(channels)
        assert len(scores) == 1
        assert scores[0]["score"] >= 70
        assert scores[0]["grade"] in ("healthy", "warning")

    def test_inactive_channel(self):
        channels = {
            "jc": {"display_name": "JC", "status": "disabled", "fee_rate": "2%"},
        }
        scores = compute_health_scores(channels)
        assert scores[0]["score"] < 50
        assert scores[0]["grade"] == "disabled"

    def test_disabled_status_trimmed(self):
        """与 channel_status_format.is_channel_disabled 一致：带空格的 status 仍判为禁用"""
        channels = {
            "jc": {"display_name": "JC", "status": "  disabled  ", "fee_rate": "2%"},
        }
        scores = compute_health_scores(channels)
        assert scores[0]["grade"] == "disabled"

    def test_skips_non_dict_channel_entries(self):
        scores = compute_health_scores({"bad": None, "ep": {"display_name": "EP", "status": "正常", "fee_rate": "1%"}})
        assert len(scores) == 1
        assert scores[0]["key"] == "ep"

    def test_empty_channels(self):
        scores = compute_health_scores({})
        assert scores == []


# ── PriorityQueue ─────────────────────────────────────────────

from src.utils.priority_queue import PriorityMessageQueue, PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_NORMAL


class TestPriorityQueue:
    def test_disabled_by_default(self):
        q = PriorityMessageQueue({})
        assert q.enabled is False

    async def test_enqueue_dequeue_order(self):
        cfg = {"message_queue": {"enabled": True, "max_size": 10}}
        q = PriorityMessageQueue(cfg)
        await q.enqueue("low", PRIORITY_LOW)
        await q.enqueue("high", PRIORITY_HIGH)
        await q.enqueue("normal", PRIORITY_NORMAL)
        assert await q.dequeue(1) == "high"
        assert await q.dequeue(1) == "normal"
        assert await q.dequeue(1) == "low"

    async def test_backpressure_drops_low(self):
        cfg = {"message_queue": {"enabled": True, "max_size": 5, "backpressure_threshold": 0.5}}
        q = PriorityMessageQueue(cfg)
        for i in range(3):
            await q.enqueue(f"msg{i}", PRIORITY_NORMAL)
        ok = await q.enqueue("lowmsg", PRIORITY_LOW)
        assert ok is False
        stats = q.get_stats()
        assert stats["dropped"] >= 1

    async def test_stats(self):
        cfg = {"message_queue": {"enabled": True, "max_size": 100}}
        q = PriorityMessageQueue(cfg)
        await q.enqueue("test")
        stats = q.get_stats()
        assert stats["enqueued"] == 1
        assert stats["size"] == 1
