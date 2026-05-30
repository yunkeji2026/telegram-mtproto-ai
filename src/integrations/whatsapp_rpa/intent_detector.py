"""P15-f: 轻量意图检测模块 - 识别停止联系/投诉意图。

结合规则匹配 + 文本特征，替代纯关键词匹配，降低误判。
保留关键词作为兜底，形成双层过滤。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


class StopContactIntentDetector:
    """检测用户是否要求停止联系或表达不满/投诉。"""

    # 强意图模式（直接要求停止）
    STRONG_PATTERNS: List[Tuple[str, float]] = [
        # 中文
        (r"停止.*联系|别.*联系|不要再?.*发|不要.*消息|别再.*找|别.*打扰|别.*烦", 0.95),
        (r"退订|取消.*订阅|取消.*关注|拉黑|屏蔽|举报|投诉", 0.90),
        (r"滚|别.*骚扰|骚扰.*我|不.*再聊|结束.*对话|不用.*回复|不用.*联系", 0.92),
        # 英文
        (r"\bstop\s+(?:contacting?|messaging?|texting?|calling?|emailing?)\b", 0.95),
        (r"\bunsubscribe\b|\bopt[\s-]?out\b|\bremove\s+me\b", 0.92),
        (r"\bdo\s+not\s+(?:contact|call|text|message|email)\b", 0.95),
        (r"\b(?:block|report|spam|harass)\b", 0.85),
        (r"\bstop\s+sending\s+(?:me\s+)?(?:messages?|texts?|emails?)\b", 0.93),
        (r"\bleave\s+me\s+alone\b|\bnever\s+contact\b", 0.96),
    ]

    # 弱意图模式（表达不满，可能想停止）
    WEAK_PATTERNS: List[Tuple[str, float]] = [
        # 中文
        (r"烦|讨厌|不要.*发|不想.*收到|太多.*消息|太.*频繁", 0.70),
        (r"不需要|没兴趣|不用.*推|不用.*发", 0.65),
        # 英文
        (r"\bannoying\b|\btoo\s+(?:many|much)\b|\bspam\b", 0.68),
        (r"\bnot\s+interested\b|\bno\s+need\b", 0.62),
    ]

    # 否定模式（排除误报）- 用户可能在讨论而非要求停止
    NEGATIVE_PATTERNS: List[str] = [
        r"不要停止|别停止|继续.*发|保持.*联系|经常.*联系",
        r"don'?t\s+stop|\bkeep\s+(?:contacting|sending|texting)\b",
        r"别停|继续|经常联系",
    ]

    def __init__(
        self,
        strong_threshold: float = 0.85,
        weak_threshold: float = 0.70,
        enable_negative_check: bool = True,
    ) -> None:
        self.strong_threshold = strong_threshold
        self.weak_threshold = weak_threshold
        self.enable_negative_check = enable_negative_check

    def detect(self, text: str) -> Dict[str, any]:
        """检测意图，返回置信度和判断依据。

        Returns:
            {
                "is_stop_contact": bool,
                "confidence": float,
                "level": "strong" | "weak" | "none",
                "matched_patterns": List[str],
                "blocked_by_negative": bool,
            }
        """
        if not text or not text.strip():
            return {
                "is_stop_contact": False,
                "confidence": 0.0,
                "level": "none",
                "matched_patterns": [],
                "blocked_by_negative": False,
            }

        text_lower = text.lower().strip()

        # 1. 检查否定模式（排除误报）
        blocked_by_negative = False
        if self.enable_negative_check:
            for neg_pat in self.NEGATIVE_PATTERNS:
                if re.search(neg_pat, text_lower, re.IGNORECASE):
                    blocked_by_negative = True
                    break

        # 2. 匹配强意图
        max_confidence = 0.0
        matched_patterns = []
        level = "none"

        for pattern, conf in self.STRONG_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                matched_patterns.append(f"strong:{pattern}")
                if conf > max_confidence:
                    max_confidence = conf
                    level = "strong"

        # 3. 匹配弱意图（仅当没有强意图时）
        if max_confidence < self.strong_threshold:
            for pattern, conf in self.WEAK_PATTERNS:
                if re.search(pattern, text_lower, re.IGNORECASE):
                    matched_patterns.append(f"weak:{pattern}")
                    if conf > max_confidence:
                        max_confidence = conf
                        level = "weak"

        # 4. 综合判断
        is_stop_contact = False
        if blocked_by_negative:
            # 被否定模式阻挡，降低置信度
            max_confidence *= 0.3
            is_stop_contact = False
        elif level == "strong" and max_confidence >= self.strong_threshold:
            is_stop_contact = True
        elif level == "weak" and max_confidence >= self.weak_threshold:
            is_stop_contact = True

        return {
            "is_stop_contact": is_stop_contact,
            "confidence": round(max_confidence, 3),
            "level": level if max_confidence > 0 else "none",
            "matched_patterns": matched_patterns,
            "blocked_by_negative": blocked_by_negative,
        }


def create_detector(config: Optional[Dict] = None) -> StopContactIntentDetector:
    """工厂函数，从配置创建检测器。"""
    cfg = config or {}
    return StopContactIntentDetector(
        strong_threshold=cfg.get("strong_threshold", 0.85),
        weak_threshold=cfg.get("weak_threshold", 0.70),
        enable_negative_check=cfg.get("enable_negative_check", True),
    )
