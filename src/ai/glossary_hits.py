"""P60：术语命中统计（进程级单例）。

记录每个术语/保护词在翻译中**实际被触发**的次数，量化术语库价值
（哪些术语真在用、哪些是僵尸条目）。供术语库控制台展示。

无新增依赖；绝不记录原文/译文，只存「术语 -> 次数」计数。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterable, Optional


class GlossaryHitStats:
    __slots__ = ("_lock", "_terms", "_protect", "_last_ts")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._terms: Dict[str, int] = {}
        self._protect: Dict[str, int] = {}
        self._last_ts = 0.0

    def record_terms(self, terms: Iterable[str]) -> None:
        with self._lock:
            for t in terms:
                t = str(t)
                if t:
                    self._terms[t] = self._terms.get(t, 0) + 1
                    self._last_ts = time.time()

    def record_protect(self, words: Iterable[str]) -> None:
        with self._lock:
            for w in words:
                w = str(w)
                if w:
                    self._protect[w] = self._protect.get(w, 0) + 1
                    self._last_ts = time.time()

    def term_hits(self, term: str) -> int:
        with self._lock:
            return int(self._terms.get(str(term), 0))

    def protect_hits(self, word: str) -> int:
        with self._lock:
            return int(self._protect.get(str(word), 0))

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "terms": dict(self._terms),
                "protect": dict(self._protect),
                "total_term_hits": sum(self._terms.values()),
                "total_protect_hits": sum(self._protect.values()),
                "last_record_ts": self._last_ts,
            }

    def reset(self) -> None:
        with self._lock:
            self._terms.clear()
            self._protect.clear()
            self._last_ts = 0.0


_SINGLETON: Optional[GlossaryHitStats] = None
_LOCK = threading.Lock()


def get_glossary_hits() -> GlossaryHitStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = GlossaryHitStats()
    return _SINGLETON


__all__ = ["GlossaryHitStats", "get_glossary_hits"]
