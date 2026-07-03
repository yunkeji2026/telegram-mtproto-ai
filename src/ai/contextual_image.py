"""Stage B：对话上下文「按需生图」——判断该不该生图 + 产出生图 API 的关键词(prompt)。

与 Stage A（人设自拍，``companion_selfie``）分工：
  - Stage A：对方想看「你(人设)长什么样」→ 人设肖像（可用相册基础图 img2img 锁脸）。
  - Stage B（本模块）：对方想看「对话里提到的东西」，如"你煮的面拍张照给我看"——
    从上下文抽出主体(面) → 组 text2img 关键词(prompt)。

本模块是**纯逻辑**（可单测、离线、无副作用）：意图判断 / 主体抽取 / prompt 构造都是纯函数；
真正出图/发送在 ``SelfieProvider`` + ``skill_manager``。强制 SFW（安全约束写进 prompt）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 复用 Stage A 定义的"你煮的/你做的…"标记（单一事实来源，避免两处漂移）。
from src.ai.companion_selfie import _OBJECT_PHOTO_MARKERS

IMAGE_KIND_OBJECT = "object"

# 泛化的"要一张图"信号（简繁 + 英文）。仅在已命中 _OBJECT_PHOTO_MARKERS 后作二次确认，
# 故这些较宽的词（看看/send）不会单独误触发。
_PHOTO_CUES = (
    "拍张", "拍張", "拍个", "拍個", "拍一", "拍下", "拍照", "拍给我", "拍給我", "拍来", "拍來",
    "照片", "相片", "发张图", "發張圖", "发个图", "發個圖", "发张照", "發張照", "来张", "來張",
    "给我看看", "給我看看", "看一下", "看一看", "看看", "晒", "曬",
    "photo", "pic", "picture", "image", "show me", "send me",
)

# 常见"可拍主体"中→英映射（生图 prompt 用英文更稳；缺失回落原词）。
_SUBJECT_CN2EN = {
    "面": "a bowl of noodles", "面条": "a bowl of noodles", "麵": "a bowl of noodles",
    "麵條": "a bowl of noodles", "拉面": "a bowl of ramen", "拉麵": "a bowl of ramen",
    "饭": "a bowl of rice", "米饭": "a bowl of rice", "炒饭": "fried rice", "蛋炒饭": "egg fried rice",
    "汤": "a bowl of soup", "湯": "a bowl of soup", "粥": "a bowl of congee",
    "蛋糕": "a slice of cake", "面包": "bread", "麵包": "bread", "饼干": "cookies",
    "饺子": "dumplings", "餃子": "dumplings", "包子": "steamed buns", "馒头": "steamed buns",
    "火锅": "a hotpot", "火鍋": "a hotpot", "烧烤": "barbecue", "燒烤": "barbecue",
    "咖啡": "a cup of coffee", "奶茶": "a cup of milk tea", "茶": "a cup of tea",
    "菜": "a home-cooked dish", "早餐": "breakfast", "晚餐": "dinner", "午餐": "lunch",
    "沙拉": "a salad", "披萨": "a pizza", "披薩": "a pizza", "寿司": "sushi", "壽司": "sushi",
    "水果": "a plate of fruit", "甜点": "a dessert", "甜點": "a dessert", "冰淇淋": "ice cream",
    "花": "a bouquet of flowers", "猫": "a cat", "貓": "a cat", "狗": "a dog",
    "风景": "a scenic view", "風景": "a scenic view", "天空": "the sky", "海": "the sea",
    # 非食物但对方常要看的东西（衣物/礼物/宠物/场景）。
    "裙子": "a dress", "衣服": "an outfit", "鞋子": "a pair of shoes", "鞋": "a pair of shoes",
    "帽子": "a hat", "包包": "a handbag", "口红": "a lipstick", "礼物": "a gift", "禮物": "a gift",
    "书": "a book", "書": "a book", "房间": "a cozy room", "房間": "a cozy room",
    "宠物": "a pet", "寵物": "a pet", "小猫": "a kitten", "小狗": "a puppy",
}

# 已知主体词表，按长度降序 → 先匹配更长的词（"炒饭"先于"饭"、"奶茶"先于"茶"）。
_KNOWN_SUBJECTS = sorted(_SUBJECT_CN2EN.keys(), key=len, reverse=True)


def detect_object_image_request(text: str) -> bool:
    """对方是否在要「对话里提到的东西」的照片（如"你煮的…拍张照给我看"）。

    双闸：须同时命中 ①"你煮的/你做的…"物体标记 与 ②"拍/照片/看看"要图信号，
    才判为上下文要图（避免把"你煮的真好吃"这类纯夸赞当成要图）。
    """
    t = str(text or "").strip().lower()
    if not t or len(t) > 300:
        return False
    if not any(m in t for m in _OBJECT_PHOTO_MARKERS):
        return False
    return any(c in t for c in _PHOTO_CUES)


def extract_image_subject(text: str, history: Optional[List[Dict[str, Any]]] = None) -> str:
    """在当前消息 + 最近历史里找"要拍的东西"（已知主体词，最稳）；抽不到回空串。

    先扫当前消息，再倒序扫历史（人设最近说过"煮了面"）；按词长降序匹配避免"炒饭"被"饭"抢先。
    词表未覆盖的主体交由可选的 LLM 精炼补足（见 ``build_llm_prompt_refine_instruction``），
    或回落通用兜底——用**词表匹配**而非语法解析，杜绝"你煮的**肯定**很好吃"把形容词误当主体。
    """
    blobs: List[str] = [str(text or "")]
    for msg in reversed(history or []):
        try:
            blobs.append(str((msg or {}).get("content") or ""))
        except Exception:
            continue
    for blob in blobs:
        if not blob:
            continue
        for k in _KNOWN_SUBJECTS:
            if k in blob:
                return k
    return ""


def build_object_image_prompt(
    subject: str, *, style: str = "", sfw: bool = True
) -> str:
    """把主体组成生图 API 关键词（英文更稳，缺映射回落原词）。强制 SFW。"""
    subj = str(subject or "").strip()
    base = _SUBJECT_CN2EN.get(subj) or subj or "a home-cooked dish"
    parts = [f"A realistic photo of {base}",
             "close-up, natural lighting, photorealistic, high quality, no text, no watermark"]
    st = str(style or "").strip()
    if st:
        parts.append(st)
    if sfw:
        parts.append("safe-for-work")
    return ", ".join(p for p in parts if p)


def plan_contextual_image(
    text: str,
    history: Optional[List[Dict[str, Any]]] = None,
    *,
    style: str = "",
) -> Optional[Dict[str, Any]]:
    """一站式：非上下文要图 → None；否则返回 ``{kind, subject, prompt, base_image}``。

    ``base_image`` 恒空（物体图走 text2img，不应带人设的脸）；出图/发送由 skill 层完成。
    """
    if not detect_object_image_request(text):
        return None
    subject = extract_image_subject(text, history)
    prompt = build_object_image_prompt(subject, style=style)
    return {
        "kind": IMAGE_KIND_OBJECT,
        "subject": subject,
        "prompt": prompt,
        "base_image": "",
    }


def build_llm_prompt_refine_instruction(
    text: str, history: Optional[List[Dict[str, Any]]] = None, *, max_turns: int = 6
) -> str:
    """（可选）构造一段让 LLM 产出英文生图 prompt 的指令——heuristic 抽不准时的增强。

    返回的指令交给 ``ai_client.chat`` 短调用；调用与解析在 skill 层（保持本模块纯净可测）。
    """
    lines: List[str] = []
    for msg in (history or [])[-max_turns:]:
        role = str((msg or {}).get("role") or "")
        content = str((msg or {}).get("content") or "").strip()
        if content:
            who = "AI" if role == "assistant" else "User"
            lines.append(f"{who}: {content[:120]}")
    lines.append(f"User: {str(text or '').strip()[:120]}")
    convo = "\n".join(lines)
    return (
        "You are helping a chat companion decide what photo to send. Based on the "
        "conversation below, the user is asking to see a photo of something that was "
        "mentioned (food, an object, a scene — NOT the companion's face). Write a single, "
        "concise English text-to-image prompt (comma-separated keywords) describing that "
        "thing. Keep it safe-for-work, photorealistic. Reply with ONLY the prompt, no quotes.\n\n"
        f"Conversation:\n{convo}\n\nPrompt:"
    )


__all__ = [
    "IMAGE_KIND_OBJECT",
    "detect_object_image_request",
    "extract_image_subject",
    "build_object_image_prompt",
    "plan_contextual_image",
    "build_llm_prompt_refine_instruction",
]
