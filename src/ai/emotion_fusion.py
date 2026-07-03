"""情绪融合（纯函数）— 把「文字情绪」与「音频声学情绪」合成一个落库信号。

上游：
  - 文字标签来自 `chat_assistant_service.quick_analyze`（中文词表：低落/生气/焦虑/积极/平稳）。
  - 文字强度来自 `emotional_context.analyze_emotion`（primary_intensity）。
  - 音频情绪来自 `speech_emotion.map_audio_emotion`（同中文词表 + confident 标志）。

下游：融合结果写 `conversation_meta.last_emotion / last_emotion_intensity`，被共情回复、
主动护栏、危机分级、出站情感声共用——即「换更准信号源」，不新增下游改动。

融合原则（保守、可解释、绝不比纯文字更糟）：
  1. 无音频 / 音频不置信 → **原样返回文字**（现状零变更）。
  2. 文字中性、音频置信非中性 → **音频胜**（言不由衷：话平淡但语气有情绪）。
  3. 文字/音频同维度 → 保留文字标签（内容更具体），强度取二者较大。
  4. 文字/音频维度冲突（如文字积极、音频负面=反讽）→ 仅当音频**高置信**(≥high_conflict_score)
     才采信音频语气，否则保留文字（避免声学误判翻转正确的文字判断）。
纯函数、无 IO；任何缺失字段都安全退化。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# 中文情绪标签 → 维度（与 speech_emotion._E2V_TO_DIM 同口径）。
_CN_NEG = ("低落", "生气", "焦虑")
_CN_POS = ("积极",)


def cn_label_dimension(label: Any) -> str:
    """中文情绪标签 → positive/negative/neutral。未知/中性/简短 → neutral。"""
    s = str(label or "").strip()
    if s in _CN_NEG:
        return "negative"
    if s in _CN_POS:
        return "positive"
    return "neutral"


def fuse_emotion(
    *,
    text_label: str,
    text_intensity: float = -1.0,
    audio_emo: Optional[Dict[str, Any]] = None,
    high_conflict_score: float = 0.7,
) -> Dict[str, Any]:
    """融合文字 + 音频情绪，返回落库用信号。

    Returns dict:
      - ``label``：融合后中文情绪标签（写 last_emotion）
      - ``intensity``：融合后强度（写 last_emotion_intensity；-1=未知）
      - ``dimension``：positive/negative/neutral
      - ``source``：``text`` | ``audio`` | ``fused``
      - ``audio_used``：音频是否影响了结果（bool，供观测）
    """
    t_label = str(text_label or "").strip()
    t_dim = cn_label_dimension(t_label)
    try:
        t_int = float(text_intensity)
    except (TypeError, ValueError):
        t_int = -1.0

    # 1) 无有效音频 → 原样文字
    if not isinstance(audio_emo, dict) or not audio_emo.get("confident"):
        return {
            "label": t_label,
            "intensity": t_int,
            "dimension": t_dim,
            "source": "text",
            "audio_used": False,
        }

    a_label = str(audio_emo.get("primary_emotion") or "").strip()
    a_dim = str(audio_emo.get("dimension") or "neutral").strip()
    try:
        a_int = float(audio_emo.get("primary_intensity") or 0.0)
    except (TypeError, ValueError):
        a_int = 0.0
    try:
        a_score = float(audio_emo.get("score") or 0.0)
    except (TypeError, ValueError):
        a_score = 0.0

    # 2) 文字中性、音频置信非中性 → 音频胜（言不由衷）
    if t_dim == "neutral" and a_dim != "neutral":
        return {
            "label": a_label,
            "intensity": a_int,
            "dimension": a_dim,
            "source": "audio",
            "audio_used": True,
        }

    # 音频中性、文字非中性 → 保留文字
    if a_dim == "neutral":
        return {
            "label": t_label,
            "intensity": t_int,
            "dimension": t_dim,
            "source": "text",
            "audio_used": False,
        }

    # 3) 同维度 → 保留文字标签，强度取较大
    if a_dim == t_dim:
        fused_int = max(t_int, a_int) if t_int >= 0 else a_int
        return {
            "label": t_label,
            "intensity": round(float(fused_int), 4),
            "dimension": t_dim,
            "source": "fused",
            "audio_used": True,
        }

    # 4) 维度冲突 → 仅高置信采信音频语气（反讽等），否则保留文字
    if a_score >= float(high_conflict_score):
        return {
            "label": a_label,
            "intensity": a_int,
            "dimension": a_dim,
            "source": "audio",
            "audio_used": True,
        }
    return {
        "label": t_label,
        "intensity": t_int,
        "dimension": t_dim,
        "source": "text",
        "audio_used": False,
    }


__all__ = ["fuse_emotion", "cn_label_dimension"]
