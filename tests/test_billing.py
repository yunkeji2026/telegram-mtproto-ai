"""C2-1 计费对账测试：账期窗口 / 对账单聚合 / 席位超额 / CSV 导出 / 路由。"""

import time

from src.inbox.models import InboxMessage
from src.inbox.store import InboxStore
from src.utils.billing import (
    DEFAULT_PRICING,
    compute_charges,
    compute_statement,
    month_window,
    parse_period,
    statement_to_csv,
)


def test_month_window_is_half_open():
    since, until = month_window(2026, 3)
    assert since == time.mktime((2026, 3, 1, 0, 0, 0, 0, 0, -1))
    assert until == time.mktime((2026, 4, 1, 0, 0, 0, 0, 0, -1))
    # 12 月跨年
    s2, u2 = month_window(2026, 12)
    assert u2 == time.mktime((2027, 1, 1, 0, 0, 0, 0, 0, -1))


def test_parse_period_valid_and_fallback():
    assert parse_period("2026-05") == (2026, 5)
    lt = time.localtime()
    assert parse_period("garbage") == (lt.tm_year, lt.tm_mon)
    assert parse_period("2026-13") == (lt.tm_year, lt.tm_mon)


def test_compute_statement_aggregates_only_in_period(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    since, until = month_window(2026, 3)
    in_ts = since + 5 * 86400
    out_of_window = until + 86400  # 次月，不应计入
    store.ingest_message(InboxMessage(
        conversation_id="c1", platform_msg_id="m1", direction="in",
        text="hi", ts=in_ts))
    store.ingest_message(InboxMessage(
        conversation_id="c1", platform_msg_id="m2", direction="out",
        text="yo", ts=in_ts + 60))
    store.record_draft_audit("d1", action="autosend", autopilot_level="L2",
                             conversation_id="c1", ts=in_ts + 60)
    store.ingest_message(InboxMessage(
        conversation_id="c2", platform_msg_id="m3", direction="in",
        text="next month", ts=out_of_window))
    stmt = compute_statement(store, 2026, 3,
                             license_status={"plan": "pro", "seats": 5, "state": "active"})
    assert stmt["available"] is True
    assert stmt["period"] == "2026-03"
    assert stmt["usage"]["messages_total"] == 2  # 次月那条被排除
    assert stmt["usage"]["ai_calls"] == 1
    assert stmt["usage"]["ai_sent"] == 1
    assert len(stmt["line_items"]) == 6
    store.close()


def test_reconcile_flags_over_seats(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    since, _ = month_window(2026, 3)
    for i in range(3):
        store.record_draft_audit(f"d{i}", action="approved", agent_id=f"agent{i}",
                                 conversation_id="c1", ts=since + 100)
    stmt = compute_statement(store, 2026, 3,
                             license_status={"plan": "basic", "seats": 2, "state": "active"})
    rec = stmt["reconcile"]
    assert rec["active_agents"] == 3
    assert rec["over_seats"] == 1
    assert rec["within_quota"] is False
    store.close()


def test_reconcile_unlimited_seats(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    since, _ = month_window(2026, 3)
    store.record_draft_audit("d1", action="approved", agent_id="a1",
                             conversation_id="c1", ts=since + 100)
    stmt = compute_statement(store, 2026, 3,
                             license_status={"plan": "flagship", "seats": 0, "state": "active"})
    rec = stmt["reconcile"]
    assert rec["over_seats"] == 0
    assert rec["within_quota"] is True
    store.close()


def test_statement_to_csv_contains_meta_and_items(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    since, _ = month_window(2026, 3)
    store.ingest_message(InboxMessage(
        conversation_id="c1", platform_msg_id="m1", direction="in",
        text="hi", ts=since + 10))
    stmt = compute_statement(store, 2026, 3,
                             license_status={"plan": "pro", "seats": 5,
                                             "state": "active", "customer": "Acme"})
    csv_text = statement_to_csv(stmt)
    assert "billing_statement" in csv_text
    assert "2026-03" in csv_text
    assert "Acme" in csv_text
    assert "messages_total" in csv_text
    store.close()


def test_statement_unavailable_without_store():
    stmt = compute_statement(None, 2026, 3, license_status={"seats": 5})
    assert stmt["available"] is False
    assert stmt["ok"] is True


def test_billing_route_registered():
    import inspect
    from src.web.routes import unified_inbox_usage_routes as m
    src = inspect.getsource(m.register_usage_routes)
    assert "/api/workspace/billing" in src
    assert "csv" in src.lower()


# ── C2-2 计费金额化 ────────────────────────────────────────────

def test_compute_charges_base_fee_only_no_overage():
    stmt = {"plan": "pro", "seats": 5,
            "usage": {"messages_total": 100, "active_agents": 2}}
    ch = compute_charges(stmt, DEFAULT_PRICING)
    assert ch["base_fee"] == 149
    assert ch["message_overage_amount"] == 0
    assert ch["seat_overage_amount"] == 0
    assert ch["total"] == 149
    assert ch["currency"] == "USD"


def test_compute_charges_message_and_seat_overage():
    pricing = {
        "currency": "USD",
        "plans": {"basic": {"monthly": 49, "included_messages": 1000,
                            "included_seats": 2}},
        "overage": {"per_message": 0.01, "per_seat": 25},
    }
    stmt = {"plan": "basic", "seats": 2,
            "usage": {"messages_total": 1500, "active_agents": 4}}
    ch = compute_charges(stmt, pricing)
    assert ch["message_overage_qty"] == 500
    assert ch["message_overage_amount"] == 5.0  # 500 * 0.01
    assert ch["seat_overage_qty"] == 2
    assert ch["seat_overage_amount"] == 50.0  # 2 * 25
    assert ch["total"] == 49 + 5.0 + 50.0


def test_compute_charges_included_zero_means_unlimited():
    pricing = {
        "plans": {"flagship": {"monthly": 499, "included_messages": 0,
                               "included_seats": 0}},
        "overage": {"per_message": 1, "per_seat": 1},
    }
    stmt = {"plan": "flagship", "seats": 0,
            "usage": {"messages_total": 99999, "active_agents": 99}}
    ch = compute_charges(stmt, pricing)
    assert ch["message_overage_qty"] == 0
    assert ch["seat_overage_qty"] == 0
    assert ch["total"] == 499


def test_compute_charges_seat_included_falls_back_to_license_seats():
    pricing = {
        "plans": {"custom": {"monthly": 100, "included_messages": 0,
                             "included_seats": 0}},
        "overage": {"per_message": 0, "per_seat": 10},
    }
    # included_seats=0 → 回退 statement.seats=3
    stmt = {"plan": "custom", "seats": 3,
            "usage": {"messages_total": 0, "active_agents": 5}}
    ch = compute_charges(stmt, pricing)
    assert ch["included_seats"] == 3
    assert ch["seat_overage_qty"] == 2
    assert ch["seat_overage_amount"] == 20.0


def test_statement_includes_charges(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    since, _ = month_window(2026, 3)
    store.ingest_message(InboxMessage(
        conversation_id="c1", platform_msg_id="m1", direction="in",
        text="hi", ts=since + 10))
    stmt = compute_statement(store, 2026, 3,
                             license_status={"plan": "pro", "seats": 5,
                                             "state": "active"})
    assert "charges" in stmt
    assert stmt["charges"]["base_fee"] == 149
    store.close()


def test_csv_contains_charges():
    stmt = {"period": "2026-03", "plan": "pro", "seats": 5,
            "line_items": [{"metric": "messages_total", "label": "消息总量", "qty": 10}],
            "reconcile": {"active_agents": 1, "over_seats": 0, "within_quota": True},
            "charges": {"currency": "USD", "total": 149,
                        "lines": [{"label": "pro 套餐月费", "amount": 149}]}}
    csv_text = statement_to_csv(stmt)
    assert "charges" in csv_text
    assert "total" in csv_text
    assert "149" in csv_text
