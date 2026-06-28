"""出站自动翻译纯核心 + 译发助手单测（增量8：补「全自动聊天翻译」闭环）。

锁定：
  - parse_outbound_translate_cfg 缺省全关、读 inbox.l2_autosend.translate
  - normalize_target / should_translate 决策（空/未知/同语跳过）
  - translate_outbound_text：成功译→返回译文并记录映射；失败/同文/无目标→回落原文且不记录
  - 绝不抛：translation_service 抛异常时回落原文
"""

from __future__ import annotations

import pytest

from src.inbox.outbound_translate import (
    normalize_target,
    parse_outbound_translate_cfg,
    should_translate,
    translate_outbound_text,
)


# ── 纯决策函数 ──────────────────────────────────────────────

def test_parse_cfg_default_off():
    cfg = parse_outbound_translate_cfg({})
    assert cfg == {"enabled": False, "source_lang": "zh", "style": "chat"}


def test_parse_cfg_reads_nested():
    cfg = parse_outbound_translate_cfg({
        "inbox": {"l2_autosend": {"translate": {
            "enabled": True, "source_lang": "ZH", "style": "formal"}}}})
    assert cfg["enabled"] is True
    assert cfg["source_lang"] == "zh"
    assert cfg["style"] == "formal"


def test_normalize_target():
    assert normalize_target("zh-CN") == "zh"
    assert normalize_target("EN") == "en"
    assert normalize_target("unknown") == ""
    assert normalize_target("auto") == ""
    assert normalize_target("") == ""


def test_should_translate():
    assert should_translate("你好", "en", "zh") is True
    assert should_translate("你好", "zh-CN", "zh") is False   # 同语
    assert should_translate("你好", "unknown", "zh") is False  # 目标未知
    assert should_translate("", "en", "zh") is False           # 空正文
    assert should_translate("你好", "", "zh") is False          # 无目标


# ── 译发助手（async） ────────────────────────────────────────

class _FakeRes:
    def __init__(self, translated, ok=True, provider="deepl", error=""):
        self.translated_text = translated
        self.ok = ok
        self.provider = provider
        self.error = error


class _FakeTS:
    def __init__(self, res, detect=""):
        self._res = res
        self._detect = detect      # detect_language 返回值（""=未知）
        self.calls = []

    def detect_language(self, text):
        return self._detect

    async def translate(self, text, *, target_lang, source_lang, style="chat"):
        self.calls.append((text, target_lang, source_lang, style))
        return self._res


class _FakeStore:
    def __init__(self, language="en"):
        self._language = language
        self.recorded = []

    def get_conversation(self, cid):
        return {"conversation_id": cid, "language": self._language}

    def record_outbound_translation(self, cid, sent, orig, **kw):
        self.recorded.append((cid, sent, orig, kw))
        return True


@pytest.mark.asyncio
async def test_translate_success_records_and_returns_translation():
    ts = _FakeTS(_FakeRes("Hello~"))
    store = _FakeStore(language="en")
    out = await translate_outbound_text(
        {"conversation_id": "x1", "text": "你好呀~"},
        translation_service=ts, store=store, source_lang="zh")
    assert out == "Hello~"
    assert ts.calls[0][1] == "en"           # target
    assert len(store.recorded) == 1
    cid, sent, orig, kw = store.recorded[0]
    assert (cid, sent, orig) == ("x1", "Hello~", "你好呀~")
    assert kw["target_lang"] == "en" and kw["source_lang"] == "zh"


@pytest.mark.asyncio
async def test_skip_when_same_language():
    ts = _FakeTS(_FakeRes("不应被调用"))
    store = _FakeStore(language="zh")   # 客户也是中文 → 不翻译
    out = await translate_outbound_text(
        {"conversation_id": "x1", "text": "你好"},
        translation_service=ts, store=store, source_lang="zh")
    assert out == "你好"
    assert ts.calls == []               # 根本没调引擎
    assert store.recorded == []


@pytest.mark.asyncio
async def test_skip_when_language_unknown():
    ts = _FakeTS(_FakeRes("x"))
    store = _FakeStore(language="")      # 会话语言未知 → 回落原文
    out = await translate_outbound_text(
        {"conversation_id": "x1", "text": "你好"},
        translation_service=ts, store=store, source_lang="zh")
    assert out == "你好"
    assert ts.calls == []


@pytest.mark.asyncio
async def test_fallback_on_translation_failure():
    ts = _FakeTS(_FakeRes("", ok=False, error="provider_unavailable"))
    store = _FakeStore(language="en")
    out = await translate_outbound_text(
        {"conversation_id": "x1", "text": "你好"},
        translation_service=ts, store=store, source_lang="zh")
    assert out == "你好"               # 回落原文
    assert store.recorded == []        # 失败不记录


@pytest.mark.asyncio
async def test_fallback_when_identity_translation():
    ts = _FakeTS(_FakeRes("你好", provider="identity"))  # 译文==原文
    store = _FakeStore(language="en")
    out = await translate_outbound_text(
        {"conversation_id": "x1", "text": "你好"},
        translation_service=ts, store=store, source_lang="zh")
    assert out == "你好"
    assert store.recorded == []


@pytest.mark.asyncio
async def test_never_raises_on_engine_exception():
    class _Boom:
        async def translate(self, *a, **k):
            raise RuntimeError("engine down")

    store = _FakeStore(language="en")
    out = await translate_outbound_text(
        {"conversation_id": "x1", "text": "你好"},
        translation_service=_Boom(), store=store, source_lang="zh")
    assert out == "你好"               # 异常被吞，发原文


@pytest.mark.asyncio
async def test_no_service_returns_original():
    out = await translate_outbound_text(
        {"conversation_id": "x1", "text": "你好"},
        translation_service=None, store=_FakeStore())
    assert out == "你好"


# ── 源语言检测护栏（防 garble，覆盖主动触达已 in-lang 的消息） ──────────────

@pytest.mark.asyncio
async def test_detection_skips_when_text_already_target_language():
    # 文本已是客户语言（英文），会话目标也是英文 → 即便 config 源=zh 也必须跳过，绝不 garble
    ts = _FakeTS(_FakeRes("garbled"), detect="en")
    store = _FakeStore(language="en")
    out = await translate_outbound_text(
        {"conversation_id": "x1", "text": "Hi, how are you?"},
        translation_service=ts, store=store, source_lang="zh")
    assert out == "Hi, how are you?"   # 原样返回
    assert ts.calls == []              # 检测命中目标 → 根本没调引擎
    assert store.recorded == []


@pytest.mark.asyncio
async def test_detection_uses_detected_source_over_config():
    # 文本实际是日文，config 假定 zh → 应以检测到的 ja 作源语言翻译到 en
    ts = _FakeTS(_FakeRes("Hello"), detect="ja")
    store = _FakeStore(language="en")
    out = await translate_outbound_text(
        {"conversation_id": "x1", "text": "こんにちは"},
        translation_service=ts, store=store, source_lang="zh")
    assert out == "Hello"
    assert ts.calls[0][2] == "ja"      # source_lang = 检测值，而非 config 的 zh
    assert store.recorded[0][3]["source_lang"] == "ja"


@pytest.mark.asyncio
async def test_detection_unknown_falls_back_to_config_source():
    # 检测器返回未知 → 回落 config 源语言 zh，照常翻译
    ts = _FakeTS(_FakeRes("Hello"), detect="")
    store = _FakeStore(language="en")
    out = await translate_outbound_text(
        {"conversation_id": "x1", "text": "你好呀"},
        translation_service=ts, store=store, source_lang="zh")
    assert out == "Hello"
    assert ts.calls[0][2] == "zh"
