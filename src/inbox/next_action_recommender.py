"""P37 — 智能下一步动作推荐引擎（情感陪伴场景）。

基于会话当前状态（风险信号 / 亲密度 / 流失风险 / 沉默时长 / 轮次等），
推荐最适合的下一步动作，帮助坐席快速决策。

场景聚焦：情感陪伴 / 聊天进阶（非电商）
  - 情感共鸣优先于产品推介
  - 进阶互动（亲密度提升）优先于关闭话题
  - 定期回访维系长期关系

动作类型（action_type）：
  template   — 发送预设话术模板
  task       — 创建坐席跟进任务
  tag        — 为会话打标签
  escalate   — 升级至人工/主管
  chain      — 触发工作链
  note       — 添加内部注解提醒
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ── 内置场景动作库（情感陪伴场景） ─────────────────────────────────────────

_BUILTIN_ACTIONS: List[Dict[str, Any]] = [
    {
        "action_id": "__empathy",
        "icon": "💝",
        "name": "情感共鸣回应",
        "action_type": "template",
        "builtin": True,
        "priority": 100,
        "config": {
            "hint": "表达理解与陪伴，避免说教，以倾听为主",
            "template_text": "我能理解你现在的感受，能多说说吗？我在这里陪着你。",
        },
        "trigger_conditions": ["sentiment_negative", "complaint", "churn_intent"],
    },
    {
        "action_id": "__deepen_topic",
        "icon": "🎯",
        "name": "深化话题引导",
        "action_type": "template",
        "builtin": True,
        "priority": 80,
        "config": {
            "hint": "对方聊得投入时，引导进入更深层的话题或分享",
            "template_text": "听你说这些，我很想多了解你。你平时最享受什么样的时光呢？",
        },
        "trigger_conditions": ["high_engagement", "long_conversation"],
    },
    {
        "action_id": "__advance_intimacy",
        "icon": "🌟",
        "name": "进阶互动建议",
        "action_type": "template",
        "builtin": True,
        "priority": 75,
        "config": {
            "hint": "对话轮次多、关系稳定后，适时升温互动",
            "template_text": "和你聊天总有很多收获，我们可以更多分享彼此的生活吗？",
        },
        "trigger_conditions": ["intimacy_growing", "many_turns"],
    },
    {
        "action_id": "__special_care",
        "icon": "🎁",
        "name": "特别关怀问候",
        "action_type": "template",
        "builtin": True,
        "priority": 70,
        "config": {
            "hint": "对方沉默一段时间后，主动发起温暖问候",
            "template_text": "最近没有见到你，想知道你还好吗？希望你一切都顺心。",
        },
        "trigger_conditions": ["silent_3d", "silent_7d"],
    },
    {
        "action_id": "__schedule_followup",
        "icon": "📅",
        "name": "创建回访任务",
        "action_type": "task",
        "builtin": True,
        "priority": 65,
        "config": {
            "hint": "会话即将结束时，安排下一次联系时间",
            "due_hours": 72,
            "note": "定期回访，维持情感连接",
        },
        "trigger_conditions": ["conversation_closing", "silent_3d"],
    },
    {
        "action_id": "__add_mood_tag",
        "icon": "🏷",
        "name": "标记情绪状态",
        "action_type": "tag",
        "builtin": True,
        "priority": 55,
        "config": {
            "hint": "为当前情绪状态打标签，便于后续个性化",
            "tag_options": ["情绪低落", "积极开朗", "需要关注", "进展顺利"],
        },
        "trigger_conditions": ["any"],
    },
    {
        "action_id": "__human_escalate",
        "icon": "🔴",
        "name": "升级人工接管",
        "action_type": "escalate",
        "builtin": True,
        "priority": 120,   # 最高优先级
        "config": {
            "hint": "情绪极度负面或对话陷入危机时，立即转人工",
            "reason": "高风险情绪干预",
        },
        "trigger_conditions": ["crisis_signal", "escalation_intent", "churn_intent_high"],
    },
    {
        "action_id": "__add_internal_note",
        "icon": "📝",
        "name": "添加内部备注",
        "action_type": "note",
        "builtin": True,
        "priority": 40,
        "config": {
            "hint": "记录关键信息供团队共享",
        },
        "trigger_conditions": ["any"],
    },
]

# ── 场景检测规则 ──────────────────────────────────────────────────────────────

_CRISIS_KW = ["不想活了", "活着没意思", "想消失", "轻生", "自杀",
              "don't want to live", "no reason to live", "end it all"]

_NEGATIVE_KW = ["难过", "伤心", "孤独", "寂寞", "绝望", "痛苦", "迷茫",
                "sad", "lonely", "hopeless", "depressed", "hurt"]


class NextActionRecommender:
    """P37：情感陪伴场景下一步动作推荐器。"""

    def recommend(
        self,
        *,
        risk_signals: Optional[List[Dict[str, Any]]] = None,
        last_msg_text: str = "",
        last_msg_direction: str = "in",
        message_count: int = 0,
        silence_hours: float = 0.0,
        churn_risk_level: str = "",
        qa_score: int = -1,
        custom_actions: Optional[List[Dict[str, Any]]] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """推荐最适合的下一步动作（内置 + 自定义合并）。

        Returns:
            [{action_id, name, icon, action_type, config, reason, priority}]
            按 priority 降序，最多返回 limit 条
        """
        risk_signals = risk_signals or []
        custom_actions = custom_actions or []

        # 检测当前会话信号
        signals = self._detect_signals(
            risk_signals=risk_signals,
            last_msg_text=last_msg_text,
            last_msg_direction=last_msg_direction,
            message_count=message_count,
            silence_hours=silence_hours,
            churn_risk_level=churn_risk_level,
        )

        # 内置动作评分
        candidates: List[Dict[str, Any]] = []
        for act in _BUILTIN_ACTIONS:
            matched = self._match_triggers(act["trigger_conditions"], signals)
            if matched:
                candidates.append({
                    **act,
                    "reason": self._build_reason(matched, signals),
                    "matched_signals": matched,
                    "_score": act["priority"] + len(matched) * 5,
                })

        # 自定义动作评分
        for act in custom_actions:
            if not act.get("enabled", True):
                continue
            triggers = act.get("trigger_conditions") or ["any"]
            if isinstance(triggers, str):
                try:
                    import json as _j
                    triggers = _j.loads(triggers)
                except Exception:
                    triggers = [triggers]
            matched = self._match_triggers(triggers, signals)
            if matched or "any" in triggers:
                candidates.append({
                    **act,
                    "reason": f"自定义动作：{act.get('name', '')}",
                    "matched_signals": matched,
                    "_score": int(act.get("sort_order") or 0) + len(matched) * 5,
                })

        # 排序并截断
        candidates.sort(key=lambda x: x.get("_score", 0), reverse=True)
        # 清理内部排序字段
        for c in candidates:
            c.pop("_score", None)
            c.pop("trigger_conditions", None)
            c.pop("matched_signals", None)

        return candidates[:limit]

    # ── 信号检测 ────────────────────────────────────────────────────────────

    def _detect_signals(
        self,
        *,
        risk_signals: List[Dict[str, Any]],
        last_msg_text: str,
        last_msg_direction: str,
        message_count: int,
        silence_hours: float,
        churn_risk_level: str,
    ) -> List[str]:
        """把各维度输入转换为统一信号标签列表。"""
        sigs: List[str] = []
        text_lc = last_msg_text.lower() if last_msg_text else ""

        # 危机信号（最高优先级）
        if any(kw in text_lc for kw in _CRISIS_KW):
            sigs.append("crisis_signal")

        # 情绪负面
        if any(kw in text_lc for kw in _NEGATIVE_KW):
            sigs.append("sentiment_negative")

        # 外部传入的风险信号
        for rs in risk_signals:
            sigs.append(rs.get("signal", ""))

        # 流失风险
        if churn_risk_level == "high":
            sigs.append("churn_intent_high")
        elif churn_risk_level == "medium":
            sigs.append("churn_intent")

        # 沉默时段
        if silence_hours >= 168:     # 7 天
            sigs.append("silent_7d")
        elif silence_hours >= 72:    # 3 天
            sigs.append("silent_3d")

        # 轮次相关
        if message_count >= 20:
            sigs.append("long_conversation")
            sigs.append("many_turns")
        if message_count >= 8:
            sigs.append("high_engagement")

        # 进阶互动条件
        if message_count >= 10 and churn_risk_level not in ("high",):
            sigs.append("intimacy_growing")

        # 末条为出站（坐席刚回）
        if last_msg_direction in ("out", "outbound"):
            sigs.append("conversation_closing")

        # 通配符
        sigs.append("any")
        return list(dict.fromkeys(sigs))  # 去重保序

    @staticmethod
    def _match_triggers(trigger_conditions: List[str], signals: List[str]) -> List[str]:
        """返回命中的信号列表（空列表=未命中）。"""
        if "any" in trigger_conditions:
            return ["any"]
        return [t for t in trigger_conditions if t in signals]

    @staticmethod
    def _build_reason(matched: List[str], signals: List[str]) -> str:
        _REASON_MAP = {
            "crisis_signal":       "⚠ 检测到危机信号，建议立即人工介入",
            "sentiment_negative":  "情绪偏负面，建议先共情",
            "complaint":           "用户有投诉情绪",
            "churn_intent":        "有流失倾向信号",
            "churn_intent_high":   "高流失风险，需主动挽留",
            "escalation_intent":   "对话有升级趋势",
            "silent_7d":           "沉默超 7 天，关系维护关键期",
            "silent_3d":           "沉默超 3 天，适合主动问候",
            "long_conversation":   "对话轮次充足，关系稳定",
            "high_engagement":     "对话活跃，互动良好",
            "intimacy_growing":    "亲密度成长阶段，适合深化",
            "many_turns":          "多轮深入交流",
            "conversation_closing":"对话即将结束",
            "any":                 "通用动作，适用于任何场景",
        }
        return "；".join(_REASON_MAP.get(m, m) for m in matched if m in _REASON_MAP)
