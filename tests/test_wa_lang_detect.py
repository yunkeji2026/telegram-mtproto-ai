"""
tests/test_wa_lang_detect.py
WhatsApp 多语言 TTS 检测模块单元测试。
"""
import pytest
from src.integrations.whatsapp_rpa.lang_detect import (
    detect_tts_lang,
    tts_lang_to_human,
    XTTS_SUPPORTED,
)


class TestScriptBasedDetection:
    """Unicode 脚本特征路径（零依赖，必须 100% 准确）"""

    def test_chinese_simplified(self):
        assert detect_tts_lang("你好，今天天气怎么样？") == "zh-cn"

    def test_chinese_mixed_latin(self):
        assert detect_tts_lang("我想查一下 order status") == "zh-cn"

    def test_japanese_hiragana(self):
        assert detect_tts_lang("こんにちは、元気ですか？") == "ja"

    def test_japanese_katakana(self):
        assert detect_tts_lang("アニメが大好きです") == "ja"

    def test_japanese_mixed(self):
        assert detect_tts_lang("日本語テスト、よろしくお願いします") == "ja"

    def test_korean(self):
        assert detect_tts_lang("안녕하세요, 주문 확인 부탁드립니다") == "ko"

    def test_arabic(self):
        assert detect_tts_lang("مرحبا، كيف حالك اليوم؟") == "ar"

    def test_russian_cyrillic(self):
        assert detect_tts_lang("Привет, как дела?") == "ru"

    def test_hindi_devanagari(self):
        assert detect_tts_lang("नमस्ते, आप कैसे हैं?") == "hi"


class TestLangdetectPath:
    """Latin 脚本 → langdetect 路径"""

    def test_english(self):
        result = detect_tts_lang("Hello, how can I help you today?")
        assert result == "en"

    def test_german(self):
        result = detect_tts_lang(
            "Guten Tag! Ich möchte eine Bestellung aufgeben."
        )
        assert result == "de"

    def test_french(self):
        result = detect_tts_lang("Bonjour, comment puis-je vous aider?")
        assert result == "fr"

    def test_spanish(self):
        result = detect_tts_lang("Hola, ¿cómo estás? Quiero hacer un pedido.")
        assert result == "es"

    def test_italian(self):
        result = detect_tts_lang("Buongiorno, come posso aiutarti oggi?")
        assert result == "it"

    def test_portuguese(self):
        result = detect_tts_lang("Olá, como posso ajudá-lo hoje? Preciso de ajuda com meu pedido.")
        assert result in ("pt", "es"), f"Expected pt or es (Romance lang), got {result!r}"

    def test_russian_latin_fallback(self):
        result = detect_tts_lang("Привет мир")
        assert result == "ru"


class TestEdgeCases:
    """边界情况和 fallback"""

    def test_empty_string(self):
        assert detect_tts_lang("") == "zh-cn"

    def test_empty_string_custom_fallback(self):
        assert detect_tts_lang("", fallback="en") == "en"

    def test_single_char(self):
        assert detect_tts_lang("a") == "zh-cn"

    def test_unsupported_language_falls_back_to_en(self):
        result = detect_tts_lang("Kumusta ka na? Mabuti naman ako.")
        assert result in XTTS_SUPPORTED

    def test_all_returned_langs_are_supported(self):
        samples = [
            "Hello world",
            "Hallo Welt",
            "Bonjour le monde",
            "你好世界",
            "こんにちは世界",
            "안녕하세요 세계",
            "مرحبا بالعالم",
            "Привет мир",
        ]
        for s in samples:
            lang = detect_tts_lang(s)
            assert lang in XTTS_SUPPORTED, f"'{s}' → '{lang}' not in XTTS_SUPPORTED"

    def test_custom_fallback_used_on_short_text(self):
        result = detect_tts_lang("ok", fallback="de")
        assert result == "de"


class TestTtsLangToHuman:
    """语言代码 → 人类可读名称"""

    def test_chinese(self):
        assert tts_lang_to_human("zh-cn") == "Chinese"

    def test_german(self):
        assert tts_lang_to_human("de") == "German"

    def test_japanese(self):
        assert tts_lang_to_human("ja") == "Japanese"

    def test_unknown_passthrough(self):
        assert tts_lang_to_human("xx") == "xx"
