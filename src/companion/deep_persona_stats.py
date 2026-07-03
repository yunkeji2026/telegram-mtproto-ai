"""深度人设运行观测（进程级单例，风格对齐 speech_emotion_stats/voice_synth_stats）。

证明深度人设"真的在长出来"：巩固次数、画像/内部梗产出、经历/未收尾话题累积、
回指触发、话题收尾。经 dump()→/api/workspace/metrics、dump_prom()→Prometheus。
best-effort：record 任何异常吞掉，绝不阻塞主链路。绝不记录任何原文。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class DeepPersonaStats:
    __slots__ = ("_lock", "_c", "_started_at", "_last_ts")

    _KEYS = (
        "consolidations", "profiles_built", "jokes_detected",
        "experiential_added", "open_loops_added", "loops_resolved",
        "callbacks_emitted", "drift_blocked", "life_shares",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._c: Dict[str, int] = {k: 0 for k in self._KEYS}
        self._started_at = time.time()
        self._last_ts = 0.0

    def incr(self, key: str, n: int = 1) -> None:
        try:
            k = str(key)
            if k not in self._c:
                return
            with self._lock:
                self._c[k] += int(n)
                self._last_ts = time.time()
        except Exception:
            pass

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            d = dict(self._c)
            d["started_at"] = self._started_at
            d["last_record_ts"] = self._last_ts
            return d

    def dump_prom(self) -> str:
        lines = []
        with self._lock:
            for k in self._KEYS:
                lines.append(f"# HELP deep_persona_{k}_total Deep-persona {k}")
                lines.append(f"# TYPE deep_persona_{k}_total counter")
                lines.append(f"deep_persona_{k}_total {self._c[k]}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            for k in self._KEYS:
                self._c[k] = 0
            self._last_ts = 0.0


_SINGLETON: Optional[DeepPersonaStats] = None
_LOCK = threading.Lock()


def get_deep_persona_stats() -> DeepPersonaStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = DeepPersonaStats()
    return _SINGLETON


__all__ = ["DeepPersonaStats", "get_deep_persona_stats"]
