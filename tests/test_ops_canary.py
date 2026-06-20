"""G3 金丝雀放量单测：is_held 白名单语义 + plan_expansion 纯函数 + 持久扩面集。"""
import pytest

from src.ops import canary as canary_mod
from src.ops.canary import (
    CanaryStore, active_cohort, canary_enabled, is_held, member_key, plan_expansion,
)


def _cfg(enabled=True, mode="manual", pinned=None, **kw):
    c = {"enabled": enabled, "mode": mode, "pinned_accounts": pinned or []}
    c.update(kw)
    return {"ops": {"canary": c}}


# ── 基础语义 ─────────────────────────────────────────────────────────────────

def test_disabled_never_holds():
    assert is_held("telegram", "1", _cfg(enabled=False)) == (False, "")
    assert is_held("telegram", "1", {}) == (False, "")  # 无配置→零破坏


def test_enabled_empty_cohort_holds_all():
    # 启用但 cohort 空 = 最保守「先全不放」
    held, reason = is_held("telegram", "1", _cfg(pinned=[]))
    assert held is True and reason == "canary_hold"


def test_pinned_member_passes_others_held():
    cfg = _cfg(pinned=["telegram:1001"])
    assert is_held("telegram", "1001", cfg)[0] is False
    assert is_held("telegram", "9999", cfg)[0] is True


def test_bare_id_treated_as_telegram():
    cfg = _cfg(pinned=["1001"])
    assert is_held("telegram", "1001", cfg)[0] is False
    assert member_key("telegram", "1001") == "telegram:1001"


def test_platform_scoped_member():
    cfg = _cfg(pinned=["line:abc"])
    assert is_held("line", "abc", cfg)[0] is False
    assert is_held("telegram", "abc", cfg)[0] is True  # 平台不匹配仍 hold


# ── plan_expansion 纯函数 ────────────────────────────────────────────────────

def test_expansion_adds_up_to_step_when_green():
    out = plan_expansion({"telegram:1"}, ["telegram:2", "telegram:3", "telegram:4"],
                         fleet_ok=True, step=2)
    assert out == {"telegram:1", "telegram:2", "telegram:3"}


def test_expansion_halts_when_not_green():
    out = plan_expansion({"telegram:1"}, ["telegram:2", "telegram:3"],
                         fleet_ok=False, step=5)
    assert out == {"telegram:1"}  # 出现 paused/banned → 不推进


def test_expansion_skips_existing_members():
    out = plan_expansion({"telegram:1", "telegram:2"}, ["telegram:1", "telegram:3"],
                         fleet_ok=True, step=5)
    assert out == {"telegram:1", "telegram:2", "telegram:3"}


# ── auto_health 模式：持久扩面集并入 cohort ──────────────────────────────────

def test_auto_health_cohort_merges_store(tmp_path, monkeypatch):
    store = CanaryStore(tmp_path / "rf.db")
    store.add({"telegram:2002"})
    cfg = _cfg(mode="auto_health", pinned=["telegram:1001"])
    cohort = active_cohort(cfg, store)
    assert cohort == {"telegram:1001", "telegram:2002"}
    # is_held 经 store 注入后放行扩面号
    assert is_held("telegram", "2002", cfg, store)[0] is False


def test_store_remove_and_clear(tmp_path):
    store = CanaryStore(tmp_path / "rf.db")
    store.add({"telegram:1", "telegram:2"})
    store.remove("telegram:1")
    assert store.members() == {"telegram:2"}
    store.clear()
    assert store.members() == set()


def test_store_persists_across_reopen(tmp_path):
    db = tmp_path / "rf.db"
    CanaryStore(db).add({"telegram:7"})
    assert CanaryStore(db).members() == {"telegram:7"}
