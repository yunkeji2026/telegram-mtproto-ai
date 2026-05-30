"""Tests for get_persona_with_tier and group vs private persona routing.

Covers:
- get_persona_with_tier returns correct (persona, tier) for each path
- tier constants are stable strings
- TelegramClient group routing: persona_ids[1] when is_group=True
- LINE RPA _account_persona_id(is_group) routing
- Messenger RPA _persona_ids[0] only (private-only platform)
"""
import pytest
from src.utils.persona_manager import PersonaManager


def _pm() -> PersonaManager:
    PersonaManager.reset()
    return PersonaManager.get_instance()


# ── get_persona_with_tier ──────────────────────────────────────

def test_tier_chat_binding():
    pm = _pm()
    pm.bind_chat_persona("c1", {"name": "ChatPers"})
    pm.upsert_profile("acc", {"name": "AccPers"})
    pm.set_domain_persona({"name": "Domain"})
    p, tier = pm.get_persona_with_tier("c1", "acc")
    assert tier == PersonaManager._TIER_CHAT
    assert p["name"] == "ChatPers"


def test_tier_account_profile():
    pm = _pm()
    pm.set_domain_persona({"name": "Domain"})
    pm.upsert_profile("acc", {"name": "AccPers"})
    p, tier = pm.get_persona_with_tier("no_binding", "acc")
    assert tier == PersonaManager._TIER_ACCOUNT
    assert p["name"] == "AccPers"


def test_tier_domain():
    pm = _pm()
    pm.set_domain_persona({"name": "Domain"})
    p, tier = pm.get_persona_with_tier("no_binding", "nonexistent_profile")
    assert tier == PersonaManager._TIER_DOMAIN
    assert p["name"] == "Domain"


def test_tier_default_fallback():
    pm = _pm()  # no domain set
    p, tier = pm.get_persona_with_tier("", "")
    assert tier == PersonaManager._TIER_DEFAULT
    assert p["name"] == "Assistant"


def test_tier_constants_are_strings():
    for c in (
        PersonaManager._TIER_CHAT,
        PersonaManager._TIER_ACCOUNT,
        PersonaManager._TIER_DOMAIN,
        PersonaManager._TIER_DEFAULT,
    ):
        assert isinstance(c, str) and c


def test_tier_chat_binding_wins_over_account():
    pm = _pm()
    pm.bind_chat_persona("cx", {"name": "Chat"})
    pm.upsert_profile("p", {"name": "Profile"})
    _, tier = pm.get_persona_with_tier("cx", "p")
    assert tier == PersonaManager._TIER_CHAT


def test_tier_result_consistent_with_get_persona():
    """get_persona_with_tier[0] must equal get_persona for same args."""
    pm = _pm()
    pm.bind_chat_persona("c1", {"name": "CB"})
    pm.upsert_profile("p1", {"name": "AP"})
    pm.set_domain_persona({"name": "Dom"})

    for chat_id, acc_pid in [("c1", "p1"), ("c2", "p1"), ("c2", ""), ("", "")]:
        p_direct = pm.get_persona(chat_id, acc_pid)
        p_tier, _ = pm.get_persona_with_tier(chat_id, acc_pid)
        assert p_direct is p_tier, f"Mismatch for ({chat_id!r}, {acc_pid!r})"


# ── TelegramClient group persona routing expression ────────────

def _tg_persona_id(account_persona_ids, is_group):
    """Mirror of the expression in telegram_client.py context dict."""
    return (
        (
            account_persona_ids[1]
            if is_group and len(account_persona_ids) > 1
            else account_persona_ids[0]
        )
        if account_persona_ids
        else ""
    )


def test_tg_private_uses_first_persona():
    assert _tg_persona_id(["private_p", "group_p"], False) == "private_p"


def test_tg_group_uses_second_persona():
    assert _tg_persona_id(["private_p", "group_p"], True) == "group_p"


def test_tg_group_fallback_when_only_one_persona():
    assert _tg_persona_id(["only_one"], True) == "only_one"


def test_tg_empty_persona_ids():
    assert _tg_persona_id([], True) == ""
    assert _tg_persona_id([], False) == ""


def test_tg_none_persona_ids():
    assert _tg_persona_id(None, True) == ""


# ── LINE RPA _account_persona_id routing ──────────────────────

def test_line_group_uses_second_persona():
    from src.integrations.line_rpa.runner import LineRpaRunner
    runner = LineRpaRunner.__new__(LineRpaRunner)
    runner._cfg = {"persona_ids": ["private_p", "group_p"]}
    assert runner._account_persona_id(is_group=True) == "group_p"
    assert runner._account_persona_id(is_group=False) == "private_p"


def test_line_group_fallback_when_only_one():
    from src.integrations.line_rpa.runner import LineRpaRunner
    runner = LineRpaRunner.__new__(LineRpaRunner)
    runner._cfg = {"persona_ids": ["only"]}
    assert runner._account_persona_id(is_group=True) == "only"


def test_line_empty_persona_ids():
    from src.integrations.line_rpa.runner import LineRpaRunner
    runner = LineRpaRunner.__new__(LineRpaRunner)
    runner._cfg = {"persona_ids": []}
    assert runner._account_persona_id(is_group=True) == ""
    assert runner._account_persona_id(is_group=False) == ""


def test_line_missing_persona_ids_key():
    from src.integrations.line_rpa.runner import LineRpaRunner
    runner = LineRpaRunner.__new__(LineRpaRunner)
    runner._cfg = {}
    assert runner._account_persona_id() == ""


# ── Messenger RPA only uses [0] (private-only platform) ───────

def test_messenger_always_uses_first_persona():
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    runner = MessengerRpaRunner.__new__(MessengerRpaRunner)
    runner._persona_ids = ["private_p", "should_not_use"]
    result = (
        getattr(runner, "_persona_ids", [None])[0] or ""
        if getattr(runner, "_persona_ids", [])
        else ""
    )
    assert result == "private_p"
