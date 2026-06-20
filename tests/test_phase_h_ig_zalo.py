"""Phase H：Instagram + Zalo 平台广度单测。

覆盖：共享 official_inbound 骨架 / IG send+解析+护栏+handler / Zalo send+验签+解析+handler /
official worker 对 ig+zalo 的 send 分发（chat_key 归一）。
"""
import hashlib
import hmac
import json

import pytest

import src.integrations.protocol_bridge as pb


@pytest.fixture
def capture_sink(monkeypatch):
    events = []
    monkeypatch.setattr(pb, "_sink", lambda m: events.append(m), raising=False)
    return events


# ── 共享骨架 process_official_inbound ────────────────────────────────────────

async def test_skeleton_mirror_only_when_pipeline_off(capture_sink, monkeypatch):
    async def _boom(p):
        raise AssertionError("pipeline off 不应调用 maybe_auto_reply")
    monkeypatch.setattr(pb, "_reply_hook", _boom, raising=False)
    from src.integrations.shared.official_inbound import process_official_inbound

    handed = await process_official_inbound(
        platform="instagram", account_id="IG", chat_key="ig:user:1",
        text="hi", name="1", use_pipeline=False)
    assert handed is False
    assert capture_sink and capture_sink[0]["direction"] == "in"
    assert capture_sink[0]["platform"] == "instagram"

async def test_skeleton_delegates_when_pipeline_on(capture_sink, monkeypatch):
    got = []
    async def _reply(p):
        got.append(p)
    monkeypatch.setattr(pb, "_reply_hook", _reply, raising=False)
    from src.integrations.shared.official_inbound import process_official_inbound

    handed = await process_official_inbound(
        platform="zalo", account_id="OA", chat_key="zalo:user:9",
        text="alo", name="9", use_pipeline=True)
    assert handed is True
    assert got and got[0]["platform"] == "zalo" and got[0]["text"] == "alo"


# ── Instagram ────────────────────────────────────────────────────────────────

def test_ig_extract_messages_text_only_skip_echo():
    from src.integrations.instagram_webhook import extract_ig_messages
    body = {"object": "instagram", "entry": [{"messaging": [
        {"sender": {"id": "IGS1"}, "message": {"mid": "m1", "text": "hello"}},
        {"sender": {"id": "IGS2"}, "message": {"mid": "m2", "text": "echo", "is_echo": True}},
        {"sender": {"id": "IGS3"}, "message": {"mid": "m3"}},  # 无文本
    ]}]}
    out = extract_ig_messages(body)
    assert out == [{"sender": "IGS1", "text": "hello", "mid": "m1"}]

def test_ig_extract_ignores_non_instagram_object():
    from src.integrations.instagram_webhook import extract_ig_messages
    assert extract_ig_messages({"object": "page", "entry": []}) == []

async def test_ig_send_blocked_by_kill_switch(monkeypatch):
    from src.integrations import instagram_webhook as ig
    monkeypatch.setattr(
        "src.integrations.shared.rpa_send_guard.rpa_send_blocked",
        lambda p, a, **k: (True, "platform:instagram"))
    out = await ig.ig_send_text("IGS1", "hi", "IGID", "T", account_id="IGID")
    assert out["ok"] is False and "kill_switch" in out["error"]

async def test_ig_handler_pipeline_delegates(capture_sink, monkeypatch):
    from src.integrations import instagram_webhook as ig
    got = []
    monkeypatch.setattr(pb, "_reply_hook", lambda p: got.append(p) or _noop(), raising=False)

    async def _send(*a, **k):
        raise AssertionError("pipeline 模式不应自答")
    monkeypatch.setattr(ig, "ig_send_text", _send)

    class _SM:
        async def process_message(self, **k):
            raise AssertionError("pipeline 模式不应调用 SM")

    await ig._handle_ig_message(sender="IGS1", text="hi", mid="m1", sm=_SM(),
                                ig_id="IGID", ig_account_id="IGID", page_token="T",
                                use_pipeline=True)
    assert got and got[0]["platform"] == "instagram"


def _noop():
    async def _a():
        return None
    return _a()


# ── Zalo ─────────────────────────────────────────────────────────────────────

def test_zalo_verify_signature_hmac():
    from src.integrations.zalo_webhook import verify_zalo_signature
    body = b'{"event_name":"user_send_text"}'
    secret = "s3cr3t"
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_zalo_signature(body, mac, secret) is True
    assert verify_zalo_signature(body, "mac=" + mac, secret) is True
    assert verify_zalo_signature(body, "deadbeef", secret) is False
    assert verify_zalo_signature(body, mac, "") is False

def test_zalo_extract_user_send_text():
    from src.integrations.zalo_webhook import extract_zalo_messages
    body = {"event_name": "user_send_text", "sender": {"id": "U9"},
            "message": {"text": "alo", "msg_id": "z1"}}
    assert extract_zalo_messages(body) == [{"sender": "U9", "text": "alo", "msg_id": "z1"}]
    assert extract_zalo_messages({"event_name": "user_send_image"}) == []

async def test_zalo_send_blocked_by_kill_switch(monkeypatch):
    from src.integrations import zalo_webhook as zw
    monkeypatch.setattr(
        "src.integrations.shared.rpa_send_guard.rpa_send_blocked",
        lambda p, a, **k: (True, "global"))
    out = await zw.zalo_send_text("U9", "hi", "T", account_id="OA")
    assert out["ok"] is False and "kill_switch" in out["error"]

async def test_zalo_send_treats_error_code_as_failure(monkeypatch):
    from src.integrations import zalo_webhook as zw

    class _Resp:
        status = 200
        async def text(self):
            return json.dumps({"error": -213, "message": "user not in 7-day window"})
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _Sess:
        def __init__(self, *a, **k): pass
        def post(self, *a, **k): return _Resp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(zw.aiohttp, "ClientSession", _Sess)
    monkeypatch.setattr("src.integrations.shared.rpa_send_guard.rpa_send_blocked",
                        lambda p, a, **k: (False, ""))
    out = await zw.zalo_send_text("U9", "hi", "T", account_id="OA")
    assert out["ok"] is False


# ── official worker 对 ig/zalo 的 send 分发（chat_key 归一） ──────────────────

async def test_official_worker_instagram_dispatch(monkeypatch):
    from src.integrations.official_api_worker import OfficialApiWorker
    captured = {}
    async def _fake(igsid, text, ig_id, token, *, account_id="default", check_kill_switch=True):
        captured.update(igsid=igsid, ig_id=ig_id, account_id=account_id)
        return {"ok": True, "data": {"message_id": "mid"}}
    import src.integrations.instagram_webhook as ig
    monkeypatch.setattr(ig, "ig_send_text", _fake)

    acc = {"platform": "instagram", "account_id": "IGID",
           "meta": {"page_access_token": "T", "ig_id": "IGID"}}
    w = OfficialApiWorker(acc, {})
    await w.start()
    out = await w.send("ig:user:IGS1", "yo")
    assert out["delivered"] is True
    assert captured["igsid"] == "IGS1" and captured["account_id"] == "IGID"

async def test_official_worker_zalo_dispatch(monkeypatch):
    from src.integrations.official_api_worker import OfficialApiWorker
    captured = {}
    async def _fake(uid, text, token, *, message_type="cs", account_id="default", check_kill_switch=True):
        captured.update(uid=uid, message_type=message_type, account_id=account_id)
        return {"ok": True, "data": {"data": {"message_id": "z9"}}}
    import src.integrations.zalo_webhook as zw
    monkeypatch.setattr(zw, "zalo_send_text", _fake)

    acc = {"platform": "zalo", "account_id": "OA",
           "meta": {"access_token": "T", "message_type": "cs"}}
    w = OfficialApiWorker(acc, {})
    await w.start()
    out = await w.send("zalo:user:U9", "alo")
    assert out["delivered"] is True and out["message_id"] == "z9"
    assert captured["uid"] == "U9" and captured["message_type"] == "cs"
