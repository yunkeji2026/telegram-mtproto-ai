"""Chat analysis and reply suggestions for companion-style inbox workflows."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.ai.translation_service import detect_language


@dataclass
class ReplySuggestion:
    style: str
    title: str
    text: str
    risk_level: str = "low"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "style": self.style,
            "title": self.title,
            "text": self.text,
            "risk_level": self.risk_level,
        }


@dataclass
class ChatAnalysis:
    language: str
    emotion: str
    intent: str
    risk_level: str
    risk_reasons: List[str] = field(default_factory=list)
    relationship_stage: str = "待判断"
    next_step: str = ""
    suggestions: List[ReplySuggestion] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "language": self.language,
            "emotion": self.emotion,
            "intent": self.intent,
            "risk_level": self.risk_level,
            "risk_reasons": list(self.risk_reasons),
            "relationship_stage": self.relationship_stage,
            "next_step": self.next_step,
            "suggestions": [s.to_dict() for s in self.suggestions],
        }


class ChatAssistantService:
    """Rule-first analyzer with an optional AI upgrade path.

    The MVP keeps output stable and auditable. Later phases can add LLM scoring
    behind the same return shape.
    """

    def __init__(self, *, ai_client: Optional[Any] = None) -> None:
        self.ai_client = ai_client

    async def analyze(
        self,
        *,
        text: str,
        messages: Optional[List[Dict[str, Any]]] = None,
        chat: Optional[Dict[str, Any]] = None,
    ) -> ChatAnalysis:
        raw = str(text or "").strip()
        msgs = list(messages or [])
        lang = (chat or {}).get("language") or detect_language(raw)
        emotion = _detect_emotion(raw)
        intent = _detect_intent(raw, emotion=emotion)
        risk_level, reasons = _detect_risk(raw, emotion=emotion, intent=intent)
        stage = _relationship_stage(chat, len(msgs))
        next_step = _next_step(intent, emotion, risk_level)
        suggestions = _suggestions(raw, lang=lang, intent=intent, emotion=emotion, risk=risk_level)
        return ChatAnalysis(
            language=lang,
            emotion=emotion,
            intent=intent,
            risk_level=risk_level,
            risk_reasons=reasons,
            relationship_stage=stage,
            next_step=next_step,
            suggestions=suggestions,
        )


def _detect_emotion(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ("sad", "tired", "lonely", "难过", "累", "孤独", "失落", "想哭")):
        return "低落"
    if any(k in t for k in ("angry", "mad", "生气", "烦", "滚", "讨厌")):
        return "生气"
    if any(k in t for k in ("worried", "anxious", "焦虑", "担心", "害怕")):
        return "焦虑"
    if any(k in t for k in ("haha", "lol", "happy", "开心", "哈哈", "喜欢", "thanks", "谢谢")):
        return "积极"
    if len(text.strip()) <= 6:
        return "简短"
    return "平稳"


def _detect_intent(text: str, *, emotion: str) -> str:
    t = text.lower().strip()
    if not t:
        return "空消息"
    if re.fullmatch(r"(hi|hey|hello|在吗|在|你好|哈喽|嗨|hola|olá|bonjour|こんにちは)", t):
        return "打招呼"
    if any(k in t for k in ("stop", "don't contact", "unsubscribe", "别联系", "别发", "不要再")):
        return "停止联系"
    if emotion in {"低落", "焦虑"}:
        return "需要安抚"
    if emotion == "生气":
        return "不满/投诉"
    if len(t) <= 8:
        return "短句接话"
    if "?" in t or "？" in t:
        return "提问"
    return "继续聊天"


def _detect_risk(text: str, *, emotion: str, intent: str) -> tuple[str, List[str]]:
    t = text.lower()
    reasons: List[str] = []
    high_terms = {
        "self_harm": ("suicide", "kill myself", "自杀", "不想活"),
        "money": ("money", "transfer", "bank", "crypto", "转账", "银行卡", "借钱", "打款"),
        "privacy": ("passport", "password", "address", "密码", "护照", "住址", "身份证"),
        "adult": ("nude", "sex", "裸照", "成人视频"),
    }
    for label, kws in high_terms.items():
        if any(k in t for k in kws):
            reasons.append(label)
    if intent == "停止联系":
        reasons.append("stop_contact")
    if reasons:
        return "high", reasons
    if emotion == "生气" or intent == "不满/投诉":
        return "medium", ["negative_emotion"]
    return "low", []


def _relationship_stage(chat: Optional[Dict[str, Any]], msg_count: int) -> str:
    rel = (chat or {}).get("relationship") or {}
    stage = str(rel.get("stage") or "").strip()
    if stage:
        return stage
    if msg_count >= 20:
        return "稳定陪伴"
    if msg_count >= 8:
        return "升温"
    return "初识"


def _next_step(intent: str, emotion: str, risk_level: str) -> str:
    if risk_level == "high":
        return "先人工审核，避免自动发送。"
    if intent == "需要安抚":
        return "先接住情绪，再轻问一个具体问题。"
    if intent == "打招呼":
        return "短句温柔回应，不要客服腔。"
    if intent == "短句接话":
        return "顺着对方语气轻接一句，避免连续追问。"
    return "自然承接本句，再给一个轻量开放话题。"


def _suggestions(
    text: str,
    *,
    lang: str,
    intent: str,
    emotion: str,
    risk: str,
) -> List[ReplySuggestion]:
    if risk == "high":
        return [
            ReplySuggestion("review", "人工审核", "这条内容可能涉及敏感边界，我先帮你转人工确认后再回复。", "high"),
            ReplySuggestion("calm", "克制回应", "我先认真看一下你说的，等确认清楚再回你，避免误会。", "medium"),
            ReplySuggestion("boundary", "边界提醒", "这个话题我不能随便处理，我们先换个安全一点的方式聊。", "medium"),
        ]
    if intent == "打招呼":
        base = [
            ("warm", "温柔版", "在呢，刚好看到你消息。今天过得还好吗？"),
            ("short", "简短版", "在呀，我听着呢。"),
            ("playful", "轻松版", "来啦，你这一声我立刻上线。"),
        ]
    elif intent == "需要安抚":
        base = [
            ("comfort", "安抚版", "听起来你今天真的有点累，我先陪你缓一缓。发生什么了？"),
            ("short", "简短版", "我在，先别急，慢慢跟我说。"),
            ("deep", "深入版", "你不用一下子讲清楚，我们可以一点点来。我更想先知道，最让你难受的是哪一部分？"),
        ]
    elif intent == "短句接话":
        base = [
            ("warm", "温柔版", "嗯嗯，我懂你这个感觉。"),
            ("continue", "继续聊", "那我就顺着你说的问一句，你更偏喜欢哪种感觉？"),
            ("playful", "轻松版", "哈哈，这个回答有点可爱。"),
        ]
    else:
        base = [
            ("warm", "温柔版", "我明白你的意思了，这个话题还挺值得慢慢聊的。"),
            ("short", "简短版", "懂了，那我们就先这样聊下去。"),
            ("deep", "深入版", "你刚刚这句话里我最在意的是你的感受，不只是事情本身。"),
        ]
    return [ReplySuggestion(style=s, title=title, text=body) for s, title, body in base]

