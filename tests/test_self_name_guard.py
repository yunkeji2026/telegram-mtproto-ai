"""Regression: bot must never speak the domain role label ('线上陪伴') as its own name.

Root cause (observed 2026-07-01, conv telegram:8244899900:8921664288): when the
account persona failed to resolve, the reply engine fell back to the domain persona
whose display *name* is '线上陪伴' (a role label, not a spoken name), and the model
sent 「我叫线上陪伴哦，不是莫莉啦」to the customer. The self-name guard could not fix
it because it had no correct persona name to substitute.

These tests pin `_sanitize_assistant_reply`'s two passes:
  1. name-declaration rewrite (needs a resolved name)
  2. forbidden self-name floor (works even with no resolved name)
"""
import pytest


class _FakePM:
    def __init__(self, personas=None, domain_name="线上陪伴"):
        self._personas = personas or {}
        self._domain_persona = {"name": domain_name} if domain_name else {}

    def get_persona_by_id(self, pid):
        return self._personas.get(str(pid))


def _bare_sm():
    from src.skills.skill_manager import SkillManager
    return SkillManager.__new__(SkillManager)


@pytest.fixture
def patch_pm(monkeypatch):
    def _apply(personas=None, domain_name="线上陪伴"):
        from src.utils.persona_manager import PersonaManager
        pm = _FakePM(personas=personas, domain_name=domain_name)
        monkeypatch.setattr(PersonaManager, "get_instance", lambda: pm)
        return pm
    return _apply


def test_forbidden_name_rewritten_when_persona_resolved(patch_pm):
    patch_pm(personas={"lin_xiaoyu": {"name": "林小雨"}})
    sm = _bare_sm()
    ctx = {"account_persona_id": "lin_xiaoyu"}
    out = sm._sanitize_assistant_reply("嗯呢，我叫线上陪伴哦～", ctx)
    assert "线上陪伴" not in out
    assert "林小雨" in out


def test_woshi_phrasing_also_caught(patch_pm):
    """'我是线上陪伴' — the phrasing gap the old guard missed."""
    patch_pm(personas={"lin_xiaoyu": {"name": "林小雨"}})
    sm = _bare_sm()
    ctx = {"account_persona_id": "lin_xiaoyu"}
    out = sm._sanitize_assistant_reply("你好，我是线上陪伴，很高兴认识你", ctx)
    assert "线上陪伴" not in out
    assert "林小雨" in out


def test_forbidden_claim_dropped_when_no_persona(patch_pm):
    """The exact 07-01 leak, but with unresolved persona → claim is dropped, never spoken."""
    patch_pm(personas={})
    sm = _bare_sm()
    ctx = {}  # no account_persona_id → no correct name
    out = sm._sanitize_assistant_reply("我叫线上陪伴哦，不是莫莉啦 😊", ctx)
    assert "我叫线上陪伴" not in out
    assert "线上陪伴" not in out


def test_domain_persona_name_is_forbidden_even_if_custom(patch_pm):
    """Whatever the domain persona's display name is, it must not be spoken."""
    patch_pm(personas={}, domain_name="在线小助手")
    sm = _bare_sm()
    out = sm._sanitize_assistant_reply("我是在线小助手", {})
    assert "在线小助手" not in out


def test_legit_woshi_sentences_untouched(patch_pm):
    """Pass 2 must only fire on role labels, never corrupt normal '我是...' sentences."""
    patch_pm(personas={"lin_xiaoyu": {"name": "林小雨"}})
    sm = _bare_sm()
    ctx = {"account_persona_id": "lin_xiaoyu"}
    for txt in ("我是说真的啦", "我是学生，平时挺忙的", "我是真的想你了"):
        assert sm._sanitize_assistant_reply(txt, ctx) == txt


def test_correct_name_left_untouched(patch_pm):
    patch_pm(personas={"lin_xiaoyu": {"name": "林小雨"}})
    sm = _bare_sm()
    ctx = {"account_persona_id": "lin_xiaoyu"}
    txt = "哈哈，我叫林小雨呀～"
    assert sm._sanitize_assistant_reply(txt, ctx) == txt


def test_other_wrong_name_still_rewritten(patch_pm):
    """Existing behavior preserved: any wrong declared name → correct name."""
    patch_pm(personas={"lin_xiaoyu": {"name": "林小雨"}})
    sm = _bare_sm()
    ctx = {"account_persona_id": "lin_xiaoyu"}
    out = sm._sanitize_assistant_reply("我叫小明", ctx)
    assert "小明" not in out
    assert "林小雨" in out


def test_empty_and_nonstring_safe(patch_pm):
    patch_pm(personas={})
    sm = _bare_sm()
    assert sm._sanitize_assistant_reply("", {}) == ""
    assert sm._sanitize_assistant_reply(None, {}) is None


# ── source-level spoken-name resolver (PersonaManager.resolve_spoken_name) ──────

def test_resolve_spoken_name_rejects_role_label():
    """The domain label '线上陪伴' must never survive as the spoken name."""
    from src.utils.persona_manager import PersonaManager
    out = PersonaManager.resolve_spoken_name({"name": "线上陪伴"})
    assert out != "线上陪伴"
    assert out == PersonaManager._DEFAULT_SPOKEN_NAME


def test_resolve_spoken_name_real_persona_passes_through():
    from src.utils.persona_manager import PersonaManager
    assert PersonaManager.resolve_spoken_name({"name": "林小雨"}) == "林小雨"


def test_resolve_spoken_name_uses_fallback_when_label():
    from src.utils.persona_manager import PersonaManager
    out = PersonaManager.resolve_spoken_name({"name": "线上陪伴"}, fallback="小暖")
    assert out == "小暖"


def test_resolve_spoken_name_override_wins():
    from src.utils.persona_manager import PersonaManager
    out = PersonaManager.resolve_spoken_name({"name": "线上陪伴"}, name_override="Mia")
    assert out == "Mia"


def test_resolve_spoken_name_override_role_label_ignored():
    """Even an override that is itself a role label is rejected."""
    from src.utils.persona_manager import PersonaManager
    out = PersonaManager.resolve_spoken_name(
        {"name": "林小雨"}, name_override="客服"
    )
    assert out == "林小雨"


def test_format_instructions_never_locks_domain_label():
    """End-to-end: the identity-lock line must not instruct answering '线上陪伴'."""
    from src.utils.persona_manager import PersonaManager
    pm = PersonaManager()
    block = pm._format_persona_instructions({"name": "线上陪伴", "role": "陪伴"})
    assert "线上陪伴" not in block
