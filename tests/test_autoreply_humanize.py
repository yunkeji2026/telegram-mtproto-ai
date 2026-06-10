"""Phase 6 拟人化(营业时段/延迟) + 接管摘标 单测。"""

from __future__ import annotations

import pytest

from src.integrations import protocol_autoreply as pa


@pytest.fixture(autouse=True)
def _clear_state():
    pa._last_reply.clear()
    yield
    pa._last_reply.clear()


# ── 营业时段 ──────────────────────────────────────────────────────────────

def test_business_hours_disabled_is_always_on():
    assert pa.within_business_hours({}, now=0) is True
    assert pa.within_business_hours(
        {"protocol_autoreply": {"hours": {"enabled": False}}}, now=0) is True


def test_business_hours_day_window():
    cfg = {"protocol_autoreply": {"hours": {
        "enabled": True, "start": "09:00", "end": "23:00", "tz_offset": 0}}}
    assert pa.within_business_hours(cfg, now=12 * 3600) is True   # 12:00
    assert pa.within_business_hours(cfg, now=2 * 3600) is False   # 02:00


def test_business_hours_overnight_window():
    cfg = {"protocol_autoreply": {"hours": {
        "enabled": True, "start": "22:00", "end": "06:00", "tz_offset": 0}}}
    assert pa.within_business_hours(cfg, now=2 * 3600) is True    # 02:00 在跨夜窗
    assert pa.within_business_hours(cfg, now=12 * 3600) is False  # 12:00 不在


def test_business_hours_tz_offset():
    cfg = {"protocol_autoreply": {"hours": {
        "enabled": True, "start": "09:00", "end": "23:00", "tz_offset": 8}}}
    # UTC 02:00 + 8h = 本地 10:00 → 在窗内
    assert pa.within_business_hours(cfg, now=2 * 3600) is True


# ── 拟人化延迟 ────────────────────────────────────────────────────────────

def test_pick_delay_none():
    assert pa.pick_delay({}) == 0.0
    assert pa.pick_delay({"protocol_autoreply": {"delay": {"max_sec": 0}}}) == 0.0


def test_pick_delay_fixed():
    cfg = {"protocol_autoreply": {"delay": {"min_sec": 2, "max_sec": 2}}}
    assert pa.pick_delay(cfg) == 2.0


def test_pick_delay_range():
    cfg = {"protocol_autoreply": {"delay": {"min_sec": 1, "max_sec": 4}}}
    for _ in range(20):
        d = pa.pick_delay(cfg)
        assert 1.0 <= d <= 4.0


# ── 接管摘标 ──────────────────────────────────────────────────────────────

class _Store:
    def __init__(self, tags):
        self._t = {"c1": list(tags)}

    def get_conv_tags(self, cid):
        return self._t.get(cid, [])

    def set_conv_tags(self, cid, tags):
        self._t[cid] = list(tags)


def test_clear_needs_human_removes_tag():
    s = _Store([pa.HANDOFF_TAG, "vip"])
    assert pa.clear_needs_human(s, "c1") is True
    assert s.get_conv_tags("c1") == ["vip"]


def test_clear_needs_human_noop_when_absent():
    s = _Store(["vip"])
    assert pa.clear_needs_human(s, "c1") is False
    assert s.get_conv_tags("c1") == ["vip"]


def test_clear_needs_human_none_store():
    assert pa.clear_needs_human(None, "c1") is False


# ── 与 run_autoreply 集成 ─────────────────────────────────────────────────

class _Reg:
    def get(self, p, a):
        return {"meta": {"auto_reply": True}}


def _payload(text="在吗"):
    return {"platform": "telegram", "account_id": "tg1",
            "chat_key": "1", "text": text, "direction": "in"}


async def _gen(**kw):
    return "你好"


@pytest.mark.asyncio
async def test_run_autoreply_off_hours_skips():
    sent = []

    async def _send(**kw):
        sent.append(kw)

    cfg = {"protocol_autoreply": {"enabled": True, "hours": {
        "enabled": True, "start": "09:00", "end": "23:00", "tz_offset": 0}}}
    res = await pa.run_autoreply(
        _payload(), registry=_Reg(), cfg=cfg, generate=_gen, send=_send,
        risk_fn=lambda t: "low", now=2 * 3600)  # 02:00 → 营业外
    assert res["reason"] == "off_hours"
    assert sent == []


@pytest.mark.asyncio
async def test_run_autoreply_applies_delay_before_send():
    sent = []
    slept = []

    async def _send(**kw):
        sent.append(kw)

    async def _sleep(d):
        slept.append(d)

    cfg = {"protocol_autoreply": {"enabled": True,
                                  "delay": {"min_sec": 2, "max_sec": 2}}}
    res = await pa.run_autoreply(
        _payload(), registry=_Reg(), cfg=cfg, generate=_gen, send=_send,
        risk_fn=lambda t: "low", now=1000, sleep=_sleep)
    assert res["sent"] is True
    assert slept == [2.0]      # 发送前等了拟人延迟
    assert len(sent) == 1
