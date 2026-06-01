"""预测器适配：把不同意图分析实现包成统一 ``predict(text) -> label``。"""

from __future__ import annotations

from typing import Callable

from src.ai.chat_assistant_service import _detect_emotion, _detect_intent


def rule_intent_predictor() -> Callable[[str], str]:
    """规则版意图预测器（离线确定性、零 API），走 ChatAssistantService 同一管线。

    作为可复现基线：LLM 升级后用同一数据集对比，量化提升。
    """
    def _predict(text: str) -> str:
        t = str(text or "")
        return _detect_intent(t, emotion=_detect_emotion(t))

    return _predict
