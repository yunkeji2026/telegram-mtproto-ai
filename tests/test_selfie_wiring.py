"""Stage A：skill_manager 形象照请求接线（轻量绑定，免全量 init）。

校验：未开→None、非请求→None、关系浅→搪塞、未解锁→付费引导、准入+provider 关→文字兜底。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.ai.companion_selfie import SELFIE_FEATURE, reset_selfie_provider
from src.utils.companion_funnel_store import (
    get_companion_funnel_store,
    reset_companion_funnel_store,
)

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


class _SM:
    _selfie_cfg = _SMcls._selfie_cfg
    _monetization_gate_enabled = _SMcls._monetization_gate_enabled
    _handle_selfie_request = _SMcls._handle_selfie_request
    _record_selfie_event = _SMcls._record_selfie_event
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


# ── Stage B：埋点接线（准入态 → 自拍漏斗） ────────────────────────────────

@pytest.mark.asyncio
async def test_funnel_records_locked_event():
    reset_companion_funnel_store()
    funnel = get_companion_funnel_store(":memory:")
    try:
        sm = _SM(selfie_cfg=dict(_ON, free_daily=0), gate=True)
        ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
        await sm._handle_selfie_request("想看你的照片", "u_lk", ctx, "c1")
        rows = funnel.selfie_recent(limit=10)
        assert len(rows) == 1
        assert rows[0]["contact_key"] == "u_lk"
        assert rows[0]["kind"] == "locked"
    finally:
        reset_companion_funnel_store()


@pytest.mark.asyncio
async def test_funnel_records_delivered_and_too_soon():
    reset_companion_funnel_store()
    funnel = get_companion_funnel_store(":memory:")
    reset_selfie_provider()
    try:
        sm = _SM(selfie_cfg=_ON, gate=True)
        await sm._handle_selfie_request(
            "发张照片", "u_dl", {"intimacy_score": 60,
                              "entitlement": {"grants": [], "unlocked": []}}, "c1")
        await sm._handle_selfie_request(
            "发张自拍", "u_ts", {"intimacy_score": 3}, "c1")
        kinds = {r["contact_key"]: r["kind"] for r in funnel.selfie_recent(limit=10)}
        assert kinds["u_dl"] == "delivered"
        assert kinds["u_ts"] == "too_soon"
    finally:
        reset_companion_funnel_store()


@pytest.mark.asyncio
async def test_funnel_noop_when_store_not_initialized():
    reset_companion_funnel_store()  # 无单例 → peek 返回 None → 不记录、不报错
    sm = _SM(selfie_cfg=_ON, gate=True)
    ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
    out = await sm._handle_selfie_request("想看你的照片", "u1", ctx, "c1")
    assert out is not None  # 主流程不受埋点缺失影响


# ── Stage D：A 线主客户端 send_photo 直发兜底 ───────────────────────────────

@pytest.mark.asyncio
async def test_try_send_selfie_media_direct_callback():
    sm = _SM(selfie_cfg=_ON)
    sent = {}

    async def _fake_send(chat_id, path, caption):
        sent["args"] = (chat_id, path, caption)
        return True

    # 无 platform/account → 编排器路跳过 → 走 A 线直发回调
    ok = await sm._try_send_selfie_media(
        {"_send_photo_to_chat": _fake_send}, 12345, "/tmp/x.png", "hi")
    assert ok is True
    assert sent["args"] == (12345, "/tmp/x.png", "hi")


@pytest.mark.asyncio
async def test_try_send_selfie_media_no_channel_returns_false():
    sm = _SM(selfie_cfg=_ON)
    ok = await sm._try_send_selfie_media({}, 1, "/tmp/x.png", "hi")
    assert ok is False


@pytest.mark.asyncio
async def test_try_send_selfie_media_no_image_returns_false():
    sm = _SM(selfie_cfg=_ON)

    async def _fake_send(c, p, cap):
        return True

    ok = await sm._try_send_selfie_media(
        {"_send_photo_to_chat": _fake_send}, 1, "", "hi")
    assert ok is False


@pytest.mark.asyncio
async def test_try_send_selfie_media_callback_failure_soft_false():
    sm = _SM(selfie_cfg=_ON)

    async def _boom(c, p, cap):
        raise RuntimeError("net down")

    ok = await sm._try_send_selfie_media(
        {"_send_photo_to_chat": _boom}, 1, "/tmp/x.png", "hi")
    assert ok is False  # 直发失败软兜底，不抛


@pytest.mark.asyncio
async def test_sender_send_photo_success_failure_and_no_client():
    from src.client.sender import TelegramSenderMixin

    class _Cli:
        def __init__(self, fail=False):
            self.fail = fail
            self.calls = []

        async def send_photo(self, chat_id, photo, caption=""):
            if self.fail:
                raise RuntimeError("rpc")
            self.calls.append((chat_id, photo, caption))

    class _S(TelegramSenderMixin):
        def __init__(self, cli):
            self.client = cli
            self.logger = logging.getLogger("test_sender")
            self.account_id = "a"

    ok = _S(_Cli())
    assert await ok.send_photo(7, "/p.png", "cap") is True
    assert ok.client.calls == [(7, "/p.png", "cap")]
    assert await _S(_Cli(fail=True)).send_photo(7, "/p.png", "cap") is False
    assert await _S(None).send_photo(7, "/p.png") is False
    assert await _S(_Cli()).send_photo(7, "") is False  # 空路径不发


@pytest.mark.asyncio
async def test_allow_direct_send_returns_empty_when_photo_sent(monkeypatch):
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    prov = cs.get_selfie_provider({"enabled": True, "backend": "disabled"})

    async def _fake_gen(prompt, **kw):
        return cs.SelfieResult(ok=True, image_path="/tmp/fake.png", provider="x")

    monkeypatch.setattr(prov, "generate", _fake_gen)
    sent = {}

    async def _fake_send(chat_id, path, caption):
        sent["path"] = path
        return True

    try:
        sm = _SM(selfie_cfg=_ON, gate=False)  # 准入不限
        ctx = {"intimacy_score": 60, "entitlement": None,
               "_send_photo_to_chat": _fake_send}
        out = await sm._handle_selfie_request("发张照片", "u1", ctx, 999)
        assert out == ""  # 媒体已发出 → 空串(不再补普通文字回复)
        assert sent["path"] == "/tmp/fake.png"
    finally:
        cs.reset_selfie_provider()
