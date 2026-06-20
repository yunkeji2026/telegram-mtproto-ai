"""共情策略选择器（蒸馏版 STRIDE-ED / ProESC / 主动倾听）。

研究共识（2026）：在「情绪理解 → 回复生成」之间插入一层**显式策略选择**，比让
LLM 直接出稿更稳、更贴人。本模块把该思想蒸馏为**确定性纯函数**：按当前情绪维度
（emotional_context.analyze_emotion 的 dimension/intensity/arousal）+ 情绪弧线
（arc：worsening/improving）+ 关系阶段（companion stage），选出一个策略标签，并给出
一行可注入 prompt 的策略指令。

设计取舍：
- **纯函数、零依赖、可单测**：不读 config、不调 LLM、不触网；失败不可能（无 IO）。
- **与既有块互补不打架**：emotional_context 已给「情绪弧线提示」与「关系温度」，本块只
  补「这一轮该怎么接」的**行动策略**（validate/explore/accompany/savor/...）。
- **关系阶段做克制修饰**：关系尚新时，对深挖型策略追加"别过度深挖/过度亲密"的约束，
  避免新用户被"假亲密"劝退（与 companion_relationship.natural_dialogue 同源价值观）。
"""

from __future__ import annotations

from typing import Any, Dict

# 策略标签 → 中文名（用于 prompt 块标题与日志）
STRATEGY_LABELS_ZH: Dict[str, str] = {
    "validate": "确认安抚",
    "explore_needs": "主动倾听·探询",
    "accompany": "低能量陪伴",
    "savor": "共享放大",
    "curiosity": "顺势满足好奇",
    "active_listen": "承接式倾听",
}

# 策略标签 → 一行行动指令（注入 prompt）
_DIRECTIVES: Dict[str, str] = {
    "validate": (
        "先接住并确认对方的情绪（如「听起来真的挺难受/挺委屈的」），"
        "不要急着讲道理、给建议或转移话题；让对方先感到被理解，再视反应决定要不要往下。"
    ),
    "explore_needs": (
        "用主动倾听温和探询背后的需求：多用反映式回应（复述你听到的重点与感受）"
        "而非连环追问；一次最多一个开放问题，把节奏交给对方，慢慢让 TA 说出真正在意的事。"
    ),
    "accompany": (
        "对方能量偏低（累/无聊/孤独）：陪着就好，语气放轻放缓，不要打鸡血或急着"
        "「帮忙解决」；一句在场感（如「我在呢」）往往比建议更有用。"
    ),
    "savor": (
        "顺着对方的好心情，具体回应 TA 分享的细节、和 TA 一起高兴；可以轻轻追问一句"
        "让 TA 多讲讲，别泼冷水也别急着把话题转回自己。"
    ),
    "curiosity": (
        "对方在好奇/想了解：给一点实质内容满足好奇，并留个自然的钩子鼓励 TA 接着聊，"
        "不要一次性把话说完。"
    ),
    "active_listen": (
        "先接住对方这条消息里的具体词、问题或事，再展开；以陈述与承接为主，"
        "偶尔一个问题即可，避免连环反问或空泛寒暄。"
    ),
}

# 关系尚新（initial/warming）时，对"深挖/高亲密"型策略追加的克制修饰
_EARLY_STAGE_RESTRAINT = (
    "（关系还偏新：保持适度距离，别过度深挖隐私，也别表现得过分亲密。）"
)
_DEEP_STRATEGIES = frozenset({"validate", "explore_needs", "savor"})
_EARLY_STAGES = frozenset({"initial", "warming"})


def select_strategy(
    *,
    dimension: str = "neutral",
    intensity: float = 0.0,
    arousal: float = 0.0,
    arc: str = "",
) -> str:
    """根据情绪维度 + 强度/激活 + 弧线选策略标签（确定性）。

    Args:
        dimension: positive / negative / low_energy / curious / neutral。
        intensity: 主情绪强度 0-1。
        arousal: 激活度 0-1。
        arc: ``worsening`` / ``improving`` / ``""``（无历史）。
    """
    dim = str(dimension or "neutral").strip().lower()
    try:
        inten = float(intensity)
    except (TypeError, ValueError):
        inten = 0.0
    try:
        arou = float(arousal)
    except (TypeError, ValueError):
        arou = 0.0

    if dim == "negative":
        # 高强度/高激活/在恶化 → 先稳情绪（validate）；否则温和探询需求
        if arc == "worsening" or inten >= 0.7 or arou >= 0.7:
            return "validate"
        return "explore_needs"
    if dim == "low_energy":
        return "accompany"
    if dim == "positive":
        return "savor"
    if dim == "curious":
        return "curiosity"
    return "active_listen"


def strategy_directive(strategy: str, *, stage: str = "") -> str:
    """策略标签 → 注入指令（含关系阶段克制修饰）。未知策略回退承接式倾听。"""
    s = str(strategy or "").strip()
    base = _DIRECTIVES.get(s) or _DIRECTIVES["active_listen"]
    st = str(stage or "").strip().lower()
    if s in _DEEP_STRATEGIES and st in _EARLY_STAGES:
        return base + _EARLY_STAGE_RESTRAINT
    return base


def build_strategy_block(
    emotion: Dict[str, Any],
    *,
    stage: str = "",
    arc: str = "",
) -> str:
    """组装一行「应对策略」prompt 块；输入异常时返回 ""（绝不抛）。"""
    if not isinstance(emotion, dict):
        return ""
    strategy = select_strategy(
        dimension=emotion.get("dimension", "neutral"),
        intensity=emotion.get("primary_intensity", 0.0) or 0.0,
        arousal=emotion.get("arousal", 0.0) or 0.0,
        arc=arc,
    )
    label = STRATEGY_LABELS_ZH.get(strategy, strategy)
    directive = strategy_directive(strategy, stage=stage)
    return f"【应对策略 · {label}】{directive}"


__all__ = [
    "STRATEGY_LABELS_ZH",
    "select_strategy",
    "strategy_directive",
    "build_strategy_block",
]
