"""优化 A — spam HIGH/LOW 分级判定单测。

回归保护：
- HIGH 关键词必须返 ("high", kw)
- LOW 关键词必须返 ("low", kw)
- 历史误判 case：「Hi. I'm right here Saw you sent a bunch of messages」不该命中
- is_likely_spam 向后兼容（HIGH/LOW 都返 True，无命中返 False）
"""

from __future__ import annotations

import pytest

from src.integrations.messenger_rpa.chat_reader import PeerMessage


def _msg(text: str) -> PeerMessage:
    return PeerMessage(role="peer", kind="text", content=text, desc="", raw=text)


# ── HIGH-confidence ───────────────────────────────────────


def test_high_gambling_brand_fc8win():
    hit, level, kw = _msg("Try fc8win for big payout").spam_match()
    assert hit and level == "high" and kw == "fc8win"


def test_high_gambling_zh():
    hit, level, kw = _msg("快来投注").spam_match()
    assert hit and level == "high" and kw == "投注"


def test_high_telegram_link():
    hit, level, kw = _msg("Add me on https://t.me/abc").spam_match()
    assert hit and level == "high"
    assert kw == "https://t.me/"


def test_high_whatsapp_short_link():
    hit, level, kw = _msg("contact wa.me/12345 fast").spam_match()
    assert hit and level == "high" and kw == "wa.me/"


def test_high_promo_param():
    hit, level, kw = _msg("https://abc.cc/?id=12345").spam_match()
    assert hit and level == "high"
    assert kw == ".cc/?id="


# ── LOW-confidence ────────────────────────────────────────


def test_low_check_my():
    hit, level, kw = _msg("Hi check my page later").spam_match()
    assert hit and level == "low" and kw == "check my"


def test_low_bonus():
    hit, level, _ = _msg("got a bonus today").spam_match()
    assert hit and level == "low"


def test_low_win_with_space():
    hit, level, _ = _msg("you can win big").spam_match()
    assert hit and level == "low"


def test_low_register_now():
    hit, level, kw = _msg("click register now to get free").spam_match()
    assert hit and level == "low"


# ── 历史误判 case：さとう たかひろ 类消息不该命中 ─────────


def test_satou_japanese_normal_message_not_spam():
    """関 さとう たかひろ 这种正常日文消息不应命中任何 spam keyword。"""
    txt = "Hi. I'm right here Saw you sent a bunch of messages, did the test go smoothly?"
    hit, level, kw = _msg(txt).spam_match()
    assert not hit, f"误判：level={level} kw={kw} txt={txt!r}"


def test_japanese_greeting_not_spam():
    hit, level, _ = _msg("こんにちは、調子はどうですか？").spam_match()
    assert not hit


def test_japanese_with_kanji_not_spam():
    hit, level, _ = _msg("今日は良い天気ですね").spam_match()
    assert not hit


def test_normal_english_inquiry_not_spam():
    hit, level, _ = _msg("Can you tell me the order status please?").spam_match()
    assert not hit


# ── HIGH 优先于 LOW（同时含两类时返 high）──────────────


def test_high_takes_precedence_over_low():
    # 含 LOW "bonus" 也含 HIGH "投注"
    hit, level, kw = _msg("special bonus 投注").spam_match()
    assert hit and level == "high" and kw == "投注"


# ── 向后兼容 is_likely_spam ───────────────────────────────


def test_is_likely_spam_returns_true_for_high():
    assert _msg("fc8win").is_likely_spam is True


def test_is_likely_spam_returns_true_for_low():
    assert _msg("check my page").is_likely_spam is True


def test_is_likely_spam_returns_false_for_normal():
    assert _msg("hello, how are you").is_likely_spam is False


# ── 边界 ──────────────────────────────────────────────────


def test_empty_text_not_spam():
    hit, level, _ = _msg("").spam_match()
    assert not hit and level == ""


def test_whitespace_only_not_spam():
    hit, level, _ = _msg("   ").spam_match()
    assert not hit
