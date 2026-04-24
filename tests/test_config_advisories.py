"""Smoke tests for startup production advisories."""

from __future__ import annotations

from src.utils.config_advisories import (
    AdvisoryEvent,
    collect_production_advisories,
    record_warning_advisories_to_audit,
)


def test_collect_web_bind_all_interfaces_warning():
    ev = collect_production_advisories({"web_admin": {"host": "0.0.0.0"}})
    codes = {e.code for e in ev}
    assert "web_bind_all_interfaces" in codes
    w = next(x for x in ev if x.code == "web_bind_all_interfaces")
    assert w.level == "warning"


def test_collect_memory_backfill_both_info():
    cfg = {
        "memory": {
            "vector": {
                "backfill_on_startup": {"enabled": True},
                "backfill_periodic": {"enabled": True},
            }
        }
    }
    ev = collect_production_advisories(cfg)
    assert any(e.code == "memory_backfill_both_enabled" for e in ev)


def test_record_warning_advisories_to_audit_skips_non_warning():
    calls: list[tuple[str, str, str, str]] = []

    class FakeAudit:
        def log(self, user_id, action, target, old_val="", new_val="", snapshot_id=""):
            calls.append((user_id, action, target, new_val))

    events = [
        AdvisoryEvent("warning", "w1", "msg1"),
        AdvisoryEvent("info", "i1", "msg2"),
        AdvisoryEvent("debug", "d1", "msg3"),
    ]
    n = record_warning_advisories_to_audit(FakeAudit(), events)
    assert n == 1
    assert len(calls) == 1
    assert calls[0] == ("system", "config_advisory", "w1", "msg1")


def test_record_warning_advisories_to_audit_empty():
    assert record_warning_advisories_to_audit(None, []) == 0


def test_metrics_store_startup_advisory_snapshot():
    from src.monitoring.metrics_store import get_metrics_store

    ms = get_metrics_store()
    ms.set_startup_advisory_counts(4, 2)
    ms.set_startup_advisory_audit_logged(1)
    snap = ms.snapshot()
    sa = snap.get("startup_advisories") or {}
    assert sa.get("total") == 4
    assert sa.get("warnings") == 2
    assert sa.get("audit_logged_warnings") == 1


def test_audit_store_receives_config_advisory(tmp_path):
    from src.utils.audit_store import AuditStore

    audit = AuditStore(db_path=tmp_path / "audit_test.db")
    ev = collect_production_advisories({"web_admin": {"host": "0.0.0.0"}})
    n = record_warning_advisories_to_audit(audit, ev)
    assert n >= 1
    rows = audit.query(limit=20, action="config_advisory")
    assert any(r.get("target") == "web_bind_all_interfaces" for r in rows)
