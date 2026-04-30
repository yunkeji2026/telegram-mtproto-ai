from __future__ import annotations

from src.integrations.messenger_rpa.chat_reader import PeerMessage
from src.integrations.messenger_rpa.runner import MessengerRpaRunner


class _AI:
    def _detect_message_language(self, text: str) -> str:
        if "こんにちは" in text:
            return "ja"
        if "hola" in text.lower():
            return "es"
        if text.lower().strip() in {"ok", "hello"}:
            return "en"
        return "zh"


class _SM:
    ai_client = _AI()
    _context_store = None


def _runner(cfg: dict) -> MessengerRpaRunner:
    r = object.__new__(MessengerRpaRunner)
    r._cfg = cfg
    r._sm = _SM()
    return r


def test_pick_reply_profile_by_peer_name():
    r = _runner(
        {
            "reply_profiles": {
                "default": "warm",
                "profiles": [
                    {"id": "warm", "style_hint": "default"},
                    {
                        "id": "vip",
                        "match_names": ["Alice"],
                        "style_hint": "vip tone",
                    },
                ],
            }
        }
    )

    picked = r._pick_reply_profile("messenger_rpa:1", "Alice Chen")
    assert picked["id"] == "vip"


def test_reply_lang_ignores_local_chinese_media_label_and_uses_profile():
    r = _runner({"default_reply_lang": "zh"})
    msg = PeerMessage(
        role="peer",
        kind="image",
        content="",
        desc="smiling selfie",
        raw='{"kind":"image"}',
    )

    lang = r._resolve_reply_lang(
        peer_msg=msg,
        text_for_ai="[图片：smiling selfie]",
        chat_key="messenger_rpa:alice",
        profile={"language": "ja"},
    )

    assert lang == "ja"


def test_reply_lang_detects_current_text_language():
    r = _runner({"default_reply_lang": "zh"})
    msg = PeerMessage(
        role="peer",
        kind="text",
        content="こんにちは",
        desc="",
        raw="こんにちは",
    )

    lang = r._resolve_reply_lang(
        peer_msg=msg,
        text_for_ai="こんにちは",
        chat_key="messenger_rpa:alice",
        profile={"language": "auto"},
    )

    assert lang == "ja"
