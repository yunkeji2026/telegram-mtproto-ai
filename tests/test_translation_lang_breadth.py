"""Phase E：翻译语种广度扩充（20→60+）单测。

确认 LANG_NAMES 扩到 60+ 主流语种、新增码有显示名，且**确定性检测行为不回归**
（扩表只影响显示名 + 统计回退白名单，不改脚本/关键词检测）。
"""
import pytest

from src.ai.translation_service import LANG_NAMES, detect_language, normalize_lang


def test_lang_count_at_least_60():
    # 排除 unknown 占位
    real = {k for k in LANG_NAMES if k != "unknown"}
    assert len(real) >= 60, f"仅 {len(real)} 语种，未达 60+"


@pytest.mark.parametrize("code,name", [
    ("nl", "Dutch"), ("pl", "Polish"), ("uk", "Ukrainian"), ("fa", "Persian"),
    ("ur", "Urdu"), ("bn", "Bengali"), ("ta", "Tamil"), ("sw", "Swahili"),
    ("my", "Burmese"), ("ka", "Georgian"),
])
def test_new_codes_have_names(code, name):
    assert LANG_NAMES.get(code) == name


def test_existing_20_preserved():
    for code in ("zh", "en", "ja", "ko", "ar", "ru", "hi", "es", "pt", "fr",
                 "de", "it", "tr", "vi", "id", "th", "ms", "tl", "km", "he", "el"):
        assert code in LANG_NAMES


def test_deterministic_detection_not_regressed():
    # 扩表不应改变既有脚本/关键词检测结果
    assert detect_language("你好，今天天气不错") == "zh"
    assert detect_language("こんにちは、元気ですか") == "ja"
    assert detect_language("안녕하세요 반갑습니다") == "ko"
    assert detect_language("Здравствуйте, как дела") == "ru"
    assert detect_language("Hola, gracias por todo") == "es"


def test_unknown_still_last_and_present():
    assert LANG_NAMES.get("unknown") == "Unknown"
    keys = list(LANG_NAMES.keys())
    assert keys[-1] == "unknown"
