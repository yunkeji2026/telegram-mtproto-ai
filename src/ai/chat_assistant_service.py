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


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


class ChatAssistantService:
    """Rule-first analyzer with an optional LLM upgrade path (Phase C1).

    规则做兜底、LLM 做提升，返回 shape 始终是 ChatAnalysis。关键不变量：
    - LLM 故障/超时/非 JSON → 静默回落规则版，不抛错。
    - 风险只升不降：final_risk = max(rule, llm)；规则命中的硬底线（money/privacy
      /self_harm 等）LLM 不能调低。
    - use_llm=False / ai_client=None 时行为与改造前完全一致（向后兼容）。
    """

    def __init__(
        self,
        *,
        ai_client: Optional[Any] = None,
        use_llm: bool = False,
        analysis_store: Optional[Any] = None,
        timeout_sec: float = 8.0,
    ) -> None:
        self.ai_client = ai_client
        self._use_llm = bool(use_llm)
        self._analysis_store = analysis_store
        self._timeout = float(timeout_sec or 8.0)

    def _rule_analyze(
        self,
        raw: str,
        msgs: List[Dict[str, Any]],
        chat: Optional[Dict[str, Any]],
    ) -> ChatAnalysis:
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

    async def analyze(
        self,
        *,
        text: str,
        messages: Optional[List[Dict[str, Any]]] = None,
        chat: Optional[Dict[str, Any]] = None,
        use_llm: Optional[bool] = None,
    ) -> ChatAnalysis:
        raw = str(text or "").strip()
        msgs = list(messages or [])
        base = self._rule_analyze(raw, msgs, chat)
        analyzer = "rule"

        want_llm = self._use_llm if use_llm is None else bool(use_llm)
        if want_llm and self.ai_client is not None and raw:
            from src.ai.intent_llm import llm_score

            llm = await llm_score(self.ai_client, raw, msgs, chat, timeout=self._timeout)
            if llm:
                base = _merge_analysis(base, llm)
                analyzer = "llm"

        self._save_analysis(base, chat, analyzer)
        return base

    def _save_analysis(
        self, analysis: ChatAnalysis, chat: Optional[Dict[str, Any]], analyzer: str
    ) -> None:
        store = self._analysis_store
        if store is None or not hasattr(store, "save_analysis"):
            return
        conv_id = str((chat or {}).get("conversation_id") or "")
        if not conv_id:
            return
        try:
            from src.inbox.models import MessageAnalysis

            store.save_analysis(MessageAnalysis(
                message_id="",
                conversation_id=conv_id,
                intent=analysis.intent,
                emotion=analysis.emotion,
                risk_level=analysis.risk_level,
                risk_reasons=list(analysis.risk_reasons),
                relationship_stage=analysis.relationship_stage,
                summary=getattr(analysis, "summary", "") or "",
                order_no=getattr(analysis, "order_no", "") or "",
                confidence=getattr(analysis, "confidence", 0.0) or 0.0,
                analyzer=analyzer,
            ))
        except Exception:
            pass


def _merge_analysis(base: ChatAnalysis, llm: Dict[str, Any]) -> ChatAnalysis:
    """合并规则 baseline 与 LLM 结果。风险只升不降，其余 LLM 非空优先。"""
    rule_risk = base.risk_level
    llm_risk = llm.get("risk_level") or rule_risk
    final_risk = rule_risk if _RISK_ORDER.get(rule_risk, 0) >= _RISK_ORDER.get(llm_risk, 0) else llm_risk
    reasons = list(base.risk_reasons)
    for r in llm.get("risk_reasons", []) or []:
        if r not in reasons:
            reasons.append(r)
    base.intent = llm.get("intent") or base.intent
    base.emotion = llm.get("emotion") or base.emotion
    base.risk_level = final_risk
    base.risk_reasons = reasons
    if llm.get("relationship_stage"):
        base.relationship_stage = llm["relationship_stage"]
    # ChatAnalysis 没有 summary/order_no/confidence 字段，附加为动态属性供落库读取
    if llm.get("summary"):
        setattr(base, "summary", llm["summary"])
    if llm.get("order_no"):
        setattr(base, "order_no", llm["order_no"])
    if llm.get("confidence") is not None:
        setattr(base, "confidence", llm["confidence"])
    return base


def quick_risk(text: str) -> "tuple[str, list]":
    """同步零成本规则风险评估（不调 LLM）。返回 (risk_level, risk_reasons)。

    供 DraftService 在列草稿时实时给风险徽章上色（high/medium/low），
    走与 ChatAssistantService 规则版同一管线（emotion→intent→risk），
    硬底线（money/privacy/self_harm/adult/stop_contact）命中即 high。
    """
    t = str(text or "")
    emotion = _detect_emotion(t)
    intent = _detect_intent(t, emotion=emotion)
    return _detect_risk(t, emotion=emotion, intent=intent)


def _detect_emotion(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ("sad", "tired", "lonely", "难过", "累", "孤独", "失落", "想哭",
                            "低落", "失眠", "疲惫", "提不起", "没精神")):
        return "低落"
    if any(k in t for k in ("angry", "mad", "生气", "烦", "滚", "讨厌",
                            "气人", "气死", "太气", "气炸", "破服务", "什么破")):
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
    # 时段问候 / 久别问候（评测难例：非白名单短语，如「早上好」「晚上好呀」「好久不见」）。
    # 放在「短句接话」短路之前；这些表达虽短，语义上是问候而非泛接话。
    if any(k in t for k in ("早上好", "早安", "上午好", "中午好", "午安", "下午好",
                            "晚上好", "晚安", "好久不见", "好久没见")):
        return "打招呼"
    # 停止联系：固定短语 + 「别/不要/勿/停止 …(≤4字)… 联系/打扰/骚扰/发消息」正则，
    # 兼容「别再联系」「不要打扰我」「不要再发消息了」等非连续表达（评测发现的漏判）。
    if any(k in t for k in ("stop", "don't contact", "unsubscribe",
                            "别联系", "别发", "不要发", "勿扰")) \
            or re.search(r"(别|不要|不想|勿|停止)\S{0,4}(联系|打扰|骚扰|发消息|发信息)", t):
        return "停止联系"
    if emotion in {"低落", "焦虑"}:
        return "需要安抚"
    if emotion == "生气":
        return "不满/投诉"
    # 投诉关键词（无显性愤怒情绪也成立，评测难例：「我要投诉」「非常不满意」「态度差」）。
    if any(k in t for k in ("投诉", "不满意", "不满", "差评", "退一赔", "维权")) \
            or re.search(r"态度.{0,4}差", t):
        return "不满/投诉"
    # 提问判定须在「短句」短路之前：否则「能便宜点吗？」等短问句会被误判为短句接话。
    if "?" in t or "？" in t:
        return "提问"
    if len(t) <= 8:
        return "短句接话"
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

