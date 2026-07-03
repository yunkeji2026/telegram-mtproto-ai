"""音频情绪识别（SER）用量观测（进程级单例）。

用途：证明「音频情绪」在生产**真的在跑、听出了什么、可用性如何**——按声学情绪标签
聚合分布、统计置信命中率与不可用（软降级）次数。供 ops-overview「🎧 音频情绪」卡
与 Prometheus 观测「哪种情绪最多、模型是否常掉线回落」。

风格对齐 `src/ai/voice_synth_stats.py`：无新增依赖；`dump()` 供 /api/workspace/metrics，
`dump_prom()` 供 Prometheus。**绝不记录任何音频原文/转写文本**，只记情绪标签与计数。
best-effort：``record`` 任何异常都吞掉，绝不阻塞识别主链路。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class SpeechEmotionStats:
    __slots__ = ("_lock", "_total", "_ok", "_confident", "_unavailable",
                 "_by_emotion", "_started_at", "_last_ts")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._total = 0          # 尝试识别的语音条数
        self._ok = 0             # 模型成功返回（不含软降级）
        self._confident = 0      # 达到 min_confidence 的有效声学信号
        self._unavailable = 0    # 不可用/加载失败/异常 → 软降级次数
        self._by_emotion: Dict[str, int] = {}  # 置信命中的情绪分布 {sad:N, ...}
        self._started_at = time.time()
        self._last_ts = 0.0

    def record(self, *, ok: bool, emotion: str = "",
               confident: bool = False) -> None:
        """记一次识别。``emotion`` 为标准英文类名（仅在 confident 时计入分布）。"""
        try:
            emo = str(emotion or "").strip().lower()
        except Exception:
            emo = ""
        with self._lock:
            self._total += 1
            self._last_ts = time.time()
            if ok:
                self._ok += 1
                if confident and emo:
                    self._confident += 1
                    self._by_emotion[emo] = self._by_emotion.get(emo, 0) + 1
            else:
                self._unavailable += 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            total = self._total
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "total": int(total),
                "ok": int(self._ok),
                "confident": int(self._confident),
                "unavailable": int(self._unavailable),
                "unavailable_rate": round(self._unavailable / total, 4) if total else 0,
                "by_emotion": dict(sorted(self._by_emotion.items())),
            }

    def dump_prom(self) -> str:
        lines = [
            "# HELP speech_emotion_total Total voice clips submitted to SER",
            "# TYPE speech_emotion_total counter",
            "# HELP speech_emotion_ok_total SER calls that returned a result",
            "# TYPE speech_emotion_ok_total counter",
            "# HELP speech_emotion_unavailable_total SER calls that soft-degraded",
            "# TYPE speech_emotion_unavailable_total counter",
            "# HELP speech_emotion_by_emotion_total Confident SER hits by emotion",
            "# TYPE speech_emotion_by_emotion_total counter",
        ]
        with self._lock:
            lines.append(f"speech_emotion_total {self._total}")
            lines.append(f"speech_emotion_ok_total {self._ok}")
            lines.append(f"speech_emotion_unavailable_total {self._unavailable}")
            for emo, n in sorted(self._by_emotion.items()):
                lines.append(
                    f'speech_emotion_by_emotion_total{{emotion="{_esc(emo)}"}} {int(n)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._total = 0
            self._ok = 0
            self._confident = 0
            self._unavailable = 0
            self._by_emotion.clear()
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[SpeechEmotionStats] = None
_LOCK = threading.Lock()


def get_speech_emotion_stats() -> SpeechEmotionStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = SpeechEmotionStats()
    return _SINGLETON


__all__ = ["SpeechEmotionStats", "get_speech_emotion_stats"]
