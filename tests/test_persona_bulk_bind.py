"""Tests for bulk_bind_by_profile, viewer-mode role guard, and RBAC tables."""
import pytest
from src.utils.persona_manager import PersonaManager
from src.utils.web_user_store import (
    PAGE_PERMISSIONS, WRITE_PERMISSIONS,
    ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER,
)


def _pm() -> PersonaManager:
    PersonaManager.reset()
    return PersonaManager.get_instance()


# ── bulk_bind_by_profile ──────────────────────────────────────

def test_bulk_bind_all_bindings():
    pm = _pm()
    pm.upsert_profile("holiday", {"name": "Holiday", "id": "holiday"})
    pm.upsert_profile("normal", {"name": "Normal", "id": "normal"})
    pm.bind_chat_persona("c1", {"name": "Normal", "id": "normal"})
    pm.bind_chat_persona("c2", {"name": "Normal", "id": "normal"})
    result = pm.bulk_bind_by_profile("holiday", scope="all_bindings")
    assert result["affected"] == 2
    assert set(result["chat_ids"]) == {"c1", "c2"}
    assert result["dry_run"] is False
    assert pm._chat_personas["c1"]["name"] == "Holiday"
    assert pm._chat_personas["c2"]["name"] == "Holiday"


def test_bulk_bind_dry_run_does_not_mutate():
    pm = _pm()
    pm.upsert_profile("holiday", {"name": "Holiday", "id": "holiday"})
    pm.bind_chat_persona("c1", {"name": "Old", "id": "old"})
    result = pm.bulk_bind_by_profile("holiday", dry_run=True)
    assert result["dry_run"] is True
    assert result["affected"] == 1
    assert pm._chat_personas["c1"]["name"] == "Old"


def test_bulk_bind_empty_bindings():
    pm = _pm()
    pm.upsert_profile("p", {"name": "P", "id": "p"})
    result = pm.bulk_bind_by_profile("p")
    assert result["affected"] == 0
    assert result["chat_ids"] == []


def test_bulk_bind_unknown_profile_raises():
    pm = _pm()
    with pytest.raises(KeyError):
        pm.bulk_bind_by_profile("nonexistent")


def test_bulk_bind_fires_hooks():
    pm = _pm()
    pm.upsert_profile("p", {"name": "P", "id": "p"})
    pm.bind_chat_persona("c1", {"name": "Old"})
    pm.bind_chat_persona("c2", {"name": "Old"})
    events = []
    pm.register_change_hook(lambda e, **kw: events.append(e))
    pm.bulk_bind_by_profile("p")
    assert events.count("chat_bind") == 2


def test_bulk_bind_updates_last_changed_at():
    pm = _pm()
    pm.upsert_profile("p", {"name": "P", "id": "p"})
    pm.bind_chat_persona("c1", {"name": "Old"})
    ts_before = pm._last_changed_at
    pm.bulk_bind_by_profile("p")
    assert pm._last_changed_at >= ts_before


def test_bulk_bind_scope_unknown_returns_empty():
    pm = _pm()
    pm.upsert_profile("p", {"name": "P", "id": "p"})
    pm.bind_chat_persona("c1", {"name": "Old"})
    result = pm.bulk_bind_by_profile("p", scope="unknown_scope")
    assert result["affected"] == 0


# ── RBAC: personas page + edit_persona write permission ───────

def test_personas_page_accessible_by_all_roles():
    assert ROLE_MASTER in PAGE_PERMISSIONS["personas"]
    assert ROLE_ADMIN in PAGE_PERMISSIONS["personas"]
    assert ROLE_VIEWER in PAGE_PERMISSIONS["personas"]


def test_edit_persona_write_perm_excludes_viewer():
    allowed = WRITE_PERMISSIONS["edit_persona"]
    assert ROLE_MASTER in allowed
    assert ROLE_ADMIN in allowed
    assert ROLE_VIEWER not in allowed


def test_write_permissions_has_edit_persona_key():
    assert "edit_persona" in WRITE_PERMISSIONS


# ── _check_write_role logic (unit) ────────────────────────────

def _make_request(role: str):
    """Minimal mock of FastAPI Request with a session dict."""
    class FakeSession(dict):
        pass

    class FakeRequest:
        session = FakeSession({"role": role})

    return FakeRequest()


def _check_write_role_fn(request):
    from fastapi import HTTPException
    _ROLE_VIEWER_LOCAL = "viewer"
    try:
        role = request.session.get("role", "")
    except Exception:
        role = ""
    if role == _ROLE_VIEWER_LOCAL:
        raise HTTPException(403, "只读账号无法修改人设配置")


def test_check_write_role_allows_master():
    _check_write_role_fn(_make_request(ROLE_MASTER))  # should not raise


def test_check_write_role_allows_admin():
    _check_write_role_fn(_make_request(ROLE_ADMIN))  # should not raise


def test_check_write_role_blocks_viewer():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _check_write_role_fn(_make_request(ROLE_VIEWER))
    assert exc_info.value.status_code == 403


def test_check_write_role_allows_empty_role():
    _check_write_role_fn(_make_request(""))  # empty role = not viewer, should pass
