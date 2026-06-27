"""D3 健康看门狗测试：去抖签名 / 告警触发 / 恢复通知 / EventBus 发布 / 配置阈值。"""

import types

from src.inbox.health_watchdog import (
    HealthWatchdog,
    health_signature,
    problems_of,
)


def _fake_app(*, db_ok=True, ai_provider="openai", workers=None, pending=10):
    """构造一个最小 app（含 .state）模拟运行时信号。"""
    state = types.SimpleNamespace()
    # inbox_store with ping + （channel_status 读 config，不依赖 store）
    state.inbox_store = types.SimpleNamespace(ping=lambda: db_ok)
    # workers
    if workers is not None:
        for attr, snap in workers.items():
            setattr(state, attr, types.SimpleNamespace(status_snapshot=lambda s=snap: s))
    # draft_service
    state.draft_service = types.SimpleNamespace(
        list_drafts=lambda status="pending", limit=1000: [{} for _ in range(pending)])
    app = types.SimpleNamespace(state=state)
    return app


class _CM:
    def __init__(self, config):
        self.config = config


def test_signature_changes_with_problems():
    h1 = {"components": [{"id": "db", "status": "ok"}]}
    h2 = {"components": [{"id": "db", "status": "fail"}]}
    assert health_signature(h1) == ""
    assert health_signature(h2) == "db:fail"
    assert health_signature(h1) != health_signature(h2)


def test_problems_of_filters_only_bad():
    h = {"components": [
        {"id": "db", "status": "ok", "name": "DB", "detail": "ok"},
        {"id": "ai", "status": "fail", "name": "AI", "detail": "no key"},
        {"id": "queue", "status": "warn", "name": "Q", "detail": "backlog"},
    ]}
    probs = problems_of(h)
    ids = {p["id"] for p in probs}
    assert ids == {"ai", "queue"}


def test_tick_emits_alert_on_red(monkeypatch):
    published = []
    from src.integrations.shared import event_bus as eb

    class _Bus:
        def publish(self, t, d): published.append((t, d))
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())

    # AI 未配置 → red
    cm = _CM({"ai": {"provider": ""}})
    app = _fake_app(db_ok=True)
    wd = HealthWatchdog(app=app, config_manager=cm, interval_sec=60)
    wd._tick()
    assert published, "red 状态应发出 health_alert"
    typ, data = published[0]
    assert typ == "health_alert"
    assert data["recovered"] is False
    assert any(p["id"] == "ai" for p in data["problems"])


def test_tick_dedup_same_signature(monkeypatch):
    published = []
    from src.integrations.shared import event_bus as eb

    class _Bus:
        def publish(self, t, d): published.append((t, d))
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())

    cm = _CM({"ai": {"provider": ""}})
    app = _fake_app()
    wd = HealthWatchdog(app=app, config_manager=cm, interval_sec=60)
    wd._tick()
    wd._tick()  # 同样的 red，签名不变 → 不重复发
    assert len(published) == 1


def test_tick_emits_recovery(monkeypatch):
    published = []
    from src.integrations.shared import event_bus as eb

    class _Bus:
        def publish(self, t, d): published.append((t, d))
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())

    # 先 red（AI 缺）
    cm = _CM({"ai": {"provider": ""}})
    app = _fake_app()
    wd = HealthWatchdog(app=app, config_manager=cm, interval_sec=60)
    wd._tick()
    assert len(published) == 1
    # 修好 AI → 绿 → 应补发恢复
    cm.config = {"ai": {"provider": "openai", "api_key": "sk-real-123"}}
    wd._tick()
    assert len(published) == 2
    assert published[1][1]["recovered"] is True


def test_warn_does_not_alert_by_default(monkeypatch):
    published = []
    from src.integrations.shared import event_bus as eb

    class _Bus:
        def publish(self, t, d): published.append((t, d))
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())

    # DB ok + AI ok + 无渠道(warn) → yellow，不应默认告警
    cm = _CM({"ai": {"provider": "openai", "api_key": "sk-real-123"}})
    app = _fake_app()
    wd = HealthWatchdog(app=app, config_manager=cm, interval_sec=60, alert_on_warn=False)
    wd._tick()
    assert published == []
    assert wd.last_light == "yellow"


def test_warn_alerts_when_enabled(monkeypatch):
    published = []
    from src.integrations.shared import event_bus as eb

    class _Bus:
        def publish(self, t, d): published.append((t, d))
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())

    cm = _CM({"ai": {"provider": "openai", "api_key": "sk-real-123"}})
    app = _fake_app()
    wd = HealthWatchdog(app=app, config_manager=cm, interval_sec=60, alert_on_warn=True)
    wd._tick()
    assert len(published) == 1
    assert published[0][1]["light"] == "yellow"


def test_status_snapshot_shape():
    cm = _CM({"ai": {"provider": "openai", "api_key": "sk-real-123"}})
    app = _fake_app()
    wd = HealthWatchdog(app=app, config_manager=cm, interval_sec=120)
    snap = wd.status_snapshot()
    assert snap["interval_sec"] == 120
    assert "total_alerts" in snap
    assert "last_light" in snap


def test_health_alert_event_alias_and_message():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert "health_alert" in _EVENT_ALIASES
    title, text = _build_message("health_alert", {
        "light": "red", "problems": [{"name": "AI 大模型", "detail": "api_key 为空"}]})
    assert "健康告警" in title
    assert "AI 大模型" in text
    # 恢复消息
    t2, x2 = _build_message("health_alert", {"recovered": True})
    assert "恢复" in t2


def test_billing_reconciles_stale_incident_on_startup(monkeypatch):
    """修复某计费异常后重启：进程内 _last_billing_sig 为空，但 DB 里仍挂着上一进程
    的 open 计费事件。首次巡检发现无异常时应静默 resolve 掉它（不外发恢复通知）。"""
    published = []
    from src.integrations.shared import event_bus as eb

    class _Bus:
        def publish(self, t, d): published.append((t, d))
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())

    class _FakeInbox:
        def __init__(self):
            self.resolved_kinds = []

        def ping(self):
            return True

        def get_usage_stats(self, since, until_ts=None):
            # 1 个活跃坐席、seats=0（community）→ over_seats=0 → 无计费异常
            return {"messages_in": 1, "messages_out": 1, "messages_total": 2,
                    "ai_calls": 1, "ai_sent": 0, "active_agents": 1,
                    "active_agent_ids": ["alice"], "trend": []}

        def resolve_open_incidents(self, *, kind="", ts=None):
            self.resolved_kinds.append(kind)
            return 1  # 模拟确有一条遗留 open 事件被关闭

    inbox = _FakeInbox()
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    cm = _CM({"ai": {"provider": "openai", "api_key": "sk-real-123"}})
    wd = HealthWatchdog(app=app, config_manager=cm, interval_sec=60)

    wd._check_billing()

    # 静默 reconcile：resolve 被调用，但没有外发 billing_alert 恢复事件
    assert inbox.resolved_kinds == ["billing"]
    assert all(t != "billing_alert" for t, _ in published)
    assert wd._last_billing_sig is None


def test_collect_health_reused_by_route():
    import inspect
    from src.web.routes import runtime_health_routes
    src = inspect.getsource(runtime_health_routes)
    assert "collect_health" in src
