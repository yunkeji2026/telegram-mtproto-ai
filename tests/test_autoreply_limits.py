"""Phase 5 限速 + 熔断单测（限流器本体 + 与 run_autoreply 集成）。"""

from __future__ import annotations

import pytest

from src.integrations import protocol_autoreply as pa
from src.integrations.protocol_autoreply_limits import AutoReplyLimiter


@pytest.fixture(autouse=True)
def _clear_state():
    pa._last_reply.clear()
    yield
    pa._last_reply.clear()


# ── 限流器本体 ────────────────────────────────────────────────────────────

def test_quota_hour():
    lim = AutoReplyLimiter(hourly=2, daily=100)
    k = "telegram:tg1"
    assert lim.allow(k, now=1000)[0] is True
    lim.record_sent(k, now=1000)
    lim.record_sent(k, now=1001)
    ok, why = lim.allow(k, now=1002)
    assert ok is False and why == "quota_hour"


def test_quota_day():
    lim = AutoReplyLimiter(hourly=0, daily=2)  # hourly=0 关闭小时限
    k = "telegram:tg1"
    lim.record_sent(k, now=1000)
    lim.record_sent(k, now=2000)
    ok, why = lim.allow(k, now=3000)
    assert ok is False and why == "quota_day"


def test_hour_window_slides():
    lim = AutoReplyLimiter(hourly=1, daily=100)
    k = "telegram:tg1"
    lim.record_sent(k, now=1000)
    assert lim.allow(k, now=1001)[1] == "quota_hour"
    # 一小时后旧记录滑出小时窗
    assert lim.allow(k, now=1000 + 3601)[0] is True


def test_breaker_opens_and_blocks():
    lim = AutoReplyLimiter(breaker_threshold=2, breaker_cooldown=300)
    k = "telegram:tg1"
    assert lim.record_failure(k, now=1000) is False
    assert lim.record_failure(k, now=1001) is True  # 第2次触发
    assert lim.allow(k, now=1002) == (False, "circuit_open")
    # 冷却后半开放行
    assert lim.allow(k, now=1001 + 301)[0] is True


def test_breaker_success_closes():
    lim = AutoReplyLimiter(breaker_threshold=2, breaker_cooldown=300)
    k = "telegram:tg1"
    lim.record_failure(k, now=1000)
    lim.record_success(k)  # 成功清零
    assert lim.record_failure(k, now=1001) is False  # 计数已清，未触发


def test_snapshot():
    lim = AutoReplyLimiter(hourly=10, daily=50)
    k = "telegram:tg1"
    lim.record_sent(k, now=1000)
    lim.record_sent(k, now=1001)
    s = lim.snapshot(k, now=1002)
    assert s["hour_used"] == 2 and s["hour_limit"] == 10
    assert s["day_used"] == 2 and s["day_limit"] == 50
    assert s["circuit_open"] is False


# ── 与 run_autoreply 集成 ─────────────────────────────────────────────────

class _Reg:
    def get(self, p, a):
        return {"meta": {"auto_reply": True}}


def _payload(chat="1", text="在吗"):
    return {"platform": "telegram", "account_id": "tg1",
            "chat_key": chat, "text": text, "direction": "in"}


async def _ok_gen(**kw):
    return "你好"


@pytest.mark.asyncio
async def test_run_autoreply_quota_blocks():
    sent = []

    async def _send(**kw):
        sent.append(kw)

    lim = AutoReplyLimiter(hourly=1, daily=100)
    cfg = {"protocol_autoreply": {"enabled": True}}
    first = await pa.run_autoreply(
        _payload(chat="1"), registry=_Reg(), cfg=cfg,
        generate=_ok_gen, send=_send, risk_fn=lambda t: "low",
        now=1000, limiter=lim)
    second = await pa.run_autoreply(
        _payload(chat="2"), registry=_Reg(), cfg=cfg,
        generate=_ok_gen, send=_send, risk_fn=lambda t: "low",
        now=1010, limiter=lim)
    assert first["sent"] is True
    assert second["reason"] == "quota_hour"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_run_autoreply_breaker_on_send_failure():
    async def _bad_send(**kw):
        raise RuntimeError("network down")

    lim = AutoReplyLimiter(breaker_threshold=1, breaker_cooldown=300)
    cfg = {"protocol_autoreply": {"enabled": True}}
    res = await pa.run_autoreply(
        _payload(chat="1"), registry=_Reg(), cfg=cfg,
        generate=_ok_gen, send=_bad_send, risk_fn=lambda t: "low",
        now=1000, limiter=lim)
    assert res["reason"] == "send_error"
    assert res["breaker_opened"] is True
    # 断路器已开，下一条直接 circuit_open
    nxt = await pa.run_autoreply(
        _payload(chat="2"), registry=_Reg(), cfg=cfg,
        generate=_ok_gen, send=_bad_send, risk_fn=lambda t: "low",
        now=1010, limiter=lim)
    assert nxt["reason"] == "circuit_open"
