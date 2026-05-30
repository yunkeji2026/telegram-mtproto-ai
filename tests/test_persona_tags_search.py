"""Tests for profile tags, summary list, and get_profiles_by_tag."""
import pytest
from src.utils.persona_manager import PersonaManager


def _pm() -> PersonaManager:
    PersonaManager.reset()
    return PersonaManager.get_instance()


# ── list_profiles_summary ──────────────────────────────────────

def test_summary_empty():
    pm = _pm()
    assert pm.list_profiles_summary() == []


def test_summary_includes_all_profiles():
    pm = _pm()
    pm.upsert_profile("p1", {"name": "A", "role": "r1", "tags": ["holiday"]})
    pm.upsert_profile("p2", {"name": "B"})
    summaries = pm.list_profiles_summary()
    ids = {s["id"] for s in summaries}
    assert ids == {"p1", "p2"}


def test_summary_fields_present():
    pm = _pm()
    pm.upsert_profile("p", {"name": "Alice", "role": "Companion", "tags": ["vip", "work"]})
    s = pm.list_profiles_summary()[0]
    assert s["id"] == "p"
    assert s["name"] == "Alice"
    assert s["role"] == "Companion"
    assert s["tags"] == ["vip", "work"]
    assert "has_voice" in s
    assert "has_history" in s


def test_summary_has_voice_false_by_default():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X"})
    assert pm.list_profiles_summary()[0]["has_voice"] is False


def test_summary_has_voice_true_when_voice_set():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "voice_profile": {"voice": "ja-JP-NanamiNeural"}})
    assert pm.list_profiles_summary()[0]["has_voice"] is True


def test_summary_has_history_false_initially():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    assert pm.list_profiles_summary()[0]["has_history"] is False


def test_summary_has_history_true_after_overwrite():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    pm.upsert_profile("p", {"name": "V2"})
    assert pm.list_profiles_summary()[0]["has_history"] is True


def test_summary_has_history_clears_after_revert_exhausted():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1"})
    pm.upsert_profile("p", {"name": "V2"})
    pm.revert_profile("p")  # history consumed
    assert pm.list_profiles_summary()[0]["has_history"] is False


def test_summary_tags_empty_list_when_missing():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X"})
    assert pm.list_profiles_summary()[0]["tags"] == []


def test_summary_name_falls_back_to_id():
    pm = _pm()
    pm.upsert_profile("my-id", {"role": "role only"})
    s = pm.list_profiles_summary()[0]
    assert s["name"] == "my-id"


# ── get_profiles_by_tag ────────────────────────────────────────

def test_get_by_tag_empty_tag_returns_all():
    pm = _pm()
    pm.upsert_profile("p1", {"name": "A", "tags": ["x"]})
    pm.upsert_profile("p2", {"name": "B"})
    assert len(pm.get_profiles_by_tag("")) == 2


def test_get_by_tag_matches():
    pm = _pm()
    pm.upsert_profile("p1", {"name": "A", "tags": ["holiday", "vip"]})
    pm.upsert_profile("p2", {"name": "B", "tags": ["work"]})
    result = pm.get_profiles_by_tag("holiday")
    assert len(result) == 1
    assert result[0]["name"] == "A"


def test_get_by_tag_case_insensitive():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "tags": ["Holiday"]})
    assert len(pm.get_profiles_by_tag("holiday")) == 1
    assert len(pm.get_profiles_by_tag("HOLIDAY")) == 1


def test_get_by_tag_no_match_returns_empty():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "tags": ["work"]})
    assert pm.get_profiles_by_tag("holiday") == []


def test_get_by_tag_profile_with_no_tags_not_matched():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X"})
    assert pm.get_profiles_by_tag("any") == []


def test_get_by_tag_returns_copies():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "tags": ["a"]})
    result = pm.get_profiles_by_tag("a")
    result[0]["name"] = "MUTATED"
    assert pm.get_persona_by_id("p")["name"] == "X"


def test_get_by_tag_multi_match():
    pm = _pm()
    pm.upsert_profile("p1", {"name": "A", "tags": ["vip"]})
    pm.upsert_profile("p2", {"name": "B", "tags": ["vip"]})
    pm.upsert_profile("p3", {"name": "C", "tags": ["regular"]})
    result = pm.get_profiles_by_tag("vip")
    names = {p["name"] for p in result}
    assert names == {"A", "B"}


# ── Tags round-trip through upsert/read ───────────────────────

def test_tags_saved_and_returned_via_get_persona_by_id():
    pm = _pm()
    pm.upsert_profile("p", {"name": "X", "tags": ["a", "b", "c"]})
    stored = pm.get_persona_by_id("p")
    assert stored["tags"] == ["a", "b", "c"]


def test_tags_persist_through_history_and_revert():
    pm = _pm()
    pm.upsert_profile("p", {"name": "V1", "tags": ["old"]})
    pm.upsert_profile("p", {"name": "V2", "tags": ["new"]})
    pm.revert_profile("p")
    restored = pm.get_persona_by_id("p")
    assert restored["tags"] == ["old"]
    assert restored["name"] == "V1"
