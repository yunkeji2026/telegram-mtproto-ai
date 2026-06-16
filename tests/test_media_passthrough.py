"""P61-1：媒体字段端到端贯通 契约测试（零行为变化安全网）。

锁定：平台 source 的媒体信息 → message_obj → ingest → store → 读回 全程不丢，
且纯文本消息行为完全不变。
"""

import os
import tempfile

from src.inbox.ingest import _msg_from_obj
from src.inbox.media_resolver import media_kind, resolve_for_translate, resolve_media_path
from src.inbox.models import InboxConversation
from src.inbox.normalizer import extract_media, message_obj
from src.inbox.store import InboxStore


# ── extract_media ────────────────────────────────────────────────────────
def test_extract_media_image_by_field():
    mt, ref = extract_media({"image_path": "/tmp/a.jpg"})
    assert mt == "image" and ref == "/tmp/a.jpg"


def test_extract_media_voice_by_field():
    mt, ref = extract_media({"voice_path": "/tmp/v.ogg"})
    assert mt == "voice" and ref == "/tmp/v.ogg"


def test_extract_media_explicit_type_and_ref():
    mt, ref = extract_media({"media_type": "image", "media_ref": "/x/y.png"})
    assert mt == "image" and ref == "/x/y.png"


def test_extract_media_plain_text_empty():
    assert extract_media({"text": "hello"}) == ("", "")
    assert extract_media(None) == ("", "")


# ── message_obj 携带（加法，零行为变化）────────────────────────────────────
def test_message_obj_carries_media_from_source():
    m = message_obj(text="", source={"image_path": "/tmp/p.png"})
    assert m["media_type"] == "image" and m["media_ref"] == "/tmp/p.png"


def test_message_obj_plain_text_has_empty_media_and_keeps_shape():
    m = message_obj(text="hello", ts=5, direction="in")
    assert m["media_type"] == "" and m["media_ref"] == ""
    # 既有字段不变
    for k in ("message_id", "direction", "text", "original_text", "translated_text",
              "language", "translation", "ts", "source"):
        assert k in m
    assert m["text"] == "hello" and m["translated_text"] == "hello"


def test_message_obj_explicit_media_overrides_source_extract():
    m = message_obj(text="", source={"image_path": "/a.png"}, media_type="voice", media_ref="/b.ogg")
    assert m["media_type"] == "voice" and m["media_ref"] == "/b.ogg"


# ── ingest 携带 ────────────────────────────────────────────────────────────
def test_msg_from_obj_carries_media():
    m = message_obj(text="看图", source={"image_path": "/tmp/x.jpg"})
    im = _msg_from_obj("telegram:default:c1", m, platform="telegram")
    assert im.media_type == "image" and im.media_ref == "/tmp/x.jpg"


def test_msg_from_obj_falls_back_to_source_media():
    # message dict 没有 media 字段，但 source 里有 → 仍能抽出
    raw = {"text": "", "direction": "in", "ts": 1, "source": {"voice_path": "/v.ogg"}}
    im = _msg_from_obj("telegram:default:c1", raw, platform="telegram")
    assert im.media_type == "voice" and im.media_ref == "/v.ogg"


# ── 端到端：source → obj → store → 读回 ────────────────────────────────────
def test_media_survives_store_roundtrip():
    store = InboxStore(":memory:")
    conv = InboxConversation(
        conversation_id="telegram:default:c1", platform="telegram",
        account_id="default", chat_key="c1", display_name="客户A",
    )
    m = message_obj(text="这是收据", ts=100, direction="in",
                    source={"media_type": "image", "media_ref": "/tmp/receipt.jpg"})
    im = _msg_from_obj(conv.conversation_id, m, platform="telegram")
    store.ingest_batch(conv, [im])
    rows = store.list_messages(conv.conversation_id)
    assert len(rows) == 1
    assert rows[0]["media_type"] == "image"
    assert rows[0]["media_ref"] == "/tmp/receipt.jpg"


# ── resolve_media_path ─────────────────────────────────────────────────────
def test_resolve_absolute_existing_file():
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        assert resolve_media_path({"media_ref": path}) == path
    finally:
        os.remove(path)


def test_resolve_relative_with_base_dirs():
    d = tempfile.mkdtemp()
    fname = "img.png"
    full = os.path.join(d, fname)
    with open(full, "wb") as f:
        f.write(b"x")
    try:
        assert resolve_media_path({"media_ref": fname}, base_dirs=[d]) == full
    finally:
        os.remove(full)
        os.rmdir(d)


def test_resolve_remote_returns_none():
    assert resolve_media_path({"media_ref": "https://cdn/x.jpg"}) is None


def test_resolve_missing_returns_none():
    assert resolve_media_path({"media_ref": "/no/such/file_xyz.png"}) is None
    assert resolve_media_path({}) is None


def test_media_kind_inference():
    assert media_kind({"media_type": "image"}) == "image"
    assert media_kind({"media_ref": "/x/a.OGG"}) == "voice"
    assert media_kind({"media_ref": "/x/a.png"}) == "image"
    assert media_kind({}) == ""


def test_resolve_for_translate_reasons():
    assert resolve_for_translate({})[2] == "no_ref"
    assert resolve_for_translate({"media_ref": "https://x/a.jpg"})[2] == "remote_unsupported"
    assert resolve_for_translate({"media_ref": "/nope.jpg"})[2] == "not_found"
    assert resolve_for_translate({"media_ref": "/nope.txt", "media_type": "file"})[2] == "unsupported_kind"
