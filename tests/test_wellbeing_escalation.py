"""R8 危机人工接管/升级：severe 连续命中触发 handoff 告警（复用 escalation 通道）。

聚焦 SkillManager._maybe_escalate_crisis 的连击计数、阈值/冷却/开关门控与触发标记，
不实发 webhook（loop 内 create_task 在无运行 loop 时静默跳过）。
"""

from __future__ import annotations

from types import SimpleNamespace


def _make_sm(wellbeing_cfg=None):
    from src.skills.skill_manager import SkillManager
    cfg = SimpleNamespace(config={"companion": {"wellbeing": wellbeing_cfg or {}}})
    sm = SkillManager.__new__(SkillManager)
    sm.config = cfg
    sm._crisis_escalation_cooldown = {}
    return sm


def _run(sm, *, level, ctx, uid="u1"):
    ctx["_wellbeing_crisis_level"] = level
    sm._maybe_escalate_crisis(
        user_id=uid, chat_id=123, user_context=ctx, log_prefix="",
    )
    return ctx


# ── 连击计数 ────────────────────────────────────────────────────────────

def test_streak_increments_on_severe():
    sm = _make_sm({"enabled": True})
    ctx: dict = {}
    _run(sm, level="severe", ctx=ctx)
    assert ctx["_wellbeing_crisis_streak"] == 1
    _run(sm, level="severe", ctx=ctx)
    assert ctx["_wellbeing_crisis_streak"] == 2


def test_streak_resets_on_normal():
    sm = _make_sm({"enabled": True})
    ctx: dict = {}
    _run(sm, level="severe", ctx=ctx)
    _run(sm, level="none", ctx=ctx)
    assert ctx["_wellbeing_crisis_streak"] == 0


def test_streak_held_on_elevated():
    sm = _make_sm({"enabled": True})
    ctx: dict = {}
    _run(sm, level="severe", ctx=ctx)
    _run(sm, level="elevated", ctx=ctx)
    assert ctx["_wellbeing_crisis_streak"] == 1  # 维持不增不减


# ── 触发门控 ────────────────────────────────────────────────────────────

def test_no_escalation_when_disabled():
    sm = _make_sm({"enabled": True, "crisis_escalation": False})
    ctx = _run(sm, level="severe", ctx={})
    assert "_crisis_escalation_triggered" not in ctx


def test_escalation_triggers_when_enabled():
    sm = _make_sm({"enabled": True, "crisis_escalation": True, "escalate_after": 1})
    ctx = _run(sm, level="severe", ctx={})
    assert ctx.get("_crisis_escalation_triggered") is True


def test_escalation_respects_escalate_after():
    sm = _make_sm({"enabled": True, "crisis_escalation": True, "escalate_after": 2})
    ctx: dict = {}
    _run(sm, level="severe", ctx=ctx)
    assert "_crisis_escalation_triggered" not in ctx  # streak 1 < 2
    _run(sm, level="severe", ctx=ctx)
    assert ctx.get("_crisis_escalation_triggered") is True  # streak 2


def test_escalation_cooldown_blocks_repeat():
    sm = _make_sm({"enabled": True, "crisis_escalation": True, "escalate_after": 1})
    ctx: dict = {}
    _run(sm, level="severe", ctx=ctx)
    assert ctx.get("_crisis_escalation_triggered") is True
    # 清掉标记再来一次：冷却内不应再次触发
    ctx.pop("_crisis_escalation_triggered", None)
    _run(sm, level="severe", ctx=ctx)
    assert "_crisis_escalation_triggered" not in ctx


def test_no_escalation_for_non_severe():
    sm = _make_sm({"enabled": True, "crisis_escalation": True, "escalate_after": 1})
    ctx = _run(sm, level="elevated", ctx={})
    assert "_crisis_escalation_triggered" not in ctx


def test_wellbeing_off_still_counts_but_no_fire():
    # enabled=false：连击仍计（供观测），但绝不告警
    sm = _make_sm({"enabled": False, "crisis_escalation": True})
    ctx = _run(sm, level="severe", ctx={})
    assert ctx["_wellbeing_crisis_streak"] == 1
    assert "_crisis_escalation_triggered" not in ctx
