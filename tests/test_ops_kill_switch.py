"""G1 全局 Kill-Switch 单测（反封号护栏三件套之一）。

覆盖：作用域链 / 三级匹配 / TTL 自动恢复 / 重启回填持久化 / 非法 scope 校验，
以及与协议 autoreply（B 线 run_autoreply）的接线——冻结时决策期早退、不生成不发。
"""
import importlib
import time

import pytest

from src.ops import kill_switch as ks_mod
from src.ops.kill_switch import (
    KillSwitch,
    normalize_scope,
    scope_chain,
)


# ── 作用域工具 ───────────────────────────────────────────────────────────────

def test_scope_chain_coarse_to_fine():
    assert scope_chain("telegram", "123") == [
        "global", "platform:telegram", "account:telegram:123",
    ]
    assert scope_chain("Telegram", "") == ["global", "platform:telegram"]
    assert scope_chain("", "") == ["global"]


def test_normalize_scope_valid():
    assert normalize_scope("global") == "global"
    assert normalize_scope("platform:Telegram") == "platform:telegram"
    assert normalize_scope("account:LINE:Ab9") == "account:line:Ab9"


@pytest.mark.parametrize("bad", ["", "foo", "platform:", "account:telegram", "account::x"])
def test_normalize_scope_invalid(bad):
    with pytest.raises(ValueError):
        normalize_scope(bad)


# ── 核心：set / clear / is_blocked / 三级匹配 ────────────────────────────────

def _fresh(tmp_path):
    return KillSwitch(tmp_path / "rf.db")

def test_disabled_by_default(tmp_path):
    ks = _fresh(tmp_path)
    blocked, scope, _ = ks.is_blocked("telegram", "1")
    assert blocked is False and scope == ""


def test_global_freezes_everything(tmp_path):
    ks = _fresh(tmp_path)
    ks.set("global", reason="emergency", actor="alice")
    for plat, acc in [("telegram", "1"), ("line", "x"), ("whatsapp", "z")]:
        blocked, scope, reason = ks.is_blocked(plat, acc)
        assert blocked is True and scope == "global" and reason == "emergency"


def test_platform_scope_isolates(tmp_path):
    ks = _fresh(tmp_path)
    ks.set("platform:telegram")
    assert ks.is_blocked("telegram", "1")[0] is True
    assert ks.is_blocked("line", "1")[0] is False


def test_account_scope_isolates(tmp_path):
    ks = _fresh(tmp_path)
    ks.set("account:telegram:bad")
    assert ks.is_blocked("telegram", "bad")[0] is True
    assert ks.is_blocked("telegram", "good")[0] is False


def test_clear_releases(tmp_path):
    ks = _fresh(tmp_path)
    ks.set("global")
    assert ks.clear("global") is True
    assert ks.is_blocked("telegram", "1")[0] is False
    assert ks.clear("global") is False  # 已无


# ── TTL 自动恢复（doc 方案之外的增强）──────────────────────────────────────

def test_ttl_auto_recovers(tmp_path):
    ks = _fresh(tmp_path)
    t0 = 1000.0
    ks.set("global", ttl_sec=30, now=t0)
    assert ks.is_blocked("telegram", "1", now=t0 + 10)[0] is True
    # 到点后惰性恢复
    assert ks.is_blocked("telegram", "1", now=t0 + 31)[0] is False
    assert ks.status(now=t0 + 31) == []


# ── 重启回填：状态落盘，重开实例仍在 ────────────────────────────────────────

def test_persisted_across_restart(tmp_path):
    db = tmp_path / "rf.db"
    ks1 = KillSwitch(db)
    ks1.set("account:telegram:42", reason="probe")
    # 模拟重启：新实例读同一 DB
    ks2 = KillSwitch(db)
    blocked, scope, reason = ks2.is_blocked("telegram", "42")
    assert blocked is True and scope == "account:telegram:42" and reason == "probe"


def test_status_lists_active_global_first(tmp_path):
    ks = _fresh(tmp_path)
    ks.set("account:line:9")
    ks.set("global")
    scopes = [i["scope"] for i in ks.status()]
    assert scopes[0] == "global"
    assert set(scopes) == {"global", "account:line:9"}


# ── 模块级便捷入口：单例未初始化 → 永不拦截（零破坏）──────────────────────

def test_module_level_no_singleton_returns_unblocked(monkeypatch):
    monkeypatch.setattr(ks_mod, "_singleton", None, raising=False)
    assert ks_mod.is_blocked("telegram", "1") == (False, "", "")


# ── 接线：B 线 run_autoreply 冻结时决策期早退（不生成、不发）────────────────

@pytest.fixture
def _singleton_tmp(tmp_path, monkeypatch):
    ks = KillSwitch(tmp_path / "rf.db")
    monkeypatch.setattr(ks_mod, "_singleton", ks, raising=False)
    return ks


async def test_run_autoreply_frozen_short_circuits(_singleton_tmp):
    from src.integrations import protocol_autoreply as pa

    _singleton_tmp.set("global", reason="freeze")
    calls = {"gen": 0, "send": 0}

    async def _gen(**kw):
        calls["gen"] += 1
        return "hi"

    async def _send(**kw):
        calls["send"] += 1

    class _Reg:
        def get(self, p, a):
            return {"meta": {"auto_reply": True}}

    res = await pa.run_autoreply(
        {"platform": "telegram", "account_id": "1", "chat_key": "c", "text": "hello",
         "direction": "in"},
        registry=_Reg(),
        cfg={"protocol_autoreply": {"enabled": True}},
        generate=_gen,
        send=_send,
    )
    assert res["reason"] == "kill_switch"
    assert res.get("sent") is not True
    assert calls == {"gen": 0, "send": 0}  # 冻结时不生成、不发，省 token


async def test_run_autoreply_normal_when_not_frozen(_singleton_tmp):
    from src.integrations import protocol_autoreply as pa

    async def _gen(**kw):
        return "hi there"

    sent = {"n": 0}

    async def _send(**kw):
        sent["n"] += 1

    class _Reg:
        def get(self, p, a):
            return {"meta": {"auto_reply": True}}

    res = await pa.run_autoreply(
        {"platform": "telegram", "account_id": "1", "chat_key": "c", "text": "hello",
         "direction": "in"},
        registry=_Reg(),
        cfg={"protocol_autoreply": {"enabled": True}},
        generate=_gen,
        send=_send,
    )
    assert res["reason"] == "ok" and res.get("sent") is True and sent["n"] == 1
