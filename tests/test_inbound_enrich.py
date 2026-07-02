"""inbound_enrich — 收件箱入站上下文补全单测。"""

from src.inbox.inbound_enrich import (
    apply_inbound_enrichments,
    build_language_switch_hint,
    build_short_inbound_hint,
    peer_media_context,
)


def test_peer_media_sticker_placeholder():
    ctx = peer_media_context("[贴纸]")
    assert ctx["_peer_message_is_media"] is True
    assert ctx["_media_kind"] == "sticker"


def test_peer_media_image_content_desc():
    ctx = peer_media_context("[图片内容] 宝宝在笑", media_type="image")
    assert ctx["_media_desc"] == "宝宝在笑"


def test_language_switch_hint_en_to_ja():
    hist = [
        {"role": "user", "content": "How are you today?"},
        {"role": "assistant", "content": "Hey, good!"},
    ]
    hint = build_language_switch_hint(
        hist, current_lang="ja", current_text="私も悪くないよ",
    )
    assert "日语" in hint
    assert "英语" in hint


def test_short_inbound_hint_interjection():
    assert "语气词" in build_short_inbound_hint("嗯嗯")


def test_apply_inbound_enrichments_sets_media_and_short():
    uc: dict = {}
    apply_inbound_enrichments(
        uc, text="Hi", history=[], reply_lang="en", platform="telegram",
    )
    assert uc["last_message"] == "Hi"
    assert uc["_current_user_message_for_lang"] == "Hi"
    assert "极短英文" in uc.get("_inbound_short_hint", "")


def test_apply_sticker_media_patch():
    uc: dict = {}
    apply_inbound_enrichments(uc, text="[贴纸]", platform="telegram")
    assert uc["_peer_message_is_media"] is True
    assert uc["_media_kind"] == "sticker"
