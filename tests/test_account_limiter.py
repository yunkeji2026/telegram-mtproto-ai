"""AccountLimiter 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore
from src.skills.account_limiter import AccountLimiter


@pytest.fixture
def store(tmp_path):
    s = ContactStore(db_path=tmp_path / "contacts.db")
    yield s
    s.close()


class TestBasic:
    def test_reserve_under_cap(self, store):
        lim = AccountLimiter(store, daily_cap=3)
        for i in range(3):
            d = lim.check_and_reserve("acc-A")
            assert d.ok
            assert d.account_count_today == i + 1
        assert lim.remaining_for("acc-A") == 0

    def test_over_cap_rejected(self, store):
        lim = AccountLimiter(store, daily_cap=2)
        assert lim.check_and_reserve("acc-A").ok
        assert lim.check_and_reserve("acc-A").ok
        d = lim.check_and_reserve("acc-A")
        assert d.ok is False
        assert d.reason == "account_cap_exceeded"

    def test_per_account_isolated(self, store):
        lim = AccountLimiter(store, daily_cap=1)
        assert lim.check_and_reserve("acc-A").ok
        # B 仍可用
        assert lim.check_and_reserve("acc-B").ok
        # A 已满
        assert lim.check_and_reserve("acc-A").ok is False


class TestGlobalCap:
    def test_global_blocks_all(self, store):
        lim = AccountLimiter(store, daily_cap=10, global_cap=3)
        assert lim.check_and_reserve("acc-A").ok
        assert lim.check_and_reserve("acc-B").ok
        assert lim.check_and_reserve("acc-C").ok
        d = lim.check_and_reserve("acc-D")
        assert d.ok is False
        assert d.reason == "global_cap_exceeded"


class TestReset:
    def test_reset_clears(self, store):
        lim = AccountLimiter(store, daily_cap=1)
        assert lim.check_and_reserve("acc-A").ok
        assert lim.check_and_reserve("acc-A").ok is False
        lim.reset("acc-A")
        assert lim.remaining_for("acc-A") == 1
        assert lim.check_and_reserve("acc-A").ok


class TestCounts:
    def test_get_counts(self, store):
        lim = AccountLimiter(store, daily_cap=5, global_cap=10)
        lim.check_and_reserve("acc-A")
        lim.check_and_reserve("acc-A")
        lim.check_and_reserve("acc-B")
        c = lim.get_counts("acc-A")
        assert c["account_count"] == 2
        assert c["account_remaining"] == 3
        assert c["global_count"] == 3
        assert c["daily_cap"] == 5


class TestUtcDay:
    def test_day_rollover_resets(self, store):
        """把计数写到昨天的日期，查今天应该归零。"""
        lim = AccountLimiter(store, daily_cap=2)
        yesterday = "2024-01-01"
        store.incr_account_handoff_counter("acc-A", yesterday)
        store.incr_account_handoff_counter("acc-A", yesterday)
        # 今天仍可用
        d = lim.check_and_reserve("acc-A")
        assert d.ok
        assert d.account_count_today == 1
