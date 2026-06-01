"""预测器适配：把不同意图分析实现包成统一 ``predict(text) -> label``。"""

from __future__ import annotations

from typing import Callable, List, Optional

from src.ai.chat_assistant_service import _detect_emotion, _detect_intent

# ChatAssistantService 规则版意图标签空间（LLM 预测器约束到同一集合，保证可对比）
INTENT_LABELS: List[str] = [
    "打招呼", "停止联系", "需要安抚", "不满/投诉",
    "短句接话", "提问", "继续聊天", "空消息",
]


def rule_intent_predictor() -> Callable[[str], str]:
    """规则版意图预测器（离线确定性、零 API），走 ChatAssistantService 同一管线。

    作为可复现基线：LLM 升级后用同一数据集对比，量化提升。
    """
    def _predict(text: str) -> str:
        t = str(text or "")
        return _detect_intent(t, emotion=_detect_emotion(t))

    return _predict


def build_intent_classify_prompt(text: str, labels: List[str]) -> str:
    """构造「约束分类」提示：让 LLM 只能从给定标签里选一个，便于与规则版对比。"""
    opts = "、".join(labels)
    return (
        "你是意图分类器。把下面这条用户消息归类到且仅归类到以下标签之一，"
        f"只输出标签本身，不要解释：\n标签集合：{opts}\n\n消息：{text}\n标签："
    )


def parse_intent_label(raw: str, labels: List[str]) -> str:
    """从 LLM 原始输出里解析出标签：精确匹配 > 子串包含 > 兜底「继续聊天」。"""
    s = str(raw or "").strip()
    if s in labels:
        return s
    for lab in labels:
        if lab and lab in s:
            return lab
    return "继续聊天" if "继续聊天" in labels else (labels[0] if labels else "")


def llm_intent_predictor(
    generate_fn: Callable[[str], str],
    labels: Optional[List[str]] = None,
) -> Callable[[str], str]:
    """LLM 意图预测器（约束到 labels）。

    generate_fn：同步 ``(prompt) -> raw_text``（真实场景可包 ai_client；
    单测注入 fake，不联网）。空消息直接判定；LLM 异常兜底「继续聊天」。
    """
    labs = labels or list(INTENT_LABELS)

    def _predict(text: str) -> str:
        t = str(text or "")
        if not t.strip():
            return "空消息"
        try:
            raw = generate_fn(build_intent_classify_prompt(t, labs))
        except Exception:
            return "继续聊天"
        return parse_intent_label(raw, labs)

    return _predict
