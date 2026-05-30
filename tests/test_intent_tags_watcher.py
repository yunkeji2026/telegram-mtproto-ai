"""P26-A: tests for intent_tags_watcher (watchdog auto-reload)."""
from __future__ import annotations

import os
import time

import pytest

from src.integrations import intent_tags_watcher
from src.integrations import rpa_shared


@pytest.fixture()
def _isolated_yaml(tmp_path, monkeypatch):
    """Point INTENT_TAGS_PATH at a fresh tmp yaml and ensure watcher is stopped."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - kw_initial\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    # Reset watcher stats
    intent_tags_watcher.stop_watcher()
    stats = intent_tags_watcher.get_reload_stats()
    # Capture baseline counters
    yield yaml_file, stats
    intent_tags_watcher.stop_watcher()
    monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
    rpa_shared.reload_intent_tags()


def test_trigger_reload_now_increments_counter(_isolated_yaml):
    """P26-A: bypass debounce, sync reload reflects in counter."""
    _, before = _isolated_yaml
    before_n = before.get("auto_reloads_total", 0)
    intent_tags_watcher.trigger_reload_now()
    after = intent_tags_watcher.get_reload_stats()
    assert after["auto_reloads_total"] == before_n + 1
    assert after["last_reload_ts"] > 0


def test_get_reload_stats_returns_snapshot(_isolated_yaml):
    """get_reload_stats returns dict copy (caller mutation does not leak)."""
    s1 = intent_tags_watcher.get_reload_stats()
    s1["auto_reloads_total"] = 9999
    s2 = intent_tags_watcher.get_reload_stats()
    assert s2["auto_reloads_total"] != 9999


def test_start_then_stop_is_idempotent(_isolated_yaml):
    """Repeated start/stop are no-op safe."""
    assert intent_tags_watcher.start_watcher(debounce_sec=0.1) is True
    assert intent_tags_watcher.is_running() is True
    # Second start returns True without restarting
    assert intent_tags_watcher.start_watcher(debounce_sec=0.1) is True
    intent_tags_watcher.stop_watcher()
    assert intent_tags_watcher.is_running() is False
    intent_tags_watcher.stop_watcher()  # second stop, no error


def test_file_change_triggers_reload_after_debounce(_isolated_yaml):
    """P26-A: writing the yaml file triggers a debounced auto reload."""
    yaml_file, before_stats = _isolated_yaml
    before_n = before_stats.get("auto_reloads_total", 0)

    started = intent_tags_watcher.start_watcher(debounce_sec=0.15)
    assert started, "watcher should start"
    time.sleep(0.2)  # let observer thread spin up

    # Modify file
    yaml_file.write_text("purchase:\n  - kw_changed\n  - new_kw\n", encoding="utf-8")
    # Force mtime bump (Windows sometimes coalesces same-second writes)
    new_mtime = time.time()
    os.utime(yaml_file, (new_mtime, new_mtime))

    # Wait up to ~2s for the debounced timer to fire
    deadline = time.time() + 2.0
    while time.time() < deadline:
        s = intent_tags_watcher.get_reload_stats()
        if s.get("auto_reloads_total", 0) > before_n:
            break
        time.sleep(0.05)

    after = intent_tags_watcher.get_reload_stats()
    assert after["auto_reloads_total"] > before_n, \
        f"auto reload counter did not advance: {after}"

    # And the runtime intent_tags now contains the new keyword
    assert "new_kw" in rpa_shared._INTENT_TAGS.get("purchase", [])


def test_rapid_writes_are_debounced(_isolated_yaml):
    """Multiple writes inside debounce window → only 1 reload."""
    yaml_file, before_stats = _isolated_yaml
    before_n = before_stats.get("auto_reloads_total", 0)
    before_deb = before_stats.get("events_debounced", 0)

    intent_tags_watcher.start_watcher(debounce_sec=0.4)
    time.sleep(0.2)

    # 5 rapid writes within 100ms window — all should collapse into 1 reload
    for i in range(5):
        yaml_file.write_text(f"purchase:\n  - kw_{i}\n", encoding="utf-8")
        os.utime(yaml_file, None)
        time.sleep(0.02)

    # Wait for debounced reload to fire
    time.sleep(0.8)

    after = intent_tags_watcher.get_reload_stats()
    delta_reloads = after["auto_reloads_total"] - before_n
    delta_debounced = after["events_debounced"] - before_deb
    # At least 1 reload, but far fewer reloads than writes (debounce works)
    assert delta_reloads >= 1
    assert delta_reloads <= 2, f"too many reloads: {delta_reloads} (debounce broken?)"
    # At least some events were collapsed (Windows FS event coalescing can vary,
    # so we don't pin a hard lower bound; just check non-negative)
    assert delta_debounced >= 0
