"""audio_pipeline smoke — 防止未来 import 层面被破坏。

2026-04-24 PR 里修 asyncio.get_event_loop() → asyncio.to_thread() 时
发现该模块在 tests/ 下零覆盖（whisper 是 heavy 依赖 lazy import, 真正
跑 transcribe 需要音频 fixture）。至少加个纯 import smoke, CI 上能
exercise 模块语法 + top-level import chain。
"""
from __future__ import annotations


def test_audio_pipeline_imports_clean():
    from src.ai import audio_pipeline
    # 默认状态验证：TranscribeResult 有合理默认值
    r = audio_pipeline.TranscribeResult()
    assert r.ok is False
    assert r.text == ""
    assert r.latency_ms == 0


def test_audio_pipeline_class_discoverable():
    """确保 AudioPipeline 类依然可导入（防止模块结构被破坏）"""
    from src.ai.audio_pipeline import AudioPipeline  # noqa: F401


def test_audio_pipeline_uses_fallback_when_primary_empty(monkeypatch):
    import asyncio
    from src.ai.audio_pipeline import AudioPipeline, TranscribeResult

    class FakeFallback:
        async def _transcribe_file_once(self, path, *, language_hint=None, timeout_sec=30):
            return TranscribeResult(ok=True, text="fallback text", model="openai:whisper-1")

    async def primary_once(self, path, *, language_hint=None, timeout_sec=30):
        return TranscribeResult(ok=False, error="local failed", model="faster_whisper:base")

    p = AudioPipeline({"enabled": True, "fallback_enabled": True, "fallback_backend": "openai"})
    monkeypatch.setattr(p, "_transcribe_file_once", primary_once.__get__(p, AudioPipeline))
    monkeypatch.setattr(p, "_build_fallback_pipeline", lambda: FakeFallback())

    rv = asyncio.run(p.transcribe_file("dummy.wav"))

    assert rv.ok is True
    assert rv.text == "fallback text"
    assert rv.extra["fallback_used"] is True
    assert rv.extra["primary_error"] == "local failed"


def test_audio_pipeline_keeps_primary_when_long_enough(monkeypatch):
    import asyncio
    from src.ai.audio_pipeline import AudioPipeline, TranscribeResult

    async def primary_once(self, path, *, language_hint=None, timeout_sec=30):
        return TranscribeResult(ok=True, text="clear primary text", model="faster_whisper:base")

    p = AudioPipeline({
        "enabled": True,
        "fallback_enabled": True,
        "fallback_backend": "openai",
        "min_text_chars": 5,
    })
    monkeypatch.setattr(p, "_transcribe_file_once", primary_once.__get__(p, AudioPipeline))
    monkeypatch.setattr(
        p,
        "_build_fallback_pipeline",
        lambda: (_ for _ in ()).throw(AssertionError("fallback should not run")),
    )

    rv = asyncio.run(p.transcribe_file("dummy.wav"))

    assert rv.ok is True
    assert rv.text == "clear primary text"


def test_audio_pipeline_fallback_on_low_confidence(monkeypatch):
    import asyncio
    from src.ai.audio_pipeline import AudioPipeline, TranscribeResult

    class FakeFallback:
        async def _transcribe_file_once(self, path, *, language_hint=None, timeout_sec=30):
            return TranscribeResult(ok=True, text="online text", model="openai:whisper-1")

    async def primary_once(self, path, *, language_hint=None, timeout_sec=30):
        return TranscribeResult(
            ok=True,
            text="weak text",
            model="faster_whisper:base",
            extra={"avg_logprob": -1.2, "language_probability": 0.91},
        )

    p = AudioPipeline({
        "enabled": True,
        "fallback_enabled": True,
        "fallback_backend": "openai",
        "fallback_on_low_confidence": True,
        "min_avg_logprob": -0.8,
    })
    monkeypatch.setattr(p, "_transcribe_file_once", primary_once.__get__(p, AudioPipeline))
    monkeypatch.setattr(p, "_build_fallback_pipeline", lambda: FakeFallback())

    rv = asyncio.run(p.transcribe_file("dummy.wav"))

    assert rv.ok is True
    assert rv.text == "online text"
    assert rv.extra["primary_text"] == "weak text"


def test_audio_pipeline_model_label_uses_online_model():
    from src.ai.audio_pipeline import AudioPipeline

    p = AudioPipeline({
        "enabled": True,
        "backend": "openai",
        "model": "whisper-1",
    })

    assert p._model_label() == "openai:whisper-1"
