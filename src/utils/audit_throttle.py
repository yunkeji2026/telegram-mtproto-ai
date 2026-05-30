"""P26-D: per-key audit-log throttle utility.

Refactored from the duplicated audit-throttle blocks introduced in P24-A
(`src/web/routes/rpa_overview_routes.py::_audit_rate_limited`) and P25-B
(`src/web/admin.py::_audit_oversize`). Both used the same pattern:

    - LRU cache of (key -> last_emit_ts)
    - silent-drop if (now - last) < window
    - O(1) eviction via OrderedDict.popitem(last=False)
    - threading.Lock for safe concurrent writes
    - best-effort: never raise to caller

This module centralizes that pattern so any future audit hook reuses one
well-tested implementation.

Public API:
    throttle = AuditThrottle(window_sec=1.0, max_keys=4096)
    if throttle.should_emit(key):
        audit_store.log(...)

Notes:
    - `should_emit(key)` is the one-shot probe that returns True at most once
      per `window_sec` for a given key. It atomically records the emission
      timestamp on success.
    - Use `(client_ip, endpoint)` or `client_ip` as the key depending on
      desired granularity.
"""
from __future__ import annotations

import time
import threading
from collections import OrderedDict
from typing import Hashable


class AuditThrottle:
    """Lock-protected LRU throttle for audit-log emission.

    P25-C behavior (preserved):
      - Same key within ``window_sec`` → False (caller should drop)
      - First time or window elapsed → True (caller emits) + ts recorded
      - When >max_keys, oldest is evicted in O(1)
    """

    __slots__ = ("_window", "_max", "_data", "_lock")

    def __init__(self, window_sec: float = 1.0, max_keys: int = 4096) -> None:
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")
        self._window = float(window_sec)
        self._max = int(max_keys)
        self._data: "OrderedDict[Hashable, float]" = OrderedDict()
        self._lock = threading.Lock()

    def should_emit(self, key: Hashable, now: float | None = None) -> bool:
        """Return True iff caller should emit an audit row for `key`.

        Atomically records the emission ts on True. Safe under threads.

        `now` overridable for deterministic tests; defaults to time.monotonic().
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            last = self._data.get(key, 0.0)
            if now - last < self._window:
                return False
            self._data[key] = now
            self._data.move_to_end(key)
            # O(1) eviction
            while len(self._data) > self._max:
                try:
                    self._data.popitem(last=False)
                except KeyError:
                    break
            return True

    def peek(self, key: Hashable, now: float | None = None) -> bool:
        """P27-D: Read-only probe — would `should_emit(key)` return True right now?

        Does NOT update state. Useful for:
          - Debugging / admin UI ("is this IP currently throttled?")
          - Pre-flight checks before expensive work
          - Tests asserting throttle behavior without consuming the window

        Note: result can race with concurrent `should_emit` — caller must accept
        that another thread may consume the slot between peek() and use.
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            last = self._data.get(key, 0.0)
            return (now - last) >= self._window

    def remaining_sec(self, key: Hashable, now: float | None = None) -> float:
        """P27-D: Seconds until next emit allowed for `key` (0.0 if not throttled).

        Useful for surfacing Retry-After-like values to admin UI / API responses.
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            last = self._data.get(key, 0.0)
            if last <= 0.0:
                return 0.0
            remaining = self._window - (now - last)
            return max(0.0, remaining)

    def clear(self) -> None:
        """Test hook — drop all state."""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
