"""G2 封号信号自动急停单测：classify 纯函数 + apply_action 复用 G1 Kill-Switch。"""
import time

import pytest

from src.ops import kill_switch as ks_mod
from src.ops.ban_signal import apply_action, classify, handle_send_exception
from src.ops.kill_switch import KillSwitch


# ── 伪异常（模拟 pyrogram.errors，按类名分类，无需真 pyrogram）──────────────

class FloodWait(Exception):
    def __init__(self, value):
        self.value = value
        super().__init__(f"FloodWait {value}")

class PeerFlood(Exception):
    pass

class UserDeactivatedBan(Exception):
    pass

class Unauthorized(Exception):
    pass


# ── classify ─────────────────────────────────────────────────────────────────

def test_classify_floodwait_is_backoff():
    a = classify(FloodWait(30))
    assert a["kind"] == "backoff" and a["cooldown_sec"] == 30.0

def test_classify_peerflood_is_pause():
    assert classify(PeerFlood())["kind"] == "pause"

def test_classify_ban_signals():
    assert classify(UserDeactivatedBan())["kind"] == "ban"
    assert classify(Unauthorized())["kind"] == "ban"

def test_classify_unknown_is_none():
    assert classify(ValueError("boom"))["kind"] == "none"

def test_classify_own_control_flow_is_none():
    # 我们自己抛的 gate/kill_switch 异常不能被当成封号信号
    assert classify(RuntimeError("send_gate_blocked:warmup_cap"))["kind"] == "none"
    assert classify(RuntimeError("kill_switch_blocked:global"))["kind"] == "none"


# ── apply_action：pause/ban 落到账号级 Kill-Switch（复用 G1）─────────────────

def test_pause_sets_account_killswitch_with_ttl(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    apply_action("telegram", "42", classify(PeerFlood()),
                 kill_switch=ks, pause_minutes=60)
    blocked, scope, reason = ks.is_blocked("telegram", "42")
    assert blocked is True and scope == "account:telegram:42"
    assert "auto_pause" in reason
    # 其它号不受影响
    assert ks.is_blocked("telegram", "other")[0] is False


def test_pause_auto_recovers_after_ttl(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    t0 = 1000.0
    apply_action("telegram", "42", {"kind": "pause", "cooldown_sec": 30, "reason": "PeerFlood"},
                 kill_switch=ks, now=t0)
    assert ks.is_blocked("telegram", "42", now=t0 + 10)[0] is True
    assert ks.is_blocked("telegram", "42", now=t0 + 31)[0] is False


def test_ban_sets_permanent_and_marks_registry(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")

    class _Reg:
        def __init__(self):
            self.row = {"meta": {}}
            self.upserts = []
        def get(self, p, a):
            return self.row
        def upsert(self, p, a, *, meta=None, **kw):
            self.upserts.append(meta)
            self.row["meta"] = meta

    reg = _Reg()
    alerts = []
    apply_action("telegram", "99", classify(UserDeactivatedBan()),
                 kill_switch=ks, registry=reg,
                 alert=lambda k, p, d: alerts.append((k, p, d)))
    blocked, scope, _ = ks.is_blocked("telegram", "99")
    assert blocked is True and scope == "account:telegram:99"
    assert reg.row["meta"]["banned"] is True
    assert alerts and alerts[0][0] == "account_banned"


def test_backoff_does_not_pause(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    res = apply_action("telegram", "42", classify(FloodWait(20)), kill_switch=ks)
    assert res["applied"] == "backoff"
    assert ks.is_blocked("telegram", "42")[0] is False  # 限速不停号


# ── handle_send_exception：用进程单例，绝不抛 ───────────────────────────────

def test_handle_uses_singleton(tmp_path, monkeypatch):
    ks = KillSwitch(tmp_path / "rf.db")
    monkeypatch.setattr(ks_mod, "_singleton", ks, raising=False)
    out = handle_send_exception("telegram", "7", PeerFlood())
    assert out["kind"] == "pause"
    assert ks.is_blocked("telegram", "7")[0] is True


def test_handle_never_raises_when_killswitch_broken():
    # 注入一个 set 会抛错的 ks → 处置失败也不能掩盖/抛出原始发送错误
    class _BrokenKS:
        def set(self, *a, **k):
            raise RuntimeError("db down")

    out = handle_send_exception("telegram", "7", Unauthorized(), kill_switch=_BrokenKS())
    assert out["applied"] == "error" and out["kind"] == "none"
