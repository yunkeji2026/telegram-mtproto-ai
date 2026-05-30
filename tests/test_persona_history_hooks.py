"""Profile version history, change hooks, and channel routing tests.

Covers:
- upsert_profile tracks history (up to maxlen=3)
- revert_profile restores previous version
- delete_profile fires hook
- bind/unbind_chat_persona fires hooks
- _last_changed_at updated on every mutation
- register_change_hook receives correct events + kwargs
- history persists through profiles_runtime.yaml roundtrip
- TelegramClient persona_ids[2] for channel type
- TelegramClient persona_ids[1] for group/supergroup
"""
import copy
import tempfile
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from src.utils.persona_manager import PersonaManager, _HISTORY_MAXLEN, PROFILES_RUNTIME_FILENAME


def _pm() -> PersonaManager:
    PersonaManager.reset()
    return PersonaManager.get_instance()


def _mock_cm(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("# stub", encoding="utf-8")
    cm = MagicMock()
    cm.config_path = str(cfg_file)
    cm.config = {}
    return cm


# ── Profile history ────────────────────────────────────────────

def test_no_history_on_first_upsert():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    assert pm.get_profile_history("p") == []


def test_history_recorded_on_overwrite():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    pm.upsert_profile("p", {"name": "V2"})
    h = pm.get_profile_history("p")
    assert len(h) == 1
    assert h[0]["persona"]["name"] == "V1"
    assert "ts" in h[0]


def test_history_capped_at_maxlen():
    pm = _pm()
    # 1st upsert: nothing to push. 2nd-5th each push prev version.
    # After 5 upserts the deque(maxlen=3) holds V2, V3, V4 — V1 was evicted.
    pm.upsert_profile("p", {"name": "V1"})
    pm.upsert_profile("p", {"name": "V2"})
    pm.upsert_profile("p", {"name": "V3"})
    pm.upsert_profile("p", {"name": "V4"})
    pm.upsert_profile("p", {"name": "V5"})  # V1 evicted here
    h = pm.get_profile_history("p")
    assert len(h) == _HISTORY_MAXLEN
    names = [e["persona"]["name"] for e in h]
    assert "V1" not in names
    assert "V2" in names
    assert "V4" in names


def test_revert_restores_previous():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    pm.upsert_profile("p", {"name": "V2"})
    assert pm.revert_profile("p") is True
    assert pm.get_persona_by_id("p")["name"] == "V1"


def test_revert_consumes_history_entry():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    pm.upsert_profile("p", {"name": "V2"})
    pm.upsert_profile("p", {"name": "V3"})
    pm.revert_profile("p")  # back to V2
    pm.revert_profile("p")  # back to V1
    assert pm.get_persona_by_id("p")["name"] == "V1"
    assert pm.revert_profile("p") is False  # no more history


def test_revert_returns_false_when_no_history():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    assert pm.revert_profile("p") is False


def test_revert_returns_false_for_unknown_id():
    pm = _pm()
    assert pm.revert_profile("nonexistent") is False


def test_track_history_false_skips_recording():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    pm.upsert_profile("p", {"name": "V2"}, _track_history=False)
    assert pm.get_profile_history("p") == []


# ── Change hooks ───────────────────────────────────────────────

def test_hook_fires_on_upsert():
    pm = _pm()
    events = []
    pm.register_change_hook(lambda e, **kw: events.append((e, kw)))
    pm.upsert_profile("p", {"name": "X"})
    assert events[0][0] == "profile_upsert"
    assert events[0][1]["profile_id"] == "p"


def test_hook_fires_on_delete():
    pm = _pm()
    events = []
    pm.register_change_hook(lambda e, **kw: events.append(e))
    pm.upsert_profile("p", {"name": "X"})
    pm.delete_profile("p")
    assert "profile_delete" in events


def test_hook_fires_on_bind_unbind():
    pm = _pm()
    events = []
    pm.register_change_hook(lambda e, **kw: events.append(e))
    pm.bind_chat_persona("c1", {"name": "A"})
    pm.unbind_chat_persona("c1")
    assert events == ["chat_bind", "chat_unbind"]


def test_hook_fires_on_revert():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    pm.upsert_profile("p", {"name": "V2"})
    events = []
    pm.register_change_hook(lambda e, **kw: events.append(e))
    pm.revert_profile("p")
    assert "profile_revert" in events


def test_hook_exception_does_not_propagate():
    pm = _pm()
    def bad_hook(e, **kw):
        raise RuntimeError("hook failure")
    pm.register_change_hook(bad_hook)
    pm.upsert_profile("p", {"name": "X"})  # should not raise


def test_last_changed_at_updated():
    pm = _pm()
    assert pm._last_changed_at == 0.0
    pm.upsert_profile("p", {"name": "X"})
    assert pm._last_changed_at > 0.0
    t1 = pm._last_changed_at
    pm.bind_chat_persona("c", {"name": "Y"})
    assert pm._last_changed_at >= t1


def test_multiple_hooks_all_fire():
    pm = _pm()
    fired = [0, 0]
    pm.register_change_hook(lambda e, **kw: fired.__setitem__(0, fired[0] + 1))
    pm.register_change_hook(lambda e, **kw: fired.__setitem__(1, fired[1] + 1))
    pm.upsert_profile("p", {"name": "X"})
    assert fired == [1, 1]


# ── History roundtrip through profiles_runtime.yaml ───────────

def test_history_persisted_and_restored(tmp_path):
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    pm.upsert_profile("p", {"name": "V2"})
    cm = _mock_cm(tmp_path)
    pm.persist_profiles(cm)

    pm2 = _pm()
    pm2.load_profiles_runtime(tmp_path / "config.yaml")
    assert pm2.get_persona_by_id("p")["name"] == "V2"
    h = pm2.get_profile_history("p")
    assert len(h) == 1
    assert h[0]["persona"]["name"] == "V1"
    assert pm2.revert_profile("p") is True
    assert pm2.get_persona_by_id("p")["name"] == "V1"


def test_history_not_required_in_file(tmp_path):
    """Files without _history section load fine."""
    pm = _pm()
    pm.upsert_profile("p", {"name": "X"})
    cm = _mock_cm(tmp_path)
    pm.persist_profiles(cm)
    # Remove _history from file manually
    path = tmp_path / PROFILES_RUNTIME_FILENAME
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.pop("_history", None)
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")

    pm2 = _pm()
    n = pm2.load_profiles_runtime(tmp_path / "config.yaml")
    assert n == 1
    assert pm2.get_profile_history("p") == []


# ── TelegramClient channel routing expression ─────────────────

def _tg_persona_id(persona_ids, chat_type_str):
    is_group = chat_type_str in ('group', 'supergroup', 'channel')
    return (
        (
            persona_ids[2]
            if chat_type_str == 'channel' and len(persona_ids) > 2
            else persona_ids[1]
            if is_group and len(persona_ids) > 1
            else persona_ids[0]
        )
        if persona_ids else ""
    )


def test_channel_uses_index_2():
    assert _tg_persona_id(["pri", "grp", "chan"], 'channel') == "chan"


def test_channel_falls_back_to_index_1_if_no_index_2():
    assert _tg_persona_id(["pri", "grp"], 'channel') == "grp"


def test_channel_falls_back_to_index_0_if_only_one():
    assert _tg_persona_id(["pri"], 'channel') == "pri"


def test_supergroup_uses_index_1():
    assert _tg_persona_id(["pri", "grp", "chan"], 'supergroup') == "grp"


def test_group_uses_index_1():
    assert _tg_persona_id(["pri", "grp", "chan"], 'group') == "grp"


def test_private_uses_index_0():
    assert _tg_persona_id(["pri", "grp", "chan"], 'private') == "pri"


def test_empty_list_returns_empty():
    assert _tg_persona_id([], 'channel') == ""
    assert _tg_persona_id(None, 'group') == ""
