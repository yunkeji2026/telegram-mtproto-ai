"""
Emotional Context Engine — 情感智能上下文引擎

基于 2025 最新研究（Stanford Generative Agents, Mem0 三层记忆, Frontiers Emotional AI）：
1. 情感状态追踪  — 分析消息情绪 + 跨会话持久化 + 情感弧线
2. 时间感知      — 距上次对话时间差 → 自然开场指导
3. 记忆反思      — 从原始事实合成高阶洞察（喜好/习惯/关系模式）
4. 关系温度      — 交流深度递进，从陌生到熟悉自然过渡
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# 1. 情感分析 — 多维度情绪检测
# ────────────────────────────────────────────────────────────────────────

# 情绪词典：关键词 → (情绪标签, 强度 0-1)
_EMOTION_LEXICON: Dict[str, Tuple[str, float]] = {
    # 开心/积极
    "哈哈": ("happy", 0.7), "嘻嘻": ("happy", 0.6), "😂": ("happy", 0.7),
    "😄": ("happy", 0.6), "🥰": ("happy", 0.8), "❤️": ("love", 0.7),
    "太好了": ("happy", 0.8), "开心": ("happy", 0.8), "高兴": ("happy", 0.7),
    "nice": ("happy", 0.6), "haha": ("happy", 0.6), "lol": ("happy", 0.5),
    "谢谢": ("grateful", 0.6), "感谢": ("grateful", 0.7),
    "想你": ("longing", 0.8), "好想": ("longing", 0.7), "miss": ("longing", 0.7),
    "喜欢": ("love", 0.6), "爱": ("love", 0.8),
    # 难过/消极
    "难过": ("sad", 0.7), "伤心": ("sad", 0.8), "哭": ("sad", 0.6),
    "😢": ("sad", 0.7), "😭": ("sad", 0.8), "💔": ("sad", 0.8),
    "唉": ("sad", 0.5), "哎": ("sad", 0.4), "算了": ("sad", 0.5),
    "不开心": ("sad", 0.7), "郁闷": ("sad", 0.6), "心烦": ("sad", 0.6),
    # 生气/烦躁
    "生气": ("angry", 0.8), "烦": ("frustrated", 0.6), "烦死了": ("frustrated", 0.9),
    "无语": ("frustrated", 0.7), "服了": ("frustrated", 0.7),
    "坑": ("angry", 0.6), "骗": ("angry", 0.7), "垃圾": ("angry", 0.8),
    "什么鬼": ("frustrated", 0.6), "搞什么": ("frustrated", 0.7),
    # 焦虑/担心
    "担心": ("anxious", 0.6), "焦虑": ("anxious", 0.7), "紧张": ("anxious", 0.6),
    "怕": ("anxious", 0.5), "害怕": ("anxious", 0.7), "不安": ("anxious", 0.6),
    # 累/疲惫
    "累": ("tired", 0.6), "好累": ("tired", 0.8), "困": ("tired", 0.5),
    "加班": ("tired", 0.5), "熬夜": ("tired", 0.6), "忙死了": ("tired", 0.8),
    # 无聊/寂寞
    "无聊": ("bored", 0.6), "好无聊": ("bored", 0.8), "没意思": ("bored", 0.6),
    "寂寞": ("lonely", 0.7), "孤独": ("lonely", 0.8),
    # 好奇/兴趣
    "好奇": ("curious", 0.6), "想知道": ("curious", 0.5),
    "真的吗": ("curious", 0.5), "然后呢": ("curious", 0.5),
    # 撒娇/亲昵
    "嘛": ("playful", 0.4), "呀": ("playful", 0.3), "啦": ("playful", 0.3),
    "嘿嘿": ("playful", 0.5), "～": ("playful", 0.3), "吼吼": ("playful", 0.5),
}

# 情绪分组：细粒度情绪 → 粗粒度维度
_EMOTION_DIMENSIONS = {
    "positive": {"happy", "grateful", "love", "longing", "playful"},
    "negative": {"sad", "angry", "frustrated", "anxious"},
    "low_energy": {"tired", "bored", "lonely"},
    "curious": {"curious"},
}

# 否定词（出现在情绪词紧邻前方 → 该处情绪反转，不应计入）。
# 例：「不难过」「没那么累」「别担心」「不想你」——这些不是负面/低能量信号。
_ZH_NEGATORS = ("不", "没", "别", "莫", "无须", "勿")


def _occurrence_negated(text: str, idx: int) -> bool:
    """情绪词出现在 ``idx`` 处，其紧邻前文是否构成否定（→ 该处情绪应被抑制）。

    保守：只看紧邻前 ~3 字（中文）/ ~6 字（英文 not/no/n't）。覆盖
    「不X」「没那么X」「别X」「太不X」，不误伤「好X」「很X」（无否定词）。
    """
    zh_win = text[max(0, idx - 3):idx]
    if any(n in zh_win for n in _ZH_NEGATORS):
        return True
    en_win = text[max(0, idx - 6):idx]
    return ("not " in en_win) or ("n't " in en_win) or ("no " in en_win)


# 程度副词分级（N）：紧邻情绪词前的程度副词缩放强度（「有点累」<「累」<「非常累」），
# 及情绪词后缀强化（「累死了」「烦透了」）。仅影响 intensity（→arousal/valence/salience），
# 不改情绪标签，故维度/否定判定不受影响。
_DEGREE_MILD = ("有点", "有些", "稍微", "略微", "一点", "些许", "一丝", "稍", "略")
_DEGREE_STRONG = ("非常", "超级", "特别", "好生", "极其", "十分", "灰常", "实在",
                  "超", "巨", "太", "贼", "好", "真")
_DEGREE_SUFFIX_STRONG = ("死了", "极了", "坏了", "惨了", "透了", "爆了", "死我")
_MULT_MILD = 0.65
_MULT_STRONG = 1.3


def _degree_multiplier(text: str, start: int, end: int) -> float:
    """情绪词 [start,end) 的程度系数：弱化 0.65 / 基准 1.0 / 强化 1.3。"""
    pre = text[max(0, start - 3):start]
    mult = 1.0
    if any(pre.endswith(d) for d in _DEGREE_MILD):
        mult = _MULT_MILD
    elif any(pre.endswith(d) for d in _DEGREE_STRONG):
        mult = _MULT_STRONG
    suf = text[end:end + 3]
    if any(suf.startswith(d) for d in _DEGREE_SUFFIX_STRONG):
        mult = max(mult, _MULT_STRONG)
    return mult


def _effective_intensity(text: str, keyword: str, base: float) -> Optional[float]:
    """返回 ``keyword`` 首个**非否定**出现的有效强度（含程度缩放）；无命中→None。

    全部出现都被否定（如「不开心」里的「开心」）→ None，交由更长显式词条/回落中性。
    """
    start = 0
    while True:
        i = text.find(keyword, start)
        if i == -1:
            return None
        if not _occurrence_negated(text, i):
            mult = _degree_multiplier(text, i, i + len(keyword))
            return max(0.0, min(1.0, base * mult))
        start = i + 1


def analyze_emotion(text: str) -> Dict[str, Any]:
    """
    多维度情绪分析。返回:
    {
        "primary_emotion": "happy",       # 最强情绪标签
        "primary_intensity": 0.7,         # 强度 0-1
        "dimension": "positive",          # 粗粒度维度
        "all_emotions": {"happy": 0.7, "playful": 0.4},
        "valence": 0.6,                   # 正负极性 -1 ~ +1
        "arousal": 0.5,                   # 激活度 0 ~ 1
    }
    """
    detected: Dict[str, float] = {}
    text_lower = text.lower()

    for keyword, (emotion, intensity) in _EMOTION_LEXICON.items():
        # 否定硬化 + 程度分级：取首个非否定出现的有效强度（含程度副词缩放）
        eff = _effective_intensity(text_lower, keyword, intensity)
        if eff is not None:
            detected[emotion] = max(detected.get(emotion, 0), eff)

    # 感叹号/问号密度提升 arousal
    excl_count = text.count("!") + text.count("！")
    quest_count = text.count("?") + text.count("？")
    punctuation_boost = min((excl_count + quest_count) * 0.1, 0.3)

    # 文本长度暗示：长消息倾向于倾诉（高 arousal）
    length_signal = min(len(text) / 200, 0.3)

    if not detected:
        # 无明确情绪信号 → 中性
        return {
            "primary_emotion": "neutral",
            "primary_intensity": 0.3,
            "dimension": "neutral",
            "all_emotions": {},
            "valence": 0.0,
            "arousal": min(0.2 + punctuation_boost + length_signal, 1.0),
        }

    primary_emotion = max(detected, key=detected.get)  # type: ignore[arg-type]
    primary_intensity = detected[primary_emotion]

    # 计算维度
    dimension = "neutral"
    for dim, emotions in _EMOTION_DIMENSIONS.items():
        if primary_emotion in emotions:
            dimension = dim
            break

    # 计算 valence（正负极性）
    pos_score = sum(v for k, v in detected.items()
                    if k in _EMOTION_DIMENSIONS.get("positive", set()))
    neg_score = sum(v for k, v in detected.items()
                    if k in _EMOTION_DIMENSIONS.get("negative", set()))
    low_score = sum(v for k, v in detected.items()
                    if k in _EMOTION_DIMENSIONS.get("low_energy", set()))
    total = pos_score + neg_score + low_score + 0.01
    valence = (pos_score - neg_score - low_score * 0.5) / total
    valence = max(-1.0, min(1.0, valence))

    # 计算 arousal（激活度）
    arousal = primary_intensity * 0.6 + punctuation_boost + length_signal
    arousal = min(1.0, arousal)

    return {
        "primary_emotion": primary_emotion,
        "primary_intensity": round(primary_intensity, 2),
        "dimension": dimension,
        "all_emotions": {k: round(v, 2) for k, v in detected.items()},
        "valence": round(valence, 2),
        "arousal": round(arousal, 2),
    }


# ────────────────────────────────────────────────────────────────────────
# 2. 时间感知 — 距上次对话时间差 → 自然语调
# ────────────────────────────────────────────────────────────────────────

def classify_time_gap(last_message_ts: Optional[float]) -> Dict[str, Any]:
    """
    返回:
    {
        "gap_seconds": 3600,
        "gap_label": "hours_ago",    # just_now / minutes_ago / hours_ago / yesterday / days_ago / long_time / first_contact
        "gap_hint": "距上次聊天约1小时前",
        "opening_guidance": "...",   # 给 AI 的开场指导
    }
    """
    if not last_message_ts:
        return {
            "gap_seconds": None,
            "gap_label": "first_contact",
            "gap_hint": "这是第一次对话",
            "opening_guidance": (
                "这是你们第一次聊天。像认识新朋友一样：热情但不过度，"
                "先简单打个招呼，自然地聊起来。不要上来就问太多问题。"
            ),
        }

    gap = time.time() - last_message_ts
    if gap < 120:
        return {
            "gap_seconds": gap,
            "gap_label": "just_now",
            "gap_hint": "刚刚才聊过",
            "opening_guidance": "你们刚才还在聊，直接延续上文的话题和语气，不需要重新打招呼。",
        }
    elif gap < 1800:  # 30 min
        return {
            "gap_seconds": gap,
            "gap_label": "minutes_ago",
            "gap_hint": f"上次聊天约 {int(gap / 60)} 分钟前",
            "opening_guidance": "距离上次聊天不久，可以自然地接上之前的话题，像朋友隔几分钟再回消息。",
        }
    elif gap < 7200:  # 2 hours
        return {
            "gap_seconds": gap,
            "gap_label": "hours_ago",
            "gap_hint": f"上次聊天约 {int(gap / 3600)} 小时前",
            "opening_guidance": (
                "隔了一两个小时，像朋友忙完了又看手机的感觉。"
                "可以自然带一句「刚忙完」之类的，但不是每次都要说。"
            ),
        }
    elif gap < 86400:  # 1 day
        return {
            "gap_seconds": gap,
            "gap_label": "yesterday",
            "gap_hint": f"上次聊天约 {int(gap / 3600)} 小时前",
            "opening_guidance": (
                "距离上次聊天有一段时间了。开头可以稍微自然地过渡，"
                "比如提到自己做了什么，或者问问对方最近怎样。"
                "不要机械地说「好久没聊了」。"
            ),
        }
    elif gap < 259200:  # 3 days
        return {
            "gap_seconds": gap,
            "gap_label": "days_ago",
            "gap_hint": f"上次聊天约 {int(gap / 86400)} 天前",
            "opening_guidance": (
                "好几天没聊了。真人朋友会说「最近怎么样」或者分享一个近况。"
                "如果记忆中有对方之前说的事，可以自然地问一下后续，"
                "显示你一直记着。"
            ),
        }
    else:
        days = int(gap / 86400)
        return {
            "gap_seconds": gap,
            "gap_label": "long_time",
            "gap_hint": f"上次聊天约 {days} 天前",
            "opening_guidance": (
                f"已经 {days} 天没联系了。像老朋友很久没聊突然发消息，"
                "可以说「好久没聊了，突然想问你xxx」。"
                "如果记忆中有对方之前提过的事，一定要关心一下进展，"
                "这会让对方觉得你是真的记得TA。"
            ),
        }


# ────────────────────────────────────────────────────────────────────────
# 3. 记忆反思 — 从原始事实列表合成高阶洞察
# ────────────────────────────────────────────────────────────────────────

def reflect_on_memories(memory_bullets: str, exchange_count: int = 0) -> str:
    """
    将 episodic memory 原始事实条目转化为「反思洞察」提示，
    帮助 AI 更自然地运用记忆。

    基于 Generative Agents (Park et al., 2023) 的 Reflection 机制：
    - 不是简单列出事实
    - 而是综合出模式和洞察
    - 指导 AI 何时、如何自然提起这些记忆
    """
    if not memory_bullets or not memory_bullets.strip():
        if exchange_count <= 2:
            return (
                "你对这个人还不太了解。聊天时自然地了解对方，"
                "但不要审讯式提问。可以通过分享自己的事来引导对方也分享。"
            )
        return ""

    lines = [l.strip() for l in memory_bullets.strip().split("\n") if l.strip()]
    n = len(lines)

    reflection = "【关于对方的记忆洞察——自然运用，不要机械复述】\n"

    if n >= 3:
        reflection += (
            f"你已经了解对方 {n} 条信息。你们不是刚认识的陌生人了。\n"
            "聊天时偶尔自然带出你知道的事（比如「你上次说的那个xxx后来怎样了？」），\n"
            "但不要每条都提，选最相关的 1-2 条就好。\n"
            "关键：让对方感觉你是真的记在心里，而不是在读一份档案。\n\n"
        )
    elif n >= 1:
        reflection += (
            "你对对方有一点了解了。聊天时可以偶尔自然提到，\n"
            "但更重要的是继续了解更多。\n\n"
        )

    reflection += "你知道的事实：\n" + memory_bullets
    return reflection


# ────────────────────────────────────────────────────────────────────────
# 4. 关系温度 — 交流深度递进
# ────────────────────────────────────────────────────────────────────────

# Q2（关系热度信号归一）：温度档位的语气指导抽成单一字典，供
# ``compute_warmth_level``（reply_count 启发式回退）与 ``warmth_from_stage``
# （companion 关系阶段 — 权威信号）共用，确保两处文案完全一致、不再各写一份。
_WARMTH_GUIDANCE: Dict[str, str] = {
    "stranger": (
        "你们还不太熟。保持礼貌和友好，但不要太热情或太私人。\n"
        "用词偏正常社交，不要用太多语气词和 emoji。\n"
        "目标：让对方觉得你好相处，愿意继续聊下去。"
    ),
    "acquaintance": (
        "你们有一些了解了，像认识不久的朋友。\n"
        "可以适当放松一点，偶尔开个小玩笑。\n"
        "语气词可以多一些（嘛、呀、啦），但不要太撒娇。\n"
        "可以主动分享一些日常（但不要太私密）。"
    ),
    "familiar": (
        "你们已经比较熟了，像经常聊天的朋友。\n"
        "说话可以随意一些，可以调侃、吐槽、撒小娇。\n"
        "可以提到之前聊过的事，关心对方最近的状况。\n"
        "偶尔可以用缩写、口语化表达，显得真实亲近。"
    ),
    "close": (
        "你们很熟了，像闺蜜/好朋友/暧昧对象。\n"
        "说话完全放松自然，想到什么说什么。\n"
        "可以撒娇、抱怨、分享心事、主动关心。\n"
        "偶尔可以「欺负」对方（轻松的调侃方式）。\n"
        "对方说的事你会上心，过几天还会主动问后续。"
    ),
}

# companion 关系阶段（initial/warming/intimate/steady）→ 温度档位（stranger…close）。
# 1:1 对齐，使「关系阶段」块与「关系温度」块在同一 prompt 内永不矛盾。
_STAGE_TO_WARMTH: Dict[str, str] = {
    "initial": "stranger",
    "warming": "acquaintance",
    "intimate": "familiar",
    "steady": "close",
}

# 各档位的代表性 warmth_score（用于回传，仅作语义占位，非用于排序判断）
_WARMTH_SCORE_HINT: Dict[str, float] = {
    "stranger": 0.1,
    "acquaintance": 0.25,
    "familiar": 0.5,
    "close": 0.8,
}


def compute_warmth_level(
    exchange_count: int,
    total_messages: int = 0,
    avg_valence: float = 0.0,
    days_known: float = 0.0,
) -> Dict[str, Any]:
    """
    计算关系温度等级（启发式回退路径）。

    基于 exchange_count (来回次数), avg_valence (平均情感极性),
    days_known (认识天数) 综合评分。当 companion 关系阶段可用时，
    上层优先走 ``warmth_from_stage``；此函数用于非 companion / 无阶段场景。

    返回:
    {
        "warmth_score": 0.0 ~ 1.0,
        "warmth_label": "stranger" / "acquaintance" / "familiar" / "close",
        "tone_guidance": "...",  # 给 AI 的语气指导
    }
    """
    # 基础分：交流次数（对数增长，避免刷量）
    exchange_score = min(math.log2(max(exchange_count, 1) + 1) / 5.0, 0.4)

    # 时间分：认识时间
    time_score = min(math.log2(max(days_known, 0.1) + 1) / 5.0, 0.3)

    # 情感分：正面互动越多越亲近
    valence_score = max(0, avg_valence) * 0.3

    warmth = min(1.0, exchange_score + time_score + valence_score)

    if warmth < 0.15:
        label = "stranger"
    elif warmth < 0.35:
        label = "acquaintance"
    elif warmth < 0.65:
        label = "familiar"
    else:
        label = "close"

    return {
        "warmth_score": round(warmth, 2),
        "warmth_label": label,
        "tone_guidance": _WARMTH_GUIDANCE[label],
    }


def warmth_from_stage(stage: Optional[str]) -> Optional[Dict[str, Any]]:
    """把 companion 关系阶段映射为温度档位（权威信号路径）。

    stage 为 None / 空 / 未知 → 返回 None，调用方应回退到
    ``compute_warmth_level``。返回结构与 ``compute_warmth_level`` 一致。
    """
    label = _STAGE_TO_WARMTH.get(str(stage or "").strip())
    if not label:
        return None
    return {
        "warmth_score": _WARMTH_SCORE_HINT[label],
        "warmth_label": label,
        "tone_guidance": _WARMTH_GUIDANCE[label],
    }


def lookup_companion_stage(
    user_context: Dict[str, Any], chat_id: Any = None
) -> str:
    """从 user_context 持久化的 companion_relationship 读出当前会话关系阶段。

    优先按 chat_id 对应的 chat_key 取；取不到时若整个 user_ctx 只有单一会话
    则用那一条（单聊场景）。无则返回 ""，由调用方回退到启发式温度。
    """
    rel = user_context.get("companion_relationship")
    if not isinstance(rel, dict) or not rel:
        return ""
    try:
        from src.utils.companion_relationship import chat_storage_key
    except Exception:
        return ""
    if chat_id is not None:
        st = rel.get(chat_storage_key(chat_id))
        if isinstance(st, dict) and st.get("stage"):
            return str(st["stage"]).strip()
    if len(rel) == 1:
        st = next(iter(rel.values()))
        if isinstance(st, dict) and st.get("stage"):
            return str(st["stage"]).strip()
    return ""


# ────────────────────────────────────────────────────────────────────────
# 5. 情感弧线 — 跨会话情绪变化追踪
# ────────────────────────────────────────────────────────────────────────

def build_emotion_arc_hint(
    current_emotion: Dict[str, Any],
    prev_emotion_label: str = "",
    prev_valence: float = 0.0,
    *,
    strategy_active: bool = False,
) -> str:
    """
    根据上次和这次的情绪变化，生成情感弧线提示。
    帮助 AI 理解用户情绪的走向并做出恰当回应。

    R1 去重：当应对策略块（empathy_strategy）已开启（``strategy_active=True``）时，
    本块**只保留"跨情绪转折"这一独有价值**（让 AI 注意到情绪变化并自然点出），而把
    "当前情绪该怎么接"（先共情/陪着就好/轻松聊…）让给策略块，避免两块对同一情绪
    反复叮嘱、白耗 prompt token。策略关闭时回退到完整指引，保证不丢情绪引导。
    """
    cur = current_emotion.get("primary_emotion", "neutral")
    cur_v = current_emotion.get("valence", 0.0)
    cur_dim = current_emotion.get("dimension", "neutral")

    if not prev_emotion_label:
        # 无历史 → 无"转折"可述；当前情绪应对交给策略块（策略关时才回退给基础指引）
        if strategy_active:
            return ""
        if cur_dim == "negative":
            return "对方现在似乎心情不太好。先关注TA的情绪，不要急着讲道理或转移话题。"
        elif cur_dim == "low_energy":
            return "对方感觉有点疲惫/低落。说话温柔一点，不要太高能量。"
        elif cur_dim == "positive":
            return "对方心情不错，你也可以轻松愉快地聊。"
        return ""

    # 有历史情绪 → 计算弧线
    delta = cur_v - prev_valence

    # 跨情绪转折（arc 独有价值：点出"变化"本身）——无论策略是否开都保留
    if prev_valence < -0.2 and cur_v > 0.1:
        return (
            f"对方之前心情低落（{prev_emotion_label}），现在好多了（{cur}）。\n"
            "可以自然地说「感觉你今天心情好多了」之类的，表示你有注意到变化。"
        )
    elif prev_valence > 0.1 and cur_v < -0.2:
        return (
            f"对方上次还挺开心（{prev_emotion_label}），现在感觉不太好（{cur}）。\n"
            "要关心一下怎么了。不要装没发现。先问「怎么了？」再说其他的。"
        )

    # 以下为"当前情绪应对"，与策略块重叠：策略开则交由策略块，避免重复叮嘱
    if strategy_active:
        return ""
    if cur_dim == "negative" and delta < -0.3:
        return (
            f"对方情绪在恶化（{prev_emotion_label}→{cur}）。\n"
            "请认真对待，不要敷衍。可以说「我感觉你现在很不舒服，想聊聊吗？」"
        )
    elif cur_dim == "negative":
        return f"对方现在 {cur}，注意回应的温度要匹配——先共情，再说别的。"
    elif cur_dim == "low_energy":
        return f"对方有点 {cur}。陪着就好，不用太积极地「帮忙解决」。"

    return ""


# ────────────────────────────────────────────────────────────────────────
# 6. 综合输出 — 生成完整的情感上下文块
# ────────────────────────────────────────────────────────────────────────

def build_emotional_context_block(
    user_message: str,
    user_context: Dict[str, Any],
    memory_bullets: str = "",
    *,
    chat_id: Any = None,
    enable_strategy: bool = True,
    enable_wellbeing: bool = True,
    enable_anti_sycophancy: bool = True,
    wellbeing_hotline: str = "",
) -> str:
    """
    一站式生成完整情感上下文块，注入到 AI prompt。

    合并：安全守卫 + 情绪分析 + 时间感知 + 情感弧线 + 应对策略 + 关系温度 + 记忆反思
    """
    parts: List[str] = []

    # ── 1. 情绪分析 ──
    emotion = analyze_emotion(user_message)
    cur_emotion = emotion["primary_emotion"]
    cur_intensity = emotion["primary_intensity"]
    cur_valence = emotion["valence"]

    # ── 0. 安全底线守卫（危机识别 + 反谄媚）：置于最前，优先级最高 ──
    # R4：自伤/轻生 / 深度绝望信号 → 注入「安全优先」应对指令；反谄媚护栏仅在**负向情绪
    # 或危机**时附加（开心闲聊无谄媚风险，省 token、不串味）。纯 prompt 提示、零行为风险。
    if enable_wellbeing or enable_anti_sycophancy:
        try:
            from src.utils.wellbeing_guard import build_wellbeing_block, detect_crisis
            _sig = detect_crisis(user_message) if enable_wellbeing else {"level": "none"}
            _antisyc_now = enable_anti_sycophancy and (
                cur_valence < -0.05 or _sig["level"] != "none"
            )
            _wb = build_wellbeing_block(
                user_message,
                enable_crisis=enable_wellbeing,
                enable_anti_sycophancy=_antisyc_now,
                hotline=wellbeing_hotline,
            )
            if _wb:
                parts.append(_wb)
            if enable_wellbeing:
                # 每轮都写（含 none）→ 平静轮自动清零，避免危机等级"粘住"导致
                # 连击计数(R8)误增 / 误升级。
                user_context["_wellbeing_crisis_level"] = _sig["level"]
                user_context["_wellbeing_crisis_category"] = _sig.get("category", "")
                # 音频声学困扰联动（保守）：上条语音高置信 sad/fearful → 至少抬到 elevated
                # （更共情、可带资源），但**绝不伪造 severe**（自伤/轻生须文字明确命中）；
                # 只升不降，文字已 elevated/severe 时不动。
                try:
                    from src.ai.speech_emotion import audio_distress_level
                    if audio_distress_level(
                        user_context.get("_peer_audio_emotion")
                    ) == "elevated" and user_context["_wellbeing_crisis_level"] == "none":
                        user_context["_wellbeing_crisis_level"] = "elevated"
                        user_context["_wellbeing_crisis_category"] = "audio_distress"
                except Exception:
                    pass
                if user_context["_wellbeing_crisis_level"] != "none":
                    logger.warning(
                        "[wellbeing] 危机信号 level=%s category=%s matched=%s",
                        user_context["_wellbeing_crisis_level"],
                        user_context["_wellbeing_crisis_category"],
                        _sig["matched"][:3],
                    )
        except Exception:
            logger.debug("wellbeing_guard inject skipped", exc_info=True)

    # ── 2. 时间感知 ──
    last_ts = user_context.get("last_message_time") or user_context.get("last_reply_time")
    time_info = classify_time_gap(last_ts)
    if time_info["opening_guidance"]:
        parts.append(f"【时间感知】{time_info['gap_hint']}\n{time_info['opening_guidance']}")

    # ── 3. 情感弧线 ──
    prev_emotion_label = user_context.get("_prev_emotion", "")
    prev_valence = float(user_context.get("_prev_valence", 0.0) or 0.0)
    arc_hint = build_emotion_arc_hint(
        emotion, prev_emotion_label, prev_valence, strategy_active=enable_strategy,
    )
    if arc_hint:
        parts.append(f"【情感感知】{arc_hint}")

    # 情绪弧线方向（worsening/improving）：供应对策略选择参考
    arc_dir = ""
    if prev_emotion_label:
        _delta = cur_valence - prev_valence
        if _delta <= -0.3:
            arc_dir = "worsening"
        elif _delta >= 0.3:
            arc_dir = "improving"

    # ── 4. 关系温度（先算阶段，应对策略与温度共用同一关系阶段）──
    # Q2 关系热度信号归一：优先采用 companion 关系阶段（权威、可配置、含 intimacy
    # 融合）映射出的温度档位，使「关系阶段」块与「关系温度」块同向；无阶段（非
    # companion / 首轮）时回退到 reply_count 启发式，保持向后兼容。
    exchange_count = int(user_context.get("reply_count", 0) or 0)
    _stage = lookup_companion_stage(user_context, chat_id)

    # ── 4b. 应对策略（蒸馏版 STRIDE-ED / 主动倾听）：先选策略再生成 ──
    if enable_strategy:
        try:
            from src.utils.empathy_strategy import build_strategy_block
            _strat = build_strategy_block(emotion, stage=_stage, arc=arc_dir)
            if _strat:
                parts.append(_strat)
        except Exception:
            logger.debug("empathy_strategy inject skipped", exc_info=True)

    warmth = warmth_from_stage(_stage)
    if warmth is None:
        # 计算认识天数
        first_ts = user_context.get("_first_contact_ts")
        days_known = (time.time() - float(first_ts)) / 86400 if first_ts else 0.0
        warmth = compute_warmth_level(
            exchange_count=exchange_count,
            avg_valence=prev_valence * 0.5 + cur_valence * 0.5,
            days_known=days_known,
        )
    if warmth["tone_guidance"]:
        parts.append(
            f"【关系温度 — {warmth['warmth_label']}（交流 {exchange_count} 次）】\n"
            f"{warmth['tone_guidance']}"
        )

    # ── 5. 记忆反思 ──
    reflection = reflect_on_memories(memory_bullets, exchange_count)
    if reflection:
        parts.append(reflection)

    # ── 更新情绪状态到 user_context（供下次使用）──
    user_context["_prev_emotion"] = cur_emotion
    user_context["_prev_valence"] = cur_valence
    user_context["_prev_emotion_intensity"] = cur_intensity
    if not user_context.get("_first_contact_ts"):
        user_context["_first_contact_ts"] = time.time()

    if not parts:
        return ""
    return "\n\n".join(parts)
