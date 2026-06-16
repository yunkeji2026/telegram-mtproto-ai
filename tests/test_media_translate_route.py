"""P61-2：/api/unified-inbox/translate-message-media 端点契约测试。

覆盖解析 + 分派 + 配置门禁分支（真实 OCR/ASR 成功路径由 service 级测试覆盖）：
no_ref / remote_unsupported / not_found / outside_base_dirs /
vision_disabled / asr_disabled / store 受信 ref 优先。
"""

import os
import tempfile

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, *a, **k):
        raise AssertionError("not used")


class FakeCM:
    def __init__(self, cfg):
        self.config = cfg


class FakeAI:
    async def chat(self, prompt, context=None):
        return "你好"


def _client(cfg=None, store=None):
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_unified_inbox_routes(app, page_auth=_auth, api_auth=_auth, templates=_Templates())
    app.state.ai_client = FakeAI()
    app.state.config_manager = FakeCM(cfg or {})
    if store is not None:
        app.state.inbox_store = store
    return TestClient(app)


def _post(client, body):
    return client.post("/api/unified-inbox/translate-message-media", json=body).json()


def test_no_ref():
    r = _post(_client(), {"conversation_id": "c1", "message_id": "m1"})
    assert r["ok"] is False and r["reason"] == "no_ref" and r["fallback"] == "upload"


def test_remote_unsupported():
    r = _post(_client(), {"media_ref": "https://cdn/x.jpg", "media_type": "image"})
    assert r["ok"] is False and r["reason"] == "remote_unsupported"


def test_not_found():
    r = _post(_client(), {"media_ref": "/no/such/file_zzz.png"})
    assert r["ok"] is False and r["reason"] == "not_found"


def test_remote_fetch_disabled_keeps_remote_unsupported():
    """C-2：remote_fetch 默认关 → 远程 ref 仍回 remote_unsupported（行为不变）。"""
    cfg = {"media": {"remote_fetch": {"enabled": False}}}
    r = _post(_client(cfg=cfg), {"media_ref": "https://cdn/x.jpg", "media_type": "image"})
    assert r["ok"] is False and r["reason"] == "remote_unsupported"


def test_remote_fetch_enabled_blocks_internal_host():
    """C-2：remote_fetch 开启但 URL 指向内网 → SSRF 拦截（blocked_host），不泄漏请求。"""
    cfg = {"media": {"remote_fetch": {"enabled": True}}}
    r = _post(_client(cfg=cfg), {"media_ref": "http://127.0.0.1/x.jpg", "media_type": "image"})
    assert r["ok"] is False and r["reason"] == "blocked_host" and r["fallback"] == "upload"


def test_remote_fetch_enabled_domain_allowlist_blocks():
    """C-2：配置域名白名单 → 不在白名单的远程域被拒（domain_not_allowed）。"""
    cfg = {"media": {"remote_fetch": {"enabled": True, "allow_domains": ["telegram.org"]}}}
    r = _post(_client(cfg=cfg), {"media_ref": "https://evil.com/x.jpg", "media_type": "image"})
    assert r["ok"] is False and r["reason"] == "domain_not_allowed"


def test_resolvable_image_but_vision_disabled():
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        r = _post(_client(cfg={"vision": {"enabled": False}}),
                  {"media_ref": path, "media_type": "image"})
        # 解析成功 → 走到分派 → 命中配置门禁
        assert r["ok"] is False and r["reason"] == "vision_disabled"
    finally:
        os.remove(path)


def test_resolvable_voice_but_asr_disabled():
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    try:
        r = _post(_client(cfg={"audio_pipeline": {"enabled": False}}),
                  {"media_ref": path, "media_type": "voice"})
        assert r["ok"] is False and r["reason"] == "asr_disabled"
    finally:
        os.remove(path)


def test_outside_base_dirs_blocked():
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    allowed = tempfile.mkdtemp()  # 文件不在此目录内
    try:
        cfg = {"vision": {"enabled": True}, "media": {"base_dirs": [allowed]}}
        r = _post(_client(cfg=cfg), {"media_ref": path, "media_type": "image"})
        assert r["ok"] is False and r["reason"] == "outside_base_dirs"
    finally:
        os.remove(path)
        os.rmdir(allowed)


def test_within_base_dirs_passes_containment():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "a.png")
    with open(path, "wb") as f:
        f.write(b"x")
    try:
        # base_dirs 包含文件 → 通过容纳检查 → 因 vision 未启用止于门禁（证明已越过容纳检查）
        cfg = {"vision": {"enabled": False}, "media": {"base_dirs": [d]}}
        r = _post(_client(cfg=cfg), {"media_ref": path, "media_type": "image"})
        assert r["reason"] == "vision_disabled"
    finally:
        os.remove(path)
        os.rmdir(d)


def test_store_ref_preferred_over_body():
    """store 里有该消息 media_ref → 优先用受信 ref（忽略 body 传的伪造 ref）。"""
    store = InboxStore(":memory:")
    conv = InboxConversation(
        conversation_id="telegram:default:c1", platform="telegram",
        account_id="default", chat_key="c1", display_name="A",
    )
    fd, real = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        store.ingest_batch(conv, [InboxMessage(
            conversation_id=conv.conversation_id, platform_msg_id="m1",
            direction="in", text="", media_type="image", media_ref=real, ts=1,
        )])
        client = _client(cfg={"vision": {"enabled": False}}, store=store)
        # body 传一个 remote ref，但 store 有受信本地 ref → 应走 store ref → vision_disabled
        r = _post(client, {"conversation_id": conv.conversation_id, "message_id": "m1",
                           "media_ref": "https://evil/x.jpg", "media_type": "image"})
        assert r["reason"] == "vision_disabled"
    finally:
        os.remove(real)
