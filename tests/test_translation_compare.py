"""Phase F：坐席多线路对照选译单测。

EngineRouter.translate_with（强制指定引擎，不故障转移）+ compare（多引擎并发对照）+
TranslationService.compare_translations（含术语强制/品牌词保护 + 不污染缓存）。
"""
import pytest

from src.ai.translation_engines import EngineResult, EngineRouter


class _StubEngine:
    def __init__(self, name, *, available=True, out=None, supports=True, raises=False):
        self.name = name
        self._available = available
        self._out = out if out is not None else f"[{name}]译文"
        self._supports = supports
        self._raises = raises

    @property
    def available(self):
        return self._available

    def supports_target(self, target_lang):
        return self._supports

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        if self._raises:
            raise RuntimeError("boom")
        return EngineResult(self._out, self.name, True)


# ── translate_with：强制指定引擎，不故障转移 ────────────────────────────────

async def test_translate_with_specific_engine():
    router = EngineRouter([_StubEngine("ai"), _StubEngine("deepl", out="DeepL out")])
    res = await router.translate_with("deepl", "hi", source_lang="en", target_lang="zh")
    assert res.ok and res.engine == "deepl" and res.text == "DeepL out"


async def test_translate_with_unknown_engine():
    router = EngineRouter([_StubEngine("ai")])
    res = await router.translate_with("deepl", "hi", source_lang="en", target_lang="zh")
    assert res.ok is False and res.error == "unknown_engine"


async def test_translate_with_unavailable_engine_no_failover():
    # 指定引擎不可用 → 直接 ok=False（不偷偷转移到别的引擎）
    router = EngineRouter([_StubEngine("ai"), _StubEngine("deepl", available=False)])
    res = await router.translate_with("deepl", "hi", source_lang="en", target_lang="zh")
    assert res.ok is False and res.engine == "deepl" and res.error == "unavailable"


# ── compare：多引擎并发对照 ──────────────────────────────────────────────────

async def test_compare_returns_row_per_engine():
    router = EngineRouter([
        _StubEngine("ai", out="AI"),
        _StubEngine("deepl", out="DL"),
        _StubEngine("google", available=False),
    ])
    rows = await router.compare("hi", source_lang="en", target_lang="zh")
    by = {r.engine: r for r in rows}
    assert by["ai"].ok and by["ai"].text == "AI"
    assert by["deepl"].ok and by["deepl"].text == "DL"
    assert by["google"].ok is False and by["google"].error == "unavailable"


async def test_compare_marks_unsupported_target():
    router = EngineRouter([_StubEngine("deepl", supports=False)])
    rows = await router.compare("hi", source_lang="en", target_lang="km")
    assert rows[0].ok is False and "unsupported_target" in rows[0].error


async def test_compare_isolates_engine_exception():
    router = EngineRouter([_StubEngine("ai", out="AI"), _StubEngine("bad", raises=True)])
    rows = await router.compare("hi", source_lang="en", target_lang="zh")
    by = {r.engine: r for r in rows}
    assert by["ai"].ok is True
    assert by["bad"].ok is False and "RuntimeError" in by["bad"].error


# ── TranslationService.compare_translations：端到端 + 术语保护 ───────────────

async def test_service_compare_with_glossary_protect():
    from src.ai.translation_service import TranslationService

    class _EchoBrandEngine:
        # 模拟引擎：把占位符原样保留（验证 restore 还原品牌词）
        def __init__(self, name):
            self.name = name
        @property
        def available(self):
            return True
        def supports_target(self, t):
            return True
        async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
            return EngineResult(f"translated: {text}", self.name, True)

    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_EchoBrandEngine("ai"), _EchoBrandEngine("deepl")])
    svc.update_glossary(protect=["LINE Pay"])

    data = await svc.compare_translations("pay with LINE Pay now", target_lang="zh", source_lang="en")
    assert data["target_lang"] == "zh"
    assert len(data["candidates"]) == 2
    for c in data["candidates"]:
        assert c["ok"] is True
        assert "LINE Pay" in c["translated_text"]  # 品牌词被还原保护，未被占位符吞掉


async def test_service_compare_does_not_pollute_cache():
    from src.ai.translation_service import TranslationService

    class _Eng:
        name = "ai"
        @property
        def available(self):
            return True
        def supports_target(self, t):
            return True
        async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
            return EngineResult("候选译文", self.name, True)

    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_Eng()])
    before = len(svc._cache)
    await svc.compare_translations("hello", target_lang="zh", source_lang="en")
    # 对照不写 L1 缓存（择优后才由正常 translate 落库）
    assert len(svc._cache) == before
