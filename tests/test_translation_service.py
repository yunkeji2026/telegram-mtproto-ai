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


def test_detect_language_southeast_asian_and_more():
    # 跨境客服高频客户语种（此前会落 en/unknown，现确定性识别）
    assert detect_language("สวัสดีครับ อยากสอบถามราคา") == "th"   # 泰语
    assert detect_language("Xin chào, tôi muốn mua sản phẩm này") == "vi"  # 越南语
    assert detect_language("ជំរាបសួរ តើតម្លៃប៉ុន្មាន") == "km"      # 高棉语
    assert detect_language("Γειά σου τι κάνεις") == "el"           # 希腊语
    assert detect_language("שלום מה שלומך") == "he"               # 希伯来语
    assert detect_language("Halo, saya mau tanya harga") == "id"   # 印尼语（关键词）
    assert detect_language("Salamat, magkano po ito") == "tl"      # 菲律宾语
    assert detect_language("") == "unknown"


def test_detect_language_thai_baht_symbol_not_misdetected():
    # 跨境电商 THB 报价：泰铢符号 ฿ 不应让纯英文消息被判成泰语
    assert detect_language("Price: 100฿ only, free shipping") == "en"


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

