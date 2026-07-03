"""入站消息 → 统一草稿引擎上下文补全（媒体 / 短消息 / 多语言切换）。

Messenger/WhatsApp RPA 在 runner 层注入 ``_peer_message_is_media`` 等字段；
Telegram 收件箱 auto-draft 此前只传纯 text，导致已开发的「像真人」规则栈
（多模态回应、短消息镜像、语言跟随）在全自动路径上形同未启用。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src.integrations.protocol_bridge import media_placeholder
from src.skills.skill_manager import _is_meaningless_interjection_only

# ingest / Telegram 侧占位符 → ai_client 理解的 media kind
_PLACEHOLDER_KIND: Dict[str, str] = {
    "[贴纸]": "sticker",
    "[动态表情]": "gif",
    "[图片]": "image",
    "[语音]": "voice",
    "[视频]": "video",
    "[文件]": "file",
    "[媒体]": "media",
}

_IMAGE_CONTENT_RE = re.compile(r"^\[图片内容\]\s*(.+)", re.DOTALL)
_IMAGE_MSG_RE = re.compile(r"^\[图片消息[^\]]*\]")
_STICKER_CONTENT_RE = re.compile(r"^\[贴纸内容\]\s*(.+)", re.DOTALL)


def _kind_from_text(text: str) -> str:
    t = (text or "").strip()
    if t in _PLACEHOLDER_KIND:
        return _PLACEHOLDER_KIND[t]
    if _IMAGE_CONTENT_RE.match(t) or _IMAGE_MSG_RE.match(t):
        return "image"
    if _STICKER_CONTENT_RE.match(t):
        return "sticker"
    if t.startswith("[语音"):
        return "voice"
    return ""


def peer_media_context(
    text: str,
    *,
    media_type: str = "",
    media_ref: str = "",
    media_desc: str = "",
) -> Dict[str, Any]:
    """从入站文本 + 可选媒体字段构造 ``user_context`` 媒体补丁。"""
    t = (text or "").strip()
    kind = str(media_type or "").strip().lower() or _kind_from_text(t)
    desc = (media_desc or "").strip()
    if not desc:
        m = _IMAGE_CONTENT_RE.match(t)
        if m:
            desc = (m.group(1) or "").strip()
    if not desc:
        m = _STICKER_CONTENT_RE.match(t)
        if m:
            desc = (m.group(1) or "").strip()
    if not kind and not desc and not media_ref:
        return {}

    out: Dict[str, Any] = {
        "_peer_message_is_media": True,
        "_media_kind": kind or "media",
    }
    if desc:
        out["_media_desc"] = desc
    if media_ref:
        out["_media_ref"] = str(media_ref)
    # Telegram 收件箱与原生 bot 共用 channel 标记，供 ai_client 多模态 prompt
    out["_inbox_peer_kind"] = kind or "media"
    return out


def build_language_switch_hint(
    history: List[Dict[str, Any]],
    *,
    current_lang: str,
    current_text: str,
) -> str:
    """近几轮用户语系与本轮不同 → 提示模型像真人一样自然跟上（不解释规则）。"""
    from src.ai.translation_service import detect_language

    # 本条到底是什么语种，以**当前文本实际检测**为准——不能只信传入的 current_lang
    # （那是 reply_lang，可能被上一轮锁成 en 等而与本条文本矛盾）。否则会出现"用户明明
    # 说中文，却被提示'突然换成英语啦'"的误判（真机语音场景实测复现）。
    text_lang = (detect_language(current_text) or "").strip()
    cur = text_lang if (text_lang and text_lang != "unknown") else (current_lang or "").strip()
    if not cur or cur in ("unknown", "zh"):
        return ""
    # 一致性护栏：传入 current_lang 与文本实际语种矛盾时，以文本为准（文本已非 zh/unknown）。
    if text_lang and text_lang != "unknown" and text_lang != cur:
        return ""
    prev_langs: List[str] = []
    for m in reversed(history or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = str(m.get("content") or "").strip()
        if not c or c == (current_text or "").strip():
            continue
        lg = detect_language(c)
        if lg and lg not in ("unknown",):
            prev_langs.append(lg)
        if len(prev_langs) >= 3:
            break
    if not prev_langs:
        return ""
    dominant = prev_langs[0]
    if dominant == cur:
        return ""
    _names = {
        "en": "英语", "ja": "日语", "ko": "韩语", "zh": "中文",
        "es": "西语", "pt": "葡语", "vi": "越南语", "th": "泰语",
    }
    prev_n = _names.get(dominant, dominant)
    cur_n = _names.get(cur, cur)
    return (
        f"【语言切换 · 自然承接】对方刚才主要用「{prev_n}」聊，本条改用了「{cur_n}」。"
        f"请用「{cur_n}」回复，并像真人一样可轻轻点一下这个切换"
        "（例如“突然换成日语啦？”这种自然反应，按语境决定，不要生硬解释语言规则）；"
        "然后直接接住本条内容，保持一致的自然私聊感。"
    )


def build_short_inbound_hint(text: str) -> str:
    """极短 / 纯语气 / 纯 emoji 入站 → Companion 短回提示（补充 natural_dialogue）。"""
    t = (text or "").strip()
    if not t:
        return (
            "【对方本轮几乎无文字（可能只有表情/贴纸）】"
            "用一两句轻松口语回应氛围即可，不要长篇；可轻轻接梗或问一句很短的跟进。"
        )
    if _is_meaningless_interjection_only(t):
        return (
            "【对方本轮偏语气词/填充音】"
            "像朋友聊天那样短回即可（嗯嗯/哈哈/怎么啦），不要展开成客服式长段或连环提问。"
        )
    # 纯 emoji（去掉 emoji 后无字母数字汉字）
    core = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F"
        r"\U0001F1E0-\U0001F1FF\s]+",
        "",
        t,
    )
    if not core and len(t) <= 12:
        return (
            "【对方本轮主要是表情符号】"
            "回应时宜轻松简短，可回表情或一句口语，不要假装读不懂表情。"
        )
    if len(t) <= 3 and re.search(r"[a-zA-Z]", t):
        return (
            "【对方本轮极短英文】"
            "用同样简短的私聊口吻回应（如 hi→hey / ok→好呀），不要突然变成长篇客服腔。"
        )
    return ""


def apply_inbound_enrichments(
    user_context: Dict[str, Any],
    *,
    text: str,
    history: Optional[List[Dict[str, Any]]] = None,
    reply_lang: str = "",
    media_type: str = "",
    media_ref: str = "",
    media_desc: str = "",
    platform: str = "",
) -> None:
    """就地补全 user_context（供 generate_inbox_draft 调用）。"""
    t = str(text or "").strip()
    user_context["last_message"] = t
    user_context["_current_user_message_for_lang"] = t
    if platform:
        user_context["platform"] = platform
        if platform == "telegram":
            user_context["channel"] = "telegram"

    media_patch = peer_media_context(
        t, media_type=media_type, media_ref=media_ref, media_desc=media_desc,
    )
    user_context.update(media_patch)

    hint = build_language_switch_hint(
        list(history or []),
        current_lang=reply_lang,
        current_text=t,
    )
    if hint:
        user_context["_topic_switch_hint"] = hint

    short_hint = build_short_inbound_hint(t)
    if short_hint:
        prev = (user_context.get("_inbound_short_hint") or "").strip()
        user_context["_inbound_short_hint"] = f"{prev}\n{short_hint}".strip() if prev else short_hint


__all__ = [
    "apply_inbound_enrichments",
    "build_language_switch_hint",
    "build_short_inbound_hint",
    "peer_media_context",
    "media_placeholder",
]
