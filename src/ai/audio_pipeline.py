"""P7-2：音频理解流水线骨架（Whisper / faster-whisper）。

设计哲学：**懒加载 + 软降级**。
- 模型仅在 *首次 transcribe* 时加载（冷启动不付钱）。
- 不可用就返回空文本 + reason，调用链自动 fallback 到 media_ack。
- circuit breaker：模型加载失败后 5 分钟内不再重试，避免拖慢主循环。

MVP 只负责 **"有文件 → 文字 + 语种"**。
获取 voice 文件这一步（ADB pull / 屏幕录音捕获）由 `voice_grabber.py` 负责，
MVP 期间只提供抽象接口，不保证 Android 非 root 设备能拉到。

使用：
    from src.ai.audio_pipeline import get_audio_pipeline
    ap = get_audio_pipeline(config.get("audio_pipeline") or {})
    result = await ap.transcribe_file("/tmp/msg.m4a")
    if result.ok:
        print(result.text, result.language)
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class TranscribeResult:
    ok: bool = False
    text: str = ""
    language: str = ""       # ISO 639-1，如 zh / en / ja
    duration_sec: float = 0.0
    latency_ms: int = 0
    model: str = ""
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class AudioPipeline:
    """懒加载的 faster-whisper 包装。

    Config：
        enabled: true/false
        backend: faster_whisper | openai | disabled
        model_size: tiny | base | small | medium | large-v3
        device: cpu | cuda | auto
        compute_type: int8 | int8_float16 | float16
        language: auto | en | zh ...
        download_root: models/whisper
        api_key: online backend API key
        base_url: optional OpenAI-compatible base URL
        model: online ASR model name, defaults to whisper-1
        fallback_enabled: true/false
        fallback_backend: openai
        fallback_model: online ASR model for fallback
        min_text_chars: fallback when primary returns too little text
        fallback_on_low_confidence: true/false
        min_avg_logprob: fallback when faster-whisper avg logprob is lower
        cb_cooldown_sec: 300   # 加载失败后多久不再重试
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self._cfg = dict(cfg)
        self.enabled = bool(cfg.get("enabled", False))
        self.backend = str(cfg.get("backend", "faster_whisper")).strip().lower()
        self.model_size = str(cfg.get("model_size", "base")).strip()
        self.device = str(cfg.get("device", "cpu")).strip().lower()
        self.compute_type = str(cfg.get("compute_type", "int8")).strip()
        self.language = str(cfg.get("language", "auto")).strip().lower()
        self.download_root = str(cfg.get("download_root", "models/whisper"))
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.online_model = str(
            cfg.get("model") or cfg.get("online_model") or "whisper-1"
        ).strip()
        self.min_text_chars = int(cfg.get("min_text_chars", 1) or 1)
        self.fallback_on_low_confidence = bool(
            cfg.get("fallback_on_low_confidence", False)
        )
        self.min_avg_logprob = float(cfg.get("min_avg_logprob", -1.0) or -1.0)
        self.min_language_probability = float(
            cfg.get("min_language_probability", 0.0) or 0.0
        )
        self.fallback_enabled = bool(
            cfg.get("fallback_enabled", False)
            or isinstance(cfg.get("fallback"), dict)
        )
        fallback_cfg = cfg.get("fallback") if isinstance(cfg.get("fallback"), dict) else {}
        self.fallback_backend = str(
            cfg.get("fallback_backend")
            or fallback_cfg.get("backend")
            or "openai"
        ).strip().lower()
        self.fallback_model = str(
            cfg.get("fallback_model")
            or fallback_cfg.get("model")
            or cfg.get("online_model")
            or cfg.get("model")
            or "whisper-1"
        ).strip()
        self.fallback_api_key = str(
            cfg.get("fallback_api_key")
            or fallback_cfg.get("api_key")
            or self.api_key
        ).strip()
        self.fallback_base_url = str(
            cfg.get("fallback_base_url")
            or fallback_cfg.get("base_url")
            or self.base_url
            or ""
        ).strip().rstrip("/")
        self.cb_cooldown_sec = float(cfg.get("cb_cooldown_sec", 300) or 300)

        self._model: Any = None
        self._lock = threading.Lock()
        self._cb_open_until: float = 0.0
        self._last_error: str = ""
        self._load_attempts: int = 0

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        if self._cb_open_until > 0 and time.time() < self._cb_open_until:
            return False
        return True

    def _model_label(self) -> str:
        if self.backend == "faster_whisper":
            return f"{self.backend}:{self.model_size}"
        return f"{self.backend}:{self.online_model}"

    def _load_model(self) -> bool:
        if self._model is not None:
            return True
        if self._cb_open_until > 0 and time.time() < self._cb_open_until:
            return False
        if self.backend == "disabled":
            return False

        with self._lock:
            if self._model is not None:
                return True
            t0 = time.monotonic()
            try:
                if self.backend == "faster_whisper":
                    # 延迟 import：缺依赖直接走 circuit break
                    from faster_whisper import WhisperModel  # type: ignore
                    os.makedirs(self.download_root, exist_ok=True)
                    device = self.device if self.device != "auto" else "cpu"
                    self._model = WhisperModel(
                        self.model_size,
                        device=device,
                        compute_type=self.compute_type,
                        download_root=self.download_root,
                    )
                elif self.backend == "openai":
                    from openai import OpenAI  # type: ignore
                    if not self.api_key:
                        self._last_error = "missing api_key for openai ASR"
                        self._cb_open_until = time.time() + self.cb_cooldown_sec
                        return False
                    kwargs: Dict[str, Any] = {"api_key": self.api_key}
                    if self.base_url:
                        kwargs["base_url"] = self.base_url
                    self._model = OpenAI(**kwargs)
                else:
                    self._last_error = f"unknown backend {self.backend}"
                    self._cb_open_until = time.time() + self.cb_cooldown_sec
                    return False
                ms = int((time.monotonic() - t0) * 1000)
                logger.info(
                    "[audio_pipeline] model loaded backend=%s size=%s device=%s "
                    "compute=%s took=%dms",
                    self.backend, self.model_size, self.device,
                    self.compute_type, ms,
                )
                self._load_attempts += 1
                return True
            except ImportError as ex:
                self._last_error = (
                    f"missing dependency: {ex!s} "
                    "(pip install faster-whisper)"
                )
                self._cb_open_until = time.time() + self.cb_cooldown_sec
                logger.warning(
                    "[audio_pipeline] %s, circuit open for %.0fs",
                    self._last_error, self.cb_cooldown_sec,
                )
                return False
            except Exception as ex:
                self._last_error = f"{type(ex).__name__}: {ex}"
                self._cb_open_until = time.time() + self.cb_cooldown_sec
                logger.warning(
                    "[audio_pipeline] model load failed: %s, circuit open",
                    self._last_error, exc_info=True,
                )
                return False

    async def transcribe_file(
        self,
        path: str,
        *,
        language_hint: Optional[str] = None,
        timeout_sec: float = 30.0,
    ) -> TranscribeResult:
        """异步转写。失败返回 ok=False + error；调用方自行降级。"""
        rv = await self._transcribe_file_once(
            path,
            language_hint=language_hint,
            timeout_sec=timeout_sec,
        )
        if not self._should_try_fallback(rv):
            return rv
        fallback = self._build_fallback_pipeline()
        if fallback is None:
            return rv
        fb = await fallback._transcribe_file_once(
            path,
            language_hint=language_hint,
            timeout_sec=timeout_sec,
        )
        fb.extra["primary_ok"] = rv.ok
        fb.extra["primary_text"] = rv.text[:200]
        fb.extra["primary_error"] = rv.error[:300]
        fb.extra["primary_model"] = rv.model
        fb.extra["fallback_used"] = True
        if fb.ok and fb.text:
            return fb
        rv.extra["fallback_attempted"] = True
        rv.extra["fallback_error"] = fb.error[:300]
        rv.extra["fallback_model"] = fb.model
        return rv

    def _should_try_fallback(self, rv: TranscribeResult) -> bool:
        if not self.fallback_enabled:
            return False
        if self.backend == self.fallback_backend:
            return False
        if not rv.ok:
            return True
        text_len = len((rv.text or "").strip())
        if text_len < max(1, self.min_text_chars):
            return True
        if not self.fallback_on_low_confidence:
            return False
        avg = rv.extra.get("avg_logprob")
        if isinstance(avg, (int, float)) and float(avg) < self.min_avg_logprob:
            return True
        lang_prob = rv.extra.get("language_probability")
        if (
            isinstance(lang_prob, (int, float))
            and self.min_language_probability > 0
            and float(lang_prob) < self.min_language_probability
        ):
            return True
        return False

    def _build_fallback_pipeline(self) -> Optional["AudioPipeline"]:
        if not self.fallback_backend or self.fallback_backend == "disabled":
            return None
        cfg = dict(self._cfg)
        cfg["enabled"] = True
        cfg["backend"] = self.fallback_backend
        cfg["model"] = self.fallback_model
        cfg["api_key"] = self.fallback_api_key
        cfg["base_url"] = self.fallback_base_url
        cfg["fallback_enabled"] = False
        if self.fallback_backend != "faster_whisper":
            cfg.setdefault("model_size", self.model_size)
        return AudioPipeline(cfg)

    async def _transcribe_file_once(
        self,
        path: str,
        *,
        language_hint: Optional[str] = None,
        timeout_sec: float = 30.0,
    ) -> TranscribeResult:
        rv = TranscribeResult(model=self._model_label())
        if not self.enabled:
            rv.error = "pipeline_disabled"
            return rv
        if self._cb_open_until > 0 and time.time() < self._cb_open_until:
            rv.error = f"cb_open: {self._last_error[:200]}"
            return rv
        if not os.path.isfile(path):
            rv.error = f"file_not_found: {path}"
            return rv

        t0 = time.monotonic()
        loaded = await asyncio.to_thread(self._load_model)
        if not loaded:
            rv.error = f"model_load_failed: {self._last_error[:200]}"
            return rv

        def _do_transcribe() -> TranscribeResult:
            out = TranscribeResult(model=self._model_label())
            try:
                if self.backend == "faster_whisper":
                    lang = (
                        None
                        if (self.language in ("auto", ""))
                        and not language_hint
                        else (language_hint or self.language)
                    )
                    segments, info = self._model.transcribe(
                        path,
                        language=lang,
                        beam_size=1,      # 最快
                        vad_filter=True,  # 过滤静音
                    )
                    texts = []
                    avg_logprobs = []
                    no_speech_probs = []
                    compression_ratios = []
                    for seg in segments:
                        txt = str(getattr(seg, "text", "") or "").strip()
                        if txt:
                            texts.append(txt)
                        if getattr(seg, "avg_logprob", None) is not None:
                            avg_logprobs.append(float(getattr(seg, "avg_logprob")))
                        if getattr(seg, "no_speech_prob", None) is not None:
                            no_speech_probs.append(float(getattr(seg, "no_speech_prob")))
                        if getattr(seg, "compression_ratio", None) is not None:
                            compression_ratios.append(float(getattr(seg, "compression_ratio")))
                    out.text = " ".join(texts).strip()
                    out.language = str(getattr(info, "language", "") or "")
                    out.duration_sec = float(
                        getattr(info, "duration", 0.0) or 0.0
                    )
                    lang_prob = getattr(info, "language_probability", None)
                    if lang_prob is not None:
                        out.extra["language_probability"] = float(lang_prob)
                    if avg_logprobs:
                        out.extra["avg_logprob"] = sum(avg_logprobs) / len(avg_logprobs)
                    if no_speech_probs:
                        out.extra["max_no_speech_prob"] = max(no_speech_probs)
                    if compression_ratios:
                        out.extra["max_compression_ratio"] = max(compression_ratios)
                    out.extra["segments"] = len(texts)
                    out.ok = bool(out.text)
                elif self.backend == "openai":
                    lang = (
                        None
                        if (self.language in ("auto", "")) and not language_hint
                        else (language_hint or self.language)
                    )
                    with open(path, "rb") as f:
                        kwargs: Dict[str, Any] = {
                            "model": self.online_model,
                            "file": f,
                            "response_format": "json",
                        }
                        if lang:
                            kwargs["language"] = lang
                        resp = self._model.audio.transcriptions.create(**kwargs)
                    text = str(getattr(resp, "text", "") or "").strip()
                    out.text = text
                    out.language = lang or ""
                    out.ok = bool(text)
                else:
                    out.error = f"unknown_backend: {self.backend}"
            except Exception as ex:
                out.error = f"{type(ex).__name__}: {ex}"
            return out

        try:
            rv = await asyncio.wait_for(
                asyncio.to_thread(_do_transcribe),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            rv.ok = False
            rv.error = f"transcribe_timeout({timeout_sec:.0f}s)"
        rv.latency_ms = int((time.monotonic() - t0) * 1000)
        rv.model = self._model_label()
        logger.info(
            "[audio_pipeline] transcribe path=%s ok=%s lang=%s dur=%.1fs "
            "len=%d latency=%dms err=%r",
            path, rv.ok, rv.language, rv.duration_sec, len(rv.text),
            rv.latency_ms, rv.error[:120],
        )
        return rv

    def reset_circuit_breaker(self) -> None:
        self._cb_open_until = 0.0
        self._last_error = ""

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "model_size": self.model_size,
            "device": self.device,
            "loaded": self._model is not None,
            "load_attempts": self._load_attempts,
            "cb_open": self._cb_open_until > time.time(),
            "cb_remaining_sec": max(0.0, self._cb_open_until - time.time()),
            "last_error": self._last_error,
        }


# ── 单例 ────────────────────────────────────────────

_pipeline_singleton: Optional[AudioPipeline] = None
_pipeline_lock = threading.Lock()


def get_audio_pipeline(cfg: Optional[Dict[str, Any]] = None) -> AudioPipeline:
    """进程级单例；首次传 cfg 初始化，之后复用（不再覆盖 cfg）。"""
    global _pipeline_singleton
    if _pipeline_singleton is not None:
        return _pipeline_singleton
    with _pipeline_lock:
        if _pipeline_singleton is None:
            _pipeline_singleton = AudioPipeline(cfg or {})
    return _pipeline_singleton


def reset_audio_pipeline() -> None:
    """测试辅助：清空单例。"""
    global _pipeline_singleton
    with _pipeline_lock:
        _pipeline_singleton = None
