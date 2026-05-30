"""Tests for Strategy Analytics enhancements: B1 (score breakdown), B2 (period compare)."""
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.utils.strategy_advisor import (
    compute_quality_score,
    compute_quality_score_breakdown,
)


# ── B1: Score breakdown ──────────────────────────────────────────────────────

def test_breakdown_returns_all_components():
    """Breakdown should return total + 4 sub-scores + 4 raw contributions."""
    s = {"avg_ms": 300, "silence_rate": 70, "same_intent_rate": 10, "template_hit_rate": 60}
    bd = compute_quality_score_breakdown(s)
    assert "total" in bd
    for key in ("response", "silence", "same_intent", "template",
                "response_raw", "silence_raw", "same_intent_raw", "template_raw"):
        assert key in bd, f"missing key: {key}"
    # total should equal sum of raw contributions
    expected_total = bd["response_raw"] + bd["silence_raw"] + bd["same_intent_raw"] + bd["template_raw"]
    assert abs(bd["total"] - round(expected_total, 1)) <= 0.2


def test_breakdown_matches_original_score():
    """compute_quality_score should return the same total as breakdown."""
    s = {"avg_ms": 1500, "silence_rate": 55, "same_intent_rate": 25, "template_hit_rate": 40}
    assert compute_quality_score(s) == compute_quality_score_breakdown(s)["total"]


def test_breakdown_perfect_score():
    """Fast response + high silence + low same_intent + high template = high score."""
    s = {"avg_ms": 100, "silence_rate": 80, "same_intent_rate": 0, "template_hit_rate": 80}
    bd = compute_quality_score_breakdown(s)
    assert bd["total"] >= 90
    assert bd["response"] == 100


def test_breakdown_worst_score():
    """Slow response + low silence + high same_intent + no template = low score."""
    s = {"avg_ms": 7000, "silence_rate": 0, "same_intent_rate": 60, "template_hit_rate": 0}
    bd = compute_quality_score_breakdown(s)
    assert bd["total"] <= 10
    assert bd["response"] == 0


# ── B2: Period comparison (strategy_summary offset) ──────────────────────────

def test_strategy_summary_offset():
    """strategy_summary with offset should query a historical window."""
    from src.utils.strategy_tracker import StrategyTracker
    import tempfile, os

    db_path = Path(tempfile.mkdtemp()) / "test_events.db"
    tracker = StrategyTracker(db_path=db_path)

    now = time.time()
    # Insert events in current period (last 1 hour)
    for i in range(5):
        tracker.record(
            strategy_id="strat_a", intent="greet", user_id="u1",
            response_ms=200, used_ai=True, model_id="m1"
        )
    # Insert events in previous period (1-2 hours ago) by manipulating ts_epoch
    for i in range(3):
        tracker._conn.execute(
            "UPDATE strategy_events SET ts_epoch = ? WHERE rowid = ?",
            (now - 5400, i + 1)  # 1.5h ago
        )
    tracker._conn.commit()

    current = tracker.strategy_summary(hours=1, offset_hours=0)
    previous = tracker.strategy_summary(hours=1, offset_hours=1)

    # Current should have 2 events (5 total - 3 moved to previous)
    cur_total = sum(s["total"] for s in current) if current else 0
    prev_total = sum(s["total"] for s in previous) if previous else 0

    assert cur_total == 2
    assert prev_total == 3

    # Cleanup
    tracker._conn.close()
