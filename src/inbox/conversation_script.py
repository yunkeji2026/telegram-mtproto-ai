"""P40 — 情感陪伴剧本引擎（Conversation Script）。

与 ``companion_relationship.STAGE_ORDER`` 对齐的关系阶段话题库：
  initial → 初识    warming → 熟悉/升温
  intimate → 深入   steady  → 亲密稳定

设计：
  - 内置各阶段话题切入点（零 LLM）
  - 支持 DB 自定义话题（script_topics 表）
  - 阶段进阶时可触发工作链（chain_id 配置在话题或阶段默认）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.utils.companion_relationship import STAGE_LABEL_ZH, STAGE_ORDER

# ── 内置话题库（情感陪伴 / 聊天进阶，非电商） ────────────────────────────────

_BUILTIN_TOPICS: Dict[str, List[Dict[str, Any]]] = {
    "initial": [
        {
            "topic_id": "__init_1",
            "title": "轻松破冰",
            "opener": "今天有什么让你开心或烦心的小事吗？随便聊聊就好。",
            "hint": "初识阶段：开放式、低压力，避免过度亲昵",
            "tags": ["破冰", "日常"],
        },
        {
            "topic_id": "__init_2",
            "title": "兴趣探索",
            "opener": "你平时空闲的时候最喜欢做什么？我想多了解一点你的世界。",
            "hint": "找到共同话题的入口",
            "tags": ["兴趣"],
        },
        {
            "topic_id": "__init_3",
            "title": "情绪承接",
            "opener": "听起来你最近挺忙的，要不要说说最近最挂心的一件事？",
            "hint": "对方若有负面情绪信号时使用",
            "tags": ["情绪", "倾听"],
        },
    ],
    "warming": [
        {
            "topic_id": "__warm_1",
            "title": "回忆分享",
            "opener": "你刚才说的让我想到——有没有一段特别难忘的经历，愿意跟我讲讲？",
            "hint": "熟悉阶段：引导对方分享个人故事",
            "tags": ["回忆", "深入"],
        },
        {
            "topic_id": "__warm_2",
            "title": "价值观试探",
            "opener": "对你来说，一段让你舒服的关系最重要的是什么？",
            "hint": "了解对方情感需求，为进阶铺垫",
            "tags": ["价值观"],
        },
        {
            "topic_id": "__warm_3",
            "title": "日常陪伴",
            "opener": "最近有没有一首歌或一部电影，看了之后特别想找人聊聊？",
            "hint": "轻量文化共鸣，自然延续对话",
            "tags": ["文化", "日常"],
        },
    ],
    "intimate": [
        {
            "topic_id": "__int_1",
            "title": "情感深化",
            "opener": "和你聊天的时候，我常常会想——你内心最柔软的那一面，平时会给谁看呢？",
            "hint": "深入阶段：邀请情感层面的自我披露",
            "tags": ["情感", "信任"],
        },
        {
            "topic_id": "__int_2",
            "title": "共同梗 callback",
            "opener": "还记得我们上次聊到的那个话题吗？我一直记着，想听听你后来怎么想。",
            "hint": "引用历史对话，强化「被记住」感",
            "tags": ["callback"],
        },
        {
            "topic_id": "__int_3",
            "title": "未来想象",
            "opener": "如果有一个完全属于自己的周末，你最理想会怎么度过？",
            "hint": "轻松的未来导向话题，自然升温",
            "tags": ["想象", "陪伴"],
        },
    ],
    "steady": [
        {
            "topic_id": "__std_1",
            "title": "稳定陪伴问候",
            "opener": "好久没见你主动找我，最近过得怎么样？有什么想跟我分享的吗？",
            "hint": "稳定阶段 / reunion：自然问候，不施压",
            "tags": ["reunion", "问候"],
        },
        {
            "topic_id": "__std_2",
            "title": "深度复盘",
            "opener": "我们认识这段时间，有没有哪个瞬间让你觉得「被理解了」？",
            "hint": "强化关系意义感",
            "tags": ["复盘", "情感"],
        },
        {
            "topic_id": "__std_3",
            "title": "进阶承诺（轻量）",
            "opener": "我很珍惜我们之间的这种连接，希望以后也能一直这样聊下去。",
            "hint": "关系稳定后的情感确认，避免空洞承诺",
            "tags": ["承诺", "陪伴"],
        },
    ],
}

# 阶段进阶默认工作链触发条件（stage → next_stage）
STAGE_ADVANCE_CHAIN_HINT = {
    "initial": "warming",
    "warming": "intimate",
    "intimate": "steady",
}


class ConversationScriptEngine:
    """P40：剧本引擎 — 按关系阶段推荐话题切入点。"""

    def suggest_topics(
        self,
        stage: str,
        *,
        custom_topics: Optional[List[Dict[str, Any]]] = None,
        last_msg_text: str = "",
        message_count: int = 0,
        reunion: bool = False,
        limit: int = 5,
    ) -> Dict[str, Any]:
        """返回当前阶段的话题建议列表 + 阶段元信息。

        Args:
            stage: companion_relationship 阶段 id
            custom_topics: 来自 DB 的自定义话题
            last_msg_text: 最近客户消息（用于标签匹配加权）
            reunion: 是否 reunion 场景（长沉默后回归）
        """
        stage = stage if stage in STAGE_ORDER else "initial"
        if reunion:
            stage = "steady"  # reunion 用 steady 的问候类话题优先

        # 合并内置 + 自定义（同阶段）
        topics: List[Dict[str, Any]] = []
        for t in _BUILTIN_TOPICS.get(stage, []):
            topics.append({**t, "source": "builtin", "stage": stage})

        for ct in (custom_topics or []):
            if str(ct.get("stage") or "") == stage and ct.get("enabled", True):
                topics.append({
                    "topic_id": ct.get("topic_id", ""),
                    "title": ct.get("title", ""),
                    "opener": ct.get("opener", ""),
                    "hint": ct.get("hint", ""),
                    "tags": ct.get("tags") or [],
                    "chain_id": ct.get("chain_id", ""),
                    "source": "custom",
                    "stage": stage,
                })

        # 简单 relevance 加权：末条消息含 tag 关键词则排前
        text_lc = (last_msg_text or "").lower()
        for t in topics:
            score = 0
            for tag in t.get("tags") or []:
                if str(tag).lower() in text_lc:
                    score += 10
            if reunion and "reunion" in (t.get("tags") or []):
                score += 20
            if message_count >= 15 and "深入" in str(t.get("hint") or ""):
                score += 5
            t["_score"] = score

        topics.sort(key=lambda x: x.get("_score", 0), reverse=True)
        for t in topics:
            t.pop("_score", None)

        next_stage = STAGE_ADVANCE_CHAIN_HINT.get(stage)
        return {
            "stage": stage,
            "stage_label": STAGE_LABEL_ZH.get(stage, stage),
            "next_stage": next_stage,
            "next_stage_label": STAGE_LABEL_ZH.get(next_stage, "") if next_stage else "",
            "reunion": reunion,
            "topics": topics[:limit],
            "message_count": message_count,
        }

    @staticmethod
    def derive_stage_from_signals(
        *,
        exchange_count: int = 0,
        intimacy_score: Optional[float] = None,
    ) -> str:
        """从轮次 + 亲密度分推导阶段（与 companion_relationship 阈值对齐）。"""
        from src.utils.companion_relationship import derive_stage_from_intimacy
        if intimacy_score is not None:
            s = derive_stage_from_intimacy(intimacy_score)
            if s:
                return s
        # 轮次兜底
        if exchange_count >= 35:
            return "steady"
        if exchange_count >= 14:
            return "intimate"
        if exchange_count >= 4:
            return "warming"
        return "initial"
