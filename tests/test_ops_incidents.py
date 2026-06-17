"""E2/E3 运维事件闭环测试：incidents 存储 + watchdog 落表/恢复 + 计费告警。"""

import types

from src.inbox.health_watchdog import HealthWatchdog
from src.inbox.store import InboxStore


# ── E2：store incidents ──────────────────────────────────────────────────

def test_open_incident_creates_and_dedups_by_signature(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    iid1 = store.open_or_update_incident(
        signature="db:fail", light="red",
        summary={"fail": 1}, problems=[{"id": "db", "detail": "down"}],
    )
    # 同签名未关闭 → 更新同一条，不新建
    iid2 = store.open_or_update_incident(
        signature="db:fail", light="red",
        summary={"fail": 1}, problems=[{"id": "db", "detail": "still down"}],
    )
    assert iid1 == iid2
    items = store.list_incidents()
    assert len(items) == 1
    assert items[0]["problems"][0]["detail"] == "still down"
    assert store.count_open_incidents() == 1


def test_resolve_open_incidents(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.open_or_update_incident(signature="ai:fail", light="red")
    store.open_or_update_incident(signature="queue:warn", light="yellow")
    assert store.count_open_incidents() == 2
    n = store.resolve_open_incidents()
    assert n == 2
    assert store.count_open_incidents() == 0
    assert all(i["status"] == "resolved" for i in store.list_incidents())


def test_ack_incident_sets_status_and_assignee(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    iid = store.open_or_update_incident(signature="db:fail", light="red")
    assert store.ack_incident(iid, assigned_to="alice") is True
    item = store.list_incidents(status="acked")[0]
    assert item["assigned_to"] == "alice"
    assert item["status"] == "acked"
    # 仍计入未关闭
    assert store.count_open_incidents() == 1


def test_new_signature_after_resolve_creates_new_incident(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.open_or_update_incident(signature="db:fail", light="red")
    store.resolve_open_incidents()
    # 同签名但前一条已 resolved → 应新建
    store.open_or_update_incident(signature="db:fail", light="red")
    assert len(store.list_incidents()) == 2
    assert store.count_open_incidents() == 1


# ── E2：watchdog 落表/恢复 ────────────────────────────────────────────────

def _cm(config):
    return types.SimpleNamespace(config=config)


def _recording_inbox():
    calls = {"opened": [], "resolved": 0}
    inbox = types.SimpleNamespace(
        ping=lambda: True,
        open_or_update_incident=lambda **kw: calls["opened"].append(kw) or 1,
        resolve_open_incidents=lambda **kw: calls.__setitem__("resolved", calls["resolved"] + 1),
    )
    return inbox, calls


def test_watchdog_records_incident_on_red(monkeypatch):
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus",
                        lambda: types.SimpleNamespace(publish=lambda t, d: None))

    inbox, calls = _recording_inbox()
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    wd = HealthWatchdog(app=app, config_manager=_cm({"ai": {"provider": ""}}), interval_sec=60)
    wd._tick()  # AI 缺 → red
    assert len(calls["opened"]) == 1
    assert calls["opened"][0]["light"] == "red"


def test_watchdog_resolves_incidents_on_recovery(monkeypatch):
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus",
                        lambda: types.SimpleNamespace(publish=lambda t, d: None))

    inbox, calls = _recording_inbox()
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    cm = _cm({"ai": {"provider": ""}})
    wd = HealthWatchdog(app=app, config_manager=cm, interval_sec=60)
    wd._tick()  # red → open
    cm.config = {"ai": {"provider": "openai", "api_key": "sk-real-123"}}
    wd._tick()  # 恢复 → resolve
    assert calls["resolved"] == 1


# ── E3：watchdog 计费异常告警 ────────────────────────────────────────────

def test_watchdog_emits_billing_alert_on_over_seats(monkeypatch):
    published = []
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus",
                        lambda: types.SimpleNamespace(publish=lambda t, d: published.append((t, d))))

    def _usage(*a, **k):
        return {"messages_total": 100, "messages_in": 50, "messages_out": 50,
                "ai_calls": 10, "ai_sent": 8, "active_agents": 8}

    inbox = types.SimpleNamespace(ping=lambda: True, get_usage_stats=_usage)
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    # AI 配好 + DB ok → 健康不是 red（无渠道为 yellow，alert_on_warn=False 不发健康告警）
    cm = _cm({"ai": {"provider": "openai", "api_key": "sk-real-123"}})
    wd = HealthWatchdog(app=app, config_manager=cm, interval_sec=60)
    # 授权 2 席位、活跃 8 人 → 超席位
    wd._license_quota = lambda: {"plan": "pro", "state": "active",
                                 "customer": "acme", "seats": 2, "channels": []}
    wd._tick()
    billing = [d for (t, d) in published if t == "billing_alert"]
    assert billing, "超席位应发出 billing_alert"
    assert billing[0]["recovered"] is False
    assert any(a["code"] == "over_seats" for a in billing[0]["anomalies"])


def test_billing_check_throttled(monkeypatch):
    published = []
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus",
                        lambda: types.SimpleNamespace(publish=lambda t, d: published.append((t, d))))

    def _usage(*a, **k):
        return {"messages_total": 100, "messages_in": 50, "messages_out": 50,
                "ai_calls": 10, "ai_sent": 8, "active_agents": 8}

    inbox = types.SimpleNamespace(ping=lambda: True, get_usage_stats=_usage)
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    cm = _cm({"ai": {"provider": "openai", "api_key": "sk-real-123"}})
    wd = HealthWatchdog(app=app, config_manager=cm, interval_sec=60, billing_interval_sec=3600)
    wd._license_quota = lambda: {"plan": "pro", "state": "active",
                                 "customer": "acme", "seats": 2, "channels": []}
    wd._check_billing(now=1000.0)
    wd._check_billing(now=1000.0 + 60)  # 距上次不足 1h → 跳过
    billing = [d for (t, d) in published if t == "billing_alert"]
    assert len(billing) == 1, "节流期内不应重复巡检/发告警"


def test_suggest_assignee_picks_least_loaded():
    from src.web.routes.ops_overview_routes import _suggest_assignee

    inbox = types.SimpleNamespace(
        list_agent_presence=lambda **k: [
            {"agent_id": "a1", "status": "online", "display_name": "A"},
            {"agent_id": "a2", "status": "online", "display_name": "B"},
        ],
        list_conversation_claims=lambda: [{"agent_id": "a1"}],  # a1 已有 1 个负载
    )
    request = types.SimpleNamespace(app=types.SimpleNamespace(
        state=types.SimpleNamespace(inbox_store=inbox)))
    cm = _cm({"workspace": {"auto_assign": {"enabled": True, "online_only": True}}})
    sug = _suggest_assignee(request, cm)
    assert sug is not None
    assert sug["agent_id"] == "a2"  # 更闲


def test_incident_dedup_is_per_kind(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    h = store.open_or_update_incident(kind="health", signature="x", light="red")
    b = store.open_or_update_incident(kind="billing", signature="x", light="red")
    assert h != b  # 同 signature 不同 kind → 两条独立事件
    assert len(store.list_incidents()) == 2
    assert len(store.list_incidents(kind="billing")) == 1


def test_billing_incident_isolated_from_health(tmp_path, monkeypatch):
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus",
                        lambda: types.SimpleNamespace(publish=lambda t, d: None))

    store = InboxStore(tmp_path / "inbox.db")
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=store))
    wd = HealthWatchdog(app=app, config_manager=_cm({}), interval_sec=60)

    # 先有一条健康事件 open
    store.open_or_update_incident(kind="health", signature="db:fail", light="red")
    # 计费异常 → 开 billing 事件
    wd._emit_billing_alert([{"code": "over_seats", "severity": "fail", "message": "超席位 3"}])
    assert len(store.list_incidents(kind="billing")) == 1
    assert store.count_open_incidents() == 2

    # 计费恢复 → 只 resolve billing，健康事件不动
    wd._emit_billing_recovery()
    assert store.list_incidents(kind="billing")[0]["status"] == "resolved"
    assert store.count_open_incidents() == 1
    assert store.list_incidents(kind="health")[0]["status"] == "open"


def test_health_recovery_does_not_resolve_billing(tmp_path, monkeypatch):
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus",
                        lambda: types.SimpleNamespace(publish=lambda t, d: None))

    store = InboxStore(tmp_path / "inbox.db")
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=store))
    wd = HealthWatchdog(app=app, config_manager=_cm({}), interval_sec=60)
    store.open_or_update_incident(kind="billing", signature="over_seats", light="red")
    # 健康恢复仅 resolve health 类
    wd._emit_recovery({"light": "green"})
    assert store.list_incidents(kind="billing")[0]["status"] == "open"


def test_purge_resolved_incidents_respects_retention(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # 旧已关闭
    store.open_or_update_incident(kind="health", signature="a", light="red", ts=1000)
    store.resolve_open_incidents(ts=1000)
    # 新已关闭
    store.open_or_update_incident(kind="health", signature="b", light="red", ts=2_000_000)
    store.resolve_open_incidents(ts=2_000_000)
    # 仍 open
    store.open_or_update_incident(kind="health", signature="c", light="red")

    n = store.purge_resolved_incidents(1500)  # 阈值落在 a、b 之间
    assert n == 1  # 只删旧的 a
    remaining = {i["signature"] for i in store.list_incidents()}
    assert remaining == {"b", "c"}


def test_watchdog_purge_throttled(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.open_or_update_incident(kind="health", signature="a", light="red", ts=1000)
    store.resolve_open_incidents(ts=1000)
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=store))
    wd = HealthWatchdog(app=app, config_manager=_cm({}), interval_sec=60,
                        incident_retention_days=30)
    now1 = 1000 + 40 * 86400  # 40 天后 → 超 30 天保留期
    assert wd._maybe_purge_incidents(now=now1) == 1
    # 节流：1 天内不再清理
    assert wd._maybe_purge_incidents(now=now1 + 60) == 0


def test_watchdog_purge_disabled_when_retention_nonpositive(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.open_or_update_incident(kind="health", signature="a", light="red", ts=1000)
    store.resolve_open_incidents(ts=1000)
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=store))
    wd = HealthWatchdog(app=app, config_manager=_cm({}), interval_sec=60,
                        incident_retention_days=0)
    assert wd._maybe_purge_incidents(now=1000 + 40 * 86400) == 0
    assert len(store.list_incidents()) == 1  # 未删


def test_get_incident_stats(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # 2 健康（1 已解决，解决耗时 3600s）+ 1 计费 open
    store.open_or_update_incident(kind="health", signature="a", light="red", ts=1000)
    store.resolve_open_incidents(kind="health", ts=4600)  # a: 3600s
    store.open_or_update_incident(kind="health", signature="b", light="red", ts=5000)
    store.open_or_update_incident(kind="billing", signature="over_seats", light="red", ts=5000)

    stats = store.get_incident_stats(since_ts=0)
    assert stats["total"] == 3
    assert stats["resolved"] == 1
    assert stats["open"] == 2
    assert stats["by_kind"]["health"] == 2
    assert stats["by_kind"]["billing"] == 1
    assert stats["mttr_sec"] == 3600.0


def test_get_incident_stats_respects_since(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.open_or_update_incident(kind="health", signature="old", light="red", ts=1000)
    store.open_or_update_incident(kind="health", signature="new", light="red", ts=9_000_000)
    stats = store.get_incident_stats(since_ts=5_000_000)
    assert stats["total"] == 1  # 只算窗口内


def test_list_incidents_cursor_pagination(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    ids = [store.open_or_update_incident(kind="health", signature="s%d" % i, light="red")
           for i in range(5)]
    page1 = store.list_incidents(limit=2)
    assert [i["id"] for i in page1] == [ids[4], ids[3]]
    page2 = store.list_incidents(limit=2, before_id=page1[-1]["id"])
    assert [i["id"] for i in page2] == [ids[2], ids[1]]
    page3 = store.list_incidents(limit=2, before_id=page2[-1]["id"])
    assert [i["id"] for i in page3] == [ids[0]]


def test_ops_overview_and_incidents_e2e(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    from src.web.routes.ops_overview_routes import register_ops_overview_routes

    store = InboxStore(tmp_path / "inbox.db")
    store.open_or_update_incident(
        kind="health", signature="db:fail", light="red",
        problems=[{"id": "db", "name": "DB", "detail": "down"}])

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.inbox_store = store
    ctx = types.SimpleNamespace(
        api_auth=lambda request: None,
        api_write=lambda perm: (lambda: None),
        page_auth=lambda: None,
        templates=None,
        config_manager=types.SimpleNamespace(config={}),
        audit_store=None,
        user_store=None,
        token="",
    )
    register_ops_overview_routes(app, ctx)
    client = TestClient(app)

    ov = client.get("/api/admin/ops-overview").json()
    assert ov["ok"] is True
    assert "kpis" in ov and "sections" in ov
    assert ov["kpis"]["open_incidents"] == 1
    assert "anomalies" in ov  # G2 趋势异动段

    inc = client.get("/api/admin/incidents?limit=10").json()
    assert inc["ok"] is True
    assert inc["open"] == 1
    assert inc["incidents"][0]["kind"] == "health"
    assert inc["next_cursor"] is None  # 仅一条，不足一页
    assert "advice" in inc["incidents"][0]  # G1 根因建议

    rep = client.get("/api/admin/ops-report?days=7").json()
    assert rep["ok"] is True
    assert rep["incidents"]["total"] == 1
    assert "headline" in rep


def test_ack_incident_writes_audit():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    from src.web.routes.ops_overview_routes import register_ops_overview_routes

    audited = []
    audit = types.SimpleNamespace(log=lambda *a, **k: audited.append(a))
    inbox = types.SimpleNamespace(ack_incident=lambda iid, assigned_to="": True)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.inbox_store = inbox
    ctx = types.SimpleNamespace(
        api_auth=lambda request: None,
        api_write=lambda perm: (lambda: None),
        page_auth=lambda: None,
        templates=None,
        config_manager=types.SimpleNamespace(config={}),
        audit_store=audit,
        user_store=None,
        token="",
    )
    register_ops_overview_routes(app, ctx)

    client = TestClient(app)
    r = client.post("/api/admin/incidents/7/ack", json={"assigned_to": "alice"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert audited, "ack 应写一条审计"
    args = audited[0]
    assert args[1] == "ack_incident"
    assert "incident:7" in args[2]
    assert args[4] == "alice"


def test_billing_alert_event_alias_and_message():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert "billing_alert" in _EVENT_ALIASES
    title, text = _build_message("billing_alert", {
        "anomalies": [{"severity": "fail", "message": "活跃坐席超授权席位 3 个"}]})
    assert "计费异常" in title
    assert "超授权席位" in text
    t2, _ = _build_message("billing_alert", {"recovered": True})
    assert "解除" in t2


# ── H1：运营周报自动外发 ───────────────────────────────────────────────────

def test_weekly_report_disabled_by_default(tmp_path, monkeypatch):
    published = []
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus",
                        lambda: types.SimpleNamespace(publish=lambda t, d: published.append((t, d))))
    store = InboxStore(tmp_path / "inbox.db")
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=store))
    wd = HealthWatchdog(app=app, config_manager=_cm({}), interval_sec=60)
    assert wd._maybe_weekly_report(now=wd._last_weekly_ts + 1_000_000) is None
    assert not [t for (t, d) in published if t == "ops_report"]


def test_weekly_report_emits_and_throttles(tmp_path, monkeypatch):
    published = []
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus",
                        lambda: types.SimpleNamespace(publish=lambda t, d: published.append((t, d))))
    store = InboxStore(tmp_path / "inbox.db")
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=store))
    wd = HealthWatchdog(app=app, config_manager=_cm({}), interval_sec=60,
                        weekly_report_enabled=True, weekly_interval_sec=604800)
    # 周报窗口相对 now（近 7 天），事件 ts 须落在 [now-604800, now] 内 → 以锚点定位。
    anchor = wd._last_weekly_ts
    store.open_or_update_incident(kind="health", signature="db:fail", light="red",
                                  ts=anchor + 600000)
    store.resolve_open_incidents(kind="health", ts=anchor + 603600)  # 解决耗时 3600s
    t0 = anchor + 700000  # 超一周触发
    rep = wd._maybe_weekly_report(now=t0)
    assert rep is not None
    assert rep["incidents"]["total"] == 1
    ops = [d for (t, d) in published if t == "ops_report"]
    assert len(ops) == 1
    assert wd.total_weekly_reports == 1
    # 节流：距上次不足一周 → 不再发
    assert wd._maybe_weekly_report(now=t0 + 60) is None
    assert len([d for (t, d) in published if t == "ops_report"]) == 1


def test_ops_report_event_alias_and_message():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert "ops_report" in _EVENT_ALIASES
    title, text = _build_message("ops_report", {
        "days": 7,
        "headline": ["近 7 天运维事件 3 起（已解决 2）"],
        "compare": {"incidents_delta": 1, "ai_share_delta_pp": 5.0},
    })
    assert "运营周报" in title
    assert "运维事件" in text
    assert "环比上周" in text
    assert "/admin/ops" in text
