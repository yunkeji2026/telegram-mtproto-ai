"""Facebook Page Webhook 单元测试（与 line_webhook 同构覆盖）。

不依赖真 FB；用 fastapi.TestClient + AsyncMock 模拟 SkillManager。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.integrations.facebook_webhook import (
    register_fb_messenger_routes,
    verify_fb_signature,
)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


def test_verify_signature_basic():
    secret = "topsecret"
    body = b'{"object":"page"}'
    assert verify_fb_signature(body, _sign(secret, body), secret) is True
    # 错的 secret
    assert verify_fb_signature(body, _sign("wrong", body), secret) is False
    # 错的 body
    assert verify_fb_signature(b"changed", _sign(secret, body), secret) is False
    # 空 secret
    assert verify_fb_signature(body, _sign(secret, body), "") is False
    # 错的 prefix
    assert verify_fb_signature(body, "sha1=abc", secret) is False


def test_register_skips_when_disabled():
    app = FastAPI()
    cm = MagicMock()
    cm.config = {"facebook_messenger": {"enabled": False}}
    register_fb_messenger_routes(app, cm, MagicMock())
    assert getattr(app.state, "fb_webhook_path", None) is None


def test_register_skips_when_missing_required_field():
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_access_token": "",  # 故意缺
            "app_secret": "abc",
            "verify_token": "v",
        }
    }
    tc = MagicMock()
    tc.skill_manager = MagicMock()
    register_fb_messenger_routes(app, cm, tc)
    assert getattr(app.state, "fb_webhook_path", None) is None


def test_get_verify_handshake_ok():
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_id": "100000",
            "page_access_token": "tok",
            "app_secret": "sec",
            "verify_token": "myvtoken",
        }
    }
    tc = MagicMock()
    tc.skill_manager = MagicMock()
    tc.skill_manager.process_message = AsyncMock(return_value="echo")
    register_fb_messenger_routes(app, cm, tc)
    assert app.state.fb_webhook_path == "/fb/webhook"

    client = TestClient(app)
    # 正确 verify_token
    r = client.get(
        "/fb/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "myvtoken",
            "hub.challenge": "1234567",
        },
    )
    assert r.status_code == 200
    assert r.text == "1234567"
    # 错的 verify_token
    r = client.get(
        "/fb/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "WRONG",
            "hub.challenge": "abc",
        },
    )
    assert r.status_code == 403


def test_post_event_signature_required():
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_id": "100000",
            "page_access_token": "tok",
            "app_secret": "sec",
            "verify_token": "v",
        }
    }
    tc = MagicMock()
    tc.skill_manager = MagicMock()
    tc.skill_manager.process_message = AsyncMock(return_value="echo")
    register_fb_messenger_routes(app, cm, tc)

    client = TestClient(app)
    body = b'{"object":"page","entry":[]}'
    # 没有签名 → 403
    r = client.post("/fb/webhook", content=body)
    assert r.status_code == 403
    # 错误签名 → 403
    r = client.post(
        "/fb/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign("WRONG", body)},
    )
    assert r.status_code == 403
    # 正确签名（空 entry）→ 200
    r = client.post(
        "/fb/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign("sec", body)},
    )
    assert r.status_code == 200


def test_post_event_ignores_echo_and_delivery(monkeypatch):
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_id": "100000",
            "page_access_token": "tok",
            "app_secret": "sec",
            "verify_token": "v",
        }
    }
    sm = MagicMock()
    sm.process_message = AsyncMock(return_value="this should not be sent")
    tc = MagicMock()
    tc.skill_manager = sm

    # 拦截真实的 SDK 网络调用
    sent: list[Any] = []

    async def fake_send(psid, text, token, **kw):
        sent.append((psid, text, kw))
        return {"ok": True, "data": {}}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_send_with_window_fallback",
        fake_send,
    )

    register_fb_messenger_routes(app, cm, tc)
    client = TestClient(app)

    body_dict = {
        "object": "page",
        "entry": [
            {
                "id": "100000",
                "time": 0,
                "messaging": [
                    # echo（Page 自己发出的回声）
                    {
                        "sender": {"id": "100000"},
                        "recipient": {"id": "USER1"},
                        "timestamp": 1,
                        "message": {"text": "ignored", "is_echo": True, "mid": "m1"},
                    },
                    # delivery（已送达回执）
                    {
                        "sender": {"id": "USER1"},
                        "recipient": {"id": "100000"},
                        "delivery": {"mids": ["m1"], "watermark": 1},
                    },
                    # read 回执
                    {
                        "sender": {"id": "USER1"},
                        "recipient": {"id": "100000"},
                        "read": {"watermark": 1},
                    },
                ],
            }
        ],
    }
    body = json.dumps(body_dict).encode("utf-8")
    r = client.post(
        "/fb/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign("sec", body)},
    )
    assert r.status_code == 200
    sm.process_message.assert_not_called()
    assert sent == []


def test_post_event_routes_text_to_skill_manager(monkeypatch):
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_id": "100000",
            "page_access_token": "tok",
            "app_secret": "sec",
            "verify_token": "v",
        }
    }
    sm = MagicMock()
    sm.process_message = AsyncMock(return_value="hello back!")
    tc = MagicMock()
    tc.skill_manager = sm

    sent: list[Any] = []

    async def fake_send(psid, text, token, **kw):
        sent.append({"psid": psid, "text": text, "kw": kw})
        return {"ok": True, "data": {}}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_send_with_window_fallback",
        fake_send,
    )

    register_fb_messenger_routes(app, cm, tc)
    client = TestClient(app)

    body_dict = {
        "object": "page",
        "entry": [
            {
                "id": "100000",
                "time": 0,
                "messaging": [
                    {
                        "sender": {"id": "USER42"},
                        "recipient": {"id": "100000"},
                        "timestamp": 1700000000000,
                        "message": {"text": "hi page", "mid": "m99"},
                    }
                ],
            }
        ],
    }
    body = json.dumps(body_dict).encode("utf-8")
    r = client.post(
        "/fb/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign("sec", body)},
    )
    assert r.status_code == 200
    sm.process_message.assert_awaited_once()
    args, kwargs = sm.process_message.await_args
    assert kwargs["text"] == "hi page"
    assert kwargs["user_id"] == "fb:USER42"
    assert kwargs["context"]["channel"] == "facebook_messenger"
    assert kwargs["context"]["fb_psid"] == "USER42"
    # 回复发出去
    assert len(sent) == 1
    assert sent[0]["psid"] == "USER42"
    assert sent[0]["text"] == "hello back!"
