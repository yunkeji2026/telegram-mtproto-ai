"""P0 表情自然化：禁止水印、会话冷却、跳过概率。"""
import pytest

from src.skills.emotion_enhancer import EmotionEnhancer


def _cfg(**nat):
    base = {
        "emoticons": {
            "emoticons": {
                "positive": ["😊", "👍"],
                "neutral": ["💭", "✨", "👉", "📝"],
                "negative": ["😔"],
            },
            "rules": {
                "max_emoticons_per_message": 3,
                "min_message_length_for_emoticon": 10,
            },
        }
    }
    if nat:
        base["emoticons"]["naturalization"] = nat
    return base


def test_forbidden_emoticons_filtered_even_if_in_pool():
    e = EmotionEnhancer(_cfg(enabled=True, forbidden_emoticons=["👉", "📝"]))
    out = e.enhance_reply(
        "这是一条长度足够的测试回复内容用于触发表情逻辑。",
        "neutral",
        {"suggested_emoticons": ["👉", "📝"]},
        "hello",
        chat_id="u1",
    )
    assert "👉" not in out
    assert "📝" not in out


def test_strip_watermark_from_model_edges():
    """模型原文在句首/句末带 👉📝 时，整段输出仍应剥除（非仅过滤待追加列表）。"""
    e = EmotionEnhancer(_cfg(enabled=True, forbidden_emoticons=["👉", "📝"]))
    raw = "👉 📝 这是一条长度足够的测试回复内容用于触发表情逻辑。"
    out = e.enhance_reply(raw, "neutral", {}, "hello", chat_id="u_wm")
    assert not out.startswith("👉")
    assert not out.endswith("📝")
    assert "👉" not in out
    assert "📝" not in out


def test_naturalization_disabled_allows_legacy_path():
    e = EmotionEnhancer(_cfg(enabled=False))
    out = e.enhance_reply(
        "这是一条长度足够的测试回复内容用于触发表情逻辑。",
        "neutral",
        {"suggested_emoticons": ["💭"]},
        "hello",
        chat_id="u2",
    )
    assert len(out) >= 10


def test_context_suggestions_replaced_not_watermark():
    """context 不再推荐 👉📝（由 context_manager 保证）"""
    from src.context.context_manager import ContextManager

    class _Cfg:
        config_path = None

    cm = ContextManager(_Cfg())
    cm.add_message("999", {"text": "随便聊一句足够长的内容用于测试表情建议", "username": "u"})
    ana = cm.analyze_context("999", "再发一条消息用于分析上下文情绪")
    sug = ana.get("suggested_emoticons") or []
    assert "👉" not in sug
    assert "📝" not in sug
