"""P33 语种检测器统一到全局 detect_language 后的行为锁定。

此前 unified_inbox_routes._detect_language 无任何测试覆盖。本测试既锁定
「保留的 P33 业务语境行为」，也验证「合并后白嫖的脚本/语种增强」。
"""

from src.web.routes.unified_inbox_helpers import _detect_language


def test_business_default_preserved():
    # 空文本与含糊拉丁回落业务主力语言 zh（P33 原行为，逐字保留）
    assert _detect_language("") == "zh"
    assert _detect_language("   ") == "zh"
    assert _detect_language("你好，请问有货吗") == "zh"
    # 无明确英文关键词的拉丁文本 → zh（不误判为英文）
    assert _detect_language("hello friend") == "zh"


def test_strong_english_keywords_still_english():
    # ≥2 个英文关键词 → en（P33 原阈值保留）
    assert _detect_language("can you tell me what the price is") == "en"


def test_indonesian_keyword_path_preserved():
    # 印尼语经全局检测器关键词命中 → id
    assert _detect_language("saya mau tanya harga ini") == "id"


def test_script_languages_now_detected():
    # 合并后白嫖全局检测器的脚本/语种增强（此前会落 zh/en）
    assert _detect_language("Xin chào, tôi muốn mua sản phẩm này") == "vi"
    assert _detect_language("สวัสดีครับ อยากสอบถามราคา") == "th"
    assert _detect_language("ជំរាបសួរ តើតម្លៃប៉ុន្មាន") == "km"   # 高棉语不再落 zh
    assert _detect_language("hola, gracias por todo") == "es"   # 此前只懂 id/en → zh


def test_thai_baht_symbol_not_misdetected():
    # 跨境 THB 报价：泰铢符号 ฿ 不应让纯英文消息被判成泰语（回落 zh，非 th）
    assert _detect_language("Price: 100฿ only, free shipping") != "th"
