"""ai_client._detect_message_language 统一到全局 detect_language 后的行为锁定。

沿用 P33 收敛模式（见 test_detect_language_consolidation.py）：脚本/拉丁关键词的
确定性核心委托全局 ``translation_service.detect_language``（单一规则来源），本类仅
保留「回复镜像」特有语义：
  - 空 / 仅 @mention → 'zh'（回复默认主力中文）
  - 含糊拉丁 → 'en'（英文客户回英文，区别于 inbox 业务默认 zh）
  - 阿拉伯/乌尔都 → 'ar_ur'（本类下游 prompt 契约，全局返回 'ar'）
  - 旁遮普 / 孟加拉脚本：全局未覆盖，本封装本地补充以保留既有能力
同时验证合并后「白嫖」的脚本增强（km/he/el）与泰铢符 ฿ 不再误判 th。
"""

from __future__ import annotations

from src.ai.ai_client import AIClient


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


def _d(text: str) -> str:
    return AIClient(_Cfg())._detect_message_language(text)


def test_empty_and_mention_only_default_zh():
    assert _d("") == "zh"
    assert _d("   ") == "zh"
    assert _d("@bot @user") == "zh"  # 仅 @mention，剥离后为空


def test_chinese_and_mention_stripping():
    assert _d("你好，今天天气真好") == "zh"
    assert _d("@someone 你好") == "zh"


def test_english_paths():
    assert _d("Hello there how are you") == "en"
    assert _d("ok") == "en"
    assert _d("hi") == "en"


def test_chinese_dominant_mixed_stays_zh():
    # 中文为主、夹少量英文词 → zh（保留原 CJK 比例规则）
    assert _d("你好 hello") == "zh"


def test_script_languages_preserved():
    assert _d("こんにちは、元気ですか？") == "ja"
    assert _d("今日は良い天気ですね") == "ja"  # 汉字+假名不被短路成 zh
    assert _d("안녕하세요 잘 지내세요") == "ko"
    assert _d("Привет, как дела сегодня") == "ru"
    assert _d("สวัสดีครับ อยากสอบถามราคา") == "th"
    assert _d("नमस्ते आप कैसे हैं") == "hi"


def test_arabic_maps_to_ar_ur():
    assert _d("مرحبا كيف حالك") == "ar_ur"


def test_punjabi_bengali_local_supplement():
    # 全局 detect_language 未含 pa/bn（会落 zh），本封装本地补充保留能力
    assert _d("ਸਤ ਸ੍ਰੀ ਅਕਾਲ ਤੁਸੀਂ ਕਿਵੇਂ ਹੋ") == "pa"
    assert _d("নমস্কার আপনি কেমন আছেন") == "bn"


def test_latin_keyword_languages():
    assert _d("hola, gracias por todo amigo") == "es"
    assert _d("olá, muito obrigado pela ajuda") == "pt"
    assert _d("bonjour, merci beaucoup pour tout") == "fr"


def test_consolidation_script_enhancements():
    # 合并后白嫖：高棉/希腊脚本此前会落 zh/en，现可识别
    assert _d("ជំរាបសួរ តើតម្លៃប៉ុន្មាន") == "km"
    assert _d("Γεια σας πώς είστε σήμερα") == "el"


def test_thai_baht_symbol_not_misdetected_as_thai():
    # 跨境 THB 报价：泰铢符 ฿ 不应让纯英文消息被误判成泰语
    assert _d("Price: 100฿ only, free shipping") != "th"
