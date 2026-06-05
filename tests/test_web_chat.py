"""Phase 2 — 网页聊天 Widget（web 渠道）测试。

覆盖：访客 token、出站 hub、服务落库 + WebInboxAdapter、AI 后台回复函数、
以及 /chat/api/* 公网端点（session/message/history）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.channel_adapters import WebInboxAdapter
from src.integrations.web_chat.hub import WebOutboundHub
from src.integrations.web_chat.service import WebChatService
from src.integrations.web_chat.tokens import (
    issue_visitor_token,
    new_visitor_id,
    verify_visitor_token,
)
from src.web.routes.web_chat_routes import register_web_chat_routes, run_web_ai_reply


# ── tokens ──────────────────────────────────────────────────────────────────

class TestVisitorTokens:
    def test_round_trip(self):
        tok = issue_visitor_token("s3cr3t", "wv_abc")
        assert verify_visitor_token("s3cr3t", tok) == "wv_abc"

    def test_wrong_secret_fails(self):
        tok = issue_visitor_token("s3cr3t", "wv_abc")
        assert verify_visitor_token("other", tok) is None

    def test_tamper_fails(self):
        tok = issue_visitor_token("s3cr3t", "wv_abc")
        body, _, sig = tok.partition(".")
        assert verify_visitor_token("s3cr3t", body + ".deadbeef") is None

    def test_expiry(self):
        tok = issue_visitor_token("s3cr3t", "wv_abc", issued_at=0)
        assert verify_visitor_token("s3cr3t", tok, max_age_sec=1) is None
        assert verify_visitor_token("s3cr3t", tok, max_age_sec=0) == "wv_abc"

    def test_new_visitor_id_unique(self):
        assert new_visitor_id() != new_visitor_id()


# ── hub ─────────────────────────────────────────────────────────────────────

class TestOutboundHub:
    async def test_publish_only_to_conversation(self):
        hub = WebOutboundHub()
        q1 = hub.subscribe("web:web:a")
        q2 = hub.subscribe("web:web:b")
        hub.publish("web:web:a", {"text": "hi"})
        assert q1.get_nowait()["text"] == "hi"
        assert q2.empty()

    def test_unsubscribe_cleans_up(self):
        hub = WebOutboundHub()
        q = hub.subscribe("c1")
        assert hub.subscriber_count == 1
        hub.unsubscribe("c1", q)
        assert hub.subscriber_count == 0


# ── service + adapter ───────────────────────────────────────────────────────

@pytest.fixture()
def store():
    d = tempfile.mkdtemp()
    return InboxStore(Path(d) / "inbox.db")


class TestServiceAndAdapter:
    def test_record_and_surface_in_workspace(self, store):
        svc = WebChatService(account_id="web")
        vid = "wv_test1"
        assert svc.record_message(store, vid, text="你好", direction="in") == 1
        assert svc.record_message(store, vid, text="您好，在的", direction="out") == 1
        # 会话应能被 WebInboxAdapter 读出（工作台可见）
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(inbox_store=store)))
        chats = WebInboxAdapter().collect_chats(req, 50)
        assert len(chats) == 1
        assert chats[0]["platform"] == "web"
        assert chats[0]["chat_key"] == vid
        assert chats[0]["conversation_id"] == "web:web:wv_test1"

    def test_history_persists(self, store):
        svc = WebChatService(account_id="web")
        svc.record_message(store, "wv_h", text="m1", direction="in")
        svc.record_message(store, "wv_h", text="m2", direction="out")
        msgs = store.list_messages("web:web:wv_h", limit=50)
        assert [m["text"] for m in msgs] == ["m1", "m2"]


# ── AI 后台回复函数 ──────────────────────────────────────────────────────────

class TestRunWebAiReply:
    async def test_ai_reply_stored_and_pushed(self, store):
        svc = WebChatService(account_id="web")
        hub = WebOutboundHub()
        cid = svc.conversation_id("wv_ai")
        q = hub.subscribe(cid)
        sm = AsyncMock()
        sm.process_message = AsyncMock(return_value="这是AI回复")

        reply = await run_web_ai_reply(
            skill_manager=sm, inbox_store=store, hub=hub, service=svc,
            visitor_id="wv_ai", text="问题",
        )
        assert reply == "这是AI回复"
        # 落库
        msgs = store.list_messages(cid, limit=10)
        assert any(m["text"] == "这是AI回复" and m["direction"] == "out" for m in msgs)
        # 推送给访客
        evt = q.get_nowait()
        assert evt["type"] == "web_outbound" and evt["text"] == "这是AI回复" and evt["by"] == "ai"

    async def test_ai_fallback_on_empty(self, store):
        svc = WebChatService(account_id="web")
        hub = WebOutboundHub()
        sm = AsyncMock()
        sm.process_message = AsyncMock(return_value="")
        reply = await run_web_ai_reply(
            skill_manager=sm, inbox_store=store, hub=hub, service=svc,
            visitor_id="wv_fb", text="x",
        )
        assert reply  # 非空兜底


# ── 公网端点 ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def chat_app(store):
    app = FastAPI()
    cfgm = SimpleNamespace(config={"web_chat": {
        "enabled": True, "account_id": "web", "default_mode": "auto_ai",
        "token_secret": "testsecret", "greeting": "你好呀",
    }})
    register_web_chat_routes(app, config_manager=cfgm)
    app.state.inbox_store = store
    sm = AsyncMock()
    sm.process_message = AsyncMock(return_value="自动回复")
    app.state.skill_manager = sm
    return app


class TestChatEndpoints:
    def test_session_issues_token_and_greeting(self, chat_app):
        c = TestClient(chat_app)
        r = c.post("/chat/api/session", json={})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] and d["visitor_id"].startswith("wv_") and d["token"]
        assert d["greeting"] == "你好呀"

    def test_message_requires_valid_token(self, chat_app):
        c = TestClient(chat_app)
        r = c.post("/chat/api/message", json={"text": "hi", "token": "bad.token"})
        assert r.status_code == 401

    def test_message_accepts_and_persists_inbound(self, chat_app):
        c = TestClient(chat_app)
        tok = c.post("/chat/api/session", json={}).json()["token"]
        r = c.post("/chat/api/message",
                   headers={"X-Visitor-Token": tok}, json={"text": "我想咨询"})
        assert r.status_code == 200
        assert r.json().get("pending") is True
        # 历史里应有入站消息
        h = c.get("/chat/api/history", params={"token": tok}).json()
        assert any(m["text"] == "我想咨询" and m["direction"] == "in"
                   for m in h["messages"])

    def test_widget_and_embed_served(self, chat_app):
        c = TestClient(chat_app)
        assert c.get("/chat/widget").status_code == 200
        js = c.get("/chat/embed.js")
        assert js.status_code == 200 and "iframe" in js.text


# ── Phase 3-1：漏斗打通（contacts/Journey）────────────────────────────────────

class TestFunnelWiring:
    def test_gateway_accepts_web_channel(self):
        """web 渠道入站 → 建 ChannelIdentity + Journey 并推进到 ENGAGED。"""
        from src.contacts import (
            ContactStore, HandoffTokenService, MergeService, ContactGateway,
        )
        from src.contacts.rpa_hooks import GatewayContactHooks
        from src.contacts.models import CHANNEL_WEB, STAGE_ENGAGED

        d = tempfile.mkdtemp()
        store = ContactStore(Path(d) / "contacts.db")
        gw = ContactGateway(store, HandoffTokenService(store), MergeService(store))
        hooks = GatewayContactHooks(gw)

        ctx = hooks.on_message(channel=CHANNEL_WEB, account_id="web",
                               external_id="wv_funnel", direction="in",
                               text_preview="你好", display_name="访客X")
        assert ctx is not None
        ci = gw.find_channel_identity(channel=CHANNEL_WEB, account_id="web",
                                      external_id="wv_funnel")
        assert ci is not None
        journey = store.get_journey_by_contact(ci.contact_id)
        assert journey is not None
        assert journey.funnel_stage == STAGE_ENGAGED

    async def test_run_ai_reply_records_funnel_out(self, store):
        svc = WebChatService(account_id="web")
        hub = WebOutboundHub()
        hooks = MagicMock()
        sm = AsyncMock()
        sm.process_message = AsyncMock(return_value="好的")
        await run_web_ai_reply(
            skill_manager=sm, inbox_store=store, hub=hub, service=svc,
            visitor_id="wv_f2", text="问", contact_hooks=hooks,
        )
        assert hooks.on_message.called
        kw = hooks.on_message.call_args.kwargs
        assert kw["channel"] == "web" and kw["direction"] == "out"

    def test_message_endpoint_records_inbound_funnel(self, chat_app):
        hooks = MagicMock()
        chat_app.state.contacts = SimpleNamespace(hooks=hooks)
        c = TestClient(chat_app)
        tok = c.post("/chat/api/session", json={}).json()["token"]
        c.post("/chat/api/message", headers={"X-Visitor-Token": tok},
               json={"text": "咨询一下"})
        assert hooks.on_message.called
        kw = hooks.on_message.call_args_list[0].kwargs
        assert kw["channel"] == "web" and kw["direction"] == "in"


# ── Phase 3-1：Origin 安全白名单 ─────────────────────────────────────────────

class TestOriginAllowlist:
    def test_origin_allowed_logic(self):
        svc = WebChatService(allowed_origins=["https://ok.com"])
        assert svc.origin_allowed("https://ok.com")
        assert svc.origin_allowed("https://ok.com/")  # 容忍尾斜杠
        assert svc.origin_allowed("")                 # 无 Origin（同源）放行
        assert not svc.origin_allowed("https://evil.com")

    def test_origin_empty_allows_all(self):
        svc = WebChatService(allowed_origins=[])
        assert svc.origin_allowed("https://anything.com")

    def test_frame_ancestors_csp(self):
        assert WebChatService(allowed_origins=[]).frame_ancestors_csp() == "frame-ancestors *"
        csp = WebChatService(allowed_origins=["https://a.com"]).frame_ancestors_csp()
        assert "frame-ancestors 'self' https://a.com" == csp

    def test_widget_sets_csp_header(self, chat_app):
        c = TestClient(chat_app)
        r = c.get("/chat/widget")
        assert "Content-Security-Policy" in r.headers

    def test_message_blocks_disallowed_origin(self, store):
        app = FastAPI()
        cfgm = SimpleNamespace(config={"web_chat": {
            "enabled": True, "token_secret": "s", "allowed_origins": ["https://ok.com"],
        }})
        register_web_chat_routes(app, config_manager=cfgm)
        app.state.inbox_store = store
        app.state.skill_manager = AsyncMock()
        c = TestClient(app)
        r = c.post("/chat/api/message", headers={"Origin": "https://evil.com"},
                   json={"text": "x", "token": "whatever"})
        assert r.status_code == 403


# ── Phase 3-2：web→LINE 自动引流 ─────────────────────────────────────────────

from src.web.routes.web_chat_routes import _attempt_web_handoff


class TestWebHandoff:
    def test_gateway_issue_handoff_accepts_web(self):
        """泛化后的 issue_handoff 接受 web 来源 ci，签 token 并推 HANDOFF_READY。"""
        from src.contacts import (
            ContactStore, HandoffTokenService, MergeService, ContactGateway,
        )
        from src.contacts.models import CHANNEL_WEB, STAGE_HANDOFF_READY

        d = tempfile.mkdtemp()
        store = ContactStore(Path(d) / "contacts.db")
        gw = ContactGateway(store, HandoffTokenService(store), MergeService(store))
        # 先来一条入站 → ENGAGED（issue_handoff 的 _transit 需要合法前驱）
        gw.on_message(channel=CHANNEL_WEB, account_id="web", external_id="wv_h",
                      direction="in", text_preview="你好", display_name="V")
        ci = gw.find_channel_identity(channel=CHANNEL_WEB, account_id="web",
                                      external_id="wv_h")
        tok = gw.issue_handoff(messenger_ci_id=ci.channel_identity_id)
        assert tok.token
        journey = store.get_journey_by_contact(ci.contact_id)
        assert journey.funnel_stage == STAGE_HANDOFF_READY

    def test_attempt_disabled_returns_none(self, store):
        svc = WebChatService(handoff_enabled=False)
        gw = MagicMock()
        assert _attempt_web_handoff(gw, svc, store, "wv", latest_in_text="hi") is None
        gw.maybe_issue_handoff.assert_not_called()

    def test_attempt_below_min_inbound_returns_none(self, store):
        svc = WebChatService(account_id="web", handoff_enabled=True, handoff_min_inbound=2)
        # 只有 1 条入站 → 不应触发
        svc.record_message(store, "wv_low", text="第一句", direction="in")
        gw = MagicMock()
        gw.find_channel_identity.return_value = SimpleNamespace(
            channel_identity_id="ci1", contact_id="c1")
        assert _attempt_web_handoff(gw, svc, store, "wv_low", latest_in_text="第一句") is None
        gw.maybe_issue_handoff.assert_not_called()

    async def test_run_ai_reply_appends_handoff(self, store):
        svc = WebChatService(account_id="web", handoff_enabled=True, handoff_min_inbound=2)
        # 预置 2 条入站，越过门槛
        svc.record_message(store, "wv_ho", text="m1", direction="in")
        svc.record_message(store, "wv_ho", text="m2", direction="in")
        hub = WebOutboundHub()
        sm = AsyncMock()
        sm.process_message = AsyncMock(return_value="这是AI回复")

        gw = MagicMock()
        gw.find_channel_identity.return_value = SimpleNamespace(
            channel_identity_id="ci1", contact_id="c1", account_id="web")
        gw._store = MagicMock()
        gw._store.get_journey_by_contact.return_value = SimpleNamespace(
            funnel_stage="ENGAGED", contact_id="c1")
        gw.maybe_issue_handoff.return_value = SimpleNamespace(
            success=True, text="加我 LINE：line_x（暗号 AB12）", token="AB12", script_id="s1")

        out = await run_web_ai_reply(
            skill_manager=sm, inbox_store=store, hub=hub, service=svc,
            visitor_id="wv_ho", text="m2", gateway=gw,
        )
        assert "这是AI回复" in out and "加我 LINE" in out
        gw.on_handoff_sent.assert_called_once()
        assert gw.on_handoff_sent.call_args.kwargs["token"] == "AB12"
