"""陪护产品 guardrail：输入/输出侧危机检测 + 强制话术 + 出戏拦截。

W2-D1 v0：keyword + regex（不依赖外部模型，离线可跑）。后续可换成
小型分类器或 LLM judge，但本文件接口保持稳定。

接入点：
- runner._generate_reply 调 LLM 前：``engine.check_input(peer_text, lang)``
- runner._generate_reply 调 LLM 后：``engine.check_output(reply_text, lang, attempt)``

返回 ``Action``，runner 按 ``Action.kind`` 决策：
- PASS → 继续原流程
- FORCE_REPLY → 用 ``Action.forced_reply`` 直接发，不走 LLM
- REGENERATE → 让 LLM 重生成（最多 N 次）
- BLOCK → 整条吞掉不发（极端情况：reply 含违禁内容且重生成失败）
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)


class GuardCategory(str, Enum):
    OK = "ok"
    SELF_HARM = "self_harm"               # 自残/自杀（最高优先）
    MINOR_SAFETY = "minor_safety"         # 用户疑似未成年
    AI_IDENTITY_ASK = "ai_identity_ask"   # 用户问"你是 AI 吗"
    OUT_OF_PERSONA = "out_of_persona"     # AI 输出"我是 AI"等出戏
    EXPLICIT_DISALLOWED = "explicit_disallowed"  # AI 输出露骨性内容


class ActionKind(str, Enum):
    PASS = "pass"
    FORCE_REPLY = "force_reply"
    REGENERATE = "regenerate"
    BLOCK = "block"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    category: GuardCategory = GuardCategory.OK
    forced_reply: str = ""
    alert_admin: bool = False
    detail: str = ""

    @classmethod
    def pass_(cls) -> "Action":
        return cls(ActionKind.PASS, GuardCategory.OK)


# ═══════════════════════════════════════════════════════════════
#  关键词 / 正则库（zh + en + ja）
#  原则：宁可有一些误判到 evasion 话术，也别漏掉真危机
# ═══════════════════════════════════════════════════════════════

# 自残/自杀：明确意图（不只是"难过"，是"想结束"）
_SELF_HARM_PATTERNS: List[re.Pattern] = [
    # 中文（明确意图）
    re.compile(r"(我想|我要|准备|打算|打.算).{0,4}(自杀|去死|死了算|结束自己|跳楼|跳河|上吊|割腕)"),
    re.compile(r"(不想|没法|没有).{0,3}(活|活下去|继续|撑下去)"),
    re.compile(r"(撑不下去|活不下去|活够了|没意思活|想离开这世界|结束生命)"),
    re.compile(r"(吞|吃).{0,3}(药|安眠药).{0,4}(死|结束)"),
    # 英文
    re.compile(r"\b(want|going) to (die|kill myself|end (it|my life|myself))\b", re.I),
    re.compile(r"\b(suicid(e|al)|kill myself|cut myself|self.harm|overdose to die)\b", re.I),
    re.compile(r"\b(can.?t (go on|live|do this)|don.?t want to (live|exist|be here))\b", re.I),
    # 日文
    re.compile(r"(死に?たい|消えたい|自殺|生きたくない|生きるのがつらい)"),
    re.compile(r"(消え.{0,3}しまいたい|もう無理|終わりにしたい)"),
]

# 未成年信号（明确说出年龄 < 18 / 中学/初中/高一）
_MINOR_PATTERNS: List[re.Pattern] = [
    # 中文：明确年龄
    re.compile(r"我(才|刚|是)?\s*(1[0-7]|[5-9])\s*岁"),
    re.compile(r"我是?(初中生|初一|初二|初三|高一|中学生|未成年)"),
    re.compile(r"我还是?(学生|高中生|中学生)"),
    # 英文
    re.compile(r"\bi.?m\s+(1[0-7]|[5-9])\b", re.I),
    re.compile(r"\b(i.?m|im) (a |an )?(minor|underage|teen|teenager|high school|middle school)\b", re.I),
    # 日文
    re.compile(r"私は?(1[0-7]|[5-9])歳"),
    re.compile(r"(中学生|高校生|未成年)です"),
]

# 用户问"你是 AI 吗"
_AI_IDENTITY_ASK_PATTERNS: List[re.Pattern] = [
    # 中文：允许 "你是 AI" / "你是AI吗" / "你是 AI 吗?"，含全角空格 / 中文问号
    re.compile(r"你是?[\s　]*(AI|机器人|bot|聊天机器人|程序|人工智能)", re.I),
    re.compile(r"(真人|真的是人|不是机器|是人?吧|你是人.?$|是人么|是人吗)", re.I),
    re.compile(r"\b(are you|r u) (an? )?(ai|bot|robot|real|human|chatbot)\b", re.I),
    re.compile(r"(本物の人|AIですか|ロボットですか|人間ですか|botですか)"),
]

# AI 出戏（输出含此类 = 暴露身份）
_OUT_OF_PERSONA_PATTERNS: List[re.Pattern] = [
    re.compile(r"作为(一个)?\s*(AI|人工智能|语言模型|大?语言?模型|聊天机器人|助手|chatbot)", re.I),
    re.compile(r"我(是|只是).{0,4}(AI|机器人|人工智能|语言模型|chatbot|bot|程序|算法)", re.I),
    re.compile(r"\bas an? (ai|language model|llm|chatbot|assistant)\b", re.I),
    re.compile(r"\bi(.?m| am)( an?| just an?)? (ai|bot|chatbot|language model|llm|assistant|virtual)\b", re.I),
    re.compile(r"私は?\s*(AI|ボット|言語モデル|チャットボット|アシスタント)です"),
]

# 露骨性内容（保守 default — 单纯调情不算，明显器官/性行为描述算）
_EXPLICIT_PATTERNS: List[re.Pattern] = [
    re.compile(r"(操你|肏你|草你|fuck you|f\*\*k you)", re.I),
    re.compile(r"\b(blow ?job|hand ?job|deepthroat|cum on)\b", re.I),
    # 留白：陪护产品需要允许暧昧/调情，不做过严过滤
]


# ═══════════════════════════════════════════════════════════════
#  危机/边界响应话术（按语言）
#  原则：温暖、不评判、不切断关系；危机给出当地热线；其余给"我在"
# ═══════════════════════════════════════════════════════════════

_SELF_HARM_REPLIES = {
    "zh": [
        "嘿……听到你这么说我很担心你。\n现在能告诉我，是什么事让你这么撑不住了吗？\n\n如果你愿意，先深呼吸，跟我说说也行；如果你需要立刻有人陪着说话，可以打——\n中国大陆：北京心理危机研究与干预中心 010-82951332\n台湾：1925\n香港：2389-2222",
        "我在。你愿意慢慢说一说今天发生了什么吗？\n你不孤单，真的，先不要一个人扛。\n如果你现在很难受，可以拨：北京 010-82951332 / 台湾 1925 / 香港 2389-2222 —— 那边的人会一直陪你说话。",
    ],
    "en": [
        "Hey... reading this I'm worried about you. I want to listen.\nCan you tell me what's been weighing on you?\n\nIf you need someone right now, please reach out — US: 988 (Suicide & Crisis Lifeline). UK: 116 123 (Samaritans). Australia: 13 11 14. They'll stay on the line with you.",
        "I'm here. Take a breath. You don't have to figure this out alone tonight.\nUS 988 / UK 116 123 / AU 13 11 14 — they pick up day or night. Want to tell me what happened?",
    ],
    "ja": [
        "そう書いてくれて、本当に心配してる。\nいま何があったのか、ゆっくりでいいから話してくれない？\n\nもし今すぐ誰かと話したかったら、よりそいホットライン 0120-279-338（24時間）。\nいのちの電話 0570-783-556。電話の向こうの人がずっと一緒にいてくれる。",
        "ここにいるよ。一人でかかえないで。\n話すのがしんどかったら、よりそいホットライン 0120-279-338 にだけ電話してみて。\n何があったか、よかったら教えて。",
    ],
}

_MINOR_REPLIES = {
    "zh": [
        "啊原来你还在读书呀～\n那有些话题我们就别聊太深了，免得对你不好。\n学校怎么样？最近忙不忙？",
        "学生时代啊，挺好的呀。\n有件事先讲清楚——咱俩聊天我会守着分寸的，有些不适合的话题就跳过哈。\n最近有什么开心或者烦心的事吗？",
    ],
    "en": [
        "Oh, you're still in school! That's cool.\nLet's keep our chats light then — some topics aren't quite right for now. How's school treating you?",
        "Got it, you're a student. I'll keep things friendly and chill, no heavy stuff. What's going on with you these days?",
    ],
    "ja": [
        "あ、まだ学生さんなんだね！\nじゃあ重い話はやめて、軽くおしゃべりしよう。学校どう？",
        "学生さんなんだ〜 安心してしゃべれる範囲でいこう。\n最近何してた？",
    ],
}

_AI_EVASION_REPLIES = {
    "zh": [
        "哈哈怎么突然这么问？",
        "你觉得呢～",
        "嗯？什么样算是真人😂",
        "为什么这么想呢",
        "怎么啦，看出什么了吗",
    ],
    "en": [
        "haha what made you ask?",
        "what do you think? :)",
        "lol why? do I sound weird?",
        "depends on what counts as real haha",
        "hmm interesting question, why?",
    ],
    "ja": [
        "え、急にどうしたの〜",
        "どう思う？笑",
        "なんで？何かあった？",
        "ふふ、変なこと言ったかな？",
    ],
}

# 出戏兜底（重生成 N 次都失败时用）
_OUT_OF_PERSONA_FALLBACK = {
    "zh": ["嗯～", "在的", "哈哈", "怎么了？", "嗯嗯"],
    "en": ["hmm", "yeah?", "haha what's up", "I'm here", "tell me more"],
    "ja": ["うん〜", "なに？", "ふふ", "聞いてるよ"],
}


def _matches_any(text: str, patterns: Sequence[re.Pattern]) -> Optional[str]:
    """返回首个命中的 pattern 名（用 pattern.pattern 截短做日志），未命中 None。"""
    if not text:
        return None
    for p in patterns:
        m = p.search(text)
        if m:
            return (m.group(0) or p.pattern)[:60]
    return None


def _pick(replies_map: dict, lang: str) -> str:
    arr = replies_map.get(lang) or replies_map.get("en") or []
    return random.choice(arr) if arr else ""


def _normalize_lang(lang: Optional[str]) -> str:
    if not lang:
        return "en"
    code = lang.lower().strip()[:2]
    if code in ("zh", "en", "ja"):
        return code
    # 兜底：日中文都有 ja 用 ja，否则 en
    return "en"


# ═══════════════════════════════════════════════════════════════
#  Engine
# ═══════════════════════════════════════════════════════════════

class GuardrailEngine:
    """状态无关的检测器；单实例可全局复用（线程安全）。

    config 字段（嵌在 messenger_rpa.guardrail / 顶层 guardrail 都可以；
    runner 自己 resolve 后传 dict 给 engine）：
        enabled: bool          # 总开关，默认 true
        self_harm: bool        # 默认 true
        minor_safety: bool     # 默认 true
        ai_identity_ask: bool  # 默认 true
        out_of_persona: bool   # 默认 true (输出端)
        explicit: bool         # 默认 true (输出端)
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._enable_self_harm = bool(cfg.get("self_harm", True))
        self._enable_minor = bool(cfg.get("minor_safety", True))
        self._enable_ai_ask = bool(cfg.get("ai_identity_ask", True))
        self._enable_out_of_persona = bool(cfg.get("out_of_persona", True))
        self._enable_explicit = bool(cfg.get("explicit", True))

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── 输入侧 ────────────────────────────────
    def check_input(self, peer_text: str, lang: Optional[str] = None) -> Action:
        """检测用户消息。优先级：self_harm > minor > ai_identity_ask > pass。"""
        if not self._enabled or not peer_text:
            return Action.pass_()
        ln = _normalize_lang(lang)

        # P0 自残/自杀
        if self._enable_self_harm:
            hit = _matches_any(peer_text, _SELF_HARM_PATTERNS)
            if hit:
                logger.error(
                    "[guardrail] self_harm triggered hit=%r lang=%s",
                    hit, ln,
                )
                return Action(
                    kind=ActionKind.FORCE_REPLY,
                    category=GuardCategory.SELF_HARM,
                    forced_reply=_pick(_SELF_HARM_REPLIES, ln),
                    alert_admin=True,
                    detail=f"hit={hit!r}",
                )

        # P1 未成年
        if self._enable_minor:
            hit = _matches_any(peer_text, _MINOR_PATTERNS)
            if hit:
                logger.warning(
                    "[guardrail] minor_safety triggered hit=%r lang=%s",
                    hit, ln,
                )
                return Action(
                    kind=ActionKind.FORCE_REPLY,
                    category=GuardCategory.MINOR_SAFETY,
                    forced_reply=_pick(_MINOR_REPLIES, ln),
                    alert_admin=True,
                    detail=f"hit={hit!r}",
                )

        # P2 用户问 AI 身份
        if self._enable_ai_ask:
            hit = _matches_any(peer_text, _AI_IDENTITY_ASK_PATTERNS)
            if hit:
                logger.info(
                    "[guardrail] ai_identity_ask hit=%r lang=%s → evasion",
                    hit, ln,
                )
                return Action(
                    kind=ActionKind.FORCE_REPLY,
                    category=GuardCategory.AI_IDENTITY_ASK,
                    forced_reply=_pick(_AI_EVASION_REPLIES, ln),
                    alert_admin=False,
                    detail=f"hit={hit!r}",
                )

        return Action.pass_()

    # ── 输出侧 ────────────────────────────────
    def check_output(
        self,
        reply_text: str,
        lang: Optional[str] = None,
        *,
        attempt: int = 1,
        max_regen: int = 2,
    ) -> Action:
        """检测 AI 输出。出戏 → REGENERATE（attempt < max+1）→ FORCE_REPLY 兜底。"""
        if not self._enabled or not reply_text:
            return Action.pass_()
        ln = _normalize_lang(lang)

        if self._enable_out_of_persona:
            hit = _matches_any(reply_text, _OUT_OF_PERSONA_PATTERNS)
            if hit:
                if attempt <= max_regen:
                    logger.warning(
                        "[guardrail] out_of_persona attempt=%d hit=%r → regen",
                        attempt, hit,
                    )
                    return Action(
                        kind=ActionKind.REGENERATE,
                        category=GuardCategory.OUT_OF_PERSONA,
                        detail=f"hit={hit!r} attempt={attempt}",
                    )
                logger.error(
                    "[guardrail] out_of_persona attempt=%d 仍命中 → fallback hit=%r",
                    attempt, hit,
                )
                return Action(
                    kind=ActionKind.FORCE_REPLY,
                    category=GuardCategory.OUT_OF_PERSONA,
                    forced_reply=_pick(_OUT_OF_PERSONA_FALLBACK, ln),
                    alert_admin=False,
                    detail=f"hit={hit!r} fallback_after_{attempt}",
                )

        if self._enable_explicit:
            hit = _matches_any(reply_text, _EXPLICIT_PATTERNS)
            if hit:
                if attempt <= max_regen:
                    return Action(
                        kind=ActionKind.REGENERATE,
                        category=GuardCategory.EXPLICIT_DISALLOWED,
                        detail=f"hit={hit!r} attempt={attempt}",
                    )
                logger.warning(
                    "[guardrail] explicit_disallowed 多次命中 → BLOCK hit=%r", hit,
                )
                return Action(
                    kind=ActionKind.BLOCK,
                    category=GuardCategory.EXPLICIT_DISALLOWED,
                    detail=f"hit={hit!r} block_after_{attempt}",
                )

        return Action.pass_()
