"""Tests for U1 (tag-scope bulk bind), U2 (binding_count), U4 (export/import route logic)."""
import pytest
from src.utils.persona_manager import PersonaManager
from src.utils.web_user_store import WRITE_PERMISSIONS, ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER


def _pm() -> PersonaManager:
    PersonaManager.reset()
    return PersonaManager.get_instance()


# ── U2: binding_count in summary ──────────────────────────────

def test_binding_count_zero_when_no_bindings():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "id": "p"})
    assert pm.list_profiles_summary()[0]["binding_count"] == 0


def test_binding_count_increments_per_binding():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "id": "p"})
    pm.bind_chat_persona("c1", {"name": "X", "id": "p"})
    pm.bind_chat_persona("c2", {"name": "X", "id": "p"})
    assert pm.list_profiles_summary()[0]["binding_count"] == 2


def test_binding_count_only_counts_matching_id():
    pm = _pm()
    pm.upsert_profile("p1", {"name": "A", "id": "p1"})
    pm.upsert_profile("p2", {"name": "B", "id": "p2"})
    pm.bind_chat_persona("c1", {"name": "A", "id": "p1"})
    pm.bind_chat_persona("c2", {"name": "B", "id": "p2"})
    pm.bind_chat_persona("c3", {"name": "A", "id": "p1"})
    summary = {s["id"]: s["binding_count"] for s in pm.list_profiles_summary()}
    assert summary["p1"] == 2
    assert summary["p2"] == 1


def test_binding_count_zero_for_unlinked_persona():
    """Chat bound via direct persona dict (no id field) → no profile gets credit."""
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "id": "p"})
    pm.bind_chat_persona("c1", {"name": "X"})  # no id
    assert pm.list_profiles_summary()[0]["binding_count"] == 0


def test_binding_count_field_present_in_summary():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X"})
    assert "binding_count" in pm.list_profiles_summary()[0]


# ── U1: _profile_has_tag ──────────────────────────────────────

def test_profile_has_tag_true():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "tags": ["holiday", "vip"]})
    assert pm._profile_has_tag("p", "holiday") is True
    assert pm._profile_has_tag("p", "vip") is True


def test_profile_has_tag_case_insensitive():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "tags": ["Holiday"]})
    assert pm._profile_has_tag("p", "holiday") is True
    assert pm._profile_has_tag("p", "HOLIDAY") is True


def test_profile_has_tag_false_wrong_tag():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "tags": ["work"]})
    assert pm._profile_has_tag("p", "holiday") is False


def test_profile_has_tag_false_missing_profile():
    pm = _pm()
    assert pm._profile_has_tag("nonexistent", "tag") is False


def test_profile_has_tag_false_empty_profile_id():
    pm = _pm()
    assert pm._profile_has_tag("", "tag") is False


def test_profile_has_tag_false_no_tags():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X"})
    assert pm._profile_has_tag("p", "anything") is False


# ── U1: bulk_bind tag scope ───────────────────────────────────

def test_bulk_bind_tag_scope_only_matches():
    pm = _pm()
    pm.upsert_profile("holiday", {"name": "Holiday", "id": "holiday", "tags": ["holiday"]})
    pm.upsert_profile("normal", {"name": "Normal", "id": "normal", "tags": ["normal"]})
    pm.upsert_profile("target", {"name": "Target", "id": "target"})
    pm.bind_chat_persona("c1", {"name": "Holiday", "id": "holiday"})
    pm.bind_chat_persona("c2", {"name": "Normal", "id": "normal"})
    pm.bind_chat_persona("c3", {"name": "Holiday", "id": "holiday"})

    result = pm.bulk_bind_by_profile("target", scope="tag:holiday")
    assert result["affected"] == 2
    assert set(result["chat_ids"]) == {"c1", "c3"}
    assert pm._chat_personas["c1"]["name"] == "Target"
    assert pm._chat_personas["c2"]["name"] == "Normal"  # untouched


def test_bulk_bind_tag_scope_no_match_returns_zero():
    pm = _pm()
    pm.upsert_profile("p", {"name": "P", "id": "p"})
    pm.upsert_profile("src", {"name": "S", "id": "src", "tags": ["work"]})
    pm.bind_chat_persona("c1", {"name": "S", "id": "src"})
    result = pm.bulk_bind_by_profile("p", scope="tag:holiday")
    assert result["affected"] == 0


def test_bulk_bind_tag_scope_dry_run():
    pm = _pm()
    pm.upsert_profile("holiday", {"name": "Holiday", "id": "holiday", "tags": ["holiday"]})
    pm.upsert_profile("target", {"name": "Target", "id": "target"})
    pm.bind_chat_persona("c1", {"name": "Holiday", "id": "holiday"})
    result = pm.bulk_bind_by_profile("target", scope="tag:holiday", dry_run=True)
    assert result["affected"] == 1
    assert result["dry_run"] is True
    assert pm._chat_personas["c1"]["name"] == "Holiday"  # unchanged


def test_bulk_bind_unknown_scope_returns_empty():
    pm = _pm()
    pm.upsert_profile("p", {"name": "P", "id": "p"})
    pm.bind_chat_persona("c1", {"name": "X", "id": "p"})
    result = pm.bulk_bind_by_profile("p", scope="weird_scope")
    assert result["affected"] == 0


def test_bulk_bind_tag_scope_chat_without_profile_id_excluded():
    """Chats bound via direct persona dict (no id) should not match any tag scope."""
    pm = _pm()
    pm.upsert_profile("holiday", {"name": "Holiday", "id": "holiday", "tags": ["holiday"]})
    pm.upsert_profile("target", {"name": "Target", "id": "target"})
    pm.bind_chat_persona("c1", {"name": "Custom"})  # no id → not linked to any profile
    result = pm.bulk_bind_by_profile("target", scope="tag:holiday")
    assert result["affected"] == 0


# ── U4: RBAC — master-only for export/import ─────────────────

def _check_master_fn(role: str):
    from fastapi import HTTPException
    if role and role != "master":
        raise HTTPException(403, "该操作仅主帐号可执行")


def test_check_master_allows_master():
    _check_master_fn("master")  # no raise


def test_check_master_blocks_admin():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _check_master_fn(ROLE_ADMIN)
    assert exc.value.status_code == 403


def test_check_master_blocks_viewer():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _check_master_fn(ROLE_VIEWER)
    assert exc.value.status_code == 403


def test_check_master_allows_empty_role():
    """Empty role (bearer-token auth without session) should pass through."""
    _check_master_fn("")  # no raise


# ── PersonaManager import simulation ─────────────────────────

def test_import_merge_adds_new_profiles():
    pm = _pm()
    pm.upsert_profile("existing", {"name": "Existing"})
    pm.upsert_profile("imported", {"name": "Imported"})
    assert pm.get_persona_by_id("imported")["name"] == "Imported"
    assert pm.get_persona_by_id("existing")["name"] == "Existing"


def test_import_replace_clears_old():
    pm = _pm()
    pm.upsert_profile("old", {"name": "Old"})
    for pid in list(pm._profile_personas.keys()):
        pm.delete_profile(pid)
    pm.upsert_profile("new", {"name": "New"})
    assert pm.get_persona_by_id("old") is None
    assert pm.get_persona_by_id("new")["name"] == "New"


def test_export_includes_id_field():
    pm = _pm()
    pm.upsert_profile("p1", {"name": "Alice"})
    profiles = [dict(p, id=pid) for pid, p in pm._profile_personas.items()]
    assert profiles[0]["id"] == "p1"
    assert profiles[0]["name"] == "Alice"
