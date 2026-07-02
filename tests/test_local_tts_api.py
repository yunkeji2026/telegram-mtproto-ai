"""API contract for /api/voice/local-tts/* (coupled IndexTTS2 toggle)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.web.routes.voice_routes import register_voice_routes


def _noop_auth(request: Request):
    return None


@pytest.fixture
def voice_app(tmp_path, monkeypatch):
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "minicpm_clone": {
            "base_url": "http://127.0.0.1:7899",
            "local_autostart": {"enabled": True, "stop_with_app": True},
        }
    }
    cm.set_overlay_flag.return_value = (True, "ok")

    sup = MagicMock()
    sup.status_snapshot.return_value = {
        "enabled": True, "mode": "managed", "model_loaded": True, "reachable": True,
    }
    sup.reload_from_config = MagicMock()
    sup.apply_enabled = AsyncMock(return_value={"runtime_action": "noop", "runtime_ok": True})

    app.state.local_tts_supervisor = sup
    register_voice_routes(app, api_auth=_noop_auth, config_manager=cm)
    return app, cm, sup


def test_local_tts_status(voice_app):
    app, cm, sup = voice_app
    c = TestClient(app)
    r = c.get("/api/voice/local-tts/status")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["configured_enabled"] is True
    assert body["mode"] == "managed"
    sup.status_snapshot.assert_called_once()


def test_local_tts_toggle(voice_app):
    app, cm, sup = voice_app
    c = TestClient(app)
    r = c.post("/api/voice/local-tts/toggle", json={"enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["enabled"] is False
    cm.set_overlay_flag.assert_called_once_with(
        "minicpm_clone.local_autostart.enabled", False)
    sup.reload_from_config.assert_called_once()
    sup.apply_enabled.assert_awaited_once_with(False)
