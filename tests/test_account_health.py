"""M7 反封号 v1 测试：account_health 纯函数 + AccountLimiter 预热爬坡集成。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore
from src.skills.account_health import account_health, fleet_health, warmup_cap
from src.skills.account_limiter import AccountLimiter


@pytest.fixture
def store(tmp_path):
    s = ContactStore(db_path=tmp_path / "contacts.db")
    yield s
    s.close()


class TestWarmupCap:
    def test_new_account_starts_low(self):
        assert warmup_cap(0, 15) == 2          # 当天 = start_cap
        assert warmup_cap(-5, 15) == 2         # 负天龄保护

    def test_ramps_to_target(self):
        assert warmup_cap(14, 15) == 15        # 满预热期 = target
        assert warmup_cap(99, 15) == 15        # 超期仍是 target

    def test_monotonic_increasing(self):
        caps = [warmup_cap(d, 15) for d in range(0, 14)]
        assert caps == sorted(caps)            # 单调不减
        assert caps[0] == 2 and caps[-1] < 15  # 第 0 天最低，第 13 天未满

    def test_target_below_start_no_ramp(self):
        assert warmup_cap(0, 1, start_cap=2) == 1   # 目标本就很低 → 直接给目标


class TestAccountHealth:
    def test_healthy_account_green(self):
        h = account_health({"age_days": 30, "sends_today": 3, "proxy_bound": True})
        assert h["light"] == "green"
        assert h["score"] >= 70
        assert h["recommended_cap"] == 15

    def test_no_proxy_penalized(self):
        h = account_health({"age_days": 30, "proxy_bound": False})
        assert h["score"] <= 70
        assert any("代理" in r for r in h["reasons"])

    def test_flood_waits_drive_red(self):
        h = account_health({"age_days": 30, "flood_waits_24h": 3, "proxy_bound": False})
        assert h["light"] in ("amber", "red")
        assert any("限频" in r for r in h["reasons"])

    def test_banned_is_zero_red(self):
        h = account_health({"banned": True})
        assert h["score"] == 0 and h["light"] == "red"

    def test_over_warmup_cap_flagged(self):
        # 新号当天建议 2，却发了 10 → over_cap + 扣分
        h = account_health({"age_days": 0, "sends_today": 10})
        assert h["over_cap"] is True
        assert h["recommended_cap"] == 2


class TestFleetHealth:
    def test_aggregate_worst_light(self):
        accts = [
            {"account_id": "a", "age_days": 30, "proxy_bound": True},
            {"account_id": "b", "banned": True},
        ]
        f = fleet_health(accts)
        assert f["fleet_light"] == "red"
        assert f["counts"]["red"] == 1 and f["counts"]["green"] == 1
        assert f["accounts"][0]["account_id"] == "b"  # 最差在前

    def test_empty_fleet_unknown(self):
        f = fleet_health([])
        assert f["fleet_light"] == "unknown" and f["total"] == 0


class TestLimiterWarmupIntegration:
    def test_warmup_disabled_keeps_daily_cap(self, store):
        """默认不开预热 → 行为与历史一致（effective == daily_cap）。"""
        lim = AccountLimiter(store, daily_cap=10)
        assert lim.effective_cap("acc-A") == 10

    def test_warmup_new_account_lower_cap(self, store):
        lim = AccountLimiter(
            store, daily_cap=10, warmup_enabled=True,
            age_days_fn=lambda aid: 0.0,  # 全新号
        )
        assert lim.effective_cap("new") == 2
        assert lim.check_and_reserve("new").ok
        assert lim.check_and_reserve("new").ok
        d = lim.check_and_reserve("new")           # 第 3 次超预热上限 2
        assert d.ok is False
        assert d.reason == "warmup_cap_exceeded"

    def test_warmup_aged_account_full_cap(self, store):
        lim = AccountLimiter(
            store, daily_cap=5, warmup_enabled=True,
            age_days_fn=lambda aid: 30.0,  # 老号
        )
        assert lim.effective_cap("old") == 5
        for _ in range(5):
            assert lim.check_and_reserve("old").ok
        assert lim.check_and_reserve("old").ok is False

    def test_age_fn_none_falls_back(self, store):
        """取不到天龄 → 回退 daily_cap（不误伤）。"""
        lim = AccountLimiter(
            store, daily_cap=8, warmup_enabled=True,
            age_days_fn=lambda aid: None,
        )
        assert lim.effective_cap("x") == 8

    def test_get_counts_reports_effective(self, store):
        lim = AccountLimiter(
            store, daily_cap=10, warmup_enabled=True,
            age_days_fn=lambda aid: 0.0,
        )
        counts = lim.get_counts("new")
        assert counts["daily_cap"] == 10
        assert counts["effective_cap"] == 2
