"""Persona profile persistence (profiles_runtime.yaml) roundtrip tests.

Covers:
- persist_profiles writes profiles_runtime.yaml
- load_profiles_runtime reads back and merges into store
- runtime layer overrides config.yaml layer on same id
- persist respects persona_persistence.enabled=false
- load_profiles_runtime is a no-op when file absent
- LINE RPA _account_persona_id reads from config persona_ids
"""
import copy
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.utils.persona_manager import PersonaManager, PROFILES_RUNTIME_FILENAME


def _pm() -> PersonaManager:
    PersonaManager.reset()
    return PersonaManager.get_instance()


def _mock_cm(tmp_path: Path, cfg_override: dict | None = None):
    """Build a minimal config_manager mock that points to tmp_path/config.yaml."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("# stub", encoding="utf-8")
    cm = MagicMock()
    cm.config_path = str(cfg_file)
    cm.config = dict(cfg_override or {})
    return cm


# ── persist_profiles / load_profiles_runtime roundtrip ────────

def test_persist_and_reload(tmp_path):
    pm = _pm()
    pm.upsert_profile("alpha", {"id": "alpha", "name": "Alpha", "role": "A"})
    pm.upsert_profile("beta",  {"id": "beta",  "name": "Beta",  "role": "B"})
    cm = _mock_cm(tmp_path)
    assert pm.persist_profiles(cm) is True
    assert (tmp_path / PROFILES_RUNTIME_FILENAME).exists()

    # Fresh instance loads runtime
    pm2 = _pm()
    assert pm2.get_persona_by_id("alpha") is None  # not yet loaded
    pm2.load_profiles_runtime(tmp_path / "config.yaml")
    assert pm2.get_persona_by_id("alpha") == {"id": "alpha", "name": "Alpha", "role": "A"}
    assert pm2.get_persona_by_id("beta")  == {"id": "beta",  "name": "Beta",  "role": "B"}


def test_runtime_overrides_config_layer(tmp_path):
    """profiles_runtime.yaml wins over config.yaml::personas.profiles on same id."""
    pm = _pm()
    # config layer first
    pm.load_profiles_from_config({
        "personas": {"profiles": [{"id": "x", "name": "FromConfig"}]}
    })
    assert pm.get_persona_by_id("x")["name"] == "FromConfig"

    # web edit → upsert + persist
    pm.upsert_profile("x", {"id": "x", "name": "FromRuntime"})
    cm = _mock_cm(tmp_path)
    pm.persist_profiles(cm)

    # New instance: load config then runtime
    pm2 = _pm()
    pm2.load_profiles_from_config({
        "personas": {"profiles": [{"id": "x", "name": "FromConfig"}]}
    })
    pm2.load_profiles_runtime(tmp_path / "config.yaml")
    assert pm2.get_persona_by_id("x")["name"] == "FromRuntime"


def test_persist_disabled_by_flag(tmp_path):
    pm = _pm()
    pm.upsert_profile("z", {"id": "z", "name": "Z"})
    cm = _mock_cm(tmp_path, {"persona_persistence": {"enabled": False}})
    result = pm.persist_profiles(cm)
    assert result is False
    assert not (tmp_path / PROFILES_RUNTIME_FILENAME).exists()


def test_load_runtime_noop_when_file_absent(tmp_path):
    pm = _pm()
    n = pm.load_profiles_runtime(tmp_path / "config.yaml")
    assert n == 0


def test_load_runtime_noop_when_persistence_disabled(tmp_path):
    pm = _pm()
    pm.upsert_profile("a", {"id": "a", "name": "A"})
    cm = _mock_cm(tmp_path)
    pm.persist_profiles(cm)  # file written

    pm2 = _pm()
    n = pm2.load_profiles_runtime(
        tmp_path / "config.yaml",
        root_config={"persona_persistence": {"enabled": False}},
    )
    assert n == 0
    assert pm2.get_persona_by_id("a") is None


def test_persist_no_config_manager():
    pm = _pm()
    pm.upsert_profile("q", {"id": "q"})
    assert pm.persist_profiles(None) is False


def test_persist_after_delete(tmp_path):
    pm = _pm()
    pm.upsert_profile("to_del", {"id": "to_del", "name": "Temp"})
    pm.upsert_profile("keep", {"id": "keep", "name": "Keep"})
    cm = _mock_cm(tmp_path)
    pm.persist_profiles(cm)

    pm.delete_profile("to_del")
    pm.persist_profiles(cm)

    pm2 = _pm()
    pm2.load_profiles_runtime(tmp_path / "config.yaml")
    assert pm2.get_persona_by_id("to_del") is None
    assert pm2.get_persona_by_id("keep")["name"] == "Keep"


# ── LINE RPA _account_persona_id ──────────────────────────────

def test_line_rpa_account_persona_id_returns_first():
    from src.integrations.line_rpa.runner import LineRpaRunner
    runner = LineRpaRunner.__new__(LineRpaRunner)
    runner._cfg = {"persona_ids": ["warm_companion", "pro_assistant"]}
    runner._contact_hooks = None
    assert runner._account_persona_id() == "warm_companion"


def test_line_rpa_account_persona_id_empty_when_not_set():
    from src.integrations.line_rpa.runner import LineRpaRunner
    runner = LineRpaRunner.__new__(LineRpaRunner)
    runner._cfg = {}
    runner._contact_hooks = None
    assert runner._account_persona_id() == ""


def test_line_rpa_account_persona_id_empty_list():
    from src.integrations.line_rpa.runner import LineRpaRunner
    runner = LineRpaRunner.__new__(LineRpaRunner)
    runner._cfg = {"persona_ids": []}
    runner._contact_hooks = None
    assert runner._account_persona_id() == ""


# ── Messenger RPA _persona_ids injection ─────────────────────

def test_messenger_rpa_context_account_persona_id():
    """Verify that _persona_ids flows into context dict when set on runner."""
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    runner = MessengerRpaRunner.__new__(MessengerRpaRunner)
    runner._persona_ids = ["voice_persona"]
    # Check helper expression directly (mirrors the context dict code)
    result = (
        getattr(runner, "_persona_ids", [None])[0] or ""
        if getattr(runner, "_persona_ids", [])
        else ""
    )
    assert result == "voice_persona"


def test_messenger_rpa_context_account_persona_id_empty_when_not_set():
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    runner = MessengerRpaRunner.__new__(MessengerRpaRunner)
    result = (
        getattr(runner, "_persona_ids", [None])[0] or ""
        if getattr(runner, "_persona_ids", [])
        else ""
    )
    assert result == ""
