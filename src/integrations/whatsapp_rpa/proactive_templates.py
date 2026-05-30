"""P15-g: 主动续聊话题模板池 + A/B 轮换系统。

解决续聊内容重复、口吻单一的问题，通过模板化 + LLM 填充实现多样化。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Dict, List, Optional, Tuple


class ProactiveTemplatePool:
    """主动续聊话题模板池，支持 A/B 轮换和效果追踪。"""

    # 模板库：关怀型 / 兴趣跟进型 / 提问型
    TEMPLATES: Dict[str, List[str]] = {
        "care": [
            "嘿，好久没看到你的消息了，最近一切都还好吗？",
            "最近忙什么呢？感觉好久不见了，有点想念你的消息呢~",
            "Hi！最近天气多变，记得照顾好自己。你那边最近怎么样？",
            "好久没聊啦，最近有什么开心的事想分享吗？",
            "突然想到你，就发条消息问候一下。最近过得怎么样？",
        ],
        "interest": [
            "上次聊到的{topic}，后来有再了解吗？我最近也看了一些相关的，挺有意思的。",
            "最近在研究{topic}，突然想起你好像也挺感兴趣的，有什么新发现吗？",
            "看到关于{topic}的消息就想到你了，你那边有什么新动态吗？",
            "之前你说的{topic}，我一直记着呢，后来有尝试吗？",
            "刷到{topic}相关内容，第一反应就是想到你，最近在关注这个吗？",
        ],
        "question": [
            "最近在想，{topic}你怎么看？想听听你的想法~",
            "有个问题想请教你：{topic}你更倾向哪种选择？",
            "突然好奇，你平时{topic}都是怎么安排的？",
            "想听听你的意见：{topic}有什么推荐吗？",
            "最近对{topic}有点迷茫，你有什么建议吗？",
        ],
    }

    def __init__(
        self,
        templates: Optional[Dict[str, List[str]]] = None,
        rotation_strategy: str = "round_robin",  # round_robin / random / weighted
        ab_test_enabled: bool = True,
    ) -> None:
        self._templates = templates or dict(self.TEMPLATES)
        self._rotation_strategy = rotation_strategy
        self._ab_test_enabled = ab_test_enabled

        # 每类别的使用计数（用于轮询）
        self._category_index: Dict[str, int] = {cat: 0 for cat in self._templates}

        # A/B 效果追踪（内存级，按类别）
        self._ab_stats: Dict[str, Dict[str, Any]] = {
            cat: {"sent": 0, "replied": 0, "templates": {}}
            for cat in self._templates
        }

    def select_template(
        self,
        category: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, int]:
        """选择模板，返回 (模板内容, 类别, 模板索引)。

        Args:
            category: 强制指定类别，None 则按策略选择
            context: 上下文，包含 last_topic/intent_tag 等用于填充变量

        Returns:
            (template_text, category, template_index)
        """
        ctx = context or {}

        # 1. 确定类别
        if category is None:
            category = self._select_category(ctx)

        if category not in self._templates:
            category = "care"  # 默认兜底

        # 2. 在类别内选择具体模板
        templates = self._templates[category]
        if not templates:
            return "最近怎么样？", category, -1

        if self._rotation_strategy == "round_robin":
            idx = self._category_index[category] % len(templates)
            self._category_index[category] += 1
        elif self._rotation_strategy == "weighted" and self._ab_test_enabled:
            idx = self._weighted_select(category)
        else:  # random
            idx = random.randint(0, len(templates) - 1)

        template = templates[idx]

        # 3. 填充变量
        template = self._fill_variables(template, ctx)

        # 4. 更新统计
        self._ab_stats[category]["sent"] += 1
        self._ab_stats[category]["templates"][idx] = \
            self._ab_stats[category]["templates"].get(idx, 0) + 1

        return template, category, idx

    def _select_category(self, ctx: Dict[str, Any]) -> str:
        """根据上下文智能选择模板类别。"""
        last_topic = ctx.get("last_topic") or ctx.get("last_peer_text") or ""
        intent_tag = ctx.get("intent_tag") or ""

        # 如果有明确话题，优先 interest 或 question
        if last_topic and len(last_topic) > 10:
            # 根据意图标签选择
            if any(tag in intent_tag for tag in ["ask", "question", "how", "what"]):
                return "question"
            return random.choice(["interest", "question"])

        # 默认关怀型
        return "care"

    def _weighted_select(self, category: str) -> int:
        """基于历史使用频率的加权选择（使用越多的模板权重越高）。-P15-h"""
        templates = self._templates.get(category, [])
        if not templates:
            return 0

        # 获取该类别的统计
        stats = self._ab_stats.get(category, {})
        tpl_counts = stats.get("templates", {})
        total_sent = stats.get("sent", 0)

        if total_sent == 0:
            # 无历史数据，随机选择
            return random.randint(0, len(templates) - 1)

        # 计算每个模板的权重（基础权重 + 使用次数加成）
        weights = []
        for idx in range(len(templates)):
            sent = tpl_counts.get(idx, 0)
            # 基础权重 1.0，每次使用增加 0.2（形成正反馈循环）
            weight = 1.0 + sent * 0.2
            weights.append(weight)

        # 加权随机选择
        total_weight = sum(weights)
        r = random.uniform(0, total_weight)
        cumulative = 0
        for idx, w in enumerate(weights):
            cumulative += w
            if r <= cumulative:
                return idx

        return len(templates) - 1

    def _fill_variables(self, template: str, ctx: Dict[str, Any]) -> str:
        """填充模板变量。"""
        # 提取话题（简化：取 last_peer_text 前 10 字或默认）
        last_peer = ctx.get("last_peer_text") or ""
        topic = last_peer[:10] if len(last_peer) > 10 else (last_peer or "这个")

        # 替换变量
        result = template.replace("{topic}", topic)

        return result

    def record_reply(self, category: str, template_idx: int) -> None:
        """记录用户回复（用于 A/B 效果统计）。"""
        if category in self._ab_stats:
            self._ab_stats[category]["replied"] += 1

    def get_stats(self) -> Dict[str, Any]:
        """返回 A/B 统计。"""
        result = {}
        for cat, data in self._ab_stats.items():
            sent = data["sent"]
            replied = data["replied"]
            result[cat] = {
                "sent": sent,
                "replied": replied,
                "reply_rate": round(replied / sent, 3) if sent > 0 else 0.0,
                "templates": data["templates"],
            }
        return result


def create_pool(config: Optional[Dict] = None) -> ProactiveTemplatePool:
    """工厂函数。"""
    cfg = config or {}
    return ProactiveTemplatePool(
        templates=cfg.get("templates"),
        rotation_strategy=cfg.get("rotation_strategy", "round_robin"),
        ab_test_enabled=cfg.get("ab_test_enabled", True),
    )
