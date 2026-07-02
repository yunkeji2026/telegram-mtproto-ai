"""V：语音克隆合成的「语言纠正」用量观测（进程级单例）。

用途：证明 P2「合成语言随文本实际语种」的纠正在生产**真的在生效、纠了多少**——
按目标语种聚合被纠正次数（config 默认语言 → 文本实际语种，如 zh→en），外加合成总数，
可算「纠正率」。定位「中文声纹念英文」缺陷修复后的上线观测（对齐 M/P 翻译置信度观测）。

风格对齐 ``src/ai/translation_engine_stats.py``：无新增依赖；JSON 供 /api/workspace/metrics，
Prometheus 文本由 Web 路由读 ``dump_prom()`` 拼接。**绝不记录任何合成文本原文**，只记语种码。
best-effort：``record`` 任何异常都吞掉，绝不阻塞合成主链路。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class VoiceSynthLangStats:
    __slots__ = ("_lock", "_total", "_corrected", "_by_lang", "_started_at", "_last_ts")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._total = 0                     # 克隆合成到达「语言决策」的总次数
        self._corrected = 0                 # 合成语言 ≠ config 默认（发生纠正）的次数
        self._by_lang: Dict[str, int] = {}  # 纠正后目标语种分布 {en: N, ja: M, ...}
        self._started_at = time.time()
        self._last_ts = 0.0

    def record(self, *, default_lang: str, used_lang: str) -> None:
        """记一次克隆合成语言决策。``used_lang`` ≠ ``default_lang`` → 记一次纠正并按目标语种归类。"""
        try:
            d = str(default_lang or "").strip().lower()
            u = str(used_lang or "").strip().lower()
        except Exception:
            return
        with self._lock:
            self._total += 1
            self._last_ts = time.time()
            if u and u != d:
                self._corrected += 1
                self._by_lang[u] = self._by_lang.get(u, 0) + 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            total = self._total
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "total_synth": int(total),
                "corrected": int(self._corrected),
                "corrected_rate": round(self._corrected / total, 4) if total else 0,
                "by_lang": dict(sorted(self._by_lang.items())),
            }

    def dump_prom(self) -> str:
        lines = [
            "# HELP voice_synth_total Total voice clone syntheses reaching language decision",
            "# TYPE voice_synth_total counter",
            "# HELP voice_synth_language_corrected_total Syntheses whose language was corrected from config default",
            "# TYPE voice_synth_language_corrected_total counter",
            "# HELP voice_synth_language_corrected_by_lang_total Language corrections by target language",
            "# TYPE voice_synth_language_corrected_by_lang_total counter",
        ]
        with self._lock:
            lines.append(f"voice_synth_total {self._total}")
            lines.append(f"voice_synth_language_corrected_total {self._corrected}")
            for lang, n in sorted(self._by_lang.items()):
                lines.append(
                    f'voice_synth_language_corrected_by_lang_total{{to_lang="{_esc(lang)}"}} {int(n)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._total = 0
            self._corrected = 0
            self._by_lang.clear()
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[VoiceSynthLangStats] = None
_LOCK = threading.Lock()


def get_voice_synth_stats() -> VoiceSynthLangStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = VoiceSynthLangStats()
    return _SINGLETON


__all__ = ["VoiceSynthLangStats", "get_voice_synth_stats"]
