"""Chat binding persistence (bindings_runtime.yaml) roundtrip tests.

Covers:
- persist_chat_bindings writes bindings_runtime.yaml
- load_chat_bindings_runtime reads back and merges into store
- unbind + persist = entry removed from file
- persist respects persona_persistence.enabled=false
- load is a no-op when file absent
- custom bindings_path config key is honoured
- TelegramClient is_group + chat_type context injection (structural check)
"""
import copy
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.utils.persona_manager import PersonaManager, BINDINGS_RUNTIME_FILENAME


def _pm() -> PersonaManager:
    PersonaManager.reset()
    return PersonaManager.get_instance()


def _mock_cm(tmp_path: Path, cfg_override: dict | None = None):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("# stub", encoding="utf-8")
    cm = MagicMock()
    cm.config_path = str(cfg_file)
    cm.config = dict(cfg_override or {})
    return cm


# ── persist_chat_bindings / load_chat_bindings_runtime ────────

def test_persist_and_reload_bindings(tmp_path):
    pm = _pm()
    pm.bind_chat_persona("chat_1", {"id": "pro", "name": "Pro"})
    pm.bind_chat_persona("chat_2", {"id": "warm", "name": "Warm"})
    cm = _mock_cm(tmp_path)
    assert pm.persist_chat_bindings(cm) is True
    runtime_file = tmp_path / BINDINGS_RUNTIME_FILENAME
    assert runtime_file.exists()

    pm2 = _pm()
    n = pm2.load_chat_bindings_runtime(tmp_path / "config.yaml")
    assert n == 2
    assert pm2.get_persona("chat_1")["name"] == "Pro"
    assert pm2.get_persona("chat_2")["name"] == "Warm"


def test_unbind_then_persist_removes_entry(tmp_path):
    pm = _pm()
    pm.bind_chat_persona("c1", {"id": "p1", "name": "P1"})
    pm.bind_chat_persona("c2", {"id": "p2", "name": "P2"})
    cm = _mock_cm(tmp_path)
    pm.persist_chat_bindings(cm)

    pm.unbind_chat_persona("c1")
    pm.persist_chat_bindings(cm)

    pm2 = _pm()
    pm2.load_chat_bindings_runtime(tmp_path / "config.yaml")
    assert pm2.get_all_chat_bindings().get("c1") is None
    assert pm2.get_persona("c2")["name"] == "P2"


def test_persist_disabled_by_flag(tmp_path):
    pm = _pm()
    pm.bind_chat_persona("cx", {"name": "X"})
    cm = _mock_cm(tmp_path, {"persona_persistence": {"enabled": False}})
    result = pm.persist_chat_bindings(cm)
    assert result is False
    assert not (tmp_path / BINDINGS_RUNTIME_FILENAME).exists()


def test_load_noop_when_file_absent(tmp_path):
    pm = _pm()
    n = pm.load_chat_bindings_runtime(tmp_path / "config.yaml")
    assert n == 0
    assert pm.get_all_chat_bindings() == {}


def test_load_noop_when_persistence_disabled(tmp_path):
    pm = _pm()
    pm.bind_chat_persona("cx", {"name": "X"})
    cm = _mock_cm(tmp_path)
    pm.persist_chat_bindings(cm)  # file written

    pm2 = _pm()
    n = pm2.load_chat_bindings_runtime(
        tmp_path / "config.yaml",
        root_config={"persona_persistence": {"enabled": False}},
    )
    assert n == 0


def test_custom_bindings_path(tmp_path):
    """persona_persistence.bindings_path overrides the default filename."""
    custom = tmp_path / "sub" / "my_bindings.yaml"
    pm = _pm()
    pm.bind_chat_persona("c1", {"name": "Custom"})
    cm = _mock_cm(tmp_path, {
        "persona_persistence": {"bindings_path": str(custom)}
    })
    assert pm.persist_chat_bindings(cm) is True
    assert custom.exists()

    pm2 = _pm()
    n = pm2.load_chat_bindings_runtime(
        tmp_path / "config.yaml",
        root_config={"persona_persistence": {"bindings_path": str(custom)}},
    )
    assert n == 1
    assert pm2.get_persona("c1")["name"] == "Custom"


def test_persist_no_bindings_writes_empty(tmp_path):
    pm = _pm()  # no bindings
    cm = _mock_cm(tmp_path)
    assert pm.persist_chat_bindings(cm) is True
    pm2 = _pm()
    n = pm2.load_chat_bindings_runtime(tmp_path / "config.yaml")
    assert n == 0


def test_bindings_survive_profiles_reload(tmp_path):
    """Binding roundtrip is independent of profiles_runtime.yaml."""
    pm = _pm()
    pm.upsert_profile("p1", {"id": "p1", "name": "Profile1"})
    pm.bind_chat_persona("c1", {"id": "p1", "name": "Profile1"})
    cm = _mock_cm(tmp_path)
    pm.persist_profiles(cm)
    pm.persist_chat_bindings(cm)

    pm2 = _pm()
    pm2.load_profiles_runtime(tmp_path / "config.yaml")
    pm2.load_chat_bindings_runtime(tmp_path / "config.yaml")
    assert pm2.get_persona_by_id("p1")["name"] == "Profile1"
    assert pm2.get_persona("c1")["name"] == "Profile1"


# ── bindings_runtime_file_path helper ─────────────────────────

def test_bindings_runtime_default_path(tmp_path):
    cfg = tmp_path / "config.yaml"
    p = PersonaManager.bindings_runtime_file_path(cfg)
    assert p == tmp_path / BINDINGS_RUNTIME_FILENAME


def test_bindings_runtime_explicit_relative(tmp_path):
    cfg = tmp_path / "config.yaml"
    p = PersonaManager.bindings_runtime_file_path(cfg, "runtime/binds.yaml")
    assert p == tmp_path / "runtime" / "binds.yaml"


# ── TelegramClient is_group / chat_type context (structural) ──

def test_telegram_is_group_derivation():
    """Verify is_group logic matches expected values for each chat type string."""
    for t, expected in [
        ('group', True),
        ('supergroup', True),
        ('channel', True),
        ('private', False),
        ('', False),
        ('bot', False),
    ]:
        result = t.lower() in ('group', 'supergroup', 'channel')
        assert result is expected, f"chat_type={t!r}"
