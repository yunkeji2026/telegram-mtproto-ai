"""M8 结构化转人工简报（纯函数，零 IO，便于单测）。

为什么需要
==========
AI 转人工最大的体验断点是：坐席接手时**两眼一抹黑**——不知道客户是谁、要什么、
之前聊到哪、情绪如何。本模块把 ``conversation_meta``（意图/情绪/风险/CSAT/摘要）
与最近往来消息汇成一份**结构化简报**，让坐席 3 秒进入状态。

纯函数：数据由路由层（store）取好传入；这里只做组装与「亮点提示」推断，便于单测。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 高风险/负面情绪标签（命中即进 highlights 提醒）
_NEG_EMOTIONS = frozenset({"愤怒", "anger", "angry", "不满", "沮丧", "frustrated", "焦虑"})
_HIGH_RISK = frozenset({"high", "critical", "高", "严重"})


def _who(direction: str) -> str:
    """消息方向 → 角色名（in=客户，out=客服/AI）。"""
    return "客户" if str(direction or "in").lower() == "in" else "客服"


def build_handoff_brief(
    conversation_id: str,
    meta: Optional[Dict[str, Any]],
    recent_messages: Optional[List[Dict[str, Any]]],
    *,
    reason: str = "",
    suggested_assignee: str = "",
    max_turns: int = 6,
) -> Dict[str, Any]:
    """组装结构化转人工简报（纯函数）。

    参数
    ----
    meta              ``store.get_conv_meta`` 输出（可为 None → 各字段优雅缺省）。
    recent_messages   消息列表（每条含 direction/text/ts），按 ts 升序；取末 max_turns 条。
    reason            转人工原因（来自 escalation / 风控）。
    suggested_assignee 建议接手坐席（路由层用 AssignmentService 算好传入，可空）。

    返回结构化简报 dict，含 profile / 最近往来 / highlights 提醒。
    """
    m = meta or {}
    intent = str(m.get("last_intent") or "")
    emotion = str(m.get("last_emotion") or "")
    emotion_trend = str(m.get("emotion_trend") or "stable")
    risk = str(m.get("last_risk") or "low")
    summary = str(m.get("summary") or "")
    msg_count = int(m.get("msg_count") or 0)
    csat_raw = m.get("csat_score")
    try:
        csat = float(csat_raw) if csat_raw is not None else -1.0
    except (TypeError, ValueError):
        csat = -1.0

    turns: List[Dict[str, Any]] = []
    for msg in (recent_messages or [])[-max(1, int(max_turns)):]:
        text = str(msg.get("text") or msg.get("translated_text") or "").strip()
        if not text:
            continue
        turns.append({
            "who": _who(msg.get("direction", "in")),
            "text": text,
            "ts": float(msg.get("ts") or 0.0),
        })

    highlights: List[str] = []
    if risk.lower() in _HIGH_RISK or risk in _HIGH_RISK:
        highlights.append(f"⚠ 高风险会话（risk={risk}），优先处理")
    if emotion and (emotion in _NEG_EMOTIONS or emotion.lower() in _NEG_EMOTIONS):
        highlights.append(f"😟 客户情绪负面（{emotion}），注意安抚")
    if emotion_trend == "rising":
        highlights.append("📈 情绪紧张度上升中")
    if 0 <= csat < 3:
        highlights.append(f"⭐ 历史满意度偏低（CSAT={csat:.0f}）")
    if intent:
        highlights.append(f"🎯 最近意图：{intent}")
    if not highlights:
        highlights.append("✅ 无特别风险信号，常规接手")

    return {
        "ok": True,
        "conversation_id": str(conversation_id or ""),
        "reason": str(reason or ""),
        "suggested_assignee": str(suggested_assignee or ""),
        "profile": {
            "intent": intent,
            "emotion": emotion,
            "emotion_trend": emotion_trend,
            "risk": risk,
            "csat": csat if csat >= 0 else None,
            "summary": summary,
            "msg_count": msg_count,
        },
        "recent_turns": turns,
        "highlights": highlights,
    }
