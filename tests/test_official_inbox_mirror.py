"""Phase G4：官方通道入站/出站镜像进统一收件箱单测。

- mirror_to_inbox 经 protocol_bridge sink 投递正确 payload；
- WhatsApp Cloud / Messenger / LINE 三端 webhook 处理器收到消息时镜像「in」、回复后镜像「out」。
- sink 未注册（inbox 关）时静默不报错（零回归）。
"""
import pytest

import src.integrations.protocol_bridge as pb
from src.integrations.shared.inbox_mirror import mirror_to_inbox


@pytest.fixture
def capture_sink(monkeypatch):
    events = []
    monkeypatch.setattr(pb, "_sink", lambda msg: events.append(msg), raising=False)
    return events


# ── 助手本体 ─────────────────────────────────────────────────────────────────

def test_mirror_builds_payload(capture_sink):
    assert mirror_to_inbox("line", "official", "line:user:U1", "hi",
                           direction="in", name="U1", msg_id="m1") is True
    assert len(capture_sink) == 1
    p = capture_sink[0]
    assert p["platform"] == "line" and p["account_id"] == "official"
    assert p["chat_key"] == "line:user:U1" and p["text"] == "hi"
    assert p["direction"] == "in" and p["msg_id"] == "m1"

def test_mirror_no_sink_is_silent(monkeypatch):
    monkeypatch.setattr(pb, "_sink", None, raising=False)
    # 不抛
    assert mirror_to_inbox("wa", "P", "c", "x") is True

def test_mirror_empty_chat_key_skipped(capture_sink):
    assert mirror_to_inbox("line", "official", "", "hi") is False
    assert capture_sink == []


# ── WhatsApp Cloud handler 镜像 ──────────────────────────────────────────────

async def test_wa_handler_mirrors_in_and_out(capture_sink, monkeypatch):
    from src.integrations import whatsapp_cloud as wac

    sent = []
    async def _fake_send(to, text, pnid, token, **k):
        sent.append((to, text))
        return {"ok": True, "data": {"messages": [{"id": "out1"}]}}
    monkeypatch.setattr(wac, "wa_send_text", _fake_send)

    class _SM:
        async def process_message(self, *, text, user_id, context):
            return "你好呀"

    msg = {"from": "8613800138000", "id": "in1", "type": "text",
           "text": {"body": "在吗"}, "_phone_number_id": "PNID"}
    await wac._handle_one_message(msg=msg, sm=_SM(), phone_number_id="PNID",
                                  access_token="T", unsupported="x")

    dirs = [(e["direction"], e["platform"], e["account_id"], e["text"]) for e in capture_sink]
    assert ("in", "whatsapp", "PNID", "在吗") in dirs
    assert ("out", "whatsapp", "PNID", "你好呀") in dirs


# ── Messenger handler 镜像 ───────────────────────────────────────────────────

async def test_fb_handler_mirrors_in_and_out(capture_sink, monkeypatch):
    from src.integrations import facebook_webhook as fbw

    async def _fake_send(psid, text, token, **k):
        return {"ok": True, "data": {}}
    monkeypatch.setattr(fbw, "fb_send_with_window_fallback", _fake_send)

    class _SM:
        async def process_message(self, *, text, user_id, context):
            return "hello back"

    ev = {"_page_id": "PAGE9", "sender": {"id": "PSID1"},
          "message": {"mid": "mid1", "text": "hi there"}}
    await fbw._handle_one_event(ev=ev, sm=_SM(), page_token="PT",
                                fallback_tag="ACCOUNT_UPDATE", unsupported="x",
                                page_id_filter="")

    dirs = [(e["direction"], e["platform"], e["account_id"]) for e in capture_sink]
    assert ("in", "messenger", "PAGE9") in dirs
    assert ("out", "messenger", "PAGE9") in dirs


# ── sink 抛错不影响主流程 ────────────────────────────────────────────────────

async def test_mirror_failure_does_not_break_handler(monkeypatch):
    from src.integrations import whatsapp_cloud as wac

    def _boom(msg):
        raise RuntimeError("sink down")
    monkeypatch.setattr(pb, "_sink", _boom, raising=False)

    sent = []
    async def _fake_send(to, text, pnid, token, **k):
        sent.append(text)
        return {"ok": True, "data": {"messages": [{"id": "o"}]}}
    monkeypatch.setattr(wac, "wa_send_text", _fake_send)

    class _SM:
        async def process_message(self, *, text, user_id, context):
            return "ok reply"

    msg = {"from": "X", "id": "i", "type": "text", "text": {"body": "hi"},
           "_phone_number_id": "P"}
    # 不抛，且回复仍发出
    await wac._handle_one_message(msg=msg, sm=_SM(), phone_number_id="P",
                                  access_token="T", unsupported="x")
    assert "ok reply" in sent
