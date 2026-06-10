"""Phase 8 按账号覆盖 + 限流覆盖 + 告警防抖 单测。"""

from __future__ import annotations

import pytest

from src.integrations import protocol_autoreply as pa
from src.integrations import protocol_autoreply_settings as s
from src.integrations.protocol_autoreply_limits import AutoReplyLimiter


@pytest.fixture(autouse=True)
def _clear():
    pa._last_reply.clear()
    pa._alert_seen.clear()
    yield
    pa._last_reply.clear()
    pa._alert_seen.clear()


# ── 覆盖合并 ──────────────────────────────────────────────────────────────

def test_sanitize_override_drops_enabled():
    out = s.sanitize_override({"enabled": True, "rate": {"hourly": 5}})
    assert "enabled" not in out
    assert out["rate"] == {"hourly": 5}


def test_merge_account_override():
    glob = {"rate": {"hourly": 30, "daily": 200}, "delay": {"min_sec": 1}}
    ov = {"rate": {"hourly": 5}}
    eff = s.merge_account_override(glob, ov)
    assert eff["rate"]["hourly"] == 5      # 覆盖
    assert eff["rate"]["daily"] == 200     # 保留
    assert eff["delay"]["min_sec"] == 1


def test_account_effective_pa_no_override():
    cfg = {"protocol_autoreply": {"rate": {"hourly": 30}}}
    row = {"meta": {}}
    assert pa._account_effective_pa(cfg, row)["rate"]["hourly"] == 30


def test_account_effective_pa_with_override():
    cfg = {"protocol_autoreply": {"rate": {"hourly": 30, "daily": 200}}}
    row = {"meta": {"autoreply_override": {"rate": {"hourly": 3}}}}
    eff = pa._account_effective_pa(cfg, row)
    assert eff["rate"]["hourly"] == 3
    assert eff["rate"]["daily"] == 200


# ── 限流按账号覆盖 ────────────────────────────────────────────────────────

def test_limiter_per_call_override():
    lim = AutoReplyLimiter(hourly=100, daily=100)
    k = "telegram:tg1"
    lim.record_sent(k, now=1000)
    # 全局上限 100 → 允许；但该账号覆盖 hourly=1 → 拦截
    assert lim.allow(k, now=1001)[0] is True
    assert lim.allow(k, now=1001, hourly=1) == (False, "quota_hour")


class _Reg:
    def __init__(self, override=None):
        self._ov = override or {}

    def get(self, p, a):
        return {"meta": {"auto_reply": True, "autoreply_override": self._ov}}


def _payload(chat="1"):
    return {"platform": "telegram", "account_id": "tg1",
            "chat_key": chat, "text": "在吗", "direction": "in"}


async def _gen(**kw):
    return "你好"


@pytest.mark.asyncio
async def test_run_autoreply_uses_account_quota_override():
    sent = []

    async def _send(**kw):
        sent.append(kw)

    lim = AutoReplyLimiter(hourly=100, daily=100)  # 全局很宽
    reg = _Reg(override={"rate": {"hourly": 1}})    # 账号收紧到 1/h
    cfg = {"protocol_autoreply": {"enabled": True}}
    first = await pa.run_autoreply(
        _payload("1"), registry=reg, cfg=cfg, generate=_gen, send=_send,
        risk_fn=lambda t: "low", now=1000, limiter=lim)
    second = await pa.run_autoreply(
        _payload("2"), registry=reg, cfg=cfg, generate=_gen, send=_send,
        risk_fn=lambda t: "low", now=1010, limiter=lim)
    assert first["sent"] is True
    assert second["reason"] == "quota_hour"   # 账号覆盖生效
    assert len(sent) == 1


# ── 告警防抖 ──────────────────────────────────────────────────────────────

def test_publish_alert_debounce(monkeypatch):
    published = []

    class _Bus:
        def publish(self, et, data):
            published.append((et, data))

    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: _Bus())
    p = {"platform": "telegram", "account_id": "tg1"}
    assert pa.publish_alert("circuit_open", p, "x", now=1000) is True
    # 30 分钟内同类同账号 → 不再发
    assert pa.publish_alert("circuit_open", p, "x", now=1100) is False
    # 超过防抖窗 → 再发
    assert pa.publish_alert("circuit_open", p, "x", now=1000 + 1801) is True
    assert len(published) == 2
    assert published[0][0] == "autoreply_alert"
    assert published[0][1]["kind"] == "circuit_open"
