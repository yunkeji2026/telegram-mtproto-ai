"""ASR（语音转录）降级观测（进程级单例）。

用途：把「主 ASR 掉线 → 全链被迫走弱一档回落」这类**静默降级**变成**看板可见**。
主 ASR（如 Qwen3-ASR）不可达时，级联转录器会无缝回落到本机 faster-whisper，转录质量下降
（"包扎→爆炸""吃午饭→吃午穿"类错字随之增多），但除非翻日志否则无从察觉。这里按
主用/回落/全失败/幻觉丢弃累计，暴露 **fallback_rate**（回落率）——回落率飙高即主 ASR 出问题。

风格对齐 ``src/ai/voice_synth_stats.py``：无新增依赖；``dump()`` 供 /api/workspace/metrics，
``dump_prom()`` 供 Prometheus。**绝不记录任何转录文本原文**，只记计数与 provider 类名。
best-effort：``record*`` 任何异常都吞掉，绝不阻塞转录主链路。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class ASRTranscribeStats:
    __slots__ = (
        "_lock", "_primary_ok", "_fallback_ok", "_all_failed",
        "_hallucination_dropped", "_by_provider", "_started_at", "_last_ts",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._primary_ok = 0                    # 链首（主 ASR）直接产出可用转录
        self._fallback_ok = 0                   # 主失败、由回落级产出（=降级信号）
        self._all_failed = 0                    # 全链均未产出可用转录
        self._hallucination_dropped = 0         # 幻觉守卫丢弃的转录（等同该级返空）
        self._by_provider: Dict[str, int] = {}  # 回落成功的 provider 分布 {FasterWhisperTranscriber: N}
        self._started_at = time.time()
        self._last_ts = 0.0

    def record(self, *, ok: bool, level: int = 0, provider: str = "") -> None:
        """记一次**顶层**转录结果。

        - ``ok=True, level==0``：主 ASR 直接成功 → primary_ok
        - ``ok=True, level>=1``：回落级成功 → fallback_ok（按 provider 归类；降级信号）
        - ``ok=False``：全链失败 → all_failed
        """
        try:
            with self._lock:
                self._last_ts = time.time()
                if ok:
                    if int(level) <= 0:
                        self._primary_ok += 1
                    else:
                        self._fallback_ok += 1
                        p = str(provider or "").strip() or "unknown"
                        self._by_provider[p] = self._by_provider.get(p, 0) + 1
                else:
                    self._all_failed += 1
        except Exception:
            pass

    def record_hallucination(self, provider: str = "") -> None:
        """记一次「幻觉守卫丢弃」（可与后续回落成功并存：主幻觉→回落救回）。"""
        try:
            with self._lock:
                self._hallucination_dropped += 1
                self._last_ts = time.time()
        except Exception:
            pass

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            attempts = self._primary_ok + self._fallback_ok + self._all_failed
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "attempts": int(attempts),
                "primary_ok": int(self._primary_ok),
                "fallback_ok": int(self._fallback_ok),
                "all_failed": int(self._all_failed),
                "hallucination_dropped": int(self._hallucination_dropped),
                "fallback_rate": round(self._fallback_ok / attempts, 4) if attempts else 0,
                "failure_rate": round(self._all_failed / attempts, 4) if attempts else 0,
                "by_fallback_provider": dict(sorted(self._by_provider.items())),
            }

    def dump_prom(self) -> str:
        lines = [
            "# HELP asr_transcribe_primary_ok_total Transcriptions produced by the primary ASR",
            "# TYPE asr_transcribe_primary_ok_total counter",
            "# HELP asr_transcribe_fallback_ok_total Transcriptions produced by a fallback ASR (degradation signal)",
            "# TYPE asr_transcribe_fallback_ok_total counter",
            "# HELP asr_transcribe_all_failed_total Transcription requests where all ASR levels failed",
            "# TYPE asr_transcribe_all_failed_total counter",
            "# HELP asr_transcribe_hallucination_dropped_total Transcriptions dropped by the hallucination guard",
            "# TYPE asr_transcribe_hallucination_dropped_total counter",
            "# HELP asr_transcribe_fallback_ok_by_provider_total Fallback successes by provider",
            "# TYPE asr_transcribe_fallback_ok_by_provider_total counter",
        ]
        with self._lock:
            lines.append(f"asr_transcribe_primary_ok_total {self._primary_ok}")
            lines.append(f"asr_transcribe_fallback_ok_total {self._fallback_ok}")
            lines.append(f"asr_transcribe_all_failed_total {self._all_failed}")
            lines.append(
                f"asr_transcribe_hallucination_dropped_total {self._hallucination_dropped}")
            for prov, n in sorted(self._by_provider.items()):
                lines.append(
                    f'asr_transcribe_fallback_ok_by_provider_total{{provider="{_esc(prov)}"}} {int(n)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._primary_ok = 0
            self._fallback_ok = 0
            self._all_failed = 0
            self._hallucination_dropped = 0
            self._by_provider.clear()
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[ASRTranscribeStats] = None
_LOCK = threading.Lock()


def get_asr_stats() -> ASRTranscribeStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = ASRTranscribeStats()
    return _SINGLETON


__all__ = ["ASRTranscribeStats", "get_asr_stats"]
