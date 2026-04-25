"""Phase 0.4 — 日文回复路径 smoke 测。

覆盖：
- AIClient._detect_message_language 对日文 / 中文 / 英文 / 韩文 的判定
- runner._detect_peer_lang 委托 ai_client 的多语言能力
- _build_system_instruction 在 reply_lang=ja 时注入「LANGUAGE RULE」+ 日文语言名
"""

from __future__ import annotations

from src.ai.ai_client import AIClient
from src.integrations.messenger_rpa.runner import _detect_peer_lang


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


def test_detect_japanese_hiragana():
    client = AIClient(_Cfg())
    assert client._detect_message_language("こんにちは、元気ですか？") == "ja"


def test_detect_japanese_katakana():
    client = AIClient(_Cfg())
    assert client._detect_message_language("ハロー、テストですよ") == "ja"


def test_detect_japanese_mixed_kanji_hiragana():
    client = AIClient(_Cfg())
    # 含汉字 + 假名 → 假名优先 → ja（不应被 CJK 短路成 zh）
    assert client._detect_message_language("今日は良い天気ですね") == "ja"


def test_detect_chinese_pure():
    client = AIClient(_Cfg())
    assert client._detect_message_language("你好，今天天气真好") == "zh"


def test_detect_english():
    client = AIClient(_Cfg())
    assert client._detect_message_language("Hello there how are you") == "en"


def test_detect_korean():
    client = AIClient(_Cfg())
    assert client._detect_message_language("안녕하세요 잘 지내세요") == "ko"


def test_runner_helper_delegates_to_ai_client():
    """runner._detect_peer_lang 拿到 ai_client 时，应代理给 AIClient 拿到日文识别。"""
    client = AIClient(_Cfg())
    assert _detect_peer_lang("こんにちは", ai_client=client) == "ja"
    assert _detect_peer_lang("Hello", ai_client=client) == "en"
    assert _detect_peer_lang("你好", ai_client=client) == "zh"


def test_runner_helper_fallback_without_ai_client():
    """没传 ai_client 时退回 zh/en/unknown 极简实现（向后兼容）。"""
    assert _detect_peer_lang("你好") == "zh"
    assert _detect_peer_lang("Hello there") == "en"
    assert _detect_peer_lang("") == "unknown"


def test_system_prompt_injects_japanese_language_rule():
    """reply_lang=ja 时，system prompt 必含 LANGUAGE RULE 段 + 日文名。"""
    client = AIClient(_Cfg())
    prompt = client._build_system_instruction({"reply_lang": "ja"})
    assert "LANGUAGE RULE" in prompt
    assert "日本語" in prompt
    # 不应再出现「zh fallback」段（"ALWAYS reply in the SAME language" 那条是 zh 默认）
    assert "ALWAYS reply in the SAME language" not in prompt


def test_system_prompt_zh_default_no_language_rule_block():
    """reply_lang=zh（默认）不应注入强制 LANGUAGE RULE，走通用多语言规则。"""
    client = AIClient(_Cfg())
    prompt = client._build_system_instruction({"reply_lang": "zh"})
    assert "LANGUAGE RULE — TOP PRIORITY" not in prompt
    assert "ALWAYS reply in the SAME language" in prompt
