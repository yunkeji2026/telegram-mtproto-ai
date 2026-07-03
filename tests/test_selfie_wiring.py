"""Stage A：skill_manager 形象照请求接线（轻量绑定，免全量 init）。

校验：未开→None、非请求→None、关系浅→搪塞、未解锁→付费引导、准入+provider 关→文字兜底。
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from src.ai.companion_selfie import SELFIE_FEATURE, reset_selfie_provider
from src.utils.companion_funnel_store import (
    get_companion_funnel_store,
    reset_companion_funnel_store,
)
from src.utils.selfie_cap import reset_selfie_cap_tracker

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


class _SM:
    _selfie_cfg = _SMcls._selfie_cfg
    _monetization_gate_enabled = _SMcls._monetization_gate_enabled
    _handle_selfie_request = _SMcls._handle_selfie_request
    _handle_contextual_image_request = _SMcls._handle_contextual_image_request
    _record_selfie_event = _SMcls._record_selfie_event
    _get_selfie_cap = _SMcls._get_selfie_cap
    _selfie_upsell_text = _SMcls._selfie_upsell_text
    _try_send_selfie_media = _SMcls._try_send_selfie_media
    _selfie_persona_for_prompt = _SMcls._selfie_persona_for_prompt
    _selfie_album_key = _SMcls._selfie_album_key
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


# ── Stage G：send_photo 纳入统一发送护栏/节流/记账（图不绕过风控） ──────────

class _PhotoCli:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def send_photo(self, chat_id, photo, caption=""):
        if self.fail:
            raise RuntimeError("rpc")
        self.calls.append((chat_id, photo, caption))


def _photo_sender(cli, *, min_interval=0, last_send=0.0):
    from src.client.sender import TelegramSenderMixin

    class _Cfg:
        def get(self, k, d=None):
            if k == "reply":
                return {"split_send": {"min_interval_seconds": min_interval}}
            return d if d is not None else {}

    class _S(TelegramSenderMixin):
        def __init__(self):
            self.client = cli
            self.logger = logging.getLogger("test_sender_g")
            self.account_id = "a"
            self.config = _Cfg()
            self._last_send_wallclock = last_send

    s = _S()
    s._shared_send_limiter = lambda cfg: None  # 不触 DB/限流器副作用
    return s


@pytest.mark.asyncio
async def test_send_photo_blocked_by_presend_guard(monkeypatch):
    s = _photo_sender(_PhotoCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda: True)  # 冻结/被闸门拦
    assert await s.send_photo(7, "/p.png", "c") is False
    assert s.client.calls == []  # 护栏拦下，照片未真发（不绕过风控）


@pytest.mark.asyncio
async def test_send_photo_paces_against_shared_wallclock(monkeypatch):
    slept = {}

    async def _fake_sleep(sec):
        slept["sec"] = sec

    monkeypatch.setattr("src.client.sender.asyncio.sleep", _fake_sleep)
    s = _photo_sender(_PhotoCli(), min_interval=5, last_send=time.time())
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    ok = await s.send_photo(7, "/p.png", "c")
    assert ok is True
    assert slept.get("sec") is not None and slept["sec"] > 0  # 距上次<5s→补足节流
    assert s.client.calls == [(7, "/p.png", "c")]
    assert s._last_send_wallclock > 0  # 记账刷新墙钟（下次文本据此排队）


@pytest.mark.asyncio
async def test_send_photo_no_pace_when_interval_zero(monkeypatch):
    slept = {}

    async def _fake_sleep(sec):
        slept["sec"] = sec

    monkeypatch.setattr("src.client.sender.asyncio.sleep", _fake_sleep)
    s = _photo_sender(_PhotoCli(), min_interval=0, last_send=time.time())
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    assert await s.send_photo(7, "/p.png", "c") is True
    assert "sec" not in slept  # min_interval=0 → 不节流（行为不变）


# ── Stage H：富媒体外发的出站镜像 + contacts 记账（坐席台/亲密度看见图） ──────

@pytest.mark.asyncio
async def test_send_photo_mirrors_and_records(monkeypatch):
    emitted = {}
    recorded = {}

    def _rec(acc, chat, direction, **kw):
        recorded.update({"acc": acc, "chat": chat, "dir": direction,
                         "prev": kw.get("text_preview", "")})

    import src.utils.companion_context as cc
    monkeypatch.setattr(cc, "record_relationship_message", _rec)

    s = _photo_sender(_PhotoCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    s._emit_inbox = lambda **kw: emitted.update(kw)

    assert await s.send_photo(7, "/p.png", "看我新裙子") is True
    # 坐席台镜像：带 [图片] 前缀 + 配文，方向 out；msg_id 供回显去重（mock 客户端无 id→空串）
    assert emitted["chat_id"] == 7
    assert emitted["text"] == "[图片] 看我新裙子"
    assert emitted["direction"] == "out"
    assert emitted.get("msg_id") == ""
    # contacts 记账：外发互动计入 IntimacyEngine（mutuality）
    assert recorded["dir"] == "out" and recorded["prev"] == "[图片] 看我新裙子"
    assert recorded["chat"] == 7 and recorded["acc"] == "a"


@pytest.mark.asyncio
async def test_send_photo_empty_caption_preview(monkeypatch):
    emitted = {}
    import src.utils.companion_context as cc
    monkeypatch.setattr(cc, "record_relationship_message", lambda *a, **k: None)
    s = _photo_sender(_PhotoCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    s._emit_inbox = lambda **kw: emitted.update(kw)
    assert await s.send_photo(7, "/p.png", "") is True
    assert emitted["text"] == "[图片]"  # 无配文 → 仅标记


@pytest.mark.asyncio
async def test_postsend_mirror_record_no_emit_attr_still_records(monkeypatch):
    recorded = {}
    import src.utils.companion_context as cc
    monkeypatch.setattr(cc, "record_relationship_message",
                        lambda *a, **k: recorded.update({"hit": True}))
    s = _photo_sender(_PhotoCli())  # 无 _emit_inbox 属性
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    assert await s.send_photo(7, "/p.png", "hi") is True  # 镜像缺省→优雅跳过、不抛
    assert recorded.get("hit") is True  # contacts 记账照常


# ── Stage F：全局每日出图预算 cap（护出图 API 账单） ──────────────────────

@pytest.mark.asyncio
async def test_global_cap_blocks_second_and_preserves_quota(monkeypatch):
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    reset_companion_funnel_store()
    reset_selfie_cap_tracker()  # 单例：清前序测试残留计数，保证从 0 起
    funnel = get_companion_funnel_store(":memory:")
    prov = cs.get_selfie_provider({"enabled": True, "backend": "openai"})

    async def _gen(p, **k):
        return cs.SelfieResult(ok=True, image_path="/tmp/x.png", provider="openai")

    monkeypatch.setattr(prov, "generate", _gen)
    try:
        sm = _SM(selfie_cfg=dict(_ON, free_daily=5, daily_global_cap=1,
                                 provider={"enabled": True, "backend": "openai"}),
                 gate=True)
        ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
        out1 = await sm._handle_selfie_request("发张照片", "u1", ctx, 1)
        assert out1 is not None and out1 != ""        # 第1次正常出图(无通道→配文)
        assert ctx.get("_selfie_used") == 1
        out2 = await sm._handle_selfie_request("再发张照片", "u1", ctx, 1)
        assert "明天" in out2                          # 第2次全局额度用尽→capped 兜底
        assert ctx.get("_selfie_used") == 1            # 未再消耗用户免费额度
        kinds = [r["kind"] for r in funnel.selfie_recent(limit=10)]
        assert "capped" in kinds and kinds.count("delivered") == 1
    finally:
        cs.reset_selfie_provider()
        reset_companion_funnel_store()
        reset_selfie_cap_tracker()


@pytest.mark.asyncio
async def test_global_cap_zero_means_unlimited(monkeypatch):
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    prov = cs.get_selfie_provider({"enabled": True, "backend": "openai"})

    async def _gen(p, **k):
        return cs.SelfieResult(ok=True, image_path="/tmp/x.png")

    monkeypatch.setattr(prov, "generate", _gen)
    try:
        sm = _SM(selfie_cfg=dict(_ON, daily_global_cap=0,
                                 provider={"enabled": True, "backend": "openai"}),
                 gate=False)
        for _ in range(3):
            out = await sm._handle_selfie_request(
                "发张照片", "u1", {"intimacy_score": 60, "entitlement": None}, 1)
            assert "明天" not in out  # cap=0 → 永不拦
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_global_cap_ignored_when_provider_disabled():
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()  # provider disabled → 无出图成本 → 不计 cap
    try:
        sm = _SM(selfie_cfg=dict(_ON, daily_global_cap=1), gate=False)
        for _ in range(3):
            out = await sm._handle_selfie_request(
                "发张照片", "u1", {"intimacy_score": 60, "entitlement": None}, 1)
            assert "明天" not in out  # 恒文字兜底，cap 不介入
    finally:
        cs.reset_selfie_provider()


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


# ── Stage B：对话上下文「按需生图」接线（"你煮的面拍张照给我看"） ────────────

_CTX_ON = {"enabled": True, "contextual_images": True, "min_bond_level": 0,
           "provider": {"enabled": True, "backend": "openai"}}


@pytest.mark.asyncio
async def test_ctx_image_disabled_returns_none():
    sm = _SM(selfie_cfg={"enabled": True})  # contextual_images 缺省关
    out = await sm._handle_contextual_image_request(
        "你煮的面拍张照给我看", "u1",
        {"intimacy_score": 60, "_conversation_history": []}, "c1")
    assert out is None


@pytest.mark.asyncio
async def test_ctx_image_not_a_request_returns_none():
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    try:
        sm = _SM(selfie_cfg=_CTX_ON)
        out = await sm._handle_contextual_image_request(
            "今天天气真好", "u1", {"intimacy_score": 60}, "c1")
        assert out is None
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_ctx_image_album_backend_defers_to_text():
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    try:
        # album 后端无法凭空生成"你煮的面" → 交普通回复(None)，不硬答
        sm = _SM(selfie_cfg=dict(_CTX_ON,
                                 provider={"enabled": True, "backend": "album"}))
        out = await sm._handle_contextual_image_request(
            "你煮的面拍张照给我看", "u1",
            {"intimacy_score": 60, "_conversation_history": []}, "c1")
        assert out is None
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_ctx_image_generates_from_context_and_sends(monkeypatch):
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    prov = cs.get_selfie_provider({"enabled": True, "backend": "openai"})

    async def _gen(prompt, **kw):
        assert "noodles" in prompt  # 从上下文"我刚煮了面"抽出的主体进了 prompt
        assert not kw.get("base_image")  # 物体图 text2img，不带人设的脸
        return cs.SelfieResult(ok=True, image_path="/tmp/noodles.png", provider="openai")

    monkeypatch.setattr(prov, "generate", _gen)
    sent = {}

    async def _fake_send(chat_id, path, caption):
        sent["args"] = (chat_id, path, caption)
        return True

    try:
        sm = _SM(selfie_cfg=_CTX_ON)
        ctx = {"intimacy_score": 60,
               "_conversation_history": [{"role": "assistant", "content": "我刚煮了面"}],
               "_send_photo_to_chat": _fake_send}
        out = await sm._handle_contextual_image_request(
            "你煮的拍张照给我看嗎", "u1", ctx, 12345)
        assert out == ""  # 图已发出 → 空串
        assert sent["args"][1] == "/tmp/noodles.png"
    finally:
        cs.reset_selfie_provider()
