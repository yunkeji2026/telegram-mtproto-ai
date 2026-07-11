"""P56：多翻译引擎路由 + 品牌词保护 + 术语库合并测试。"""

import pytest

from src.ai.translation_engines import (
    AIEngine,
    DeepLEngine,
    EngineResult,
    EngineRouter,
    GoogleEngine,
    OllamaMTEngine,
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


# ── 本地 Ollama MT 引擎（Hunyuan-MT）──────────────────────────────────────
def test_ollama_mt_unavailable_without_config():
    assert OllamaMTEngine("", "").available is False
    assert OllamaMTEngine("http://h:11434", "").available is False
    assert OllamaMTEngine("", "m").available is False
    # base_url+model 齐 → 可用（openai 库在本仓是硬依赖）
    assert OllamaMTEngine("http://h:11434", "hy-mt2").available is True


def test_ollama_mt_supports_target_within_model_card():
    e = OllamaMTEngine("http://h:11434", "hy-mt2")
    for lang in ("zh", "en", "ja", "ko", "th", "vi", "id", "km", "ar", "ru"):
        assert e.supports_target(lang) is True, lang
    # 模型卡集外（如斯瓦希里语）→ 不支持，router 让位下一引擎
    assert e.supports_target("sw") is False
    assert e.supports_target("") is False


def test_ollama_mt_prompt_formats_follow_official_card():
    e = OllamaMTEngine("http://h:11434", "hy-mt2")
    # zh 相关语对 → 中文指令 + 中文语种名
    p = e._build_prompt("hello", "en", "zh")
    assert p.startswith("把下面的文本翻译成中文，不要额外解释。")
    p = e._build_prompt("你好", "zh", "th")
    assert "泰语" in p and p.endswith("你好")
    # 非 zh 语对 → 英文指令 + 英文语种名
    p = e._build_prompt("hola", "es", "en")
    assert p.startswith("Translate the following segment into English")


@pytest.mark.asyncio
async def test_ollama_mt_translate_via_fake_client():
    e = OllamaMTEngine("http://h:11434", "hy-mt2")

    class _Msg:
        content = "Translation: hi there"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def __init__(self):
            self.kwargs = None

        async def create(self, **kw):
            self.kwargs = kw
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Cli:
        chat = _Chat()

    e._clients["http://h:11434"] = _Cli()
    r = await e.translate("你好", source_lang="zh", target_lang="en")
    assert r.ok and r.engine == "ollama_mt"
    assert r.text == "hi there"  # _clean_translation 剥掉 "Translation:" 前缀
    kw = _Cli.chat.completions.kwargs
    assert kw["model"] == "hy-mt2"
    assert kw["extra_body"] == {"keep_alive": "30m"}   # 默认防冷启动
    assert "temperature" not in kw                     # 缺省不传 → 用模型内置采样


@pytest.mark.asyncio
async def test_ollama_mt_unsupported_target_yields_to_next_engine():
    e = OllamaMTEngine("http://h:11434", "hy-mt2")
    r = await e.translate("你好", source_lang="zh", target_lang="sw")
    assert not r.ok and "unsupported_target" in r.error
    # router 场景：MT 不支持 → 顺移 ai 兜底
    ai = _FixedEngine("ai", text="habari")
    res = await EngineRouter([e, ai]).translate("你好", source_lang="zh", target_lang="sw")
    assert res.ok and res.engine == "ai"


def _fake_cli(reply: str = "ok", *, fail: bool = False):
    """构造最小 AsyncOpenAI 假客户端；fail=True 时 create 抛连接异常。"""
    class _Msg:
        content = reply

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def __init__(self):
            self.calls = 0

        async def create(self, **kw):
            self.calls += 1
            if fail:
                raise ConnectionError("boom")
            return _Resp()

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Cli:
        def __init__(self):
            self.chat = _Chat()

    return _Cli()


@pytest.mark.asyncio
async def test_ollama_mt_dual_endpoint_failover():
    # 176 挂 → 自动切 140；且 176 进冷却（下次排序降权到队尾）
    e = OllamaMTEngine(["http://a:11434", "http://b:11434"], "hy-mt2")
    bad, good = _fake_cli(fail=True), _fake_cli("hello")
    e._clients["http://a:11434"] = bad
    e._clients["http://b:11434"] = good
    r = await e.translate("你好", source_lang="zh", target_lang="en")
    assert r.ok and r.text == "hello"
    assert bad.chat.completions.calls == 1 and good.chat.completions.calls == 1
    # 冷却生效：a 降权到队尾，后续调用直接走 b（a 不再被打）
    r2 = await e.translate("再见", source_lang="zh", target_lang="en")
    assert r2.ok and bad.chat.completions.calls == 1
    assert good.chat.completions.calls == 2


@pytest.mark.asyncio
async def test_ollama_mt_all_endpoints_down_returns_error():
    e = OllamaMTEngine(["http://a:11434", "http://b:11434"], "hy-mt2")
    e._clients["http://a:11434"] = _fake_cli(fail=True)
    e._clients["http://b:11434"] = _fake_cli(fail=True)
    r = await e.translate("你好", source_lang="zh", target_lang="en")
    assert not r.ok and "ConnectionError" in r.error
    # 全端点冷却中仍保底可试（不剔除）：恢复后下一次调用即成功
    e._clients["http://a:11434"] = _fake_cli("hi again")
    r2 = await e.translate("你好", source_lang="zh", target_lang="en")
    assert r2.ok and r2.text == "hi again"


def test_ollama_mt_base_url_forms():
    # 列表 / 逗号分隔字符串 / 单字符串 三种形态等价解析
    assert OllamaMTEngine(["http://a:1", "http://b:2"], "m")._base_urls == \
        ["http://a:1", "http://b:2"]
    assert OllamaMTEngine("http://a:1, http://b:2", "m")._base_urls == \
        ["http://a:1", "http://b:2"]
    assert OllamaMTEngine("http://a:1", "m")._base_urls == ["http://a:1"]
    # _base_url 兼容属性 = 首端点
    assert OllamaMTEngine(["http://a:1", "http://b:2"], "m")._base_url == "http://a:1"


def test_build_engines_with_ollama_mt():
    cfg = {"engines": {
        "order": ["ollama_mt", "ai"],
        "ollama_mt": {"base_url": "http://h:11434", "model": "hy-mt2",
                      "timeout_sec": 20, "keep_alive": "30m"},
    }}
    eng = build_engines(cfg, ai_client=None)
    assert [e.name for e in eng] == ["ollama_mt", "ai"]
    assert eng[0].available is True
    # 缺 model → 构造成功但不可用（router 自动跳过），不影响列表
    cfg2 = {"engines": {"order": ["ollama_mt"], "ollama_mt": {"base_url": "http://h:11434"}}}
    eng2 = build_engines(cfg2, ai_client=None)
    assert eng2[0].name == "ollama_mt" and eng2[0].available is False


def test_build_engines_ollama_mt_base_urls_list():
    # base_urls（双活列表）优先于 base_url
    cfg = {"engines": {
        "order": ["ollama_mt"],
        "ollama_mt": {"base_urls": ["http://a:11434", "http://b:11434"],
                      "base_url": "http://ignored:1", "model": "hy-mt2"},
    }}
    eng = build_engines(cfg, ai_client=None)
    assert eng[0]._base_urls == ["http://a:11434", "http://b:11434"]
    assert eng[0].available is True


# ── restore 容错硬化（MT 引擎会把〔N〕规范成 [N]/吞边空格）────────────────
def test_restore_ascii_bracket_and_smart_spacing():
    m = {"\u30140\u3015": "LINE Pay", "\u30141\u3015": "support team"}
    # 拉丁语境吞空格 → 智能补空格
    assert restore_protected("pay using\u30140\u3015today", m) == "pay using LINE Pay today"
    # CJK 语境 → 不补
    assert restore_protected("你可以用\u30140\u3015付款", m) == "你可以用LINE Pay付款"
    # ASCII 化占位符 [N] → 还原
    assert restore_protected("contact [1] please", m) == "contact support team please"
    # 全角【N】变体 → 还原
    assert restore_protected("pay via\u30100\u3011ok", m) == "pay via LINE Pay ok"
    # mapping 外序号（正文自带 [7]）→ 原样保留
    assert restore_protected("see [7] footnote", m) == "see [7] footnote"


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


# ── 按目标语引擎覆写（per_lang_order：弱语对直走强引擎）────────────────────
@pytest.mark.asyncio
async def test_per_lang_order_reroutes_only_listed_lang():
    mt = _FixedEngine("ollama_mt", text="mt-out")
    ai = _FixedEngine("ai", text="ai-out")
    router = EngineRouter([mt, ai], per_lang_order={"hi": ["ai", "ollama_mt"]})
    # hi → ai 优先（mt 完全不被打）
    r = await router.translate("你好", source_lang="zh", target_lang="hi")
    assert r.engine == "ai" and mt.calls == 0
    # 其他语种 → 默认序不受影响
    r2 = await router.translate("你好", source_lang="zh", target_lang="en")
    assert r2.engine == "ollama_mt"


@pytest.mark.asyncio
async def test_per_lang_order_appends_remaining_as_fallback():
    # 覆写只列了 ai；ai 挂 → 仍能落回默认序里的 mt（覆写不丢兜底）
    mt = _FixedEngine("ollama_mt", text="mt-out")
    ai = _FixedEngine("ai", text="", ok=False, error="boom")
    router = EngineRouter([mt, ai], per_lang_order={"hi": ["ai"]})
    r = await router.translate("你好", source_lang="zh", target_lang="hi")
    assert r.ok and r.engine == "ollama_mt"


@pytest.mark.asyncio
async def test_per_lang_order_unknown_engine_ignored():
    mt = _FixedEngine("ollama_mt", text="mt-out")
    router = EngineRouter([mt], per_lang_order={"hi": ["nonexistent"]})
    r = await router.translate("你好", source_lang="zh", target_lang="hi")
    assert r.ok and r.engine == "ollama_mt"


def test_per_lang_order_reflected_in_describe():
    mt = _FixedEngine("ollama_mt", text="x")
    ai = _FixedEngine("ai", text="y")
    router = EngineRouter([mt, ai], per_lang_order={"hi": ["ai"]})
    m = router.describe("hi")
    assert m["primary"] == "ai" and m["effective"] == "ai"
    assert [r["engine"] for r in m["engines"]] == ["ai", "ollama_mt"]
    # 非覆写语种维持默认
    m2 = router.describe("en")
    assert m2["primary"] == "ollama_mt"
    # 语种码归一：hi-IN 命中 hi 覆写
    assert router.describe("hi-IN")["primary"] == "ai"


# ── 在线语义闸门（confidence_switch 进阶：抓「语言对但意思漂移」）──────────
def _embed_by_tag(mapping):
    """按子串命中返回向量的假嵌入器（批量签名，与 ai_client.embed 同形）。"""
    async def _embed(texts):
        out = []
        for t in texts:
            vec = None
            for tag, v in mapping.items():
                if tag in (t or ""):
                    vec = v
                    break
            out.append(vec if vec is not None else [1.0, 0.0])
        return out
    return _embed


@pytest.mark.asyncio
async def test_semantic_gate_switches_on_meaning_drift():
    stats = get_translation_engine_stats()
    stats.reset()
    try:
        # e1 输出「意思漂移」译文（与源文嵌入正交但确定性信号全过）；e2 输出忠实译文
        e1 = _FixedEngine("ollama_mt", text="the weather is nice today")
        e2 = _FixedEngine("ai", text="the delivery arrives tomorrow")
        embed = _embed_by_tag({
            "明天送达": [1.0, 0.0],                    # 源文
            "weather": [0.0, 1.0],                     # 漂移译文 → cos=0
            "delivery": [0.96, 0.28],                  # 忠实译文 → cos≈0.96
        })
        router = EngineRouter(
            [e1, e2], min_confidence=0.5,
            semantic_embed_fn=embed, semantic_min_similarity=0.7)
        r = await router.translate("包裹明天送达", source_lang="zh", target_lang="en")
        assert r.engine == "ai" and "delivery" in r.text
        d = stats.dump()
        assert d["semantic_low"] == 1          # e1 被语义闸门拦下
        assert d["confidence_switches"] == 1   # 且实际发生了切换
    finally:
        stats.reset()


@pytest.mark.asyncio
async def test_semantic_gate_fail_open_on_embed_error():
    # 嵌入端点抖动（抛异常/返空）→ 放行主引擎结果，绝不阻塞
    # （源文须 ≥4 有效字符，否则触发短文本跳过、走不到 embed）
    e1 = _FixedEngine("ollama_mt", text="good translation")
    embed_calls = []

    async def _broken_embed(texts):
        embed_calls.append(texts)
        raise ConnectionError("embed down")

    router = EngineRouter(
        [e1], min_confidence=0.5,
        semantic_embed_fn=_broken_embed, semantic_min_similarity=0.7)
    r = await router.translate("你好呀老朋友", source_lang="zh", target_lang="en")
    assert r.ok and r.engine == "ollama_mt"
    assert embed_calls   # 确实走到了 embed（而非被短文本跳过）

    async def _empty_embed(texts):
        return []

    router2 = EngineRouter(
        [e1], min_confidence=0.5,
        semantic_embed_fn=_empty_embed, semantic_min_similarity=0.7)
    r2 = await router2.translate("你好呀老朋友", source_lang="zh", target_lang="en")
    assert r2.ok and r2.engine == "ollama_mt"


@pytest.mark.asyncio
async def test_semantic_gate_skips_very_short_source():
    # 超短源文（<4 有效字符，如 "OK"/"哈哈"）：嵌入噪声大、漂移风险≈0 → 直接放行省往返
    e1 = _FixedEngine("ollama_mt", text="haha")
    embed_calls = []

    async def _embed(texts):
        embed_calls.append(texts)
        return [[1.0, 0.0], [0.0, 1.0]]   # 若被调用会判低相似 → 用调用记录证明没走到

    router = EngineRouter(
        [e1], min_confidence=0.5,
        semantic_embed_fn=_embed, semantic_min_similarity=0.7)
    r = await router.translate("哈哈  ", source_lang="zh", target_lang="en")
    assert r.ok and r.engine == "ollama_mt"
    assert embed_calls == []   # 全程未打嵌入端点


@pytest.mark.asyncio
async def test_semantic_gate_all_low_returns_best_candidate():
    # 两个引擎都语义低 → 返回 (conf, sim) 最高候选，不吐空
    e1 = _FixedEngine("ollama_mt", text="totally off A")
    e2 = _FixedEngine("ai", text="slightly better B")
    embed = _embed_by_tag({
        "源文": [1.0, 0.0],
        "off A": [0.0, 1.0],          # cos=0
        "better B": [0.5, 0.866],     # cos=0.5（仍低于 0.7）
    })
    router = EngineRouter(
        [e1, e2], min_confidence=0.5,
        semantic_embed_fn=embed, semantic_min_similarity=0.7)
    r = await router.translate("源文在此", source_lang="zh", target_lang="en")
    assert r.ok and r.engine == "ai"   # sim 更高者胜出


@pytest.mark.asyncio
async def test_semantic_gate_disabled_keeps_old_behavior():
    # 未注入 embed fn → 语义闸门不存在，确定性达标即返回（旧行为）
    e1 = _FixedEngine("ollama_mt", text="anything goes")
    router = EngineRouter([e1], min_confidence=0.5)
    r = await router.translate("你好", source_lang="zh", target_lang="en")
    assert r.ok and r.engine == "ollama_mt"
