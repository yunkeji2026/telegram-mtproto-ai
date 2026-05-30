"""
多语言 TTS 自动语言检测模块。

检测策略（按优先级，对误判率从低到高排列）：
  1. Unicode 脚本特征（正则，零依赖，100% 准确）
     — 日文（平仮名/カタカナ）、韩文（한글）、阿拉伯文、西里尔文、天城文（Hindi）、CJK
  2. langdetect 库（Latin 脚本：英/德/法/西班牙/葡/波/荷/捷/匈/土）
  3. 配置 fallback 默认值

检测目标优选 AI 回复文本（reply_text），而非用户消息（peer_text），原因：
  - reply_text 由 LLM 生成，语言纯净无混杂
  - 通常比 peer_text 更长，langdetect 精度更高
  - 完全对应 TTS 实际要合成的语言

用法：
    from src.integrations.whatsapp_rpa.lang_detect import detect_tts_lang
    lang = detect_tts_lang(reply_text)  # → "zh-cn" / "de" / "ja" / "en" / ...
"""
from __future__ import annotations

import logging
import re
from typing import Optional

_logger = logging.getLogger(__name__)

XTTS_SUPPORTED: frozenset = frozenset({
    "en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru",
    "nl", "cs", "ar", "zh-cn", "ja", "ko", "hu", "hi",
})

_NORMALIZE: dict = {
    "zh": "zh-cn",
    "zh-cn": "zh-cn",
    "zh-tw": "zh-cn",
    "zh_cn": "zh-cn",
    "zh_tw": "zh-cn",
}

_ISO_TO_HUMAN: dict = {
    "zh-cn": "Chinese", "en": "English", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "ja": "Japanese", "ko": "Korean", "ar": "Arabic", "hi": "Hindi",
    "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "cs": "Czech",
    "hu": "Hungarian",
}


def detect_tts_lang(text: str, fallback: str = "zh-cn") -> str:
    """检测文本语言，返回 XTTS-v2 兼容的语言代码。

    Args:
        text: 待检测文本（推荐传 reply_text）
        fallback: 检测失败/不支持语言时的默认值

    Returns:
        XTTS-v2 语言代码，如 "zh-cn" / "de" / "ja" / "en"
    """
    if not text:
        return fallback

    t = text.strip()
    if len(t) < 2:
        return fallback

    # ── 1. Unicode 脚本特征（零依赖，确定性高）──────────────────────────
    if re.search(r"[\u3040-\u309F\u30A0-\u30FF]", t):
        return "ja"
    if re.search(r"[\uAC00-\uD7AF]", t):
        return "ko"
    if re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", t):
        return "ar"
    if re.search(r"[\u0400-\u04FF]", t):
        return "ru"
    if re.search(r"[\u0900-\u097F]", t):
        return "hi"
    if re.search(r"[\u4E00-\u9FFF\u3400-\u4DBF]", t):
        return "zh-cn"

    # ── 2. Latin 脚本 → langdetect（含置信度过滤）─────────────────────
    if len(t) < 8:
        return fallback

    try:
        from langdetect import detect_langs, LangDetectException  # type: ignore
        langs = detect_langs(t)
        if langs:
            top = langs[0]
            raw = str(top.lang)
            normalized = _NORMALIZE.get(raw, raw)
            _logger.debug(
                "langdetect: %r → %s (prob=%.3f, accepted=%s)",
                t[:50], raw, top.prob, top.prob >= 0.55,
            )
            # 置信度 < 0.55：短句/混合文本易误判，回退到 "en"（Latin 最安全默认值）
            if top.prob < 0.55:
                return "en"
            if normalized in XTTS_SUPPORTED:
                return normalized
            return "en"
    except Exception:
        pass

    return fallback


_AILANG_TO_XTTS: dict = {
    "zh": "zh-cn",
    "ar_ur": "ar",
    "en": "en", "de": "de", "fr": "fr", "es": "es",
    "it": "it", "pt": "pt", "ru": "ru", "ja": "ja",
    "ko": "ko", "hi": "hi", "nl": "nl", "pl": "pl",
    "tr": "tr", "cs": "cs", "hu": "hu",
}

_XTTS_TO_AILANG: dict = {v: k for k, v in _AILANG_TO_XTTS.items()}


def ailang_to_xtts(ai_lang: str, fallback: str = "zh-cn") -> str:
    """Convert AIClient language code → XTTS-v2 language code.

    Examples:
        "zh"    → "zh-cn"
        "ar_ur" → "ar"
        "de"    → "de"
        "vi"    → "en"  (not XTTS-supported, fallback)
    """
    if not ai_lang:
        return fallback
    code = ai_lang.strip().lower()
    mapped = _AILANG_TO_XTTS.get(code, code)
    return mapped if mapped in XTTS_SUPPORTED else fallback


def tts_lang_to_human(xtts_lang: str) -> str:
    """返回语言的英文名称（用于 AI prompt 指令）。

    Examples:
        "zh-cn" → "Chinese"
        "de"    → "German"
        "ja"    → "Japanese"
    """
    return _ISO_TO_HUMAN.get(xtts_lang, xtts_lang)
