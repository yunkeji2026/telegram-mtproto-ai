"""多条消息意图分析 — AI 分类 + 路由策略。

分类结果：
- "casual"      : 随意聊天/感叹/表情，只回最新一条即可
- "combined"    : 多条消息表达同一个意思，合并成一条上下文生成单条回复
- "multi_intent": 含 2+ 个不同话题/问题，逐条引用回复
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from src.integrations.whatsapp_rpa.ui_hierarchy import IncomingMessage

# 小写情绪词 / 连接词，单独出现时分类 casual
_CASUAL_TOKENS = frozenset([
    "哈哈", "哈哈哈", "嘻嘻", "嘻嘻嘻", "喔喔", "喔喔喔",
    "嗨嗨", "喔", "嗯", "嗯嗯", "啊啊", "啊啊啊",
    "强", "酷", "赞", "克", "ok", "OK", "ok~",
    "好哟", "好呀", "好的", "形的", "拿到了", "明白了",
    "喦", "哎", "这样啊", "原来", "好的好的",
])

_QUESTION_RE = re.compile(r"[??❓❔⁇⁈⁉]")
_EMOJI_RE = re.compile(r"[\U00010000-\U0010ffff\u2600-\u26FF\u2700-\u27BF\U0001F300-\U0001FAFF]", re.UNICODE)


def _heuristic_classify(msgs: List["IncomingMessage"]) -> Optional[str]:
    """Fast-path 分类，避免 AI 调用。

    - casual : 每条消息都是纯情绪/表情
    - combined: 所有消息无实质问题且平均长度很短
    - None   : 需要 AI 判断
    """
    texts = [m.text.strip() for m in msgs]
    # 全部都是超短词 / 气口词 / emoji
    def _is_casual_token(t: str) -> bool:
        t_lower = t.lower().rstrip("~〜～..").rstrip()
        if t_lower in _CASUAL_TOKENS:
            return True
        if len(t) <= 3 and not _QUESTION_RE.search(t):
            pure_emoji = _EMOJI_RE.sub("", t).strip()
            if not pure_emoji:  # 全是 emoji
                return True
        return False

    if all(_is_casual_token(t) for t in texts):
        return "casual"

    return None  # 需要 AI


logger = logging.getLogger(__name__)


@dataclass
class MsgGroup:
    msgs: List[IncomingMessage]
    topic: str
    reply_to: IncomingMessage  # 引用回复的目标气泡（该组最后一条）
    combined_text: str = ""    # 给 AI 的合并输入文本


@dataclass
class MultiMsgAnalysis:
    mode: str  # "casual" | "combined" | "multi_intent"
    groups: List[MsgGroup] = field(default_factory=list)


async def analyze_multi_msg(
    msgs: List[IncomingMessage],
    ai_client: Any,
) -> MultiMsgAnalysis:
    """用 AI 分析连发消息意图，返回分类与分组结果。

    ai_client 需支持 `.chat(prompt, strategy_overrides=...)` 接口。
    任何异常自动 fallback 为 combined（最安全）。
    """
    if not msgs:
        return MultiMsgAnalysis(mode="combined", groups=[])

    if len(msgs) == 1:
        m = msgs[0]
        return MultiMsgAnalysis(
            mode="combined",
            groups=[MsgGroup(msgs=msgs, topic=m.text[:30], reply_to=m, combined_text=m.text)],
        )

    # ★ Heuristic fast-path：明显 casual/combined 直接返回，省去 AI 分类步骤（节约 ~12s）
    _heuristic = _heuristic_classify(msgs)
    if _heuristic == "casual":
        last = msgs[-1]
        logger.debug("[multi_msg] heuristic=casual msgs=%d", len(msgs))
        return MultiMsgAnalysis(
            mode="casual",
            groups=[MsgGroup(msgs=[last], topic="casual", reply_to=last, combined_text=last.text)],
        )
    if _heuristic == "combined":
        combined_text = "\n".join(m.text for m in msgs)
        logger.debug("[multi_msg] heuristic=combined msgs=%d", len(msgs))
        return MultiMsgAnalysis(
            mode="combined",
            groups=[MsgGroup(msgs=msgs, topic="", reply_to=msgs[-1], combined_text=combined_text)],
        )

    numbered = "\n".join(f"{i + 1}. 「{m.text}」" for i, m in enumerate(msgs))

    prompt = (
        "你是对话分析助手，只做分析，不回复用户。\n"
        "分析下面用户连续发送的消息，判断它们的意图关系，输出严格 JSON（不要 markdown 代码块）。\n\n"
        f"消息列表（按发送顺序）：\n{numbered}\n\n"
        "分类规则：\n"
        '- "casual"：纯情绪/感叹/表情/极短词（好哦/哈哈/嗯/😊/呵呵），直接回最新一条\n'
        '- "combined"：多条合起来才表达一个完整意思，或者是同一句话分行发出\n'
        '- "multi_intent"：包含 2 个以上明显不同的问题或话题，需要逐条针对性回复\n\n'
        "输出示例（multi_intent）：\n"
        '{"mode":"multi_intent","groups":['
        '{"indices":[0,1],"topic":"约今天吃饭"},'
        '{"indices":[2],"topic":"询问上次那件事"}'
        "]}\n\n"
        "输出示例（casual）：\n"
        '{"mode":"casual"}\n\n'
        "输出示例（combined）：\n"
        '{"mode":"combined","topic":"整体意图摘要"}\n\n'
        "只输出 JSON，不要任何解释。"
    )

    try:
        raw = await asyncio.wait_for(
            ai_client.chat(prompt, strategy_overrides={"temperature": 0.05, "max_tokens": 300}),
            timeout=12.0,
        )
        raw = (raw or "").strip()
        m_json = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m_json:
            raise ValueError(f"no JSON in response: {raw[:120]}")
        data = json.loads(m_json.group())
        mode = data.get("mode", "combined")

        if mode == "casual":
            last = msgs[-1]
            return MultiMsgAnalysis(
                mode="casual",
                groups=[MsgGroup(msgs=[last], topic="casual", reply_to=last, combined_text=last.text)],
            )

        if mode == "combined":
            topic = data.get("topic", "")
            combined = "\n".join(m.text for m in msgs)
            return MultiMsgAnalysis(
                mode="combined",
                groups=[MsgGroup(msgs=msgs, topic=topic, reply_to=msgs[-1], combined_text=combined)],
            )

        # multi_intent
        groups: List[MsgGroup] = []
        for g in data.get("groups", []):
            idxs = [i for i in (g.get("indices") or []) if 0 <= i < len(msgs)]
            if not idxs:
                continue
            grp_msgs = [msgs[i] for i in idxs]
            combined_text = "\n".join(m.text for m in grp_msgs)
            groups.append(MsgGroup(
                msgs=grp_msgs,
                topic=g.get("topic", ""),
                reply_to=grp_msgs[-1],
                combined_text=combined_text,
            ))
        if not groups:
            combined = "\n".join(m.text for m in msgs)
            return MultiMsgAnalysis(
                mode="combined",
                groups=[MsgGroup(msgs=msgs, topic="", reply_to=msgs[-1], combined_text=combined)],
            )
        return MultiMsgAnalysis(mode="multi_intent", groups=groups)

    except asyncio.TimeoutError:
        logger.warning("[multi_msg] classify timeout, fallback combined")
    except Exception as e:
        logger.warning("[multi_msg] classify failed (%s), fallback combined", e)

    combined = "\n".join(m.text for m in msgs)
    return MultiMsgAnalysis(
        mode="combined",
        groups=[MsgGroup(msgs=msgs, topic="", reply_to=msgs[-1], combined_text=combined)],
    )
