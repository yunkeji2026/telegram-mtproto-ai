"""LINE Webhook 签名校验与路由注册（不启完整管理端）。"""

from __future__ import annotations

import base64
import hashlib
import hmac

from fastapi import FastAPI

from src.integrations.line_webhook import verify_line_signature


def test_verify_line_signature_accepts_valid():
    secret = "test_secret"
    body = b'{"events":[]}'
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    sig = base64.b64encode(mac).decode("utf-8")
    assert verify_line_signature(body, sig, secret) is True


def test_verify_line_signature_rejects_tamper():
    secret = "test_secret"
    body = b'{"events":[]}'
    assert verify_line_signature(body, "bogus", secret) is False


def test_register_line_skips_when_disabled():
    from src.integrations.line_webhook import register_line_routes
    from unittest.mock import MagicMock

    app = FastAPI()
    cm = MagicMock()
    cm.config = {"line": {"enabled": False}}
    register_line_routes(app, cm, MagicMock())
    assert getattr(app.state, "line_webhook_path", None) is None


def test_register_line_sets_path_and_route_when_enabled():
    from src.integrations.line_webhook import register_line_routes
    from unittest.mock import AsyncMock, MagicMock

    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "line": {
            "enabled": True,
            "channel_secret": "sec",
            "channel_access_token": "token",
            "webhook_path": "/line/webhook",
        }
    }
    tc = MagicMock()
    tc.skill_manager = MagicMock()
    tc.skill_manager.process_message = AsyncMock(return_value=None)

    register_line_routes(app, cm, tc)
    assert app.state.line_webhook_path == "/line/webhook"

    routes = [getattr(r, "path", None) for r in app.routes]
    assert "/line/webhook" in routes
