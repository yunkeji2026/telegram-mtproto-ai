"""P56：多翻译引擎路由 + 品牌词保护 + 术语库合并测试。"""

import pytest

from src.ai.translation_engines import (
    AIEngine,
    DeepLEngine,
    EngineResult,
    EngineRouter,
    GoogleEngine,
    apply_glossary_mask,
    build_engines,
    mask_protected,
    restore_protected,
)
from src.ai.translation_engine_stats import (
    TranslationEngineStats,
    get_translation_engine_stats,
)
from src.ai.translation_glossary import build_glossary
from src.ai.translation_service import TranslationService


# ── 引擎假实现 ────────────────────────────────────────────────────────────
class _FixedEngine:
    def __init__(self, name, text="", ok=True, available=True, error=""):
        self.name = name
        self._text = text
        self._ok = ok
        self.available = available
        self._error = error
        self.calls = 0

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        self.calls += 1
        return EngineResult(self._text, self.name, self._ok, self._error)


# ── mask / restore ────────────────────────────────────────────────────────
def test_mask_restore_roundtrip_longest_first():
    masked, mapping = mask_protected("用 LINE Pay 付款给 LINE", ["LINE", "LINE Pay"])
    # 最长词优先：LINE Pay 整体被遮罩，不被 LINE 截断
    assert "LINE Pay" not in masked
    assert len(mapping) == 2
    restored = restore_protected(masked, mapping)
    assert "LINE Pay" in restored and restored.count("LINE") == 2


def test_mask_noop_without_protect():
    masked, mapping = mask_protected("hello", [])
    assert masked == "hello" and mapping == {}


def test_restore_tolerates_spaced_placeholder():
    masked, mapping = mask_protected("brand X here", ["X"])
    # 模拟引擎在占位符内插了空格
    ph = list(mapping.keys())[0]
    mangled = masked.replace(ph, ph[0] + " 0 " + ph[-1])
    assert "X" in restore_protected(mangled, mapping)


# ── EngineRouter 故障转移 + 归因 ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_router_falls_back_to_next_engine():
    e1 = _FixedEngine("deepl", text="", ok=False, error="http_456")
    e2 = _FixedEngine("ai", text="你好")
    router = EngineRouter([e1, e2])
    res = await router.translate("hi", source_lang="en", target_lang="zh")
    assert res.ok and res.text == "你好" and res.engine == "ai"
    assert e1.calls == 1 and e2.calls == 1


@pytest.mark.asyncio
async def test_router_skips_unavailable_engine_without_calling():
    e1 = _FixedEngine("deepl", available=False)
    e2 = _FixedEngine("ai", text="你好")
    router = EngineRouter([e1, e2])
    res = await router.translate("hi", source_lang="en", target_lang="zh")
    assert res.engine == "ai"
    assert e1.calls == 0  # 不可用引擎不应被调用


@pytest.mark.asyncio
async def test_router_all_fail_returns_none():
    router = EngineRouter([_FixedEngine("deepl", ok=False, error="x")])
    res = await router.translate("hi", source_lang="en", target_lang="zh")
    assert not res.ok and res.engine == "none" and "deepl" in res.error


def test_router_any_available_and_names():
    router = EngineRouter([_FixedEngine("deepl", available=False), _FixedEngine("ai")])
    assert router.any_available() is True
    assert router.names() == ["deepl", "ai"]


# ── C-1：引擎能力矩阵（目标语支持 + 兜底归因）────────────────────────────
def test_engine_supports_target_capability():
    # supports_target 表达「能力」，与是否配置 key（available）无关
    assert AIEngine(None).supports_target("th") is True
    assert DeepLEngine("k").supports_target("th") is False   # DeepL 不支持泰语
    assert DeepLEngine("k").supports_target("ja") is True
    assert GoogleEngine("k").supports_target("th") is True


def test_router_describe_marks_deepl_fallback_to_ai():
    deepl = DeepLEngine("k")
    ai = _FixedEngine("ai", text="x", available=True)
    router = EngineRouter([deepl, ai])
    m = router.describe("th")
    assert m["primary"] == "deepl"
    assert m["effective"] == "ai"   # DeepL 不支持泰语 → AI 兜底
    rows = {r["engine"]: r for r in m["engines"]}
    assert rows["deepl"]["supports"] is False
    assert rows["ai"]["supports"] is True


def test_service_engine_matrix_delegates():
    router = EngineRouter([DeepLEngine("k"), _FixedEngine("ai", available=True)])
    svc = TranslationService(engine_router=router)
    m = svc.engine_matrix("th")
    assert m["primary"] == "deepl" and m["effective"] == "ai"


# ── DeepL / Google 缺 key → 不可用（路由跳过，本地零外部依赖）────────────
def test_deepl_google_unavailable_without_key():
    assert DeepLEngine("").available is False
    assert GoogleEngine("").available is False


# ── build_engines 工厂 ────────────────────────────────────────────────────
def test_build_engines_order_and_default():
    eng = build_engines({"engines": {"order": ["deepl", "ai"], "deepl": {"api_key": "k"}}}, ai_client=None)
    assert [e.name for e in eng] == ["deepl", "ai"]
    # 空配置 → 默认 ai 兜底
    assert [e.name for e in build_engines({}, ai_client=None)] == ["ai"]


# ── 服务层：多引擎归因 + 保护词端到端 ────────────────────────────────────
@pytest.mark.asyncio
async def test_service_uses_router_and_reports_engine():
    router = EngineRouter([_FixedEngine("deepl", text="你好世界")])
    svc = TranslationService(engine_router=router)
    r = await svc.translate("hello world", target_lang="zh", source_lang="en")
    assert r.ok and r.provider == "deepl" and r.translated_text == "你好世界"


@pytest.mark.asyncio
async def test_service_protects_brand_term_end_to_end():
    # 引擎把占位符原样返回（多数真实引擎对短占位符也是保留的）
    class _EchoBrand:
        name = "echo"
        available = True

        async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
            # 模拟「翻译」：把可翻译部分换掉，占位符保留
            return EngineResult(text.replace("download", "下载"), self.name, True)

    svc = TranslationService(engine_router=EngineRouter([_EchoBrand()]),
                             glossary_protect=["Acme"])
    r = await svc.translate("download Acme now", target_lang="zh", source_lang="en")
    assert "Acme" in r.translated_text   # 品牌词逐字保留
    assert "下载" in r.translated_text   # 其余被翻译


# ── 术语库合并 ────────────────────────────────────────────────────────────
def test_build_glossary_global_overrides_domain(tmp_path):
    dom = tmp_path / "d" / "prompts"
    dom.mkdir(parents=True)
    (dom / "terminology.yaml").write_text(
        "glossary:\n  SKU: 域包译法\n  protect:\n    - DomainBrand\n", encoding="utf-8"
    )
    cfg = {"translation": {"glossary": {
        "enabled": True, "extra_terms": {"SKU": "全局译法"}, "protect": ["GlobalBrand"],
    }}}
    gl = build_glossary(cfg, domain_files=[dom / "terminology.yaml"])
    assert gl.terms["SKU"] == "全局译法"          # 全局覆盖域包
    assert "GlobalBrand" in gl.protect and "DomainBrand" in gl.protect
    assert gl.version  # 非空版本 hash


def test_build_glossary_version_changes_with_content():
    v1 = build_glossary({"translation": {"glossary": {"extra_terms": {"a": "1"}}}}).version
    v2 = build_glossary({"translation": {"glossary": {"extra_terms": {"a": "2"}}}}).version
    assert v1 != v2


def test_build_glossary_disabled_returns_empty():
    gl = build_glossary({"translation": {"glossary": {"enabled": False, "extra_terms": {"a": "1"}}}})
    assert gl.empty() and gl.version == ""


# ── P57：术语对所有引擎强制（含忽略提示的 DeepL/Google 类引擎）────────────
def test_apply_glossary_mask_terms_and_protect():
    masked, mapping = apply_glossary_mask("size of LINE", {"size": "尺码"}, ["LINE"])
    assert "size" not in masked and "LINE" not in masked
    restored = restore_protected(masked, mapping)
    assert "尺码" in restored and "LINE" in restored  # term→译法、protect→原词


@pytest.mark.asyncio
async def test_term_enforced_on_non_ai_engine():
    """模拟 DeepL：忽略 glossary_hint，但术语经占位符仍被强制为偏好译法。"""
    class _Echo:
        name = "deepl"
        available = True

        async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
            return EngineResult(text.replace("shoes", "鞋"), self.name, True)

    svc = TranslationService(engine_router=EngineRouter([_Echo()]),
                             glossary_terms={"size": "尺码"})
    r = await svc.translate("size shoes", target_lang="zh", source_lang="en")
    assert r.provider == "deepl"
    assert "尺码" in r.translated_text and "鞋" in r.translated_text


# ── P57：引擎用量统计 ─────────────────────────────────────────────────────
def test_engine_stats_record_and_dump():
    s = TranslationEngineStats()
    s.record("ai", ok=True, latency_ms=10)
    s.record("ai", ok=False, latency_ms=30)
    s.record("deepl", ok=True, latency_ms=5)
    d = s.dump()
    assert d["total_attempts"] == 3
    ai_row = next(r for r in d["rows"] if r["engine"] == "ai")
    assert ai_row["calls"] == 2 and ai_row["ok"] == 1 and ai_row["fail"] == 1
    assert ai_row["success_rate"] == 0.5 and ai_row["avg_latency_ms"] == 20.0
    assert "translation_engine_attempts_total" in s.dump_prom()


@pytest.mark.asyncio
async def test_router_records_fallback_on_primary_fail():
    stats = get_translation_engine_stats()
    stats.reset()
    try:
        e1 = _FixedEngine("deepl", ok=False, error="http_500")
        e2 = _FixedEngine("ai", text="你好")
        res = await EngineRouter([e1, e2]).translate("hi", source_lang="en", target_lang="zh")
        assert res.engine == "ai"
        d = stats.dump()
        assert d["fallbacks"] == 1  # 主引擎失败 → 记一次降级
        names = {r["engine"] for r in d["rows"]}
        assert "deepl" in names and "ai" in names
    finally:
        stats.reset()
