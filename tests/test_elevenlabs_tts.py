"""ElevenLabs v3 客户端纯函数 + TTSPipeline 接线单测。"""
from __future__ import annotations

import asyncio

import pytest

from src.ai.elevenlabs_client import (
    build_tts_body,
    output_format_for,
    parse_tts_response,
)
from src.ai.voice_emotion import EmotionSpec, NEUTRAL, elevenlabs_voice_settings


# ── 纯函数 ───────────────────────────────────────────────────────────────────
def test_output_format_mapping():
    assert output_format_for("mp3") == "mp3_44100_128"
    assert output_format_for("ogg") == "opus_48000_128"
    assert output_format_for("opus") == "opus_48000_128"
    assert output_format_for("weird") == "mp3_44100_128"


def test_build_tts_body_has_model_and_settings():
    import json
    body = build_tts_body("你好", model_id="eleven_v3",
                          voice_settings={"stability": 0.3})
    data = json.loads(body.decode())
    assert data["text"] == "你好"
    assert data["model_id"] == "eleven_v3"
    assert data["voice_settings"]["stability"] == 0.3


def test_parse_tts_response_audio_passthrough():
    audio = b"\xff\xfb\x90fakeaudio"
    assert parse_tts_response(audio, "audio/mpeg") == audio


def test_parse_tts_response_json_error_raises():
    with pytest.raises(RuntimeError) as ei:
        parse_tts_response(b'{"detail": "invalid api key"}', "application/json")
    assert "elevenlabs_api_error" in str(ei.value)
    assert "invalid api key" in str(ei.value)


def test_parse_tts_response_detects_json_by_brace():
    # content-type 缺失也能靠首字节 { 判断为错误 JSON
    with pytest.raises(RuntimeError):
        parse_tts_response(b'{"detail": {"status": "quota_exceeded"}}', "")


def test_parse_tts_response_empty_raises():
    with pytest.raises(RuntimeError):
        parse_tts_response(b"", "audio/mpeg")


# ── 情绪 → voice_settings ────────────────────────────────────────────────────
def test_voice_settings_neutral_defaults():
    s = elevenlabs_voice_settings(NEUTRAL)
    assert s["stability"] == 0.5
    assert s["style"] == 0.0
    assert s["use_speaker_boost"] is True
    assert "speed" not in s  # normal pace 不带 speed


def test_voice_settings_excited_more_expressive():
    s = elevenlabs_voice_settings(EmotionSpec("excited", intensity=1.0))
    assert s["stability"] < 0.5          # 调低 → 更听情绪
    assert s["style"] > 0.0              # 放大个性
    assert s["similarity_boost"] == 0.75


def test_voice_settings_speed_from_pace():
    s = elevenlabs_voice_settings(EmotionSpec("empathetic", intensity=0.8, pace="slow"))
    assert s["speed"] == 0.92


def test_voice_settings_similarity_override():
    s = elevenlabs_voice_settings(EmotionSpec("warm"), similarity_boost=0.9)
    assert s["similarity_boost"] == 0.9


# ── TTSPipeline 接线 ─────────────────────────────────────────────────────────
def test_pipeline_elevenlabs_synthesizes(tmp_path, monkeypatch):
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache
    from src.ai import elevenlabs_client as EC

    reset_tts_cache()
    seen = {}

    def fake_synth(self, text, voice_id, out, *, emotion=None, output_format="mp3_44100_128"):
        seen["voice_id"] = voice_id
        seen["emotion"] = emotion
        seen["output_format"] = output_format
        seen["api_key"] = self.api_key
        out.write_bytes(b"\xff\xfb" + b"\x00" * 600)

    monkeypatch.setattr(EC.ElevenLabsClient, "synthesize", fake_synth)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "elevenlabs",
            "format": "mp3", "out_dir": str(tmp_path),
            "elevenlabs": {"api_key": "xi-test"},
            "voice_profile": {
                "enabled": True, "owner_consent": True,
                "backend": "elevenlabs",
                "voice": "EL_VOICE_123",      # 云端音色 ID 优先
                "speaker_id": "human_label",
            },
        })
        rv = await p.synthesize("恭喜你成交！", emotion="excited")
        assert rv.ok is True
        assert rv.provider == "elevenlabs"
        assert seen["voice_id"] == "EL_VOICE_123"
        assert seen["api_key"] == "xi-test"
        assert seen["output_format"] == "mp3_44100_128"
        assert seen["emotion"].emotion == "excited"

    asyncio.run(run())
    reset_tts_cache()


def test_pipeline_elevenlabs_missing_key_surfaces_not_fallback(tmp_path, monkeypatch):
    """缺 api_key 是本地 misconfig → 必须硬失败暴露，不得静默回落 edge。"""
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache

    reset_tts_cache()
    edge_called = {"n": 0}

    async def fake_edge(self, text, out, voice, spec=None):
        edge_called["n"] += 1
        out.write_bytes(b"ID3" + b"\x00" * 600)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "elevenlabs",
            "format": "mp3", "out_dir": str(tmp_path),
            "elevenlabs": {},  # 没有 api_key
            "voice_profile": {
                "enabled": True, "owner_consent": True,
                "backend": "elevenlabs", "voice": "EL_VOICE_123",
            },
        })
        rv = await p.synthesize("你好")
        assert rv.ok is False
        assert "elevenlabs_missing_api_key" in rv.error
        assert edge_called["n"] == 0  # 不兜底

    asyncio.run(run())
    reset_tts_cache()


def test_pipeline_elevenlabs_api_error_falls_back_to_edge(tmp_path, monkeypatch):
    """API 侧错误（如配额/401）→ 回落 edge 出声（有声音胜过硬失败）。"""
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache
    from src.ai import elevenlabs_client as EC

    reset_tts_cache()
    calls = {"el": 0, "edge": 0}

    def fake_synth(self, text, voice_id, out, *, emotion=None, output_format="mp3_44100_128"):
        calls["el"] += 1
        raise RuntimeError("elevenlabs_api_error: quota_exceeded")

    async def fake_edge(self, text, out, voice, spec=None):
        calls["edge"] += 1
        out.write_bytes(b"ID3" + b"\x00" * 600)

    monkeypatch.setattr(EC.ElevenLabsClient, "synthesize", fake_synth)
    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "elevenlabs",
            "format": "mp3", "out_dir": str(tmp_path),
            "elevenlabs": {"api_key": "xi-test"},
            "voice_profile": {
                "enabled": True, "owner_consent": True,
                "backend": "elevenlabs", "voice": "EL_VOICE_123",
            },
        })
        rv = await p.synthesize("你好")
        assert rv.ok is True
        assert rv.provider == "edge_tts"
        assert rv.extra.get("fallback_from") == "elevenlabs"
        assert "quota_exceeded" in rv.extra.get("primary_error", "")
        assert calls["el"] == 1 and calls["edge"] == 1

    asyncio.run(run())
    reset_tts_cache()
