"""语音情感层 — 把「会话上下文」翻成「TTS 引擎可消费的情绪控制」。

为什么需要：edge_tts / qwen / fish_speech 都是**平读**，听起来像机器人。
真正决定「像不像真人」的是**情绪表达**——同一句话，问候要温暖、投诉安抚要共情、
报喜要雀跃。本模块把 intent / 关系阶段 / CSAT / 文本线索 派生成一个统一的
``EmotionSpec``，再按各家引擎的能力翻成对应控制信号：

  - OpenAI gpt-4o-mini-tts → ``instructions`` 自然语言指令（"用温暖、略带笑意的语气说"）
  - ElevenLabs v3          → 内联音频标签（``[warmly]`` / ``[laughs]`` / ``[sighs]``）
  - edge_tts               → SSML 风格的 rate/pitch 调节（近似情绪）
  - 其余引擎               → 暂无情绪通道，返回原文不破坏

设计原则：
  - **纯函数、无 IO/网络**，可单测（与 voice_clone_client 的 build_* 同风格）。
  - **防御式**：任何脏输入都退化成 ``neutral``，绝不抛异常给 TTS 主流程。
  - **向后兼容**：``neutral`` 的映射 == 不加任何控制（行为与升级前一致）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

# 受支持的情绪词表（保持精简——只收各引擎都能稳定表达的）。
# 每个情绪带一组「画像」：自然语言语气词、ElevenLabs 标签、edge rate/pitch 偏移。
EMOTIONS = (
    "neutral", "warm", "happy", "excited", "playful",
    "empathetic", "apologetic", "calm", "sad", "serious",
)

# 情绪 → 各引擎画像。rate/pitch 为 edge_tts 的相对偏移（百分比/Hz 风格字符串生成用）。
_EMOTION_PROFILE: Dict[str, Dict[str, Any]] = {
    "neutral":    {"tone": "",                          "el_tag": "",            "rate": 0,   "pitch": 0},
    "warm":       {"tone": "温暖、亲切、略带笑意",        "el_tag": "warmly",      "rate": -4,  "pitch": 2},
    "happy":      {"tone": "愉快、轻松、带着微笑",        "el_tag": "happily",     "rate": 4,   "pitch": 4},
    "excited":    {"tone": "兴奋、雀跃、充满活力",        "el_tag": "excited",     "rate": 8,   "pitch": 6},
    "playful":    {"tone": "俏皮、活泼、带点调侃",        "el_tag": "playfully",   "rate": 5,   "pitch": 5},
    "empathetic": {"tone": "共情、柔和、关切",            "el_tag": "sympathetic", "rate": -8,  "pitch": -2},
    "apologetic": {"tone": "诚恳、歉意、放低姿态",        "el_tag": "apologetic",  "rate": -6,  "pitch": -2},
    "calm":       {"tone": "平静、沉稳、舒缓",            "el_tag": "calmly",      "rate": -6,  "pitch": -1},
    "sad":        {"tone": "低落、轻声、略带叹息",        "el_tag": "sadly",       "rate": -10, "pitch": -4},
    "serious":    {"tone": "认真、郑重、清晰",            "el_tag": "seriously",   "rate": -2,  "pitch": -1},
}


@dataclass(frozen=True)
class EmotionSpec:
    """一次合成的情绪规格。``intensity`` ∈ [0,1] 缩放强度。"""
    emotion: str = "neutral"
    intensity: float = 0.6
    pace: str = "normal"  # slow | normal | fast

    def __post_init__(self) -> None:
        # frozen dataclass：用 object.__setattr__ 做规整
        emo = str(self.emotion or "neutral").strip().lower()
        if emo not in EMOTIONS:
            emo = "neutral"
        object.__setattr__(self, "emotion", emo)
        try:
            inten = float(self.intensity)
        except (TypeError, ValueError):
            inten = 0.6
        object.__setattr__(self, "intensity", max(0.0, min(1.0, inten)))
        pace = str(self.pace or "normal").strip().lower()
        if pace not in ("slow", "normal", "fast"):
            pace = "normal"
        object.__setattr__(self, "pace", pace)

    def is_neutral(self) -> bool:
        return self.emotion == "neutral"

    def cache_key(self) -> str:
        """用于 TTS 缓存键的紧凑串；neutral 返回空串（== 无情绪）。"""
        if self.is_neutral():
            return ""
        return f"{self.emotion}:{self.intensity:.1f}:{self.pace}"


NEUTRAL = EmotionSpec()


# ── 情绪派生（纯函数）────────────────────────────────────────────────────────
# intent / 关系阶段 / CSAT 优先级最高；都没有时退化到文本线索；再没有 → neutral。
_INTENT_EMOTION = {
    # 关键词子串匹配（intent tag 词表各平台不同，用子串更鲁棒）
    "complaint": "empathetic", "投诉": "empathetic", "angry": "empathetic",
    "refund": "apologetic", "退款": "apologetic", "退货": "apologetic",
    "apolog": "apologetic", "道歉": "apologetic", "sorry": "apologetic",
    "greet": "warm", "问候": "warm", "hello": "warm", "打招呼": "warm",
    "thank": "warm", "感谢": "warm", "谢谢": "warm",
    "praise": "happy", "好评": "happy", "夸": "happy",
    "order": "happy", "下单": "happy", "购买": "happy", "成交": "excited",
    "farewell": "warm", "告别": "warm", "再见": "warm",
}

_TEXT_CUES = (
    # (子串元组, 情绪)；命中即取，顺序=优先级
    (("对不起", "不好意思", "抱歉", "sorry", "apolog"), "apologetic"),
    (("哈哈", "嘻嘻", "lol", "😄", "😂", "🤣", "笑死"), "playful"),
    (("谢谢", "感谢", "thank", "❤", "🥰", "么么"), "warm"),
    (("太好了", "棒", "恭喜", "🎉", "！！", "!!"), "excited"),
    (("难过", "伤心", "唉", "😢", "😭", "可惜"), "sad"),
)


def derive_emotion(
    *,
    intent: Optional[str] = None,
    rel_stage: Optional[str] = None,
    csat: Optional[float] = None,
    text: Optional[str] = None,
    default: str = "warm",
) -> EmotionSpec:
    """从会话上下文派生情绪。任何脏输入都安全退化。

    优先级：CSAT 极差 → 共情；intent 命中；文本线索；关系阶段微调；最后 ``default``。
    """
    # 1) CSAT 极差（强信号）→ 共情安抚，盖过其他
    try:
        if csat is not None and float(csat) <= 2.0:
            return EmotionSpec("empathetic", intensity=0.8, pace="slow")
    except (TypeError, ValueError):
        pass

    emo: Optional[str] = None

    # 2) intent 子串匹配
    it = str(intent or "").strip().lower()
    if it:
        for key, val in _INTENT_EMOTION.items():
            if key in it:
                emo = val
                break

    # 3) 文本线索
    if emo is None and text:
        t = str(text)
        tl = t.lower()
        for cues, val in _TEXT_CUES:
            if any(c in t or c in tl for c in cues):
                emo = val
                break

    # 4) 关系阶段微调（亲密阶段更暖更俏皮）
    rs = str(rel_stage or "").strip().lower()
    if emo is None:
        if rs in ("intimate", "close", "亲密", "lover", "好友", "friend"):
            emo = "playful"
        elif rs in ("new", "stranger", "陌生", "lead"):
            emo = "warm"

    if emo is None:
        emo = default if default in EMOTIONS else "warm"

    # 强度：亲密关系 / 强信号略增
    intensity = 0.6
    if rs in ("intimate", "close", "亲密", "lover"):
        intensity = 0.75
    return EmotionSpec(emo, intensity=intensity)


# ── 情绪 → 引擎控制映射 ───────────────────────────────────────────────────────
def coerce_emotion(value: Union[None, str, Dict[str, Any], EmotionSpec]) -> EmotionSpec:
    """把灵活输入（None/字符串/dict/EmotionSpec）规整成 EmotionSpec。"""
    if isinstance(value, EmotionSpec):
        return value
    if value is None:
        return NEUTRAL
    if isinstance(value, str):
        return EmotionSpec(value)
    if isinstance(value, dict):
        return EmotionSpec(
            emotion=str(value.get("emotion") or "neutral"),
            intensity=value.get("intensity", 0.6),
            pace=str(value.get("pace") or "normal"),
        )
    return NEUTRAL


def to_openai_instructions(spec: EmotionSpec, *, base: str = "") -> str:
    """OpenAI gpt-4o-mini-tts 的 ``instructions`` 自然语言指令。

    base 为人设/全局已配置的指令；情绪在其后追加（不覆盖运营显式设置）。
    neutral → 原样返回 base（无新增）。
    """
    base = str(base or "").strip()
    if spec.is_neutral():
        return base
    tone = _EMOTION_PROFILE[spec.emotion]["tone"]
    if not tone:
        return base
    degree = "强烈地" if spec.intensity >= 0.75 else ("略微" if spec.intensity <= 0.4 else "")
    pace_cn = {"slow": "语速放慢", "fast": "语速加快", "normal": ""}.get(spec.pace, "")
    parts = [f"用{degree}{tone}的语气说话"]
    if pace_cn:
        parts.append(pace_cn)
    instr = "；".join(parts)
    return f"{base}。{instr}" if base else instr


def to_elevenlabs_text(text: str, spec: EmotionSpec) -> str:
    """ElevenLabs v3 内联音频标签：在文本前注入情绪标签（如 ``[warmly] 你好``）。

    neutral → 原文不变。标签用 v3 的小写方括号约定。
    """
    t = str(text or "")
    if spec.is_neutral() or not t.strip():
        return t
    tag = _EMOTION_PROFILE[spec.emotion]["el_tag"]
    if not tag:
        return t
    return f"[{tag}] {t}"


# ElevenLabs v3 voice_settings：(stability, style)。
# stability 调低 → 更听情绪标签 + 更大情感起伏；style 放大音色个性。
# 这是比内联标签更**可靠**的情感杠杆（标签依赖音色/上下文，settings 始终生效）。
_EL_SETTINGS: Dict[str, tuple] = {
    "neutral":    (0.50, 0.00),
    "warm":       (0.45, 0.25),
    "happy":      (0.35, 0.40),
    "excited":    (0.30, 0.55),
    "playful":    (0.35, 0.50),
    "empathetic": (0.40, 0.30),
    "apologetic": (0.45, 0.20),
    "calm":       (0.60, 0.10),
    "sad":        (0.40, 0.35),
    "serious":    (0.60, 0.10),
}


def elevenlabs_voice_settings(
    spec: EmotionSpec, *, similarity_boost: float = 0.75,
) -> Dict[str, Any]:
    """情绪 → ElevenLabs v3 ``voice_settings``（始终返回完整 dict，含 neutral 默认）。

    ``style`` 随 intensity 放大；``speed`` 由 pace 映射。``similarity_boost`` 控制
    与克隆音源的相似度（越高越像，但也放大原录音底噪），可由调用方覆盖。
    """
    base_stab, base_style = _EL_SETTINGS.get(spec.emotion, (0.50, 0.0))
    style = round(min(1.0, base_style * (0.5 + spec.intensity)), 2)
    stability = round(max(0.0, min(1.0, base_stab)), 2)
    sim = round(max(0.0, min(1.0, float(similarity_boost))), 2)
    out: Dict[str, Any] = {
        "stability": stability,
        "similarity_boost": sim,
        "style": style,
        "use_speaker_boost": True,
    }
    speed = {"slow": 0.92, "fast": 1.08, "normal": 1.0}.get(spec.pace, 1.0)
    if speed != 1.0:
        out["speed"] = speed
    return out


def edge_prosody(spec: EmotionSpec) -> Dict[str, str]:
    """edge_tts 的 rate/pitch 字符串（按 intensity 缩放）。

    返回 ``{"rate": "+8%", "pitch": "+4Hz"}`` 形式；neutral → 空 dict（不调）。
    """
    if spec.is_neutral():
        return {}
    prof = _EMOTION_PROFILE[spec.emotion]
    scale = 0.5 + spec.intensity  # 0.5..1.5
    rate = int(round(prof["rate"] * scale))
    pitch = int(round(prof["pitch"] * scale))
    out: Dict[str, str] = {}
    if rate:
        out["rate"] = f"{rate:+d}%"
    if pitch:
        out["pitch"] = f"{pitch:+d}Hz"
    return out


__all__ = [
    "EmotionSpec", "NEUTRAL", "EMOTIONS",
    "derive_emotion", "coerce_emotion",
    "to_openai_instructions", "to_elevenlabs_text", "elevenlabs_voice_settings",
    "edge_prosody",
]
