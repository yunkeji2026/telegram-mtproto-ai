"""Phase 3 协议自动回复核心逻辑单测（全程依赖注入，无网络/无 LLM）。"""

from __future__ import annotations

import pytest

from src.integrations import protocol_autoreply as pa


class _FakeRegistry:
    def __init__(self, row):
        self._row = row

    def get(self, platform, account_id):
        return self._row


def _payload(text="你好", direction="in"):
    return {
        "platform": "telegram", "account_id": "tg1", "chat_key": "123",
        "text": text, "direction": direction,
    }


def _row(auto_reply=True, persona_id="zjg"):
    return {"platform": "telegram", "account_id": "tg1",
            "meta": {"auto_reply": auto_reply, "persona_id": persona_id}}


@pytest.fixture(autouse=True)
def _clear_state():
    pa._last_reply.clear()
    yield
    pa._last_reply.clear()


def _make_send(sink):
    async def _send(**kw):
        sink.append(kw)
        return {"delivered": True}
    return _send


def _make_gen(reply, captured=None):
    async def _gen(**kw):
        if captured is not None:
            captured.update(kw)
        return reply
    return _gen


@pytest.mark.asyncio
async def test_global_gate_off_skips():
    sent = []
    res = await pa.run_autoreply(
        _payload(), registry=_FakeRegistry(_row()),
        cfg={"protocol_autoreply": {"enabled": False}},
        generate=_make_gen("hi"), send=_make_send(sent),
        risk_fn=lambda t: "low",
    )
    assert res["skipped"] == "disabled"
    assert sent == []


@pytest.mark.asyncio
async def test_account_gate_off_skips():
    sent = []
    res = await pa.run_autoreply(
        _payload(), registry=_FakeRegistry(_row(auto_reply=False)),
        cfg={"protocol_autoreply": {"enabled": True}},
        generate=_make_gen("hi"), send=_make_send(sent),
        risk_fn=lambda t: "low",
    )
    assert res["skipped"] == "disabled"
    assert sent == []


@pytest.mark.asyncio
async def test_both_gates_on_sends_and_passes_persona():
    sent = []
    cap = {}
    res = await pa.run_autoreply(
        _payload(), registry=_FakeRegistry(_row(persona_id="zjg")),
        cfg={"protocol_autoreply": {"enabled": True}},
        generate=_make_gen("亲，在的~", cap), send=_make_send(sent),
        risk_fn=lambda t: "low",
    )
    assert res["sent"] is True
    assert res["text"] == "亲，在的~"
    assert len(sent) == 1
    assert sent[0]["text"] == "亲，在的~"
    assert cap["persona_id"] == "zjg"  # 人设透传到生成


@pytest.mark.asyncio
async def test_high_risk_reply_not_sent():
    sent = []
    res = await pa.run_autoreply(
        _payload(), registry=_FakeRegistry(_row()),
        cfg={"protocol_autoreply": {"enabled": True}},
        generate=_make_gen("请输入支付密码"), send=_make_send(sent),
        risk_fn=lambda t: "high",
    )
    assert res["skipped"] == "high_risk"
    assert sent == []


@pytest.mark.asyncio
async def test_duplicate_inbound_skipped():
    sent = []
    cfg = {"protocol_autoreply": {"enabled": True}}
    reg = _FakeRegistry(_row())
    first = await pa.run_autoreply(
        _payload("在吗"), registry=reg, cfg=cfg,
        generate=_make_gen("在的"), send=_make_send(sent),
        risk_fn=lambda t: "low", now=1000.0,
    )
    second = await pa.run_autoreply(
        _payload("在吗"), registry=reg, cfg=cfg,
        generate=_make_gen("在的"), send=_make_send(sent),
        risk_fn=lambda t: "low", now=1001.0,
    )
    assert first["sent"] is True
    assert second["skipped"] == "duplicate"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_cooldown_blocks_rapid_distinct():
    sent = []
    cfg = {"protocol_autoreply": {"enabled": True}}
    reg = _FakeRegistry(_row())
    await pa.run_autoreply(
        _payload("第一句"), registry=reg, cfg=cfg,
        generate=_make_gen("回复1"), send=_make_send(sent),
        risk_fn=lambda t: "low", now=2000.0,
    )
    res = await pa.run_autoreply(
        _payload("第二句"), registry=reg, cfg=cfg,
        generate=_make_gen("回复2"), send=_make_send(sent),
        risk_fn=lambda t: "low", now=2000.5,  # < AUTO_COOLDOWN_SEC
    )
    assert res["skipped"] == "cooldown"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_outbound_payload_ignored():
    sent = []
    res = await pa.run_autoreply(
        _payload(direction="out"), registry=_FakeRegistry(_row()),
        cfg={"protocol_autoreply": {"enabled": True}},
        generate=_make_gen("x"), send=_make_send(sent),
    )
    assert res["skipped"] == "not_inbound"
    assert sent == []


# ── Phase 3 防双发：会话已由收件箱全自动托管 → 直发链路早退 ──────────────

@pytest.mark.asyncio
async def test_inbox_autopilot_conv_skips_direct_send():
    """会话 automation_mode=auto_ai（收件箱全自动）→ protocol_autoreply 早退，不直发。"""
    sent = []
    gen_called = []

    async def _gen(**kw):
        gen_called.append(kw)
        return "不该生成"

    res = await pa.run_autoreply(
        _payload(), registry=_FakeRegistry(_row()),
        cfg={"protocol_autoreply": {"enabled": True}},
        generate=_gen, send=_make_send(sent),
        risk_fn=lambda t: "low",
        inbox_mode_fn=lambda p, a, c: "auto_ai",
    )
    assert res["skipped"] == "inbox_autopilot"
    assert sent == []
    assert gen_called == []  # 早退在生成之前，连 token 都不烧


@pytest.mark.asyncio
async def test_non_auto_ai_conv_still_direct_sends():
    """会话非 auto_ai（如 review/manual）→ 直发链路照常工作（账号级闸门开时）。"""
    sent = []
    res = await pa.run_autoreply(
        _payload(), registry=_FakeRegistry(_row()),
        cfg={"protocol_autoreply": {"enabled": True}},
        generate=_make_gen("亲，在的~"), send=_make_send(sent),
        risk_fn=lambda t: "low",
        inbox_mode_fn=lambda p, a, c: "review",
    )
    assert res["sent"] is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_inbox_mode_fn_exception_does_not_block():
    """inbox_mode_fn 抛错 → 不影响主流程（容错放行）。"""
    sent = []

    def _boom(p, a, c):
        raise RuntimeError("store down")

    res = await pa.run_autoreply(
        _payload(), registry=_FakeRegistry(_row()),
        cfg={"protocol_autoreply": {"enabled": True}},
        generate=_make_gen("亲，在的~"), send=_make_send(sent),
        risk_fn=lambda t: "low",
        inbox_mode_fn=_boom,
    )
    assert res["sent"] is True
    assert len(sent) == 1
