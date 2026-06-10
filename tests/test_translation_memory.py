"""translation_memory + TranslationService 持久化测试（Phase C2）。"""

import pytest

from src.ai.translation_memory import TranslationMemoryStore
from src.ai.translation_service import TranslationService


class _AI:
    model = "test-model"

    def __init__(self):
        self.calls = 0

    async def chat(self, prompt, overrides=None):
        self.calls += 1
        return "你好朋友"


def test_memory_store_put_get_hit_count(tmp_path):
    store = TranslationMemoryStore(tmp_path / "tm.db")
    assert store.get("k1") is None
    store.put("k1", source_text="hello", translated_text="你好",
              source_lang="en", target_lang="zh")
    row = store.get("k1")
    assert row["translated_text"] == "你好"
    store.get("k1")
    row2 = store.get("k1")
    assert row2["hit_count"] >= 2
    store.close()


@pytest.mark.asyncio
async def test_translate_persists_and_hits_across_restart(tmp_path):
    db = tmp_path / "tm.db"
    ai = _AI()
    store = TranslationMemoryStore(db)
    svc = TranslationService(ai_client=ai, memory_store=store)
    r1 = await svc.translate("hello friend", target_lang="zh")
    assert r1.ok and r1.translated_text == "你好朋友"
    assert ai.calls == 1
    store.close()

    # 模拟重启：新 store + 新 service（新进程内 L1 为空），应命中 L2 不再调 AI
    ai2 = _AI()
    store2 = TranslationMemoryStore(db)
    svc2 = TranslationService(ai_client=ai2, memory_store=store2)
    r2 = await svc2.translate("hello friend", target_lang="zh")
    assert r2.ok and r2.translated_text == "你好朋友"
    assert r2.cached is True
    assert ai2.calls == 0  # 命中持久记忆，未调 AI
    store2.close()


def test_lang_names_cover_sea_languages():
    """P55: 跨境 SEA 语种应进入 LANG_NAMES，使翻译 prompt 用语种全名而非裸 code。"""
    from src.ai.translation_service import LANG_NAMES
    for code, name in (("th", "Thai"), ("ms", "Malay"), ("tl", "Filipino")):
        assert LANG_NAMES.get(code) == name


@pytest.mark.asyncio
async def test_translate_thai_target_marks_cached_on_repeat(tmp_path):
    """P55 工作台双向翻译依赖 cached 标记复用记忆，避免重复调用 AI。"""
    ai = _AI()
    store = TranslationMemoryStore(tmp_path / "tm.db")
    svc = TranslationService(ai_client=ai, memory_store=store)
    r1 = await svc.translate("hello", target_lang="th")
    assert r1.ok and ai.calls == 1 and r1.cached is False
    r2 = await svc.translate("hello", target_lang="th")
    assert r2.ok and r2.cached is True and ai.calls == 1  # 命中缓存，未再调 AI
    store.close()


@pytest.mark.asyncio
async def test_glossary_version_change_invalidates(tmp_path):
    db = tmp_path / "tm.db"
    ai = _AI()
    store = TranslationMemoryStore(db)
    svc_v1 = TranslationService(ai_client=ai, memory_store=store, glossary_version="v1")
    await svc_v1.translate("hello friend", target_lang="zh")
    assert ai.calls == 1

    # 术语库版本变了 → cache_key 变 → 不命中旧译，重新翻译
    svc_v2 = TranslationService(ai_client=ai, memory_store=store, glossary_version="v2")
    await svc_v2.translate("hello friend", target_lang="zh")
    assert ai.calls == 2
    store.close()


@pytest.mark.asyncio
async def test_glossary_terms_injected_into_prompt(tmp_path):
    captured = {}

    class _AICap:
        model = "m"

        async def chat(self, prompt, overrides=None):
            captured["prompt"] = prompt
            return "尺码"

    svc = TranslationService(
        ai_client=_AICap(),
        glossary_terms={"size": "尺码"},
        glossary_version="v1",
    )
    await svc.translate("what size", target_lang="zh", source_lang="en")
    assert "size->尺码" in captured["prompt"]


@pytest.mark.asyncio
async def test_backward_compatible_without_memory_store():
    # memory_store=None → 行为与改造前一致（纯内存缓存）
    ai = _AI()
    svc = TranslationService(ai_client=ai)
    r = await svc.translate("hello friend", target_lang="zh")
    assert r.ok and r.translated_text == "你好朋友"
    # 内存 L1 命中
    r2 = await svc.translate("hello friend", target_lang="zh")
    assert r2.cached is True
    assert ai.calls == 1
