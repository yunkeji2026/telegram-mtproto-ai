"""P58-2：语音转写→翻译 + 媒体文本缓存 测试。"""

import os
import tempfile

import pytest

from src.ai.media_text_cache import MediaTextCache, get_media_text_cache, hash_file
from src.ai.voice_translate import VoiceTranslateService, decode_audio_to_temp


class _FakeXlate:
    async def translate(self, text, *, target_lang="zh", source_lang="", style="chat"):
        captured_src = source_lang

        class R:
            ok = True
            source_lang = captured_src or "en"

            def to_dict(self):
                return {"translated_text": "译:" + text, "provider": "ai"}

        return R()


class _Rv:
    def __init__(self, ok=True, text="", language="en", latency_ms=12, model="faster_whisper:base", extra=None):
        self.ok = ok
        self.text = text
        self.language = language
        self.latency_ms = latency_ms
        self.model = model
        self.error = ""
        self.extra = extra or {}


# ── decode_audio_to_temp ─────────────────────────────────────────────────
def test_decode_audio_valid_ogg():
    import base64
    b64 = base64.b64encode(b"OggS-fake-audio-bytes").decode()
    path, reason = decode_audio_to_temp("data:audio/ogg;base64," + b64)
    assert reason == "ok" and path and os.path.exists(path)
    os.remove(path)


def test_decode_audio_unsupported_mime():
    import base64
    b64 = base64.b64encode(b"x").decode()
    path, reason = decode_audio_to_temp("data:image/png;base64," + b64)
    assert path is None and reason.startswith("unsupported_mime")


def test_decode_audio_empty():
    path, reason = decode_audio_to_temp("")
    assert path is None and reason == "empty"


# ── VoiceTranslateService ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_voice_translate_happy_path():
    async def _tr(_p):
        return _Rv(ok=True, text="hello", language="en")

    svc = VoiceTranslateService(_FakeXlate(), _tr)
    out = await svc.translate_voice("/tmp/nope.ogg", target_lang="zh")
    assert out["ok"] is True
    assert out["transcript"] == "hello"
    assert out["asr_language"] == "en"
    assert out["translation"]["translated_text"] == "译:hello"


@pytest.mark.asyncio
async def test_voice_translate_no_speech():
    async def _tr(_p):
        return _Rv(ok=True, text="   ", language="")

    svc = VoiceTranslateService(_FakeXlate(), _tr)
    out = await svc.translate_voice("/tmp/nope.ogg")
    assert out["ok"] is False and out["reason"] == "no_speech"


@pytest.mark.asyncio
async def test_voice_translate_asr_failed():
    async def _tr(_p):
        rv = _Rv(ok=False, text="")
        rv.error = "model_load_failed"
        return rv

    svc = VoiceTranslateService(_FakeXlate(), _tr)
    out = await svc.translate_voice("/tmp/nope.ogg")
    assert out["ok"] is False and out["reason"] == "asr_failed"


@pytest.mark.asyncio
async def test_voice_translate_exception_tolerated():
    async def _tr(_p):
        raise RuntimeError("boom")

    svc = VoiceTranslateService(_FakeXlate(), _tr)
    out = await svc.translate_voice("/tmp/nope.ogg")
    assert out["ok"] is False and out["reason"] == "asr_error"


@pytest.mark.asyncio
async def test_voice_translate_records_fallback():
    from src.ai.provider_stats import get_provider_stats
    stats = get_provider_stats("asr", "asr")
    stats.reset()
    try:
        async def _tr(_p):
            return _Rv(ok=True, text="hi", extra={"fallback_used": True})

        svc = VoiceTranslateService(_FakeXlate(), _tr)
        await svc.translate_voice("/tmp/nope.ogg")
        d = stats.dump()
        assert d["fallbacks"] == 1
    finally:
        stats.reset()


@pytest.mark.asyncio
async def test_voice_translate_uses_media_cache_on_repeat():
    """同一音频文件二次识别命中缓存，不再调用 ASR。"""
    get_media_text_cache().reset()
    fd, path = tempfile.mkstemp(prefix="vt_test_", suffix=".ogg")
    with os.fdopen(fd, "wb") as f:
        f.write(b"some-stable-audio-bytes-for-hash")
    calls = {"n": 0}

    async def _tr(_p):
        calls["n"] += 1
        return _Rv(ok=True, text="repeat me", language="en")

    try:
        svc = VoiceTranslateService(_FakeXlate(), _tr)
        out1 = await svc.translate_voice(path, target_lang="zh")
        out2 = await svc.translate_voice(path, target_lang="zh")
        assert out1["ok"] and out2["ok"]
        assert calls["n"] == 1            # 第二次走缓存
        assert out2["asr_cached"] is True
    finally:
        os.remove(path)
        get_media_text_cache().reset()


# ── MediaTextCache ───────────────────────────────────────────────────────
def test_media_cache_get_put_and_eviction():
    c = MediaTextCache(max_entries=2, ttl_sec=0)
    c.put("a", "x")
    c.put("b", "y")
    assert c.get("a") == "x"
    c.put("c", "z")  # 触发淘汰（LRU：b 最久未用被淘汰，a 刚 get 过）
    assert c.get("b") is None
    assert c.get("a") == "x" and c.get("c") == "z"


def test_media_cache_ttl_expiry():
    import time
    c = MediaTextCache(max_entries=10, ttl_sec=0.05)
    c.put("k", "v")
    assert c.get("k") == "v"
    time.sleep(0.08)
    assert c.get("k") is None


def test_hash_file_missing_returns_none():
    assert hash_file("/no/such/file.bin") is None
