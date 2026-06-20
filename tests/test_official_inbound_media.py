"""Phase I1：官方通道入站媒体可见化单测。

此前官方 webhook 收到非文字（图片/语音/贴纸…）只回「不支持」且**不镜像**——坐席台看不到。
本阶段在 LINE/WhatsApp/Messenger 非文字分支镜像占位（media_type + 「[图片]」等文案）。
"""
import pytest

import src.integrations.protocol_bridge as pb
from src.integrations.shared.official_inbound import (
    media_placeholder,
    mirror_inbound_media,
)


@pytest.fixture
def capture_sink(monkeypatch):
    events = []
    monkeypatch.setattr(pb, "_sink", lambda m: events.append(m), raising=False)
    return events


# ── 占位文案 ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mt,expect", [
    ("image", "[图片]"), ("photo", "[图片]"), ("audio", "[语音]"),
    ("voice", "[语音]"), ("video", "[视频]"), ("sticker", "[贴纸]"),
    ("file", "[文件]"), ("location", "[位置]"), ("weird", "[媒体]"), ("", "[媒体]"),
])
def test_media_placeholder(mt, expect):
    assert media_placeholder(mt) == expect


def test_mirror_inbound_media_payload(capture_sink):
    ok = mirror_inbound_media(platform="whatsapp", account_id="PNID",
                              chat_key="wa:user:8613", media_type="image",
                              name="8613", msg_id="m1")
    assert ok is True
    p = capture_sink[0]
    assert p["platform"] == "whatsapp" and p["media_type"] == "image"
    assert p["text"] == "[图片]" and p["direction"] == "in" and p["chat_key"] == "wa:user:8613"


# ── WhatsApp 非文字分支镜像 ──────────────────────────────────────────────────

async def test_wa_image_inbound_mirrored(capture_sink, monkeypatch):
    from src.integrations import whatsapp_cloud as wac
    sent = []
    async def _send(to, text, pnid, token, **k):
        sent.append((to, text))
        return {"ok": True, "data": {}}
    monkeypatch.setattr(wac, "wa_send_text", _send)

    msg = {"from": "8613", "id": "i1", "type": "image",
           "image": {"id": "media1"}, "_phone_number_id": "PNID"}
    await wac._handle_one_message(msg=msg, sm=None, phone_number_id="PNID",
                                  access_token="T", unsupported="仅支持文字")
    mirrored = [e for e in capture_sink if e["direction"] == "in"]
    assert mirrored and mirrored[0]["media_type"] == "image"
    assert mirrored[0]["text"] == "[图片]"
    # 仍回了不支持提示
    assert sent and sent[0][1] == "仅支持文字"


# ── Messenger 附件分支镜像 ───────────────────────────────────────────────────

async def test_fb_attachment_inbound_mirrored(capture_sink, monkeypatch):
    from src.integrations import facebook_webhook as fbw
    async def _send(psid, text, token, **k):
        return {"ok": True, "data": {}}
    monkeypatch.setattr(fbw, "fb_send_with_window_fallback", _send)

    ev = {"_page_id": "PAGE9", "sender": {"id": "PSID1"},
          "message": {"mid": "m1", "attachments": [{"type": "image", "payload": {}}]}}
    await fbw._handle_one_event(ev=ev, sm=None, page_token="PT",
                                fallback_tag="ACCOUNT_UPDATE", unsupported="x",
                                page_id_filter="")
    mirrored = [e for e in capture_sink if e["direction"] == "in"]
    assert mirrored and mirrored[0]["platform"] == "messenger"
    assert mirrored[0]["media_type"] == "image" and mirrored[0]["text"] == "[图片]"
    assert mirrored[0]["account_id"] == "PAGE9"
