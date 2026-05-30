"""3-tier persona resolution tests.

Covers:
- get_persona: chat-binding > account-profile > domain-default
- load_profiles_from_config: reads personas.profiles[].id
- get_persona_by_id / upsert_profile / delete_profile
- get_all_chat_bindings returns full dicts (bug-fix regression)
- format_persona_block & build_system_prompt respect account_persona_id
- get_persona_name with account_persona_id
"""
import copy
import pytest
from src.utils.persona_manager import PersonaManager


def _pm() -> PersonaManager:
    PersonaManager.reset()
    return PersonaManager.get_instance()


# ── Profile store ─────────────────────────────────────────────

def test_load_profiles_from_config_basic():
    pm = _pm()
    cfg = {
        "personas": {
            "profiles": [
                {"id": "warm", "name": "温柔版", "role": "伴侣"},
                {"id": "pro",  "name": "专业版", "role": "助手"},
            ]
        }
    }
    n = pm.load_profiles_from_config(cfg)
    assert n == 2
    assert pm.get_persona_by_id("warm") == {"id": "warm", "name": "温柔版", "role": "伴侣"}
    assert pm.get_persona_by_id("pro")  == {"id": "pro",  "name": "专业版", "role": "助手"}


def test_load_profiles_skips_missing_id():
    pm = _pm()
    cfg = {"personas": {"profiles": [{"name": "no-id"}, {"id": "", "name": "empty-id"}]}}
    n = pm.load_profiles_from_config(cfg)
    assert n == 0
    assert pm.list_profile_ids() == []


def test_load_profiles_safe_when_no_personas_key():
    pm = _pm()
    assert pm.load_profiles_from_config({}) == 0
    assert pm.load_profiles_from_config({"personas": {}}) == 0
    assert pm.load_profiles_from_config({"personas": {"profiles": []}}) == 0


def test_upsert_and_delete_profile():
    pm = _pm()
    pm.upsert_profile("x", {"id": "x", "name": "X"})
    assert pm.get_persona_by_id("x") == {"id": "x", "name": "X"}
    assert pm.delete_profile("x") is True
    assert pm.get_persona_by_id("x") is None
    assert pm.delete_profile("x") is False  # already gone


def test_list_profile_ids():
    pm = _pm()
    pm.upsert_profile("a", {"name": "A"})
    pm.upsert_profile("b", {"name": "B"})
    assert set(pm.list_profile_ids()) == {"a", "b"}


# ── 3-tier get_persona ────────────────────────────────────────

def test_tier1_chat_binding_takes_priority():
    pm = _pm()
    pm.set_domain_persona({"name": "Domain"})
    pm.upsert_profile("acc_p", {"name": "Account"})
    pm.bind_chat_persona("chat99", {"name": "ChatSpecific"})
    p = pm.get_persona("chat99", "acc_p")
    assert p["name"] == "ChatSpecific"


def test_tier2_account_profile_used_when_no_chat_binding():
    pm = _pm()
    pm.set_domain_persona({"name": "Domain"})
    pm.upsert_profile("acc_p", {"name": "AccountPersona"})
    p = pm.get_persona("chat99", "acc_p")
    assert p["name"] == "AccountPersona"


def test_tier3_domain_fallback_when_no_chat_and_no_account_profile():
    pm = _pm()
    pm.set_domain_persona({"name": "Domain"})
    p = pm.get_persona("chat99", "nonexistent_profile")
    assert p["name"] == "Domain"


def test_tier3_domain_fallback_when_no_account_persona_id():
    pm = _pm()
    pm.set_domain_persona({"name": "Domain"})
    pm.upsert_profile("acc_p", {"name": "AccountPersona"})
    p = pm.get_persona("chat99")  # no account_persona_id
    assert p["name"] == "Domain"


def test_hardcoded_default_when_no_domain():
    pm = _pm()
    p = pm.get_persona("chat99", "")
    assert p["name"] == "Assistant"  # global hardcoded default


def test_account_profile_not_used_when_chat_bound():
    """Chat binding wins even when both chat binding and account profile exist."""
    pm = _pm()
    pm.bind_chat_persona("42", {"name": "ChatBound"})
    pm.upsert_profile("profile_a", {"name": "AccountLevel"})
    pm.set_domain_persona({"name": "DomainLevel"})
    assert pm.get_persona("42", "profile_a")["name"] == "ChatBound"


# ── get_all_chat_bindings returns dicts (bug-fix regression) ──

def test_get_all_chat_bindings_returns_full_dicts():
    pm = _pm()
    pm.bind_chat_persona("c1", {"id": "prof_a", "name": "Alice", "role": "companion"})
    pm.bind_chat_persona("c2", {"name": "Bob"})
    bindings = pm.get_all_chat_bindings()
    assert isinstance(bindings, dict)
    assert isinstance(bindings["c1"], dict), "value must be full persona dict"
    assert bindings["c1"]["name"] == "Alice"
    assert bindings["c1"]["id"] == "prof_a"
    assert bindings["c2"]["name"] == "Bob"


def test_get_all_chat_bindings_returns_copies():
    """Mutations to returned dict don't affect internal state."""
    pm = _pm()
    pm.bind_chat_persona("cx", {"name": "Original"})
    b = pm.get_all_chat_bindings()
    b["cx"]["name"] = "Mutated"
    assert pm.get_persona("cx")["name"] == "Original"


# ── format_persona_block / build_system_prompt with account_persona_id ──

def test_format_persona_block_uses_account_persona_id():
    pm = _pm()
    pm.set_domain_persona({"name": "DomainName"})
    pm.upsert_profile("voice_persona", {"name": "VoicePersonaName", "role": "R"})
    block = pm.format_persona_block("", account_persona_id="voice_persona", detail="full")
    assert "VoicePersonaName" in block
    assert "DomainName" not in block


def test_format_persona_block_ignores_account_id_when_chat_bound():
    pm = _pm()
    pm.bind_chat_persona("chat7", {"name": "ChatName", "role": "R"})
    pm.upsert_profile("p", {"name": "ProfileName", "role": "R"})
    block = pm.format_persona_block("chat7", account_persona_id="p")
    assert "ChatName" in block
    assert "ProfileName" not in block


def test_build_system_prompt_with_account_persona_id():
    pm = _pm()
    pm.set_domain_persona({"name": "DomainD"})
    pm.upsert_profile("acc", {"name": "AccName", "role": "Acc role"})
    prompt = pm.build_system_prompt(chat_id="", account_persona_id="acc")
    assert "AccName" in prompt


# ── get_persona_name ──────────────────────────────────────────

def test_get_persona_name_with_account_persona_id():
    pm = _pm()
    pm.set_domain_persona({"name": "DomainN"})
    pm.upsert_profile("p", {"name": "ProfileN"})
    assert pm.get_persona_name("", "p") == "ProfileN"
    assert pm.get_persona_name("", "") == "DomainN"
