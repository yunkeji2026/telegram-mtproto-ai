"""Stage A：陪伴形象照引擎（意图/提示词/准入决策/provider 骨架）。"""

from __future__ import annotations

import pytest

from src.ai.companion_selfie import (
    SELFIE_FEATURE,
    SelfieProvider,
    build_selfie_prompt,
    decide_selfie,
    detect_selfie_request,
    get_selfie_provider,
    reset_selfie_provider,
)


# ── 意图识别 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("t", [
    "给我看看你长什么样",
    "发张自拍呗",
    "想看你的照片",
    "来张照片吧",
    "what do you look like?",
    "send me a pic of you",
    "show me your face",
])
def test_detect_positive(t):
    assert detect_selfie_request(t) is True


@pytest.mark.parametrize("t", [
    "",
    "今天天气真好",
    "我给你看我的照片",   # 用户说自己的照片，不是要 AI 的
    "我们聊聊吧",
    "x" * 250,            # 超长叙述不命中
])
def test_detect_negative(t):
    assert detect_selfie_request(t) is False


# ── 提示词构造 ──────────────────────────────────────────────────────────

def test_build_prompt_uses_persona_appearance_and_sfw():
    p = {"name": "小柔", "appearance": "long black hair, soft smile, white dress"}
    out = build_selfie_prompt(p)
    assert "long black hair" in out
    assert "safe-for-work" in out  # 强制 SFW 安全约束


def test_build_prompt_fallback_to_default_then_generic():
    # persona 无外貌 + 给 default_appearance → 用 default
    out = build_selfie_prompt({"name": "A"}, default_appearance="freckled redhead")
    assert "freckled redhead" in out
    # 完全空 → 中性兜底（不抛、有内容）
    out2 = build_selfie_prompt(None)
    assert "Portrait selfie" in out2 and "safe-for-work" in out2


def test_build_prompt_scene_and_style():
    out = build_selfie_prompt("a woman", scene_hint="by the window", style="warm tone")
    assert "by the window" in out and "warm tone" in out


# ── 准入决策 ────────────────────────────────────────────────────────────

def test_decide_too_soon_when_bond_low():
    d = decide_selfie(entitlement=None, gate_enabled=True, free_used=0,
                      free_daily=1, bond_level=1, min_bond_level=2)
    assert d["action"] == "too_soon"


def test_decide_gate_off_always_allow_unlimited():
    # gate 关 → feature_allowed 恒 True → 不限、不消耗免费额度
    d = decide_selfie(entitlement=None, gate_enabled=False, free_used=99,
                      free_daily=1, bond_level=5, min_bond_level=2)
    assert d["action"] == "allow"
    assert d["used_free"] is False


def test_decide_owns_album_allow_unlimited():
    ent = {"grants": [], "unlocked": [SELFIE_FEATURE]}
    d = decide_selfie(entitlement=ent, gate_enabled=True, free_used=99,
                      free_daily=1, bond_level=5, min_bond_level=2)
    assert d["action"] == "allow"
    assert d["used_free"] is False


def test_decide_free_quota_then_locked():
    # gate 开 + 未拥有：额度内 allow(used_free) → 用尽 locked
    base = dict(entitlement={"grants": [], "unlocked": []}, gate_enabled=True,
                free_daily=1, bond_level=5, min_bond_level=2)
    d0 = decide_selfie(free_used=0, **base)
    assert d0["action"] == "allow" and d0["used_free"] is True
    d1 = decide_selfie(free_used=1, **base)
    assert d1["action"] == "locked"


# ── provider 骨架 ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provider_disabled_returns_error():
    p = SelfieProvider({"enabled": False})
    res = await p.generate("a prompt")
    assert res.ok is False
    assert res.error == "provider_disabled"


@pytest.mark.asyncio
async def test_provider_enabled_unknown_backend_soft_fails():
    p = SelfieProvider({"enabled": True, "backend": "disabled"})
    res = await p.generate("a prompt")
    assert res.ok is False  # backend disabled → 仍软失败，不抛


@pytest.mark.asyncio
async def test_provider_empty_prompt():
    p = SelfieProvider({"enabled": True, "backend": "openai"})
    res = await p.generate("   ")
    assert res.ok is False and res.error == "empty_prompt"


@pytest.mark.asyncio
async def test_provider_command_backend_generates(tmp_path):
    # 用一个最小命令模拟出图：写一个非空 png 文件到 {out}
    import sys
    script = tmp_path / "fake_gen.py"
    script.write_text(
        "import sys\n"
        "out=sys.argv[1]\n"
        "open(out,'wb').write(b'\\x89PNG fake image bytes')\n",
        encoding="utf-8")
    p = SelfieProvider({
        "enabled": True, "backend": "command",
        "out_dir": str(tmp_path / "out"),
        "command_args": [sys.executable, str(script), "{out}"],
    })
    res = await p.generate("portrait of a woman, safe-for-work")
    assert res.ok is True
    assert res.image_path.endswith(".png")


def test_singleton_reuse_and_reset():
    reset_selfie_provider()
    a = get_selfie_provider({"enabled": True})
    b = get_selfie_provider()
    assert a is b
    reset_selfie_provider()
    c = get_selfie_provider({})
    assert c is not a
