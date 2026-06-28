"""P57：翻译引擎用量观测（进程级单例）。

按引擎名聚合：调用数 / 成功 / 失败 / 延迟，外加全局「降级次数」
（主引擎失败、最终由非主引擎或全部失败时 +1）。

风格对齐 src/ai/llm_cost.py：无新增依赖；JSON 供 /api/workspace/metrics，
Prometheus 文本由 Web 路由读 dump_prom() 拼接。绝不记录任何译文原文。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class TranslationEngineStats:
    __slots__ = ("_lock", "_rows", "_started_at", "_last_ts", "_fallbacks", "_total",
                 "_low_conf", "_conf_switches")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: Dict[str, Dict[str, float]] = {}
        self._started_at = time.time()
        self._last_ts = 0.0
        self._fallbacks = 0
        self._total = 0
        # K/M：置信度智能切换观测
        self._low_conf = 0       # 一次译文被判低置信（< min_confidence）的累计次数
        self._conf_switches = 0  # 因低置信实际切换到非主引擎结果的 translate() 调用数

    def record(self, engine: str, *, ok: bool, latency_ms: int = 0) -> None:
        engine = str(engine or "unknown")
        with self._lock:
            row = self._rows.get(engine)
            if row is None:
                row = {"calls": 0, "ok": 0, "fail": 0, "latency_ms_sum": 0}
                self._rows[engine] = row
            row["calls"] += 1
            row["ok" if ok else "fail"] += 1
            row["latency_ms_sum"] += max(0, int(latency_ms or 0))
            self._total += 1
            self._last_ts = time.time()

    def record_fallback(self) -> None:
        """主引擎未能直接产出、发生了降级/全失败时 +1。"""
        with self._lock:
            self._fallbacks += 1

    def record_low_confidence(self) -> None:
        """一条引擎译文被判低置信（< min_confidence）时 +1。"""
        with self._lock:
            self._low_conf += 1

    def record_confidence_switch(self) -> None:
        """因低置信实际切换、最终采用非主引擎结果的 translate() 调用 +1。"""
        with self._lock:
            self._conf_switches += 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            rows = []
            for name, v in sorted(self._rows.items()):
                calls = v["calls"]
                rows.append({
                    "engine": name,
                    "calls": int(calls),
                    "ok": int(v["ok"]),
                    "fail": int(v["fail"]),
                    "success_rate": round(v["ok"] / calls, 4) if calls else 0,
                    "avg_latency_ms": round(v["latency_ms_sum"] / calls, 1) if calls else 0,
                })
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "total_attempts": self._total,
                "fallbacks": self._fallbacks,
                "low_confidence": self._low_conf,
                "confidence_switches": self._conf_switches,
                "rows": rows,
            }

    def dump_prom(self) -> str:
        lines = [
            "# HELP translation_engine_attempts_total Translation engine attempts by engine",
            "# TYPE translation_engine_attempts_total counter",
            "# HELP translation_engine_fail_total Translation engine failures by engine",
            "# TYPE translation_engine_fail_total counter",
            "# HELP translation_engine_fallbacks_total Translation fallbacks (primary failed)",
            "# TYPE translation_engine_fallbacks_total counter",
            "# HELP translation_engine_low_confidence_total Translations judged low-confidence",
            "# TYPE translation_engine_low_confidence_total counter",
            "# HELP translation_engine_confidence_switches_total Calls switched to non-primary by confidence",
            "# TYPE translation_engine_confidence_switches_total counter",
        ]
        with self._lock:
            lines.append(f"translation_engine_fallbacks_total {self._fallbacks}")
            lines.append(f"translation_engine_low_confidence_total {self._low_conf}")
            lines.append(f"translation_engine_confidence_switches_total {self._conf_switches}")
            for name, v in self._rows.items():
                lbl = f'engine="{_esc(name)}"'
                lines.append(f'translation_engine_attempts_total{{{lbl}}} {int(v["calls"])}')
                lines.append(f'translation_engine_fail_total{{{lbl}}} {int(v["fail"])}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._rows.clear()
            self._fallbacks = 0
            self._total = 0
            self._last_ts = 0.0
            self._low_conf = 0
            self._conf_switches = 0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[TranslationEngineStats] = None
_LOCK = threading.Lock()


def get_translation_engine_stats() -> TranslationEngineStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = TranslationEngineStats()
    return _SINGLETON


__all__ = ["TranslationEngineStats", "get_translation_engine_stats"]
