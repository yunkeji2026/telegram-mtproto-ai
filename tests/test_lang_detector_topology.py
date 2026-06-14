"""语言检测器收敛拓扑封板（防止未来又分叉出新的独立检测器）。

经统一后，全仓「语种判定」的拓扑为：

  单一确定性核心
      translation_service.detect_language  ← lingua/langdetect 可注入统计层
            ▲                ▲                       ▲
   委托 + 业务兜底       委托 + 镜像语义          委托 + 业务兜底
  ai_client.            (本测试)              unified_inbox_routes.
  _detect_message_      _detect_language       _detect_language
  language
            ▲
       委托（注入 ai_client 时）
  messenger_rpa.runner._detect_peer_lang

独立、刻意不并入的检测器：
  whatsapp_rpa.lang_detect.detect_tts_lang —— TTS 专用，XTTS 码空间（zh-cn）、
  CJK 优先（任意汉字→中文语音）、覆盖 pl/nl/cs/hu 等 XTTS 专属语种、检测对象是
  reply_text。语义与上面一族不同，强行收敛会丢能力且破坏其单元测试，故保持独立，
  仅通过 ailang_to_xtts 与主检测器衔接。
"""

from __future__ import annotations

from src.ai.translation_service import detect_language, normalize_lang
from src.ai.ai_client import AIClient
from src.web.routes.unified_inbox_routes import _detect_language as inbox_detect
from src.integrations.messenger_rpa.runner import _detect_peer_lang
from src.integrations.whatsapp_rpa.lang_detect import (
    detect_tts_lang,
    ailang_to_xtts,
    XTTS_SUPPORTED,
)


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


# 重叠语种样本：脚本类 + 明确拉丁关键词，三个同类检测器应一致识别。
_OVERLAP = {
    "ja": "こんにちは、元気ですか",
    "ko": "안녕하세요 잘 지내세요",
    "ru": "Привет, как дела сегодня",
    "th": "สวัสดีครับ อยากสอบถามราคา",
    "hi": "नमस्ते आप कैसे हैं",
    "es": "hola, gracias por todo amigo",
}


def test_three_sibling_detectors_agree_on_overlap():
    """global / ai_client / unified_inbox 在重叠语种上判定一致（归一化后）。"""
    client = AIClient(_Cfg())
    for expect, text in _OVERLAP.items():
        g = detect_language(text)
        a = normalize_lang(client._detect_message_language(text))
        i = normalize_lang(inbox_detect(text))
        assert g == expect, f"global {text!r} -> {g!r} != {expect}"
        assert a == expect, f"ai_client {text!r} -> {a!r} != {expect}"
        assert i == expect, f"inbox {text!r} -> {i!r} != {expect}"


def test_arabic_code_space_difference_normalizes_consistently():
    """ai_client 刻意用 'ar_ur'（下游 prompt 契约），归一后与全局 'ar' 一致。"""
    client = AIClient(_Cfg())
    text = "مرحبا كيف حالك"
    assert detect_language(text) == "ar"
    assert client._detect_message_language(text) == "ar_ur"
    assert normalize_lang("ar_ur") == "ar"


def test_messenger_peer_lang_delegates_to_ai_client():
    """messenger _detect_peer_lang 注入 ai_client 时，结果与 ai_client 完全一致。"""
    client = AIClient(_Cfg())
    for text in _OVERLAP.values():
        assert _detect_peer_lang(text, ai_client=client) == client._detect_message_language(text)


def test_whatsapp_tts_detector_is_independent_by_design():
    """TTS 检测器与主检测器语义不同——本测试锁定这个「刻意的差异」。"""
    # CJK 优先：任意汉字 → 中文语音（TTS 正确行为）
    assert detect_tts_lang("我想查一下 order status") == "zh-cn"
    # 全局是比例逻辑：拉丁占多 → en（两者本就不同，故 TTS 不并入收敛）
    assert detect_language("我想查一下 order status") == "en"
    # XTTS 码空间衔接契约
    assert ailang_to_xtts("zh") == "zh-cn"
    assert ailang_to_xtts("ar_ur") == "ar"


def test_whatsapp_tts_always_returns_xtts_supported():
    samples = [
        "Hello world",
        "Hallo Welt",
        "你好世界",
        "こんにちは世界",
        "안녕하세요 세계",
        "مرحبا بالعالم",
        "Привет мир",
        "สวัสดีไม่รองรับ",  # 泰语：XTTS 不支持 → 回落到受支持码
    ]
    for s in samples:
        assert detect_tts_lang(s) in XTTS_SUPPORTED, f"{s!r} -> not in XTTS_SUPPORTED"
