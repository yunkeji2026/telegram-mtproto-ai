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


# ── P0-2：置信度透传（B1 对照候选 / B2 单条 translate）───────────────────────

def _svc_with(*engines):
    from src.ai.translation_service import TranslationService

    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter(list(engines))
    return svc


async def test_compare_candidates_carry_confidence_fields():
    # good：真译中文；echo：原样回吐（未翻译 → 低置信）；down：不可用（不评分）
    svc = _svc_with(
        _StubEngine("ai", out="你好，最近怎么样"),
        _StubEngine("deepl", out="hello how are you"),
        _StubEngine("google", available=False),
    )
    data = await svc.compare_translations(
        "hello how are you", target_lang="zh", source_lang="en")
    by = {c["engine"]: c for c in data["candidates"]}

    good = by["ai"]
    assert good["ok"] is True
    assert 0.0 <= good["confidence"] <= 1.0
    assert good["confidence_tier"] == "high"
    assert good["confidence_signals"]["untranslated"] is False

    echo = by["deepl"]  # 与原文相同 + 目标语脚本缺失 → low
    assert echo["ok"] is True
    assert echo["confidence"] < 0.5
    assert echo["confidence_tier"] == "low"
    assert echo["confidence_signals"]["untranslated"] is True

    down = by["google"]  # 失败候选不评分（键不存在，前端徽标不渲染）
    assert down["ok"] is False
    assert "confidence" not in down and "confidence_tier" not in down


async def test_translate_result_exposes_confidence():
    svc = _svc_with(_StubEngine("ai", out="你好"))
    res = await svc.translate("hello", target_lang="zh", source_lang="en")
    assert res.ok
    assert 0.0 <= res.confidence <= 1.0
    d = res.to_dict()
    assert d["confidence"] == res.confidence
    # L1 缓存往返保留置信度（to_dict → kwargs 重建）
    res2 = await svc.translate("hello", target_lang="zh", source_lang="en")
    assert res2.cached is True and res2.confidence == res.confidence


async def test_translate_identity_and_failure_not_scored():
    # identity（同语种直返）与失败路径不评分 → -1，前端跳过低置信提示
    svc = _svc_with(_StubEngine("ai", out="whatever"))
    ident = await svc.translate("你好", target_lang="zh", source_lang="zh")
    assert ident.provider == "identity" and ident.confidence == -1.0

    svc2 = _svc_with(_StubEngine("ai", available=False))
    fail = await svc2.translate("hello", target_lang="zh", source_lang="en")
    assert fail.ok is False and fail.confidence == -1.0


def _translate_app(svc):
    from fastapi import FastAPI, Request
    from src.web.routes.unified_inbox_translate_routes import register_translate_routes

    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_translate_routes(app, api_auth=api_auth)
    app.state.translation_service = svc
    return app


def test_translate_route_passes_confidence_through():
    from fastapi.testclient import TestClient

    svc = _svc_with(_StubEngine("ai", out="你好，最近怎么样"))
    client = TestClient(_translate_app(svc))
    r = client.post("/api/unified-inbox/translate",
                    json={"text": "hello how are you", "target_lang": "zh",
                          "source_lang": "en"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    conf = body["translation"]["confidence"]
    assert isinstance(conf, float) and 0.0 <= conf <= 1.0


def test_compare_route_passes_confidence_through():
    from fastapi.testclient import TestClient

    svc = _svc_with(
        _StubEngine("ai", out="你好，最近怎么样"),
        _StubEngine("deepl", out="hello how are you"),
    )
    client = TestClient(_translate_app(svc))
    r = client.post("/api/unified-inbox/translate-compare",
                    json={"text": "hello how are you", "target_lang": "zh",
                          "source_lang": "en"})
    assert r.status_code == 200
    cands = r.json()["compare"]["candidates"]
    by = {c["engine"]: c for c in cands}
    assert by["ai"]["confidence_tier"] == "high"
    assert by["deepl"]["confidence_tier"] == "low"
    assert "confidence_signals" in by["ai"]
