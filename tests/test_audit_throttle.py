"""P26-D unit tests: AuditThrottle utility (LRU + window + thread safety)."""
from __future__ import annotations

import threading
import time

import pytest

from src.utils.audit_throttle import AuditThrottle


def test_first_emit_returns_true():
    th = AuditThrottle(window_sec=1.0)
    assert th.should_emit("k") is True


def test_second_emit_within_window_returns_false():
    th = AuditThrottle(window_sec=10.0)
    assert th.should_emit("k") is True
    assert th.should_emit("k") is False


def test_emit_after_window_returns_true():
    th = AuditThrottle(window_sec=0.05)
    assert th.should_emit("k") is True
    time.sleep(0.08)
    assert th.should_emit("k") is True


def test_distinct_keys_independent():
    th = AuditThrottle(window_sec=10.0)
    assert th.should_emit("a") is True
    assert th.should_emit("b") is True  # different key, not throttled
    assert th.should_emit("a") is False


def test_lru_eviction_caps_size():
    th = AuditThrottle(window_sec=10.0, max_keys=3)
    for k in ("a", "b", "c", "d", "e"):
        th.should_emit(k)
    assert len(th) == 3
    # "a", "b" should be evicted (oldest)
    # If we try to emit them again, throttle resets (they were evicted)
    assert th.should_emit("a") is True  # re-inserted, not throttled


def test_now_override_for_deterministic_tests():
    th = AuditThrottle(window_sec=5.0)
    assert th.should_emit("k", now=100.0) is True
    assert th.should_emit("k", now=102.0) is False  # within window
    assert th.should_emit("k", now=106.0) is True   # past window


def test_invalid_args_raise():
    with pytest.raises(ValueError):
        AuditThrottle(window_sec=0)
    with pytest.raises(ValueError):
        AuditThrottle(window_sec=-1)
    with pytest.raises(ValueError):
        AuditThrottle(max_keys=0)


def test_clear_resets_state():
    th = AuditThrottle(window_sec=10.0)
    th.should_emit("k")
    th.clear()
    assert len(th) == 0
    assert th.should_emit("k") is True


def test_thread_safety_concurrent_first_emit():
    """Under concurrent should_emit() on same key, exactly ONE call wins."""
    th = AuditThrottle(window_sec=10.0)
    results = []
    lock = threading.Lock()

    def worker():
        r = th.should_emit("shared_key")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(64)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r is True) == 1, \
        f"exactly one thread should win the throttle; got {sum(results)} winners"


def test_hashable_tuple_key():
    """key can be tuple (used by rpa_overview_routes as (ip, endpoint))."""
    th = AuditThrottle(window_sec=10.0)
    assert th.should_emit(("1.2.3.4", "diff")) is True
    assert th.should_emit(("1.2.3.4", "diff")) is False
    assert th.should_emit(("1.2.3.4", "restore")) is True  # different tuple


# ────────────────────────────────────────────────────────────────────────
# P27-D: peek() / remaining_sec() read-only APIs
# ────────────────────────────────────────────────────────────────────────


def test_peek_does_not_consume_window():
    """P27-D: peek() returns True without recording emission timestamp."""
    th = AuditThrottle(window_sec=10.0)
    assert th.peek("k") is True
    # Multiple peeks still return True — state untouched
    assert th.peek("k") is True
    assert th.peek("k") is True
    # First real emit also succeeds (peek did not consume)
    assert th.should_emit("k") is True
    # Now we ARE throttled
    assert th.peek("k") is False
    assert th.should_emit("k") is False


def test_peek_returns_true_after_window_elapses():
    """P27-D: peek correctly reflects passage of time via `now` override."""
    th = AuditThrottle(window_sec=5.0)
    th.should_emit("k", now=100.0)
    assert th.peek("k", now=101.0) is False
    assert th.peek("k", now=104.9) is False
    assert th.peek("k", now=105.0) is True
    assert th.peek("k", now=106.0) is True


def test_peek_unknown_key_returns_true():
    """P27-D: never-seen key is always immediately emittable."""
    th = AuditThrottle(window_sec=10.0)
    assert th.peek("never_seen") is True


def test_remaining_sec_zero_for_unknown_key():
    """P27-D: untracked key → 0.0 seconds remaining (free to emit)."""
    th = AuditThrottle(window_sec=10.0)
    assert th.remaining_sec("never_seen") == 0.0


def test_remaining_sec_decreases_over_time():
    """P27-D: remaining_sec linearly decreases until window expires."""
    th = AuditThrottle(window_sec=5.0)
    th.should_emit("k", now=100.0)
    assert th.remaining_sec("k", now=100.0) == pytest.approx(5.0, abs=0.01)
    assert th.remaining_sec("k", now=102.5) == pytest.approx(2.5, abs=0.01)
    assert th.remaining_sec("k", now=105.0) == 0.0
    assert th.remaining_sec("k", now=999.0) == 0.0   # never goes negative


def test_peek_and_remaining_consistent():
    """P27-D: peek True iff remaining_sec == 0."""
    th = AuditThrottle(window_sec=3.0)
    th.should_emit("k", now=10.0)
    for t in (10.0, 11.0, 12.5, 12.999, 13.0, 14.0):
        if th.peek("k", now=t):
            assert th.remaining_sec("k", now=t) == 0.0, f"inconsistent at t={t}"
        else:
            assert th.remaining_sec("k", now=t) > 0.0, f"inconsistent at t={t}"
