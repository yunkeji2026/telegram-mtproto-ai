import pytest

from src.ai.translation_service import TranslationService, detect_language


def test_detect_language_common_scripts():
    assert detect_language("你好，今天怎么样") == "zh"
    assert detect_language("こんにちは、元気？") == "ja"
    assert detect_language("안녕하세요") == "ko"
    assert detect_language("مرحبا كيف حالك") == "ar"
    assert detect_language("Привет как дела") == "ru"
    assert detect_language("hola, gracias") == "es"
    assert detect_language("hello friend") == "en"


@pytest.mark.asyncio
async def test_translation_service_identity_and_cache():
    svc = TranslationService(default_target_lang="zh")
    same = await svc.translate("你好", target_lang="zh")
    assert same.ok is True
    assert same.provider == "identity"
    assert same.translated_text == "你好"

    first = await svc.translate("hello friend", target_lang="zh")
    assert first.ok is False
    assert first.error == "provider_unavailable"
    second = await svc.translate("hello friend", target_lang="zh")
    assert second.cached is True


@pytest.mark.asyncio
async def test_translation_service_uses_ai_client():
    class FakeAI:
        async def chat(self, prompt, context=None):
            assert "Translate" in prompt
            return "你好朋友"

    svc = TranslationService(ai_client=FakeAI())
    rv = await svc.translate("hello friend", target_lang="zh")
    assert rv.ok is True
    assert rv.provider == "ai"
    assert rv.translated_text == "你好朋友"

