"""W4-Cap-Alert：AccountLimiter 阈值跨越 + ContactsSubsystem.wire_cap_alert_webhook。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts import ContactStore, bootstrap_contacts_subsystem
from src.skills.account_limiter import AccountLimiter


CFG_DIR = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture
def store(tmp_path):
    s = ContactStore(db_path=tmp_path / "c.db")
    yield s
    s.close()


# ── 阈值跨越检测（stateless） ──────────────────────────
class TestThresholdCrossings:
    def test_crossing_80_pct_fires_once(self, store):
        fired = []
        lim = AccountLimiter(
            store, daily_cap=10,
            alert_thresholds_pct=[80],
            on_threshold_crossed=lambda acc, pct, n, cap: fired.append(
                (acc, pct, n, cap)),
        )
        for _ in range(10):
            lim.check_and_reserve("acc-A")
        # cap=10, 80%→count=8；应在第 8 次时跨越一次
        assert len(fired) == 1
        acc, pct, count, cap = fired[0]
        assert acc == "acc-A" and pct == 80 and count == 8 and cap == 10

    def test_multiple_thresholds(self, store):
        fired = []
        lim = AccountLimiter(
            store, daily_cap=10,
            alert_thresholds_pct=[50, 80, 100],
            on_threshold_crossed=lambda acc, pct, n, cap: fired.append(pct),
        )
        for _ in range(10):
            lim.check_and_reserve("acc-A")
        assert fired == [50, 80, 100]   # 按升序依次触发

    def test_only_once_per_threshold(self, store):
        """已跨越的阈值不会在后续扣减中重复触发。"""
        fired = []
        lim = AccountLimiter(
            store, daily_cap=5,
            alert_thresholds_pct=[80],
            on_threshold_crossed=lambda *_: fired.append(1),
        )
        for _ in range(5):
            lim.check_and_reserve("acc-A")
        # cap=5, 80%=4 → 在 count=4 时触发一次；count=5 不再触发
        assert fired == [1]

    def test_different_accounts_independent(self, store):
        fired = []
        lim = AccountLimiter(
            store, daily_cap=5,
            alert_thresholds_pct=[80],
            on_threshold_crossed=lambda acc, *_: fired.append(acc),
        )
        for _ in range(4):
            lim.check_and_reserve("acc-A")   # 跨 80%
        for _ in range(4):
            lim.check_and_reserve("acc-B")   # 独立跨 80%
        assert fired == ["acc-A", "acc-B"]

    def test_no_thresholds_means_no_callback(self, store):
        fired = []
        lim = AccountLimiter(
            store, daily_cap=5,
            alert_thresholds_pct=[],    # 空
            on_threshold_crossed=lambda *_: fired.append(1),
        )
        for _ in range(5):
            lim.check_and_reserve("acc-A")
        assert fired == []

    def test_callback_exception_does_not_break_reserve(self, store):
        """callback 抛异常不能让扣减失败。"""
        def boom(*_): raise RuntimeError("oops")
        lim = AccountLimiter(
            store, daily_cap=5,
            alert_thresholds_pct=[80],
            on_threshold_crossed=boom,
        )
        # count 从 0 跑到 5，跨越 80% 触发 boom，但仍要全部扣成功
        for i in range(5):
            r = lim.check_and_reserve("acc-A")
            assert r.ok, f"扣减 #{i+1} 不应因回调失败而失败：{r.reason}"

    def test_late_binding_setter_works(self, store):
        """set_on_threshold_crossed 可以事后接回调。"""
        lim = AccountLimiter(
            store, daily_cap=5, alert_thresholds_pct=[80])
        # 先跑一次，应该无回调
        lim.check_and_reserve("acc-A")
        # 后挂回调
        fired = []
        lim.set_on_threshold_crossed(lambda *a: fired.append(a))
        for _ in range(4):
            lim.check_and_reserve("acc-A")
        # 跨 80% 时已在后段触发
        assert len(fired) == 1

    def test_tiny_cap_collapses_thresholds(self, store):
        """cap=3 时 80% 和 100% 都在 count=3 跨越——不应重复触发同一次。

        当前实现是：对每个 threshold 独立检查 old_pct<t<=new_pct，
        所以 count=3 时 old_pct=67, new_pct=100 → 两个阈值都会触发。
        这对运营意义不大但语义正确（单次扣减同时跨过两条线）。
        """
        fired = []
        lim = AccountLimiter(
            store, daily_cap=3,
            alert_thresholds_pct=[80, 100],
            on_threshold_crossed=lambda acc, pct, *_: fired.append(pct),
        )
        for _ in range(3):
            lim.check_and_reserve("acc-A")
        assert fired == [80, 100]  # 同一次扣减触发两个阈值


# ── bootstrap 配置读取 ────────────────────────────────
class TestBootstrapReadsCapAlertConfig:
    def test_disabled_by_default(self, tmp_path):
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
                "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
            }}, CFG_DIR)
        try:
            assert sub.limiter._thresholds == []
        finally:
            sub.close()

    def test_enabled_loads_thresholds(self, tmp_path):
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
                "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
                "cap_alert": {"enabled": True, "thresholds_pct": [50, 90]},
            }}, CFG_DIR)
        try:
            assert sub.limiter._thresholds == [50, 90]
        finally:
            sub.close()

    def test_enabled_false_ignores_thresholds(self, tmp_path):
        """enabled=false 时即使配了 thresholds 也空跑。"""
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
                "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
                "cap_alert": {"enabled": False, "thresholds_pct": [80]},
            }}, CFG_DIR)
        try:
            assert sub.limiter._thresholds == []
        finally:
            sub.close()


# ── wire_cap_alert_webhook ────────────────────────────
class TestWireCapAlertWebhook:
    def _fake_notifier(self, calls, enabled=True):
        class _N:
            @property
            def enabled(self): return enabled
            def notify(self, event, data): calls.append((event, data))
        return _N()

    def test_wire_returns_false_when_thresholds_empty(self, tmp_path):
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
                "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
                # 无 cap_alert
            }}, CFG_DIR)
        try:
            assert sub.wire_cap_alert_webhook(
                self._fake_notifier([])) is False
        finally:
            sub.close()

    def test_wire_returns_false_when_notifier_disabled(self, tmp_path):
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
                "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
                "cap_alert": {"enabled": True, "thresholds_pct": [80]},
            }}, CFG_DIR)
        try:
            assert sub.wire_cap_alert_webhook(
                self._fake_notifier([], enabled=False)) is False
        finally:
            sub.close()

    def test_wire_success_and_alert_fires_to_webhook(self, tmp_path):
        calls = []
        sub = bootstrap_contacts_subsystem(
            {"contacts": {
                "enabled": True, "db_path": str(tmp_path / "c.db"),
                "daily_cap": 5,
                "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
                "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
                "cap_alert": {"enabled": True, "thresholds_pct": [80]},
            }}, CFG_DIR)
        try:
            assert sub.wire_cap_alert_webhook(
                self._fake_notifier(calls)) is True
            # 触发：扣到 count=4 跨 80%
            for _ in range(4):
                sub.limiter.check_and_reserve("acc-A")
            assert len(calls) == 1
            event, data = calls[0]
            assert event == "contacts.cap_alert"
            assert data["account_id"] == "acc-A"
            assert data["pct"] == 80
            assert data["count"] == 4
            assert data["cap"] == 5
        finally:
            sub.close()
