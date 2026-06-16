"""P41 — 客户互动积分与成就系统（情感陪伴场景）。

积分维度（满分累计，无上限）：
  frequency   — 消息互动频率（近 30 天入站消息数）
  depth       — 对话深度（跨会话总消息数 + 长消息奖励）
  sentiment   — 情绪正向度（客户消息含积极词）
  consistency — 持续互动（近 30 天有消息的天数）

等级：
  新朋友 0-99 | 熟人 100-299 | 好友 300-599 | 密友 600+

成就（首次解锁写入 achievements_json）：
  first_deep_chat   — 单次会话 ≥ 20 条消息
  week_streak       — 7 天内有互动
  mood_guardian     — 坐席成功安抚负面情绪（末条出站 + 前序含负面词）
  vip_companion     — 积分 ≥ 600
  reunion_master    — 沉默 7 天后成功 re-engage（有出站回复）
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

_LEVELS = [
    (600, "密友", "vip"),
    (300, "好友", "friend"),
    (100, "熟人", "acquaintance"),
    (0,   "新朋友", "new"),
]

_POSITIVE_KW = [
    "开心", "高兴", "谢谢", "感谢", "喜欢", "棒", "好", "温暖", "感动",
    "happy", "thanks", "love", "great", "nice", "warm",
]
_NEGATIVE_KW = [
    "难过", "伤心", "孤独", "烦", "累", "失望", "焦虑",
    "sad", "lonely", "tired", "upset", "anxious",
]

_ACHIEVEMENT_DEFS = {
    "first_deep_chat": {"name": "首次深聊", "icon": "💬", "desc": "单次会话达到 20 条以上消息"},
    "week_streak":     {"name": "七日相伴", "icon": "📅", "desc": "7 天内有互动记录"},
    "mood_guardian":   {"name": "情绪守护者", "icon": "💝", "desc": "成功安抚客户负面情绪"},
    "vip_companion":   {"name": "VIP 陪伴", "icon": "🌟", "desc": "互动积分达到 600"},
    "reunion_master":  {"name": "重逢大师", "icon": "🔄", "desc": "沉默 7 天后成功 reconnect"},
}


class EngagementScorer:
    """P41：互动积分计算器。"""

    def compute(
        self,
        messages: List[Dict[str, Any]],
        *,
        existing_achievements: Optional[List[str]] = None,
        last_silence_days: float = 0.0,
    ) -> Dict[str, Any]:
        """基于消息历史计算积分 + 检测新成就。

        messages: [{direction, text, ts}] 跨会话聚合
        """
        existing = set(existing_achievements or [])
        now = time.time()
        cutoff_30d = now - 30 * 86400

        inbound = [m for m in messages if m.get("direction") in ("in", "inbound")]
        outbound = [m for m in messages if m.get("direction") in ("out", "outbound")]
        recent_in = [m for m in inbound if float(m.get("ts") or 0) >= cutoff_30d]

        # ── 积分维度 ────────────────────────────────────────────────────────
        freq_pts = min(200, len(recent_in) * 4)                    # 频率
        depth_pts = min(250, len(messages) * 2)                    # 深度
        long_msgs = sum(1 for m in inbound if len(str(m.get("text") or "")) >= 40)
        depth_pts += min(50, long_msgs * 5)

        pos_hits = sum(
            1 for m in inbound
            if any(kw in str(m.get("text") or "").lower() for kw in _POSITIVE_KW)
        )
        sentiment_pts = min(150, pos_hits * 8)

        # 持续互动：近 30 天有消息的不同日期数
        days_active = len({
            int(float(m.get("ts") or 0) // 86400) for m in recent_in
        })
        consistency_pts = min(100, days_active * 10)

        total = freq_pts + depth_pts + sentiment_pts + consistency_pts
        level_name, level_id = self._level_for(total)

        breakdown = {
            "frequency": freq_pts,
            "depth": depth_pts,
            "sentiment": sentiment_pts,
            "consistency": consistency_pts,
        }

        # ── 成就检测 ────────────────────────────────────────────────────────
        new_achievements: List[str] = []
        checks = self._check_achievements(
            messages, inbound, outbound, total, last_silence_days, existing,
        )
        for ach_id in checks:
            if ach_id not in existing:
                new_achievements.append(ach_id)

        all_achievements = sorted(existing | set(new_achievements))
        achievement_details = [
            {**_ACHIEVEMENT_DEFS[aid], "id": aid, "unlocked": True}
            for aid in all_achievements if aid in _ACHIEVEMENT_DEFS
        ]
        # 未解锁的也列出（灰色展示用）
        for aid, defn in _ACHIEVEMENT_DEFS.items():
            if aid not in all_achievements:
                achievement_details.append({**defn, "id": aid, "unlocked": False})

        return {
            "points": total,
            "level": level_id,
            "level_name": level_name,
            "breakdown": breakdown,
            "days_active_30d": days_active,
            "message_count": len(messages),
            "achievements": all_achievements,
            "new_achievements": new_achievements,
            "achievement_details": achievement_details,
            "is_vip": total >= 600,
            "computed_at": now,
        }

    @staticmethod
    def _level_for(points: int) -> Tuple[str, str]:
        for threshold, name, lid in _LEVELS:
            if points >= threshold:
                return name, lid
        return "新朋友", "new"

    @staticmethod
    def _check_achievements(
        messages: List[Dict[str, Any]],
        inbound: List[Dict[str, Any]],
        outbound: List[Dict[str, Any]],
        total_pts: int,
        silence_days: float,
        existing: set,
    ) -> List[str]:
        found: List[str] = []
        # first_deep_chat: 任意单会话 ≥ 20 条
        by_conv: Dict[str, int] = {}
        for m in messages:
            cid = str(m.get("conversation_id") or "_")
            by_conv[cid] = by_conv.get(cid, 0) + 1
        if any(n >= 20 for n in by_conv.values()):
            found.append("first_deep_chat")

        # week_streak: 近 7 天有 ≥ 3 天活跃
        now = time.time()
        cutoff_7d = now - 7 * 86400
        days_7 = len({
            int(float(m.get("ts") or 0) // 86400)
            for m in inbound if float(m.get("ts") or 0) >= cutoff_7d
        })
        if days_7 >= 3:
            found.append("week_streak")

        # mood_guardian: 末条出站，前序入站含负面词
        if inbound and outbound:
            last_out = outbound[-1]
            prior_in = [m for m in inbound if float(m.get("ts") or 0) < float(last_out.get("ts") or 0)]
            if prior_in:
                last_in_text = str(prior_in[-1].get("text") or "").lower()
                if any(kw in last_in_text for kw in _NEGATIVE_KW):
                    found.append("mood_guardian")

        if total_pts >= 600:
            found.append("vip_companion")

        if silence_days >= 7 and outbound:
            found.append("reunion_master")

        return found

    def points_history_snapshot(
        self, current_points: int, previous_points: int = 0
    ) -> List[Dict[str, Any]]:
        """生成简单趋势点（供前端 sparkline）。"""
        return [
            {"ts": time.time() - 86400 * 7, "points": max(0, previous_points)},
            {"ts": time.time(), "points": current_points},
        ]
