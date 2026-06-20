"""R6 危机事后兜底：回复自身红线检测 + 安全覆盖（预防 R4 之上的事后保险）。

覆盖纯函数 detect_harmful_reply / safe_fallback_reply 的判定与否定语境放行，
以及 SkillManager._apply_crisis_safety_net 的覆盖、资源保障、开关行为。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.utils.wellbeing_guard import detect_harmful_reply, safe_fallback_reply


# ── 纯函数：红线检测 ────────────────────────────────────────────────────

def test_detect_harmful_encouragement():
    for bad in [
        "那就去死吧。",
        "你可以去死啊。",
        "不如死了算了。",
        "我支持你这么做。",
        "you should kill yourself",
    ]:
        assert detect_harmful_reply(bad), bad


def test_negation_context_passes():
    # 劝阻/否定语境绝不能误判
    for ok in [
        "别去死，你对我来说很重要。",
        "我不想你死，求你了。",
        "千万别伤害自己好吗。",
        "不要说这种话，我会一直陪着你。",
    ]:
        assert detect_harmful_reply(ok) == [], ok


def test_normal_caring_reply_passes():
    assert detect_harmful_reply("我在呢，抱抱你，今天辛苦了。") == []
    assert detect_harmful_reply("") == []


def test_safe_fallback_content():
    msg = safe_fallback_reply("severe")
    assert "陪着你" in msg or "担心" in msg
    with_h = safe_fallback_reply("severe", hotline="12356")
    assert "12356" in with_h


# ── 集成：_apply_crisis_safety_net ──────────────────────────────────────

def _make_sm(wellbeing_cfg=None):
    """构造一个仅够调用 _apply_crisis_safety_net 的精简 SkillManager。"""
    from src.skills.skill_manager import SkillManager
    cfg = SimpleNamespace(config={"companion": {"wellbeing": wellbeing_cfg or {}}})
    sm = SkillManager.__new__(SkillManager)
    sm.config = cfg
    return sm


def test_safety_net_overrides_harmful():
    sm = _make_sm({"enabled": True})
    ctx: dict = {"_wellbeing_crisis_level": "severe"}
    out = sm._apply_crisis_safety_net("那就去死吧。", user_context=ctx, log_prefix="")
    assert "去死" not in out
    assert "陪着你" in out or "担心" in out
    assert ctx.get("_wellbeing_safety_override") is True


def test_safety_net_keeps_good_reply():
    sm = _make_sm({"enabled": True})
    ctx: dict = {"_wellbeing_crisis_level": "severe"}
    good = "我在呢，别怕，我会一直陪着你。"
    assert sm._apply_crisis_safety_net(good, user_context=ctx, log_prefix="") == good


def test_resource_assurance_appends_when_enabled():
    sm = _make_sm({
        "enabled": True,
        "crisis_resource_assurance": True,
        "crisis_resources": "全国热线 12356",
    })
    ctx: dict = {"_wellbeing_crisis_level": "severe"}
    out = sm._apply_crisis_safety_net(
        "我在呢，抱抱你。", user_context=ctx, log_prefix="",
    )
    assert "12356" in out


def test_resource_assurance_off_by_default():
    sm = _make_sm({"enabled": True, "crisis_resources": "12356"})
    ctx: dict = {"_wellbeing_crisis_level": "severe"}
    out = sm._apply_crisis_safety_net(
        "我在呢，抱抱你。", user_context=ctx, log_prefix="",
    )
    assert "12356" not in out  # assurance 默认关


def test_resource_not_appended_for_non_severe():
    sm = _make_sm({
        "enabled": True,
        "crisis_resource_assurance": True,
        "crisis_resources": "12356",
    })
    ctx: dict = {"_wellbeing_crisis_level": "elevated"}
    out = sm._apply_crisis_safety_net(
        "我在呢，抱抱你。", user_context=ctx, log_prefix="",
    )
    assert "12356" not in out


def test_safety_net_disabled_passes_through():
    sm = _make_sm({"enabled": False})
    ctx: dict = {"_wellbeing_crisis_level": "severe"}
    bad = "那就去死吧。"
    # 关闭后连红线都不拦（由调用方/配置决定）
    assert sm._apply_crisis_safety_net(bad, user_context=ctx, log_prefix="") == bad
