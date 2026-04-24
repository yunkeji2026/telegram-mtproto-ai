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
