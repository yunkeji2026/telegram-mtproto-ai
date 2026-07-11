"""Phase 5 限速 + 熔断单测（限流器本体 + 与 run_autoreply 集成）。"""

from __future__ import annotations

import pytest

from src.integrations import protocol_autoreply as pa
from src.integrations.protocol_autoreply_limits import (
    AutoReplyLimiter, SendCountStore,
)


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


# ── 持久化 SendCountStore：send-gate 日配额跨重启存活 ────────────────────────

def test_persist_day_count_survives_restart(tmp_path):
    """核心不变量：限流器实例重建（模拟进程重启）后，日计数从持久化 store 恢复，
    而非归零——修复「重启即重置日配额」的真号安全洞。

    用接近真实的时间戳（构造时按真实 wall-clock 清理 >2 天陈旧行，故测试数据须在窗内）。
    """
    import time as _t
    base = _t.time()
    db = tmp_path / "account_sends.db"
    k = "telegram:8244899900"
    lim1 = AutoReplyLimiter(hourly=0, daily=3, store=SendCountStore(db))
    lim1.record_sent(k, now=base)
    lim1.record_sent(k, now=base + 1)
    lim1.record_sent(k, now=base + 2)
    assert lim1.allow(k, now=base + 3) == (False, "quota_day")   # 已达 3
    # 模拟重启：新 store 指向同一 db + 新 limiter 实例（内存 deque 为空）
    lim2 = AutoReplyLimiter(hourly=0, daily=3, store=SendCountStore(db))
    assert lim2.snapshot(k, now=base + 4)["day_used"] == 3       # 从库恢复，非 0
    assert lim2.allow(k, now=base + 4) == (False, "quota_day")   # 仍拦，未因重启放水


def test_persist_hour_window_slides(tmp_path):
    import time as _t
    base = _t.time()
    db = tmp_path / "account_sends.db"
    k = "telegram:1"
    lim = AutoReplyLimiter(hourly=1, daily=100, store=SendCountStore(db))
    lim.record_sent(k, now=base)
    assert lim.allow(k, now=base + 1)[1] == "quota_hour"
    assert lim.allow(k, now=base + 3601)[0] is True          # 旧记录滑出小时窗（查库同样滑动）


def test_persist_prunes_stale_rows(tmp_path):
    """>2 天陈旧行清理：不影响 24h 计数，表恒小。"""
    db = tmp_path / "account_sends.db"
    store = SendCountStore(db)
    k = "telegram:1"
    now = 1_000_000.0
    store.record(k, now - 3 * 86400)     # 3 天前
    store.record(k, now - 100)           # 刚刚
    assert store.count_since(k, now - 86400) == 1   # 24h 内只 1 条
    store._prune_locked(now - 2 * 86400)
    assert store.count_since(k, 0) == 1             # 陈旧行已清


def test_store_failure_falls_back_to_memory(tmp_path, monkeypatch):
    """store 查询抛错 → 降级内存计数，绝不因 IO 卡死发送。"""
    db = tmp_path / "account_sends.db"
    lim = AutoReplyLimiter(hourly=0, daily=2, store=SendCountStore(db))
    k = "telegram:1"
    lim.record_sent(k, now=1000)   # 内存 deque + store 都记了

    def _boom(*a, **kw):
        raise RuntimeError("db locked")
    monkeypatch.setattr(lim._store, "count_since", _boom)
    # store 挂 → _counts 降级读内存 deque（仍有 1 条）
    assert lim.snapshot(k, now=1001)["day_used"] == 1


def test_no_store_keeps_legacy_memory_behavior():
    """不传 store → 纯内存（旧行为，零破坏）。"""
    lim = AutoReplyLimiter(hourly=0, daily=2)
    k = "telegram:1"
    lim.record_sent(k, now=1000)
    lim.record_sent(k, now=1001)
    assert lim.allow(k, now=1002) == (False, "quota_day")


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
