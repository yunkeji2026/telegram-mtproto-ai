"""P58-2：语音转写（ASR）→ 翻译 服务。

复用现有 `AudioPipeline`（faster-whisper / OpenAI ASR，自带 circuit breaker +
在线兜底）做转写，再把文本喂给 `TranslationService`（自动术语强制 + 品牌保护）。
ASR 用量走 P58 通用 `ProviderStats` 的 "asr" namespace；结果按媒体 hash 缓存。

设计要点：
- ``transcribe_fn`` 可注入（异步 ``(path) -> TranscribeResult-like``），单测无需真模型。
- 永不抛异常给上层：失败返回 ``ok=False`` + reason。
- 隐私：不持久化音频字节；临时文件由调用方清理。
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from src.ai.media_text_cache import get_media_text_cache, hash_file
from src.ai.provider_stats import get_provider_stats
from src.ai.translation_service import TranslationService, detect_language

logger = logging.getLogger(__name__)

_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25MB（与常见在线 ASR 上限一致）
_ALLOWED_MIME = {
    "audio/ogg", "audio/opus", "audio/mpeg", "audio/mp3", "audio/mp4",
    "audio/m4a", "audio/x-m4a", "audio/wav", "audio/x-wav", "audio/webm",
    "audio/amr", "audio/aac",
}
_MIME_SUFFIX = {
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/webm": ".webm", "audio/amr": ".amr", "audio/aac": ".aac",
}

# transcribe_fn: async (path) -> result with .ok/.text/.language/.latency_ms/.model/.extra
TranscribeFn = Callable[[str], Awaitable[Any]]


def decode_audio_to_temp(audio_b64: str) -> Tuple[Optional[str], str]:
    """把（可带 data URL 头的）base64 音频落临时文件。返回 (path|None, reason)。"""
    raw = str(audio_b64 or "").strip()
    if not raw:
        return None, "empty"
    mime = "audio/ogg"
    if raw.startswith("data:"):
        header, _, payload = raw.partition(",")
        if not payload:
            return None, "bad_data_url"
        mime = header[5:].split(";")[0].strip().lower() or mime
        raw = payload
    if mime not in _ALLOWED_MIME:
        return None, f"unsupported_mime:{mime}"
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception:
        return None, "decode_failed"
    if not data:
        return None, "empty_after_decode"
    if len(data) > _MAX_AUDIO_BYTES:
        return None, "too_large"
    suffix = _MIME_SUFFIX.get(mime, ".ogg")
    try:
        fd, path = tempfile.mkstemp(prefix="voicexl_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path, "ok"
    except Exception as exc:  # noqa: BLE001
        return None, f"write_failed:{type(exc).__name__}"


class VoiceTranslateService:
    def __init__(self, translation_service: TranslationService, transcribe_fn: TranscribeFn) -> None:
        self._xlate = translation_service
        self._transcribe = transcribe_fn

    async def translate_voice(
        self,
        audio_path: str,
        *,
        target_lang: str = "zh",
        source_lang: str = "",
        style: str = "chat",
    ) -> Dict[str, Any]:
        stats = get_provider_stats("asr", "asr")
        cache = get_media_text_cache()
        h = hash_file(audio_path)
        ck = f"asr:{h}" if h else ""
        cached = cache.get(ck) if ck else None

        transcript = ""
        language = ""
        asr_model = "cache"
        asr_cached = False
        if cached is not None:
            transcript, asr_cached = cached, True
        else:
            t0 = time.monotonic()
            try:
                rv = await self._transcribe(audio_path)
            except Exception as exc:  # noqa: BLE001
                stats.record("asr", ok=False, latency_ms=int((time.monotonic() - t0) * 1000))
                logger.warning("ASR 调用异常: %s", exc)
                return {"ok": False, "reason": "asr_error", "asr_tag": f"error:{type(exc).__name__}"}

            transcript = (getattr(rv, "text", "") or "").strip()
            language = getattr(rv, "language", "") or ""
            asr_model = getattr(rv, "model", "") or "asr"
            lat = getattr(rv, "latency_ms", None)
            if not isinstance(lat, int):
                lat = int((time.monotonic() - t0) * 1000)
            ok = bool(getattr(rv, "ok", False) and transcript)
            stats.record(asr_model, ok=ok, latency_ms=lat)
            extra = getattr(rv, "extra", None) or {}
            if extra.get("fallback_used"):
                stats.record_fallback()
            if not ok:
                reason = "no_speech" if getattr(rv, "ok", False) else "asr_failed"
                return {"ok": False, "reason": reason,
                        "asr_error": (getattr(rv, "error", "") or "")[:200],
                        "transcript": ""}
            if transcript and ck:
                cache.put(ck, transcript)

        if not transcript:
            return {"ok": False, "reason": "no_speech", "transcript": ""}

        src = source_lang or language or detect_language(transcript)
        result = await self._xlate.translate(
            transcript, target_lang=target_lang, source_lang=src, style=style,
        )
        return {
            "ok": bool(result.ok),
            "transcript": transcript,
            "asr_language": language,
            "asr_model": asr_model,
            "asr_cached": asr_cached,
            "source_lang": result.source_lang,
            "translation": result.to_dict(),
        }


def build_audio_transcribe_fn(audio_cfg: Dict[str, Any]) -> TranscribeFn:
    """生产用 transcribe fn：包一层 AudioPipeline 单例。"""

    async def _tr(audio_path: str):
        from src.ai.audio_pipeline import get_audio_pipeline
        ap = get_audio_pipeline(audio_cfg)
        return await ap.transcribe_file(audio_path)

    return _tr


__all__ = [
    "VoiceTranslateService",
    "decode_audio_to_temp",
    "build_audio_transcribe_fn",
]
