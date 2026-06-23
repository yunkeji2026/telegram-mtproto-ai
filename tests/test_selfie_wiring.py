"""Stage A：skill_manager 形象照请求接线（轻量绑定，免全量 init）。

校验：未开→None、非请求→None、关系浅→搪塞、未解锁→付费引导、准入+provider 关→文字兜底。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.ai.companion_selfie import SELFIE_FEATURE, reset_selfie_provider

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


class _SM:
    _selfie_cfg = _SMcls._selfie_cfg
    _monetization_gate_enabled = _SMcls._monetization_gate_enabled
    _handle_selfie_request = _SMcls._handle_selfie_request
    _selfie_upsell_text = _SMcls._selfie_upsell_text
    _try_send_selfie_media = _SMcls._try_send_selfie_media
    _selfie_persona_for_prompt = _SMcls._selfie_persona_for_prompt
    _get_persona_name_for_context = _SMcls._get_persona_name_for_context
    _bond_level_from_context = _SMcls._bond_level_from_context
    _effective_intimacy = _SMcls._effective_intimacy
    _story_bonus_cap = _SMcls._story_bonus_cap

    def __init__(self, *, selfie_cfg=None, gate=False):
        comp = {}
        if selfie_cfg is not None:
            comp["selfie"] = selfie_cfg
        mon = {"enabled": True, "gate": {"enabled": True}} if gate else {}
        self.config = SimpleNamespace(config={"companion": comp, "monetization": mon})
        self.logger = logging.getLogger("test_selfie")


_ON = {"enabled": True, "free_daily": 1, "min_bond_level": 2,
       "provider": {"enabled": False}}


@pytest.mark.asyncio
async def test_disabled_returns_none():
    sm = _SM(selfie_cfg={"enabled": False})
    out = await sm._handle_selfie_request("给我看看你", "u1", {"intimacy_score": 60}, "c1")
    assert out is None


@pytest.mark.asyncio
async def test_not_a_request_returns_none():
    sm = _SM(selfie_cfg=_ON)
    out = await sm._handle_selfie_request("今天天气不错", "u1", {"intimacy_score": 60}, "c1")
    assert out is None


@pytest.mark.asyncio
async def test_too_soon_when_bond_low():
    sm = _SM(selfie_cfg=_ON)
    out = await sm._handle_selfie_request(
        "发张自拍", "u1", {"intimacy_score": 3}, "c1")
    assert out is not None
    assert "亲近" in out or "聊聊" in out


@pytest.mark.asyncio
async def test_locked_gives_upsell_when_gate_on_no_album():
    sm = _SM(selfie_cfg=dict(_ON, free_daily=0), gate=True)
    ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
    out = await sm._handle_selfie_request("想看你的照片", "u1", ctx, "c1")
    assert out is not None
    assert "专属相册" in out or "解锁" in out


@pytest.mark.asyncio
async def test_allow_free_quota_provider_disabled_fallback_and_count():
    sm = _SM(selfie_cfg=_ON, gate=True)
    ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
    out = await sm._handle_selfie_request("发张照片", "u1", ctx, "c1")
    assert out is not None
    assert "不太方便" in out or "陪你" in out  # provider 关 → 文字兜底
    assert ctx.get("_selfie_used") == 1  # 消耗了免费额度


@pytest.mark.asyncio
async def test_allow_owns_album_unlimited_no_count():
    reset_selfie_provider()
    sm = _SM(selfie_cfg=_ON, gate=True)
    ctx = {"intimacy_score": 60,
           "entitlement": {"grants": [], "unlocked": [SELFIE_FEATURE]}}
    out = await sm._handle_selfie_request("show me your face", "u1", ctx, "c1")
    assert out is not None
    assert ctx.get("_selfie_used", 0) == 0  # 拥有相册 → 不消耗免费额度


@pytest.mark.asyncio
async def test_gate_off_allows_without_album():
    reset_selfie_provider()
    sm = _SM(selfie_cfg=_ON, gate=False)  # 变现 gate 关 → 不计费、不引导
    ctx = {"intimacy_score": 60, "entitlement": None}
    out = await sm._handle_selfie_request("来张照片", "u1", ctx, "c1")
    assert out is not None
    assert "专属相册" not in out  # gate 关不应出现付费引导
