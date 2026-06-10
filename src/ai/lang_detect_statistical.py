"""Phase B：可选统计语种检测适配器（与具体库无关，纯增量）。

定位：仅在 ``translation_service.detect_language`` 的**确定性核心**落到弱结果
（``en`` / ``unknown``，即含糊拉丁文本）时作为回退，精修语种判定。脚本类语种
（中日韩泰高棉阿俄等）与越南语、明确拉丁关键词不经过这里。

设计原则：
- **零新增硬依赖**：所有后端都是 try-import 可选，未安装则适配器不可用，
  调用方自动回落确定性结果。
- **确定性**：lingua 默认确定性；langdetect 固定 seed=0。
- **面向短文本**：优先 lingua（专为短文本设计），回退 langdetect。
- **后端无关**：对外只暴露 ``detect(text) -> Optional[str]``，便于替换/注入。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _build_lingua_detector() -> Optional[Callable[[str], Optional[str]]]:
    try:
        from lingua import Language, LanguageDetectorBuilder
    except Exception:
        return None
    # ISO 639-1 → lingua Language 枚举名（仅取库内确实存在的，缺失的跳过）。
    name_map = {
        "zh": "CHINESE", "en": "ENGLISH", "ja": "JAPANESE", "ko": "KOREAN",
        "ar": "ARABIC", "ru": "RUSSIAN", "hi": "HINDI", "es": "SPANISH",
        "pt": "PORTUGUESE", "fr": "FRENCH", "de": "GERMAN", "it": "ITALIAN",
        "tr": "TURKISH", "vi": "VIETNAMESE", "id": "INDONESIAN", "th": "THAI",
        "ms": "MALAY", "tl": "TAGALOG", "he": "HEBREW", "el": "GREEK",
    }
    langs = []
    for code, enum_name in name_map.items():
        lang = getattr(Language, enum_name, None)
        if lang is not None:
            langs.append(lang)
    if len(langs) < 2:
        return None
    try:
        # with_minimum_relative_distance：前两名语种过于接近时弃权（返回 None），
        # 避免短歧义文本上「自信地猜错」——契合「仅在统计确信时才覆盖确定性」。
        detector = (
            LanguageDetectorBuilder.from_languages(*langs)
            .with_minimum_relative_distance(0.25)
            .build()
        )
    except Exception:
        logger.debug("lingua detector 构建失败", exc_info=True)
        return None

    def _detect(text: str) -> Optional[str]:
        try:
            res = detector.detect_language_of(text)
            if res is None:
                return None
            return str(res.iso_code_639_1.name).lower()
        except Exception:
            return None

    logger.info("统计语种检测后端：lingua（%d 候选语种）", len(langs))
    return _detect


def _build_langdetect_detector() -> Optional[Callable[[str], Optional[str]]]:
    try:
        from langdetect import DetectorFactory, detect
    except Exception:
        return None
    try:
        DetectorFactory.seed = 0  # 固定 seed → 结果可复现
    except Exception:
        pass

    def _detect(text: str) -> Optional[str]:
        try:
            return str(detect(text) or "") or None
        except Exception:
            return None

    logger.info("统计语种检测后端：langdetect（seed=0）")
    return _detect


def build_statistical_detector() -> Optional[Callable[[str], Optional[str]]]:
    """按优先级探测可用后端：lingua（短文本更优）→ langdetect。

    返回 ``detect(text) -> Optional[str]``（ISO 639-1，未归一化）；
    无任何后端可用时返回 None，调用方应回落确定性检测。
    """
    return _build_lingua_detector() or _build_langdetect_detector()
