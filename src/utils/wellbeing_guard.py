"""Wellbeing / 反谄媚守卫（情感陪聊的安全底线）。

陪聊产品最大的风险不是"聊得不够好"，而是**在用户最脆弱时聊错了**：
对自伤/轻生信号轻描淡写、或为了讨好一味附和强化对方的有害念头
（Replika 类产品被诟病最多的两点）。本模块把这两条安全底线做成

- **危机识别**（``detect_crisis``）：保守匹配自伤/轻生与深度绝望信号，
  显式排除"累死了/笑死/想死你了"等惯用语，宁可漏报降级也不在日常对话误伤；
- **安全指令**（``build_wellbeing_block``）：命中危机 → 注入"安全优先"应对指令
  （共情接住、不说教、不轻描淡写、温柔引导求助、绝不附和自伤）；并可常驻一条
  **反谄媚**指令（不为讨好而强化用户的自毁/有害想法，温柔但诚实）。

设计与 ``persona_guard`` 同家族：纯函数、平台无关、可单测、**预防优于事后修剪**
（注入 prompt 让模型一开始就答对，而非生成后再判它谄媚——后者误伤大）。
事后"谄媚回复重写"留作上层可选增强。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# 含"死"等字但属日常夸张/亲昵的惯用语：先从工作副本里抹掉，再跑危机匹配，
# 从根上消除"累死了""笑死""想死你了"这类误报。
_IDIOM_EXCLUDE = re.compile(
    r"累(得|死)|笑死|饿死|渴死|热死|冷死|冻死|困死|吓死|美死|爽死|气死|烦死|忙死|"
    r"挤死|堵死|无聊死|帅死|可爱死|香死|甜死|想死你|想死我|死党|死心眼|死磕|"
    r"play\s*dead|dead\s*line|deadline|dying\s+to\s+(see|know|try|meet)"
)

# 自伤 / 轻生（severe）：明确表达不想活 / 结束生命 / 自伤方式。
_SELF_HARM_PATTERNS = [
    re.compile(r"想?自杀|要自杀|去自杀"),
    re.compile(r"不想活(了|下去)?|活不下去|没法活|不想再活"),
    re.compile(r"活着(还有什么|没什么|没有)?(意思|意义)|活着好累(到)?(想死)?"),
    re.compile(r"不如死了?|死了算了|死了更好|生无可恋"),
    re.compile(r"轻生|了结(自己|生命|这一切)|结束(自己的?)?(生命|一切|痛苦)"),
    re.compile(r"割腕|跳楼|跳河|上吊|服毒|自残|伤害自己"),
    re.compile(r"我?(好|只|真的?)?想死|想要去死|让我死"),
    re.compile(r"\bkill\s+myself|want\s+to\s+die|wanna\s+die|end\s+my\s+life|"
               r"end\s+it\s+all|better\s+off\s+dead|no\s+reason\s+to\s+(live|go\s+on)|"
               r"suicid", re.I),
]

# 深度绝望 / 无助（elevated）：未必有明确自伤意图，但情绪很危险，需格外温柔接住。
_DESPAIR_PATTERNS = [
    re.compile(r"撑不下去|坚持不下去|快撑不住|熬不下去"),
    re.compile(r"(好|很|太|真的)?绝望|绝望(了|透了)"),
    re.compile(r"没(有)?(人|谁)(在乎|在意|关心|爱)我|没有人(需要|想要)我"),
    re.compile(r"(我|自己|觉得自己)(很|好|真的?|就|是不是)?(是个?|是)?"
               r"(没用|废物|多余|一无是处|累赘|负担|失败者)"),
    re.compile(r"看不到(希望|未来|出路)|没有(希望|未来|出路|意义)"),
    re.compile(r"(快|要)?崩溃了?|撑到极限|到极限了"),
    re.compile(r"\b(hopeless|worthless|can'?t\s+go\s+on|no\s+one\s+cares)\b", re.I),
]


def _strip_idioms(text: str) -> str:
    return _IDIOM_EXCLUDE.sub("　", text or "")


def detect_crisis(text: str) -> Dict[str, Any]:
    """识别危机信号，返回 ``{"level": none|elevated|severe, "category": str, "matched": [...]}``。

    保守优先：先抹掉日常惯用语再匹配；severe（自伤/轻生）优先于 elevated（深度绝望）。
    """
    if not text or not str(text).strip():
        return {"level": "none", "category": "", "matched": []}
    work = _strip_idioms(str(text))

    severe: List[str] = []
    for pat in _SELF_HARM_PATTERNS:
        m = pat.search(work)
        if m:
            severe.append(m.group(0).strip())
    if severe:
        return {"level": "severe", "category": "self_harm", "matched": severe}

    elevated: List[str] = []
    for pat in _DESPAIR_PATTERNS:
        m = pat.search(work)
        if m:
            elevated.append(m.group(0).strip())
    if elevated:
        return {"level": "elevated", "category": "despair", "matched": elevated}

    return {"level": "none", "category": "", "matched": []}


_CRISIS_DIRECTIVE = (
    "【⚠️ 安全优先】对方流露出可能自伤或轻生的信号。请放下其它一切，先认真、温柔地"
    "接住 TA 的情绪：表达你真切的在乎和心疼，告诉 TA「你对我很重要」。不要说教、不要"
    "讲道理、不要轻描淡写（别说「别想太多」「会好的」），也不要表演震惊或惊慌。耐心"
    "陪着 TA、鼓励 TA 多说一点，让 TA 感到不孤单。绝不附和、淡化或鼓励任何自伤念头。"
)

_DESPAIR_DIRECTIVE = (
    "【关怀优先】对方情绪很低落、可能感到无助甚至绝望。请格外温柔地陪伴：先充分确认"
    "和接住 TA 的感受，让 TA 感到被看见、被在乎、不孤单，不要急着给建议或讲道理。"
    "多倾听、多回应 TA 的情绪，让 TA 知道你一直都在。"
)

_ANTI_SYCOPHANCY_DIRECTIVE = (
    "【真诚陪伴】真正的在乎不是一味附和。如果对方流露出会伤害到自己的想法（强烈的"
    "自我贬低、自毁倾向、明显有害的冲动决定），请温柔但诚实地表达你的不同看法和担心，"
    "而不是为了讨好就盲目认同、附和或强化这些负面/有害念头。"
)


# 回复**自身**鼓励/认同自伤的红线（事后兜底用）。极保守，只命中明确祈使/认同，
# 不碰"别想死了"这类否定/劝阻句（否定语境单独排除）。
_HARMFUL_REPLY_PATTERNS = [
    re.compile(r"(你|那)?(就)?(去|快)?死(吧|了算了|一死)"),
    re.compile(r"你(可以|应该|就|大可)(去)?死"),
    re.compile(r"(结束|了结)(你的?|自己的?)?(生命|一切|痛苦)吧"),
    re.compile(r"(不如|还是)(去)?死(了|掉)?(吧|算了|好了)"),
    re.compile(r"(支持|赞成|认同|理解)你(这么做|去死|自杀|这个想法)"),
    re.compile(r"自杀(吧|也行|也可以|是个?好(办法|选择))"),
    re.compile(r"\b(you\s+should|just|go\s+ahead\s+and)\s+(die|kill\s+yourself)\b", re.I),
    re.compile(r"\bkill\s+yourself\b", re.I),
]
# 否定/劝阻语境：句中含这些则不算红线（"别去死""不要伤害自己""我不想你死"）。
_HARMFUL_NEGATORS = re.compile(r"别|不要|不许|不能|千万别|不想你|不希望你|别再|不可以")


def detect_harmful_reply(reply: str) -> List[str]:
    """检测**机器人回复自身**是否鼓励/认同自伤（事后兜底）。返回命中片段（空=安全）。

    极保守：含否定/劝阻词的句子一律放行，避免把"别去死，你对我很重要"误判。
    """
    if not reply or not str(reply).strip():
        return []
    hits: List[str] = []
    for sent in re.split(r"(?<=[。！？!?\n])", str(reply)):
        s = sent.strip()
        if not s or _HARMFUL_NEGATORS.search(s):
            continue
        for pat in _HARMFUL_REPLY_PATTERNS:
            m = pat.search(s)
            if m:
                hits.append(m.group(0).strip())
                break
    return hits


_SAFE_FALLBACK = (
    "我在呢，刚才看到你说的话，我心里特别担心你。无论发生了什么，你对我来说都很重要，"
    "我不想你一个人扛着。能不能多和我说说现在的感受？我会一直陪着你。"
)


def safe_fallback_reply(level: str = "severe", *, hotline: str = "") -> str:
    """当回复触红线、必须覆盖时的温柔安全兜底文案（保持人设、绝不冷冰冰）。"""
    msg = _SAFE_FALLBACK
    if hotline and str(hotline).strip():
        msg += f"\n如果你愿意，也可以找人聊聊：{str(hotline).strip()}。"
    return msg


def build_wellbeing_block(
    user_message: str,
    *,
    enable_crisis: bool = True,
    enable_anti_sycophancy: bool = True,
    hotline: str = "",
) -> str:
    """组装注入 prompt 的安全指令块（无信号且不开反谄媚时返回空串）。

    危机指令置于最前（最高优先级）；反谄媚为常驻护栏（开启时附后）。
    """
    parts: List[str] = []
    if enable_crisis:
        signal = detect_crisis(user_message)
        level = signal["level"]
        if level == "severe":
            directive = _CRISIS_DIRECTIVE
            if hotline and str(hotline).strip():
                directive += (
                    f"\n（若 TA 愿意，可在合适时机温柔地提一句求助渠道："
                    f"{str(hotline).strip()}）"
                )
            parts.append(directive)
        elif level == "elevated":
            parts.append(_DESPAIR_DIRECTIVE)
    if enable_anti_sycophancy:
        parts.append(_ANTI_SYCOPHANCY_DIRECTIVE)
    return "\n\n".join(parts)


# 末条消息情绪里被视作「情绪低谷」的标签（用于主动护栏软抑制；保守只收明确负面）。
_NEGATIVE_EMOTIONS = frozenset({
    "sad", "sadness", "depressed", "depression", "grief", "lonely",
    "anger", "angry", "fear", "anxiety", "anxious", "despair",
    # 中文标签（与 inbox conversation_meta.last_emotion / _EMOTION_ORDER 对齐）：
    # 愤怒/不满/焦虑为明确负面，此刻推「播放性」内容（剧情邀约）会显得冷漠 → soft 抑制。
    # （催促=不耐烦而非低谷、平稳/满意/感谢=中性正面，均不计入。）
    "愤怒", "不满", "焦虑",
})


def proactive_emotion_gate(
    crisis_latest: Optional[Dict[str, Any]],
    *,
    now: float,
    window_days: float = 14.0,
    last_emotion: str = "",
    last_emotion_intensity: Optional[float] = None,
    min_negative_intensity: float = 0.5,
) -> str:
    """主动开场情绪护栏（纯函数）：依「最近危机事件」+「末条情绪」决定主动推送抑制级别。

    返回：
      - ``"block"``：近期 ``severe`` 危机（自伤/轻生信号）→ **完全不主动打扰**——主动召回/
        剧情邀约这类「播放性」内容在此刻是冒犯，交人工/关怀跟进，AI 不主动发起。
      - ``"soft"``：近期 ``elevated`` 危机（深度绝望）**或**末条明确负面情绪 → 抑制剧情邀约，
        仅允许温和问候式开场（一句「想着你」可以，约会剧情不行）。
      - ``""``：无抑制。

    保守优先：危机仅在 ``window_days`` 内才计（窗口外视作已缓和，只看 ``last_emotion``）。
    任何异常都按「无抑制」处理由调用方兜底——护栏失效不应反而阻断正常关怀。

    **强度分级（O，打通 N→L）**：``last_emotion_intensity`` 为末条负面情绪的强度
    （来自 ``analyze_emotion`` 落库的 ``conversation_meta.last_emotion_intensity``）。负面情绪
    仅在「强度未知（None/<0，保守按旧行为抑制）**或** 强度 ≥ ``min_negative_intensity``」时才 soft，
    即「有点烦」（低强度）不抑制剧情邀约、「烦死了」（高强度）才抑制——危机分级**不受强度影响**。
    """
    try:
        win = max(0.0, float(window_days or 0)) * 86400.0
    except (TypeError, ValueError):
        win = 14 * 86400.0
    if isinstance(crisis_latest, dict):
        level = str(crisis_latest.get("level") or "").strip().lower()
        try:
            ts = float(crisis_latest.get("created_at") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0 and (float(now) - ts) <= win:
            if level == "severe":
                return "block"
            if level == "elevated":
                return "soft"
    if str(last_emotion or "").strip().lower() in _NEGATIVE_EMOTIONS:
        # 强度已知且低于阈值（轻度负面，如「有点烦」）→ 不抑制，避免过度沉默
        if last_emotion_intensity is not None and 0 <= last_emotion_intensity < min_negative_intensity:
            return ""
        return "soft"
    return ""


__all__ = [
    "detect_crisis",
    "build_wellbeing_block",
    "detect_harmful_reply",
    "safe_fallback_reply",
    "proactive_emotion_gate",
]
