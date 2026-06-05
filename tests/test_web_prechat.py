"""Phase 5-4 — pre-chat 留资 + 身份去重合并 测试。

覆盖：
- ContactStore 属性表（set/get/find_by_attribute）
- 手机/邮箱规整 + Gateway.capture_lead（写属性 / 单命中自动合并 / 多命中入审核）
- WebChatService.prechat 配置解析
- /chat/api/profile 公网端点（落库 + 老客户识别）+ session 带 prechat
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.contacts import (
    ContactGateway,
    ContactStore,
    HandoffTokenService,
    MergeService,
)
from src.contacts.gateway import _normalize_email, _normalize_phone
from src.contacts.models import CHANNEL_LINE, CHANNEL_WEB
from src.inbox.store import InboxStore
from src.integrations.web_chat.service import WebChatService
from src.web.routes.web_chat_routes import register_web_chat_routes


@pytest.fixture()
def cstore():
    d = tempfile.mkdtemp()
    return ContactStore(Path(d) / "contacts.db")


@pytest.fixture()
def gateway(cstore):
    return ContactGateway(cstore, HandoffTokenService(cstore), MergeService(cstore))


# ── 规整 ─────────────────────────────────────────────────────────────────────

class TestNormalize:
    def test_phone_strips_separators_keeps_plus(self):
        assert _normalize_phone("+1 (650) 555-0100") == "+16505550100"
        assert _normalize_phone("0912-345-678") == "0912345678"
        assert _normalize_phone("abc") == ""

    def test_email_lowercases_and_validates(self):
        assert _normalize_email(" Foo@Bar.COM ") == "foo@bar.com"
        assert _normalize_email("noatsign") == ""
        assert _normalize_email("a@b") == ""


# ── ContactStore 属性表 ──────────────────────────────────────────────────────

class TestContactAttributes:
    def test_set_get_roundtrip(self, cstore):
        c = cstore.create_contact(primary_name="A")
        cstore.set_contact_attribute(c.contact_id, "phone", "123")
        cstore.set_contact_attribute(c.contact_id, "email", "a@b.com")
        assert cstore.get_contact_attributes(c.contact_id) == {"phone": "123", "email": "a@b.com"}

    def test_upsert_overwrites(self, cstore):
        c = cstore.create_contact()
        cstore.set_contact_attribute(c.contact_id, "phone", "111")
        cstore.set_contact_attribute(c.contact_id, "phone", "222")
        assert cstore.get_contact_attributes(c.contact_id)["phone"] == "222"

    def test_empty_value_deletes(self, cstore):
        c = cstore.create_contact()
        cstore.set_contact_attribute(c.contact_id, "phone", "111")
        cstore.set_contact_attribute(c.contact_id, "phone", "")
        assert "phone" not in cstore.get_contact_attributes(c.contact_id)

    def test_find_by_attribute_excludes_self(self, cstore):
        a = cstore.create_contact()
        b = cstore.create_contact()
        cstore.set_contact_attribute(a.contact_id, "phone", "999")
        cstore.set_contact_attribute(b.contact_id, "phone", "999")
        found = cstore.find_contacts_by_attribute("phone", "999", exclude_contact_id=a.contact_id)
        assert found == [b.contact_id]


# ── Gateway.capture_lead ─────────────────────────────────────────────────────

class TestCaptureLead:
    def test_stores_attributes_no_merge(self, gateway, cstore):
        out = gateway.capture_lead(
            channel=CHANNEL_WEB, account_id="web", external_id="wv_a",
            name="小王", phone="+1 650 555 0000", email="WANG@x.com",
        )
        assert out["ok"] and not out["merged"] and not out["is_returning"]
        attrs = cstore.get_contact_attributes(out["contact_id"])
        assert attrs["phone"] == "+16505550000"
        assert attrs["email"] == "wang@x.com"
        assert cstore.get_contact(out["contact_id"]).primary_name == "小王"

    def test_returning_customer_merged_by_phone(self, gateway, cstore):
        # 老客户：先在 LINE 出现过，且 Contact 上留过同一手机
        ctx = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                   external_id="line_user", display_name="老张")
        old_contact = ctx.contact.contact_id
        cstore.set_contact_attribute(old_contact, "phone", "+16505550001")

        # 同一人来网页，留同一手机（带不同格式）
        out = gateway.capture_lead(
            channel=CHANNEL_WEB, account_id="web", external_id="wv_b",
            phone="+1-650-555-0001",
        )
        assert out["merged"] and out["is_returning"]
        assert out["matched_contact_id"] == old_contact
        # web ci 已迁到老 Contact
        ci = gateway.find_channel_identity(channel=CHANNEL_WEB, account_id="web",
                                           external_id="wv_b")
        assert ci.contact_id == old_contact

    def test_multiple_matches_go_to_review_not_merge(self, gateway, cstore):
        a = cstore.create_contact()
        b = cstore.create_contact()
        cstore.set_contact_attribute(a.contact_id, "email", "dup@x.com")
        cstore.set_contact_attribute(b.contact_id, "email", "dup@x.com")
        out = gateway.capture_lead(
            channel=CHANNEL_WEB, account_id="web", external_id="wv_c",
            email="dup@x.com",
        )
        assert not out["merged"]
        assert out["review_id"]


# ── WebChatService.prechat 配置 ──────────────────────────────────────────────

class TestPrechatConfig:
    def test_default_fields_when_unset(self):
        svc = WebChatService()
        cfg = svc.prechat_config()
        assert cfg["enabled"] is False
        keys = [f["key"] for f in cfg["fields"]]
        assert keys == ["name", "phone", "email"]

    def test_from_config_parses_prechat(self):
        svc = WebChatService.from_config({"web_chat": {"prechat": {
            "enabled": True, "required": True, "title": "请留资",
            "fields": [{"key": "phone", "label": "电话", "required": True},
                       {"key": "bogus", "label": "x"}],
        }}})
        cfg = svc.prechat_config()
        assert cfg["enabled"] and cfg["required"] and cfg["title"] == "请留资"
        # bogus 被过滤
        assert [f["key"] for f in cfg["fields"]] == ["phone"]


# ── /chat/api/profile 端点 ───────────────────────────────────────────────────

@pytest.fixture()
def chat_app():
    d = tempfile.mkdtemp()
    inbox = InboxStore(Path(d) / "inbox.db")
    cstore = ContactStore(Path(d) / "contacts.db")
    gw = ContactGateway(cstore, HandoffTokenService(cstore), MergeService(cstore))
    from src.contacts.rpa_hooks import GatewayContactHooks

    app = FastAPI()
    cfgm = SimpleNamespace(config={"web_chat": {
        "enabled": True, "account_id": "web", "token_secret": "testsecret",
        "greeting": "你好呀",
        "prechat": {"enabled": True, "required": False},
    }})
    register_web_chat_routes(app, config_manager=cfgm)
    app.state.inbox_store = inbox
    app.state.skill_manager = AsyncMock()
    app.state.contacts = SimpleNamespace(hooks=GatewayContactHooks(gw), gateway=gw)
    app.state._cstore = cstore
    return app


class TestProfileEndpoint:
    def test_session_includes_prechat(self, chat_app):
        c = TestClient(chat_app)
        d = c.post("/chat/api/session", json={}).json()
        assert d["prechat"]["enabled"] is True

    def test_profile_stores_lead(self, chat_app):
        c = TestClient(chat_app)
        tok = c.post("/chat/api/session", json={}).json()["token"]
        r = c.post("/chat/api/profile", headers={"X-Visitor-Token": tok},
                   json={"name": "新客", "phone": "0912 345 678"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and not body["is_returning"]
        # 写进了 Contact 属性
        cstore = chat_app.state._cstore
        ci = chat_app.state.contacts.gateway.find_channel_identity(
            channel="web", account_id="web", external_id=_visitor_of(tok))
        attrs = cstore.get_contact_attributes(ci.contact_id)
        assert attrs["phone"] == "0912345678"

    def test_profile_rejects_empty(self, chat_app):
        c = TestClient(chat_app)
        tok = c.post("/chat/api/session", json={}).json()["token"]
        r = c.post("/chat/api/profile", headers={"X-Visitor-Token": tok}, json={})
        assert r.status_code == 400

    def test_profile_requires_token(self, chat_app):
        c = TestClient(chat_app)
        r = c.post("/chat/api/profile", json={"name": "x", "token": "bad.tok"})
        assert r.status_code == 401


def _visitor_of(token: str) -> str:
    from src.integrations.web_chat.tokens import verify_visitor_token
    return verify_visitor_token("testsecret", token) or ""
