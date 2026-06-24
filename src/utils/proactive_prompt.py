"""主动外发文案生成 prompt 组装（确定性纯函数，可单测）。

把「要发出去的那一句」的 prompt 拼装从 main.py 闭包里抽出来——既可单测，也修掉 Stage L
引入的**框定错配**：此前无论沉默回访还是每日仪式，都套同一句「正在主动给一位**许久未联系**的
朋友发消息」。对晨/晚安这种**每天到点的日常问候**，这个「久别重逢」框定会把文案带偏
（生成出「好久不见」式的生分感）。本模块按 ``plan.mode`` 给出贴合的框定：
- ``ritual_morning`` / ``ritual_night`` → 「每天都会惦记 TA 的人，发一句平常的早/晚安」
- 其余（follow_up / gentle_checkin / story_*）→ 「主动给许久未联系的朋友发消息」

只拼 prompt、零 IO、不调 AI；真实文案由上层把本串喂给 ``ai_client.chat`` 产出。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_RITUAL_SLOT_LABEL = {"ritual_morning": "早安", "ritual_night": "晚安"}


def build_proactive_prompt(
    ai_name: str,
    plan: Dict[str, Any],
    *,
    recent_context: str = "",
    few_shot_block: str = "",
) -> str:
    """组装主动外发文案生成 prompt（按 mode 自适应框定）。绝不抛。

    Args:
        ai_name: AI 人设名。
        plan: 发送计划，至少含 ``directive``；可选 ``mode`` / ``context_facts``。
        recent_context: 最近聊天上下文（已截断），供参考口吻，可空。
        few_shot_block: 人工认可样本拼成的风格示范块（见 build_few_shot_block），可空。
    """
    name = str(ai_name or "她")
    plan = plan or {}
    mode = str(plan.get("mode") or "")
    directive = str(plan.get("directive") or "")
    is_ritual = mode.startswith("ritual_")

    if is_ritual:
        slot = _RITUAL_SLOT_LABEL.get(mode, "问候")
        header = (
            f"你是「{name}」，正在像一个每天都会惦记着TA的人那样，给TA发一句平常的"
            f"{slot}问候——不是久别重逢，就是日常里每天一句的牵挂。"
        )
        length = "不超过30字"
    else:
        header = f"你是「{name}」，正在主动给一位许久未联系的朋友发消息。"
        length = "不超过40字"

    prompt = (
        f"{header}\n{directive}\n"
        f"要求：只输出要发出去的那一句话本身，口语化、温暖、自然，{length}，"
        f"不要解释、不要加引号、不要署名。\n"
    )

    facts = [
        str(f).strip() for f in (plan.get("context_facts") or []) if str(f).strip()
    ]
    if facts:
        prompt += (
            "\n（背景：你还记得关于TA的这些事，仅用来把这一句说得更走心，"
            "绝不要罗列、不要逐条追问）：\n- " + "\n- ".join(facts[:3]) + "\n"
        )
    if recent_context:
        prompt += f"\n（可参考你们最近的聊天，但不要复读原话）：\n{recent_context}\n"
    if few_shot_block:
        prompt += few_shot_block
    return prompt


__all__ = ["build_proactive_prompt"]
