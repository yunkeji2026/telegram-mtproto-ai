"""P6 新模块单元测试 — i18n / EventTracker / TaskScheduler / BotRouter"""

import asyncio
import os
import tempfile
import time
import pytest
from pathlib import Path

# ── i18n ──────────────────────────────────────────────────────

from src.utils.i18n import I18n


class TestI18n:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self.i18n = I18n(db_path=Path(self._tmpdir) / "test_i18n.db")

    def test_default_lang_is_zh(self):
        assert self.i18n.get_lang(12345) == "zh"

    def test_set_and_get(self):
        self.i18n.set_lang(100, "en")
        assert self.i18n.get_lang(100) == "en"

    def test_translate_zh(self):
        text = self.i18n.t("gxp_timeout", 0, sec=30, cmd="/cxye")
        assert "30" in text
        assert "超时" in text

    def test_translate_en(self):
        self.i18n.set_lang(200, "en")
        text = self.i18n.t("gxp_timeout", 200, sec=30, cmd="/cxye")
        assert "timed out" in text.lower()

    def test_fallback_to_default(self):
        text = self.i18n.t("nonexistent_key", 0)
        assert text == "nonexistent_key"

    def test_available_langs(self):
        langs = I18n.available_langs()
        assert "zh" in langs
        assert "en" in langs


# ── EventTracker ──────────────────────────────────────────────

from src.utils.event_tracker import EventTracker


class TestEventTracker:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self.tracker = EventTracker(db_path=Path(self._tmpdir) / "test_events.db")

    def test_track_and_query(self):
        self.tracker.track("gxp_command", chat_id=1, user_id="u1", detail="/cxye", response_ms=150)
        self.tracker.track("order_query", chat_id=1, user_id="u2", detail="123", response_ms=300)
        stats = self.tracker.command_stats(hours=1)
        assert len(stats) == 2
        total = self.tracker.total_events(hours=1)
        assert total == 2

    def test_hourly_trend(self):
        self.tracker.track("test", chat_id=1)
        trend = self.tracker.hourly_trend(hours=1)
        assert len(trend) >= 1
        assert trend[0]["count"] >= 1

    def test_top_users(self):
        for _ in range(5):
            self.tracker.track("cmd", user_id="top_user")
        self.tracker.track("cmd", user_id="other")
        top = self.tracker.top_users(hours=1)
        assert top[0]["user_id"] == "top_user"
        assert top[0]["count"] == 5

    def test_response_time_distribution(self):
        for ms in [100, 200, 300, 400, 500]:
            self.tracker.track("cmd", response_ms=ms)
        dist = self.tracker.response_time_distribution(hours=1)
        assert dist["count"] == 5
        assert dist["avg"] == 300
        assert dist["p50"] == 300


# ── TaskScheduler ─────────────────────────────────────────────

from src.utils.scheduler import TaskScheduler


class TestTaskScheduler:
    def test_from_config_disabled(self):
        s = TaskScheduler.from_config({}, lambda c, cmd: None)
        assert len(s._tasks) == 0

    def test_from_config_with_tasks(self):
        cfg = {
            "scheduled_tasks": {
                "enabled": True,
                "tasks": [
                    {"name": "test_rate", "interval_seconds": 60,
                     "chat_id": -100111, "command": "/hl", "enabled": True},
                ]
            }
        }
        s = TaskScheduler.from_config(cfg, lambda c, cmd: None)
        assert len(s._tasks) == 1
        info = s.list_tasks()
        assert info[0]["name"] == "test_rate"
        assert info[0]["interval"] == 60

    def test_add_remove(self):
        s = TaskScheduler()
        s.add_task("t1", 60, -100, "/hl", lambda c, cmd: None)
        assert "t1" in s._tasks
        s.remove_task("t1")
        assert "t1" not in s._tasks


# ── BotRouter ─────────────────────────────────────────────────

from src.utils.multi_bot import BotRouter


class TestBotRouter:
    def test_disabled_by_default(self):
        r = BotRouter({})
        assert not r.enabled

    def test_routing(self):
        cfg = {
            "multi_bot": {
                "enabled": True,
                "default_session": "main",
                "routes": [
                    {"session": "bot2", "chat_ids": [-100111, -100222]},
                ]
            }
        }
        r = BotRouter(cfg)
        assert r.enabled
        assert r.get_session(-100111) == "bot2"
        assert r.get_session(-999) == "main"
        assert r.should_handle(-100111, "bot2") is True
        assert r.should_handle(-100111, "main") is False
        assert r.should_handle(-999, "main") is True

    def test_list_routes(self):
        cfg = {
            "multi_bot": {
                "enabled": True,
                "default_session": "main",
                "routes": [{"session": "b2", "chat_ids": [-100]}]
            }
        }
        r = BotRouter(cfg)
        routes = r.list_routes()
        assert len(routes) == 1
        assert routes[0]["chat_id"] == -100
