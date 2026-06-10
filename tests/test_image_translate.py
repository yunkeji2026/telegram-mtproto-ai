"""P58：图片 OCR→翻译 + 通用 provider 用量统计 测试。"""

import os

import pytest

from src.ai.image_translate import (
    ImageTranslateService,
    _provider_from_tag,
    decode_image_to_temp,
)
from src.ai.provider_stats import ProviderStats, get_provider_stats

# 1x1 PNG
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _FakeXlate:
    """最小翻译服务桩：译文 = '译:'+原文。"""

    async def translate(self, text, *, target_lang="zh", source_lang="", style="chat"):
        captured_src = source_lang

        class R:
            ok = True
            source_lang = captured_src or "en"

            def to_dict(self):
                return {"translated_text": "译:" + text, "provider": "ai",
                        "source_lang": captured_src or "en", "target_lang": target_lang}

        return R()


# ── decode_image_to_temp ─────────────────────────────────────────────────
def test_decode_valid_png_writes_temp_and_cleanup():
    path, reason = decode_image_to_temp("data:image/png;base64," + _PNG_B64)
    assert reason == "ok" and path and os.path.exists(path)
    os.remove(path)


def test_decode_raw_b64_without_header():
    path, reason = decode_image_to_temp(_PNG_B64)  # 无 data: 头默认按 png
    assert reason == "ok" and path
    os.remove(path)


def test_decode_rejects_unsupported_mime():
    path, reason = decode_image_to_temp("data:application/pdf;base64," + _PNG_B64)
    assert path is None and reason.startswith("unsupported_mime")


def test_decode_empty_returns_reason():
    path, reason = decode_image_to_temp("")
    assert path is None and reason == "empty"


def test_decode_too_large_rejected():
    big = "data:image/png;base64," + ("QQQQ" * (3 * 1024 * 1024))  # ~9MB 解码后
    path, reason = decode_image_to_temp(big)
    assert path is None and reason == "too_large"


def test_provider_from_tag():
    assert _provider_from_tag("zhipu_only") == "zhipu"
    assert _provider_from_tag("ollama_ok") == "ollama"
    assert _provider_from_tag("ollama_empty|zhipu_fallback") == "zhipu"


# ── ImageTranslateService ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_image_translate_happy_path():
    async def _ocr(_path):
        return ("hello world", "zhipu_only")

    svc = ImageTranslateService(_FakeXlate(), _ocr)
    out = await svc.translate_image("/tmp/x.png", target_lang="zh")
    assert out["ok"] is True
    assert out["ocr_text"] == "hello world"
    assert out["translation"]["translated_text"] == "译:hello world"


@pytest.mark.asyncio
async def test_image_translate_empty_ocr_returns_no_text():
    async def _ocr(_path):
        return ("   ", "ollama_empty")

    svc = ImageTranslateService(_FakeXlate(), _ocr)
    out = await svc.translate_image("/tmp/x.png")
    assert out["ok"] is False and out["reason"] == "no_text"


@pytest.mark.asyncio
async def test_image_translate_ocr_exception_tolerated():
    async def _ocr(_path):
        raise RuntimeError("boom")

    svc = ImageTranslateService(_FakeXlate(), _ocr)
    out = await svc.translate_image("/tmp/x.png")
    assert out["ok"] is False and out["reason"] == "ocr_error"


@pytest.mark.asyncio
async def test_image_translate_records_ocr_stats_and_fallback():
    stats = get_provider_stats("ocr", "ocr")
    stats.reset()
    try:
        async def _ocr(_path):
            return ("hi", "ollama_empty|zhipu_fallback")

        svc = ImageTranslateService(_FakeXlate(), _ocr)
        await svc.translate_image("/tmp/x.png", target_lang="zh")
        d = stats.dump()
        assert d["total_attempts"] == 1
        assert d["fallbacks"] == 1  # tag 含 fallback → 记降级
        assert any(r["provider"] == "zhipu" for r in d["rows"])
    finally:
        stats.reset()


# ── 通用 ProviderStats ────────────────────────────────────────────────────
def test_provider_stats_dump_and_prom():
    s = ProviderStats("ocr")
    s.record("zhipu", ok=True, latency_ms=120)
    s.record("zhipu", ok=False, latency_ms=80)
    d = s.dump()
    row = d["rows"][0]
    assert row["calls"] == 2 and row["ok"] == 1 and row["fail"] == 1
    assert row["success_rate"] == 0.5 and row["avg_latency_ms"] == 100.0
    assert "ocr_attempts_total" in s.dump_prom()


def test_provider_stats_registry_is_singleton_per_namespace():
    a = get_provider_stats("asr")
    b = get_provider_stats("asr")
    assert a is b
