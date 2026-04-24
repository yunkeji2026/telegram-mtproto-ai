"""
Tests for Persona API endpoints — Phase 2B.

Tests the web admin API for persona management:
- GET /api/persona — get current persona
- GET /api/persona/bindings — list all chat bindings
- POST /api/persona/bind — bind persona to chat
- POST /api/persona/unbind — unbind persona from chat
- POST /api/persona/update-default — update default persona
- GET /api/persona/preview-prompt — preview assembled prompt
"""

import asyncio
import json
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from httpx import AsyncClient, ASGITransport
from src.web.admin import create_app
from src.utils.config_manager import ConfigManager
from src.utils.persona_manager import PersonaManager


@pytest.fixture
def test_app(tmp_path):
    """Create a minimal test app with persona support."""
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(yaml.dump({"channels": {}}), encoding="utf-8")

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()

    PersonaManager.reset()
    PersonaManager.get_instance().set_domain_persona({
        "name": "TestBot",
        "role": "测试助手",
        "personality": {"traits": ["友好"]},
    })

    app = create_app(cm)
    return app


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token"}


class TestPersonaGetAPI:
    @pytest.mark.asyncio
    async def test_get_default_persona(self, test_app, auth_headers):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/persona", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "persona" in data
            assert data["persona"]["name"] == "TestBot"
            assert data["is_default"] is True

    @pytest.mark.asyncio
    async def test_get_persona_for_chat(self, test_app, auth_headers):
        PersonaManager.get_instance().bind_chat_persona("12345", {
            "name": "GroupBot", "role": "群管"
        })
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/persona?chat_id=12345", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["persona"]["name"] == "GroupBot"
            assert data["is_default"] is False


class TestPersonaBindingsAPI:
    @pytest.mark.asyncio
    async def test_get_bindings_empty(self, test_app, auth_headers):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/persona/bindings", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "bindings" in data


class TestPersonaBindAPI:
    @pytest.mark.asyncio
    async def test_bind_persona(self, test_app, auth_headers):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/persona/bind",
                headers={**auth_headers, "Content-Type": "application/json"},
                json={
                    "chat_id": "99999",
                    "persona": {"name": "CustomBot", "role": "自定义"},
                },
            )
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            # Verify binding
            resp2 = await client.get("/api/persona?chat_id=99999", headers=auth_headers)
            assert resp2.json()["persona"]["name"] == "CustomBot"

    @pytest.mark.asyncio
    async def test_bind_missing_data(self, test_app, auth_headers):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/persona/bind",
                headers={**auth_headers, "Content-Type": "application/json"},
                json={"chat_id": "123"},
            )
            assert resp.status_code == 400


class TestPersonaUnbindAPI:
    @pytest.mark.asyncio
    async def test_unbind_persona(self, test_app, auth_headers):
        PersonaManager.get_instance().bind_chat_persona("111", {"name": "X", "role": "x"})
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/persona/unbind",
                headers={**auth_headers, "Content-Type": "application/json"},
                json={"chat_id": "111"},
            )
            assert resp.status_code == 200
            assert resp.json()["ok"] is True


class TestPersonaUpdateDefaultAPI:
    @pytest.mark.asyncio
    async def test_update_default(self, test_app, auth_headers):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/persona/update-default",
                headers={**auth_headers, "Content-Type": "application/json"},
                json={"persona": {"name": "NewDefault", "role": "新默认"}},
            )
            assert resp.status_code == 200

            resp2 = await client.get("/api/persona", headers=auth_headers)
            assert resp2.json()["persona"]["name"] == "NewDefault"


class TestPersonaPreviewAPI:
    @pytest.mark.asyncio
    async def test_preview_prompt(self, test_app, auth_headers):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/persona/preview-prompt", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "prompt" in data
            assert "TestBot" in data["prompt"]
