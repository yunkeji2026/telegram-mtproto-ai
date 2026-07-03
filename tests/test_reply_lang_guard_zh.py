"""回复语言守卫：目标中文却窜英文 → 判为不符（真机 bug 回归）。

历史缺陷：``_reply_lang_mismatch`` 对 expected='zh' 一律返回 False、``_guard_reply_language``
对 reply_lang='zh' 直接放行 → 中文会话里模型窜整段英文时**从不纠正**。此门禁锁定修复:
zh 目标 + 明显英文(几乎无中文) → 判不符；正常中文/中英混说 → 不误伤。
"""
from src.ai.ai_client import AIClient


def _mm(reply, lang):
    return AIClient._reply_lang_mismatch(reply, lang)


def test_zh_target_english_reply_is_mismatch():
    assert _mm("I was just cooking some instant noodles. What about you?", "zh") is True


def test_zh_target_chinese_reply_ok():
    assert _mm("刚做完饭，汤有点辣哈哈", "zh") is False


def test_zh_target_mixed_cjk_english_not_flagged():
    # 中英混说但有中文 → 不算窜英文（零误伤）
    assert _mm("哈哈 ok 啦，那就这样定咯", "zh") is False


def test_zh_target_short_english_word_ok():
    # 短英文词夹在中文里 / 纯短英文不足以判定 → 不误伤
    assert _mm("好的 ok", "zh") is False


def test_non_zh_unchanged():
    # 目标英文却回纯中文仍是不符（原有行为不变）
    assert _mm("你好，很高兴认识你，我们可以多聊聊天气和生活", "en") is True
    assert _mm("Hey, nice to meet you!", "en") is False
