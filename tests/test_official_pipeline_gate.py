"""Phase G4c：官方入站走 protocol_autoreply 主管道（开关默认关）。

验证：
- official_pipeline_enabled 默认 False，显式开才 True；
- use_pipeline=True 时 WA/Messenger handler **不自答**，改调 maybe_auto_reply（payload 正确）；
- use_pipeline=False（默认）时维持既有 SkillManager 自答（零回归）。
"""
import pytest

import src.integrations.protocol_bridge as pb
from src.integrations.official_api_worker import official_pipeline_enabled


# ── 开关 ─────────────────────────────────────────────────────────────────────

def test_gate_default_off():
    assert official_pipeline_enabled({}) is False
    assert official_pipeline_enabled({"official_pipeline": {}}) is False
    assert official_pipeline_enabled({"official_pipeline": {"enabled": False}}) is False

def test_gate_on():
    assert official_pipeline_enabled({"official_pipeline": {"enabled": True}}) is True


# ── WhatsApp：pipeline 模式委托 maybe_auto_reply，不自答 ──────────────────────

async def test_wa_pipeline_delegates_not_selfreply(monkeypatch):
    from src.integrations import whatsapp_cloud as wac

    sent = []
    async def _fake_send(*a, **k):
        sent.append(a)
        return {"ok": True, "data": {"messages": [{"id": "x"}]}}
    monkeypatch.setattr(wac, "wa_send_text", _fake_send)

    captured = []
    async def _fake_reply(payload):
        captured.append(payload)
    monkeypatch.setattr(pb, "_reply_hook", _fake_reply, raising=False)

    class _SM:
        async def process_message(self, **k):
            raise AssertionError("pipeline 模式不应调用 SkillManager 自答")

    msg = {"from": "8613800138000", "id": "i1", "type": "text",
           "text": {"body": "在吗"}, "_phone_number_id": "PNID"}
    await wac._handle_one_message(msg=msg, sm=_SM(), phone_number_id="PNID",
                                  access_token="T", unsupported="x", use_pipeline=True)

    assert len(captured) == 1
    p = captured[0]
    assert p["platform"] == "whatsapp" and p["account_id"] == "PNID"
    assert p["chat_key"] == "wa:user:8613800138000" and p["text"] == "在吗"
    assert p["direction"] == "in"
    assert sent == []  # 未自答


async def test_wa_default_selfreply_preserved(monkeypatch):
    from src.integrations import whatsapp_cloud as wac

    sent = []
    async def _fake_send(to, text, pnid, token, **k):
        sent.append((to, text))
        return {"ok": True, "data": {"messages": [{"id": "x"}]}}
    monkeypatch.setattr(wac, "wa_send_text", _fake_send)
    # reply hook 若被调用即失败（默认不应走管道）
    async def _boom(p):
        raise AssertionError("默认模式不应走主管道")
    monkeypatch.setattr(pb, "_reply_hook", _boom, raising=False)

    class _SM:
        async def process_message(self, **k):
            return "默认自答"

    msg = {"from": "X", "id": "i", "type": "text", "text": {"body": "hi"},
           "_phone_number_id": "P"}
    await wac._handle_one_message(msg=msg, sm=_SM(), phone_number_id="P",
                                  access_token="T", unsupported="x")  # use_pipeline 默认 False
    assert ("X", "默认自答") in sent


# ── Messenger：pipeline 模式委托 maybe_auto_reply，不自答 ─────────────────────

async def test_fb_pipeline_delegates_not_selfreply(monkeypatch):
    from src.integrations import facebook_webhook as fbw

    async def _fake_send(*a, **k):
        raise AssertionError("pipeline 模式不应直接发送")
    monkeypatch.setattr(fbw, "fb_send_with_window_fallback", _fake_send)

    captured = []
    async def _fake_reply(payload):
        captured.append(payload)
    monkeypatch.setattr(pb, "_reply_hook", _fake_reply, raising=False)

    class _SM:
        async def process_message(self, **k):
            raise AssertionError("pipeline 模式不应调用 SkillManager")

    ev = {"_page_id": "PAGE9", "sender": {"id": "PSID1"},
          "message": {"mid": "m1", "text": "hi there"}}
    await fbw._handle_one_event(ev=ev, sm=_SM(), page_token="PT",
                                fallback_tag="ACCOUNT_UPDATE", unsupported="x",
                                page_id_filter="", use_pipeline=True)

    assert len(captured) == 1
    p = captured[0]
    assert p["platform"] == "messenger" and p["account_id"] == "PAGE9"
    assert p["chat_key"] == "fb:user:PSID1" and p["direction"] == "in"
