"""Phase B：官方通道媒体/语音出站单测。

补齐 ``OfficialApiWorker.send_media``，使坐席「发送语音」(/api/unified-inbox/send-voice
→ orch.send_media) 能在 LINE/Messenger/WhatsApp 官方通道发出（与 Telegram 对齐）。
- WhatsApp：上传 → media_id 发送（无需公网 URL）。
- LINE/Messenger：按公网 https URL 发送（需 official_media.public_base_url，否则 no_public_url）。
"""
import pytest

from src.integrations.official_api_worker import OfficialApiWorker
from src.integrations.whatsapp_cloud import _wa_guess_mime, _wa_send_kind


# ── WhatsApp 纯函数：mime / message type 推断 ────────────────────────────────

@pytest.mark.parametrize("path,mt,expect", [
    ("/tmp/a.ogg", "voice", "audio/ogg"),
    ("/tmp/a.opus", "voice", "audio/ogg"),
    ("/tmp/a.mp3", "audio", "audio/mpeg"),
    ("/tmp/a.jpg", "image", "image/jpeg"),
    ("/tmp/a.unknownext", "voice", "audio/ogg"),  # 回退 media_type
])
def test_wa_guess_mime(path, mt, expect):
    assert _wa_guess_mime(path, mt) == expect


@pytest.mark.parametrize("mt,mime,expect", [
    ("voice", "audio/ogg", "audio"),
    ("audio", "audio/mpeg", "audio"),
    ("image", "image/jpeg", "image"),
    ("video", "video/mp4", "video"),
    ("", "audio/ogg", "audio"),       # 仅凭 mime 也能判 audio
    ("file", "application/pdf", "document"),
])
def test_wa_send_kind(mt, mime, expect):
    assert _wa_send_kind(mt, mime) == expect


# ── _public_media_url：相对 → 绝对公网 URL 解析 ──────────────────────────────

def test_public_media_url_absolute_passthrough():
    w = OfficialApiWorker({"platform": "line"}, {})
    assert w._public_media_url("https://cdn.x/a.ogg") == "https://cdn.x/a.ogg"


def test_public_media_url_relative_with_base():
    cfg = {"official_media": {"public_base_url": "https://pub.example.com/"}}
    w = OfficialApiWorker({"platform": "line"}, cfg)
    assert w._public_media_url("/static/x.ogg") == "https://pub.example.com/static/x.ogg"


def test_public_media_url_missing_base_returns_empty():
    w = OfficialApiWorker({"platform": "line"}, {})
    assert w._public_media_url("/static/x.ogg") == ""


# ── WhatsApp send_media：上传式，dest 归一 + 结果透出 ────────────────────────

async def test_wa_send_media_dispatch(monkeypatch):
    captured = {}

    async def _fake_wa_send_media(to, media_path, pnid, token, *, media_type="", caption=""):
        captured.update(to=to, media_path=media_path, media_type=media_type)
        return {"ok": True, "data": {"messages": [{"id": "wamid.1"}]}}

    monkeypatch.setattr(
        "src.integrations.whatsapp_cloud.wa_send_media", _fake_wa_send_media)

    acc = {"platform": "whatsapp", "account_id": "PNID",
           "meta": {"access_token": "T", "phone_number_id": "PNID"}}
    w = OfficialApiWorker(acc, {})
    await w.start()
    res = await w.send_media(
        "wa:user:8613800138000", media_path="/tmp/r.ogg",
        media_type="voice", caption="")
    assert res["delivered"] is True and res["message_id"] == "wamid.1"
    assert captured["to"] == "8613800138000"      # 裸号码（去前缀）
    assert captured["media_type"] == "voice"


# ── LINE send_media：需公网 URL；无 base → no_public_url；有 base → 调 helper ──

async def test_line_send_media_no_public_url():
    acc = {"platform": "line", "account_id": "official",
           "meta": {"channel_access_token": "T"}}
    w = OfficialApiWorker(acc, {})  # 无 official_media.public_base_url
    await w.start()
    res = await w.send_media(
        "line:user:Uabc", media_path="/tmp/r.ogg",
        media_type="voice", media_url="/static/r.ogg")
    assert res["delivered"] is False
    assert res["error_kind"] == "no_public_url"


async def test_line_send_media_with_public_url(monkeypatch):
    captured = {}

    async def _fake_push_media(to, media_url, token, *, media_type="audio",
                               duration_ms=0, preview_url="", account_id="default",
                               check_kill_switch=True):
        captured.update(to=to, media_url=media_url, media_type=media_type)
        return True

    monkeypatch.setattr(
        "src.integrations.line_webhook.line_push_media", _fake_push_media)
    # ffprobe 通常不可用/无该文件 → duration 探测返回 None（不影响断言）
    cfg = {"official_media": {"public_base_url": "https://pub.example.com"}}
    acc = {"platform": "line", "account_id": "official",
           "meta": {"channel_access_token": "T"}}
    w = OfficialApiWorker(acc, cfg)
    await w.start()
    res = await w.send_media(
        "line:user:Uabc", media_path="/tmp/r.ogg",
        media_type="voice", media_url="/static/r.ogg")
    assert res["delivered"] is True
    assert captured["to"] == "Uabc"
    assert captured["media_url"] == "https://pub.example.com/static/r.ogg"


# ── Messenger send_media：经 attachment（公网 URL）────────────────────────────

async def test_messenger_send_media_with_public_url(monkeypatch):
    captured = {}

    async def _fake_attachment(psid, media_url, token, *, media_type="audio",
                               messaging_type="RESPONSE", account_id="default",
                               check_kill_switch=True):
        captured.update(psid=psid, media_url=media_url, media_type=media_type)
        return {"ok": True, "data": {"message_id": "mid.7"}}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_send_attachment", _fake_attachment)
    cfg = {"official_media": {"public_base_url": "https://pub.example.com"}}
    acc = {"platform": "messenger", "account_id": "PAGE9",
           "meta": {"page_access_token": "T"}}
    w = OfficialApiWorker(acc, cfg)
    await w.start()
    res = await w.send_media(
        "fb:user:123", media_path="/tmp/r.ogg",
        media_type="voice", media_url="/static/r.ogg")
    assert res["delivered"] is True and res["message_id"] == "mid.7"
    assert captured["psid"] == "123"
    assert captured["media_url"] == "https://pub.example.com/static/r.ogg"


# ── 不支持的官方平台（instagram/zalo 媒体出站暂未接入）────────────────────────

async def test_unsupported_platform_media():
    acc = {"platform": "zalo", "account_id": "z1", "meta": {"access_token": "T"}}
    w = OfficialApiWorker(acc, {})
    await w.start()
    res = await w.send_media(
        "zalo:user:9", media_path="/tmp/r.ogg", media_type="voice",
        media_url="https://pub/x.ogg")
    assert res["delivered"] is False and res["error_kind"] == "not_supported"


# ── Messenger 无公网 URL → 字节上传式（免公网托管，本轮优化）──────────────────

async def test_messenger_send_media_upload_when_no_public_url(monkeypatch):
    captured = {}

    async def _fake_upload(psid, media_path, token, *, media_type="audio",
                           messaging_type="RESPONSE", account_id="default",
                           check_kill_switch=True):
        captured.update(psid=psid, media_path=media_path, media_type=media_type)
        return {"ok": True, "data": {"message_id": "up.1"}}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_send_attachment_upload", _fake_upload)
    acc = {"platform": "messenger", "account_id": "PAGE9",
           "meta": {"page_access_token": "T"}}
    w = OfficialApiWorker(acc, {})  # 无 public_base_url → 走上传
    await w.start()
    res = await w.send_media(
        "fb:user:123", media_path="/tmp/r.ogg", media_type="voice", media_url="/static/r.ogg")
    assert res["delivered"] is True and res["message_id"] == "up.1"
    assert captured["psid"] == "123" and captured["media_path"] == "/tmp/r.ogg"


# ── Instagram 媒体：需公网 URL ───────────────────────────────────────────────

async def test_instagram_send_media_no_public_url():
    acc = {"platform": "instagram", "account_id": "IG1",
           "meta": {"page_access_token": "T", "ig_id": "17841400000"}}
    w = OfficialApiWorker(acc, {})
    await w.start()
    res = await w.send_media(
        "ig:user:abc", media_path="/tmp/r.jpg", media_type="image",
        media_url="/static/r.jpg")
    assert res["delivered"] is False and res["error_kind"] == "no_public_url"


async def test_instagram_send_media_with_public_url(monkeypatch):
    captured = {}

    async def _fake_ig_att(igsid, media_url, ig_id, token, *, media_type="image",
                           account_id="default", check_kill_switch=True):
        captured.update(igsid=igsid, media_url=media_url, ig_id=ig_id, media_type=media_type)
        return {"ok": True, "data": {"message_id": "ig.9"}}

    monkeypatch.setattr(
        "src.integrations.instagram_webhook.ig_send_attachment", _fake_ig_att)
    cfg = {"official_media": {"public_base_url": "https://pub.example.com"}}
    acc = {"platform": "instagram", "account_id": "IG1",
           "meta": {"page_access_token": "T", "ig_id": "17841400000"}}
    w = OfficialApiWorker(acc, cfg)
    await w.start()
    res = await w.send_media(
        "ig:user:abc", media_path="/tmp/r.jpg", media_type="image",
        media_url="/static/r.jpg")
    assert res["delivered"] is True and res["message_id"] == "ig.9"
    assert captured["media_url"] == "https://pub.example.com/static/r.jpg"
    assert captured["ig_id"] == "17841400000"


# ── 平台 helper 的 https 守卫（非 https → 早退失败，零网络）─────────────────────

async def test_line_push_media_rejects_non_https():
    from src.integrations.line_webhook import line_push_media
    ok = await line_push_media(
        "Uabc", "http://insecure/x.ogg", "T", media_type="audio")
    assert ok is False


async def test_fb_send_attachment_rejects_non_https():
    from src.integrations.facebook_webhook import fb_send_attachment
    out = await fb_send_attachment(
        "123", "http://insecure/x.ogg", "T", media_type="audio")
    assert out["ok"] is False and out["error_kind"] == "no_public_url"


async def test_ig_send_attachment_rejects_non_https():
    from src.integrations.instagram_webhook import ig_send_attachment
    out = await ig_send_attachment(
        "abc", "http://insecure/x.jpg", "17841400000", "T", media_type="image")
    assert out["ok"] is False and out["error_kind"] == "no_public_url"


async def test_fb_attachment_upload_missing_file():
    from src.integrations.facebook_webhook import fb_send_attachment_upload
    out = await fb_send_attachment_upload(
        "123", "/no/such/file.ogg", "T", media_type="audio")
    assert out["ok"] is False and out["error_kind"] == "bad_request"


# ── orch.send_media 透传 media_url 给官方 worker（inspect 签名探测）────────────

async def test_orch_send_media_threads_media_url_to_official_worker(monkeypatch):
    import src.integrations.account_orchestrator as ao
    import src.integrations.protocol_bridge as pb

    monkeypatch.setattr(pb, "_sink", lambda m: None, raising=False)

    captured = {}

    async def _fake_attachment(psid, media_url, token, **k):
        captured.update(psid=psid, media_url=media_url)
        return {"ok": True, "data": {"message_id": "m9"}}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_send_attachment", _fake_attachment)

    cfg = {"official_media": {"public_base_url": "https://pub.example.com"}}
    acc = {"platform": "messenger", "account_id": "PAGE9",
           "meta": {"page_access_token": "T"}}
    worker = OfficialApiWorker(acc, cfg)
    await worker.start()
    orch = ao.AccountOrchestrator(config=cfg)
    m = ao._Managed(
        key=ao.account_key("messenger", "PAGE9"), platform="messenger",
        account_id="PAGE9", mode="official", worker=worker, state="running")
    orch._managed[m.key] = m
    res = await orch.send_media(
        "messenger", "PAGE9", "fb:user:123",
        media_path="/tmp/r.ogg", media_url="/static/r.ogg",
        media_type="voice", caption="")
    assert res.get("delivered") is True
    assert captured["media_url"] == "https://pub.example.com/static/r.ogg"


# ── send_media inbox_text：转写回写给坐席、不污染客户 caption ──────────────────

class _FakeVoiceWorker:
    """最小 worker：仅 send_media（签名无 media_url，似 telegram protocol）。"""

    def __init__(self):
        self.sent = {}

    async def send_media(self, chat_key, *, media_path, media_type, caption=""):
        self.sent.update(chat_key=chat_key, caption=caption, media_type=media_type)
        return {"delivered": True, "message_id": "v1"}


def _orch_with_voice_worker():
    import src.integrations.account_orchestrator as ao
    orch = ao.AccountOrchestrator(config={})
    worker = _FakeVoiceWorker()
    m = ao._Managed(
        key=ao.account_key("telegram", "acct1"), platform="telegram",
        account_id="acct1", mode="protocol", worker=worker, state="running")
    orch._managed[m.key] = m
    return orch, worker


async def test_send_media_inbox_text_overrides_writeback(monkeypatch):
    import src.integrations.protocol_bridge as pb
    cap = {}
    monkeypatch.setattr(pb, "emit_incoming", lambda msg: cap.update(msg), raising=False)
    orch, worker = _orch_with_voice_worker()
    res = await orch.send_media(
        "telegram", "acct1", "123",
        media_path="/tmp/r.ogg", media_url="/static/r.ogg",
        media_type="voice", caption="", inbox_text="你好，这是语音内容")
    assert res["delivered"] is True
    # 客户侧 caption 仍空（纯语音），收件箱回写文本=转写
    assert worker.sent["caption"] == ""
    assert cap["text"] == "你好，这是语音内容"
    assert cap["media_type"] == "voice" and cap["direction"] == "out"


async def test_send_media_inbox_text_defaults_to_caption(monkeypatch):
    import src.integrations.protocol_bridge as pb
    cap = {}
    monkeypatch.setattr(pb, "emit_incoming", lambda msg: cap.update(msg), raising=False)
    orch, worker = _orch_with_voice_worker()
    # 不传 inbox_text → 回写回落 caption（向后兼容）
    res = await orch.send_media(
        "telegram", "acct1", "123",
        media_path="/tmp/p.jpg", media_url="/static/p.jpg",
        media_type="image", caption="图片说明")
    assert res["delivered"] is True
    assert cap["text"] == "图片说明"
