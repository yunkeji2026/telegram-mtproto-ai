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
        cb_cooldown_sec: 300   # 加载失败后多久不再重试
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.backend = str(cfg.get("backend", "faster_whisper")).strip().lower()
        self.model_size = str(cfg.get("model_size", "base")).strip()
        self.device = str(cfg.get("device", "cpu")).strip().lower()
        self.compute_type = str(cfg.get("compute_type", "int8")).strip()
        self.language = str(cfg.get("language", "auto")).strip().lower()
        self.download_root = str(cfg.get("download_root", "models/whisper"))
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
                    # OpenAI Whisper REST API：由调用方注入 api_key，骨架先留口
                    import openai  # type: ignore  # noqa: F401
                    self._model = "openai-stub"
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
        rv = TranscribeResult(model=f"{self.backend}:{self.model_size}")
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
        loaded = await asyncio.get_event_loop().run_in_executor(
            None, self._load_model,
        )
        if not loaded:
            rv.error = f"model_load_failed: {self._last_error[:200]}"
            return rv

        def _do_transcribe() -> TranscribeResult:
            out = TranscribeResult(model=f"{self.backend}:{self.model_size}")
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
                    for seg in segments:
                        txt = str(getattr(seg, "text", "") or "").strip()
                        if txt:
                            texts.append(txt)
                    out.text = " ".join(texts).strip()
                    out.language = str(getattr(info, "language", "") or "")
                    out.duration_sec = float(
                        getattr(info, "duration", 0.0) or 0.0
                    )
                    out.ok = bool(out.text)
                elif self.backend == "openai":
                    # 骨架：实际接入需传 api_key + requests / openai client
                    out.error = "openai_backend_not_implemented"
                else:
                    out.error = f"unknown_backend: {self.backend}"
            except Exception as ex:
                out.error = f"{type(ex).__name__}: {ex}"
            return out

        try:
            rv = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _do_transcribe),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            rv.ok = False
            rv.error = f"transcribe_timeout({timeout_sec:.0f}s)"
        rv.latency_ms = int((time.monotonic() - t0) * 1000)
        rv.model = f"{self.backend}:{self.model_size}"
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
