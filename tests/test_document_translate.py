"""Phase L：文档整篇翻译服务单测。

覆盖：分段 / 空行保留 / 有界并发逐段翻译 / 单段失败回退 / 统计 / 引擎透传 /
上限保护（字符数、段数）/ 空输入。
"""
import pytest

from src.ai.document_translate import DocumentTranslateService, split_segments
from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_service import TranslationService


class _StubEngine:
    """把每行翻译成 ``<line>·<engine>``；指定行触发失败以测回退。"""
    def __init__(self, name="ai", *, fail_on=None):
        self.name = name
        self._fail_on = set(fail_on or [])

    @property
    def available(self):
        return True

    def supports_target(self, t):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        if text in self._fail_on:
            return EngineResult("", self.name, False, error="boom")
        return EngineResult(f"{text}·{self.name}", self.name, True)


def _svc(engine=None):
    s = TranslationService(ai_client=None)
    s._router = EngineRouter([engine or _StubEngine()])
    return DocumentTranslateService(s, max_concurrency=3)


# ── 分段 ──────────────────────────────────────────────────────────────────────

def test_split_segments_keeps_blank_lines():
    assert split_segments("a\n\nb") == ["a", "", "b"]


# ── 整篇翻译 ──────────────────────────────────────────────────────────────────

async def test_translate_document_basic_order_and_blanks():
    svc = _svc()
    out = await svc.translate_document("hello\n\nworld", target_lang="zh", source_lang="en")
    assert out["ok"] is True
    # 空行原样保留，段序不乱
    assert out["translated_text"] == "hello·ai\n\nworld·ai"
    assert out["stats"]["total"] == 3
    assert out["stats"]["translated"] == 2
    assert out["stats"]["skipped"] == 1
    assert out["stats"]["failed"] == 0


async def test_translate_document_segment_failure_falls_back_to_source():
    svc = _svc(_StubEngine(fail_on=["bad"]))
    out = await svc.translate_document("good\nbad", target_lang="zh", source_lang="en")
    assert out["ok"] is True  # 整体仍 ok（best-effort）
    assert out["translated_text"] == "good·ai\nbad"  # 失败段回退原文
    assert out["stats"]["failed"] == 1
    assert out["stats"]["translated"] == 1


async def test_translate_document_engine_passthrough():
    # engine 偏好透传到底层 translate（用 deepl 引擎名验证 provider）
    s = TranslationService(ai_client=None)
    s._router = EngineRouter([_StubEngine("ai"), _StubEngine("deepl")])
    svc = DocumentTranslateService(s)
    out = await svc.translate_document("x", target_lang="zh", source_lang="en", engine="deepl")
    assert out["segments"][0]["provider"] == "deepl"
    assert out["translated_text"] == "x·deepl"


async def test_translate_document_empty():
    out = await _svc().translate_document("   ", target_lang="zh")
    assert out["ok"] is False and out["reason"] == "empty"


async def test_translate_document_too_large():
    s = TranslationService(ai_client=None)
    s._router = EngineRouter([_StubEngine()])
    svc = DocumentTranslateService(s, max_chars=10)
    out = await svc.translate_document("a" * 20, target_lang="zh")
    assert out["ok"] is False and out["reason"] == "too_large"


async def test_translate_document_too_many_segments():
    s = TranslationService(ai_client=None)
    s._router = EngineRouter([_StubEngine()])
    svc = DocumentTranslateService(s, max_segments=2)
    out = await svc.translate_document("a\nb\nc", target_lang="zh")
    assert out["ok"] is False and out["reason"] == "too_many_segments"


async def test_translate_document_cached_count():
    svc = _svc()
    # 同一行重复 → 第二次命中缓存
    out = await svc.translate_document("dup\ndup", target_lang="zh", source_lang="en")
    assert out["stats"]["cached"] >= 1
