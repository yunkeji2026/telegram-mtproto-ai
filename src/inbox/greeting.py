"""
R1 — 智能问候触发器

功能：
  - 新会话第一条消息进入时，自动选择合适的问候回复草稿
  - 选择策略：时段（morning/afternoon/evening/night）+ 语言 + 节假日感知
  - 零 LLM，纯规则 + 模板库检索，<1ms 响应
  - 生成的草稿注入 DraftService.auto_generate_draft 流程，支持 L2（自动发送）

时段划分（本地小时）：
  morning   06:00-11:59  → "早上好"
  afternoon 12:00-17:59  → "下午好"
  evening   18:00-21:59  → "晚上好"
  night     22:00-05:59  → 通用问候（不说"夜里好"会尴尬）
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# ── 内置问候语（兜底，无模板库时使用）───────────────────────────
_GREETINGS: Dict[str, Dict[str, str]] = {
    "zh": {
        "morning":   "早上好！感谢您联系我们，我是客服助手，请问有什么可以帮您？😊",
        "afternoon": "下午好！感谢您联系我们，请问有什么可以帮您？",
        "evening":   "晚上好！感谢您的联系，请问有什么需要帮助的吗？",
        "night":     "您好！感谢您联系我们，请问有什么可以为您服务？",
    },
    "en": {
        "morning":   "Good morning! Thank you for reaching out. How can I help you today? 😊",
        "afternoon": "Good afternoon! Thank you for contacting us. How may I assist you?",
        "evening":   "Good evening! Thank you for reaching out. How can I help you?",
        "night":     "Hello! Thank you for contacting us. How may I assist you?",
    },
    "ja": {
        "morning":   "おはようございます！お問い合わせありがとうございます。",
        "afternoon": "こんにちは！お問い合わせありがとうございます。",
        "evening":   "こんばんは！お問い合わせありがとうございます。",
        "night":     "こんにちは！お問い合わせありがとうございます。",
    },
    "ko": {
        "morning":   "안녕하세요! 문의해 주셔서 감사합니다. 어떻게 도와드릴까요?",
        "afternoon": "안녕하세요! 문의해 주셔서 감사합니다. 어떻게 도와드릴까요?",
        "evening":   "안녕하세요! 문의해 주셔서 감사합니다.",
        "night":     "안녕하세요! 문의해 주셔서 감사합니다.",
    },
}
_GREETINGS_DEFAULT = _GREETINGS["zh"]  # fallback


def get_time_slot(hour: Optional[int] = None) -> str:
    """根据本地小时返回时段标签（morning/afternoon/evening/night）。"""
    if hour is None:
        hour = time.localtime().tm_hour
    if 6 <= hour < 12:
        return "morning"
    elif 12 <= hour < 18:
        return "afternoon"
    elif 18 <= hour < 22:
        return "evening"
    else:
        return "night"


def select_greeting_text(
    lang: str,
    time_slot: str,
    templates_store=None,
    *,
    custom_scene: str = "greeting",
) -> str:
    """选择最合适的问候文本。

    优先从 reply_templates 库中按 scene=greeting + language 检索；
    若无匹配则降级到内置多语言问候语。
    """
    # 1. 尝试从模板库检索（J2 reply_templates）
    if templates_store is not None:
        try:
            tpls = templates_store.list_templates(
                language=lang, scene=custom_scene, limit=5
            )
            if not tpls:
                # 通用场景 fallback
                tpls = templates_store.list_templates(scene=custom_scene, limit=5)
            if tpls:
                # 优先选包含时段关键词的模板
                slot_kws = {
                    "morning":   ["早上好", "morning", "早安"],
                    "afternoon": ["下午好", "afternoon"],
                    "evening":   ["晚上好", "evening", "晚安"],
                    "night":     ["您好", "hello", "hi"],
                }
                kws = slot_kws.get(time_slot, [])
                for tpl in tpls:
                    content = str(tpl.get("content") or "")
                    if any(kw.lower() in content.lower() for kw in kws):
                        return content
                # 无匹配时段的模板，取第一个
                return str(tpls[0].get("content") or "")
        except Exception:
            pass  # 降级到内置

    # 2. 内置问候语
    lang_key = lang.lower()[:2]
    slot_map = _GREETINGS.get(lang_key, _GREETINGS.get("zh", {}))
    return slot_map.get(time_slot, _GREETINGS_DEFAULT.get("night", "您好！"))


def should_auto_greet(
    conv_meta: Optional[Dict[str, Any]],
    *,
    enabled: bool = True,
) -> bool:
    """判断是否应触发自动问候：第一条消息（msg_count <= 0 且无历史意图）。

    conv_meta 为 None（全新会话）时也触发。
    """
    if not enabled:
        return False
    if conv_meta is None:
        return True  # 完全新会话
    msg_count = int(conv_meta.get("msg_count") or 0)
    intent_hist = conv_meta.get("intent_history") or []
    # 第一条消息 且 无意图历史 → 触发问候
    return msg_count <= 1 and len(intent_hist) <= 1


def build_greeting_draft(
    conv: Dict[str, Any],
    lang: str,
    *,
    time_slot: Optional[str] = None,
    templates_store=None,
    automation_mode: str = "review",
) -> Dict[str, Any]:
    """构建问候草稿 dict（传给 InboxStore.upsert_draft）。

    参数：
        conv:  会话 dict（含 conversation_id, platform, account_id 等）
        lang:  客户语言（detect_language 的返回值）
        time_slot:  可选，不传则自动获取
        templates_store:  InboxStore（用于 list_templates）
        automation_mode:  "review" / "auto_ai"
    """
    from src.inbox.drafts import risk_to_autopilot

    slot = time_slot or get_time_slot()
    text = select_greeting_text(lang, slot, templates_store)

    # 问候属于低风险，默认 L2（review 模式）或 L3（auto_ai 模式）
    autopilot = risk_to_autopilot("low", automation_mode)

    conv_id = str(conv.get("conversation_id") or "")
    return {
        "source_kind":     "inbox",
        "source_id":       f"greet_{conv_id}",  # 保证幂等：同一会话只一条问候草稿
        "conversation_id": conv_id,
        "platform":        str(conv.get("platform") or ""),
        "account_id":      str(conv.get("account_id") or "default"),
        "chat_key":        str(conv.get("chat_key") or ""),
        "draft_text":      text,
        "draft_lang":      lang,
        "risk_level":      "low",
        "risk_reasons":    [],
        "autopilot_level": autopilot,
        "status":          "pending",
        "source_label":    "auto_greeting",
    }
