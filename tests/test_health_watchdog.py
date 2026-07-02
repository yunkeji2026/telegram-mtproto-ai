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


# ── 草稿质量告警闭环（记忆命中率 / p95 / 风险分类回检）──────────────────────

def _reset_draft_metrics():
    from src.monitoring import metrics_store as _ms
    _ms.MetricsStore._instance = None
    return _ms.get_metrics_store()


def _qa_cm(**over):
    qa = {"enabled": True, "min_samples": 10, "memory_hit_min": 0.30,
          "p95_ms_max": 8000, "fast_path_ratio_max": 0.98}
    qa.update(over)
    return _CM({"ai": {"provider": "openai", "api_key": "sk-real-123"},
                "inbox": {"auto_draft": {"quality_alert": qa}}})


def _bus(monkeypatch, sink):
    from src.integrations.shared import event_bus as eb

    class _Bus:
        def publish(self, t, d): sink.append((t, d))
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())


def test_draft_quality_alert_on_low_memory_hit(monkeypatch):
    published = []
    _bus(monkeypatch, published)
    m = _reset_draft_metrics()
    for _ in range(30):
        m.record_inbox_draft_event("generated")
    for _ in range(3):  # 命中率 3/30 = 10% < 30%
        m.record_inbox_draft_event("memory_hit")

    app = _fake_app()
    wd = HealthWatchdog(app=app, config_manager=_qa_cm(), interval_sec=60)
    wd._check_draft_quality()

    assert published, "记忆命中率过低应发告警"
    typ, data = published[0]
    assert typ == "draft_quality_alert"
    assert data["recovered"] is False
    assert any(p["id"] == "memory_hit_low" for p in data["problems"])
    assert wd.total_draft_quality_alerts == 1


def test_draft_quality_dedup_then_recover(monkeypatch):
    published = []
    _bus(monkeypatch, published)
    m = _reset_draft_metrics()
    for _ in range(30):
        m.record_inbox_draft_event("generated")
    for _ in range(3):
        m.record_inbox_draft_event("memory_hit")

    app = _fake_app()
    wd = HealthWatchdog(app=app, config_manager=_qa_cm(), interval_sec=60)
    wd._check_draft_quality()
    wd._check_draft_quality()  # 签名不变 → 不重复发
    assert len(published) == 1

    # 拉高命中率：再来一批高命中样本 → 窗口率回到阈值上 → 恢复
    for _ in range(30):
        m.record_inbox_draft_event("generated")
        m.record_inbox_draft_event("memory_hit")
    wd._check_draft_quality()
    assert len(published) == 2
    assert published[1][1]["recovered"] is True


def test_draft_quality_silent_when_low_samples(monkeypatch):
    published = []
    _bus(monkeypatch, published)
    m = _reset_draft_metrics()
    for _ in range(5):  # < min_samples=10
        m.record_inbox_draft_event("generated")

    app = _fake_app()
    wd = HealthWatchdog(app=app, config_manager=_qa_cm(), interval_sec=60)
    wd._check_draft_quality()
    assert published == []


def test_draft_quality_risk_classify_loose(monkeypatch):
    published = []
    _bus(monkeypatch, published)
    m = _reset_draft_metrics()
    for _ in range(30):  # 全是低风险快路 + 高记忆命中（隔离 fast_path 信号）
        m.record_inbox_draft_event("generated")
        m.record_inbox_draft_event("fast_path")
        m.record_inbox_draft_event("memory_hit")

    app = _fake_app()
    wd = HealthWatchdog(app=app, config_manager=_qa_cm(), interval_sec=60)
    wd._check_draft_quality()
    assert published
    data = published[0][1]
    ids = {p["id"] for p in data["problems"]}
    assert "risk_classify_loose" in ids
    assert "memory_hit_low" not in ids  # 高命中率不应误报


def test_draft_quality_disabled_no_alert(monkeypatch):
    published = []
    _bus(monkeypatch, published)
    m = _reset_draft_metrics()
    for _ in range(30):
        m.record_inbox_draft_event("generated")

    app = _fake_app()
    wd = HealthWatchdog(app=app, config_manager=_qa_cm(enabled=False), interval_sec=60)
    wd._check_draft_quality()
    assert published == []


def test_draft_quality_severity_grading(monkeypatch):
    """严重失忆(<15%) → red+fail；轻微(<30%) → yellow+warn。"""
    # 严重：3/30 = 10% < 15%
    pub1 = []
    _bus(monkeypatch, pub1)
    m = _reset_draft_metrics()
    for _ in range(30):
        m.record_inbox_draft_event("generated")
    for _ in range(3):
        m.record_inbox_draft_event("memory_hit")
    wd = HealthWatchdog(app=_fake_app(), config_manager=_qa_cm(), interval_sec=60)
    wd._check_draft_quality()
    data = pub1[0][1]
    assert data["light"] == "red"
    assert any(p["id"] == "memory_hit_low" and p["status"] == "fail"
               for p in data["problems"])

    # 轻微：6/30 = 20%（15%~30%）→ yellow+warn
    pub2 = []
    _bus(monkeypatch, pub2)
    m2 = _reset_draft_metrics()
    for _ in range(30):
        m2.record_inbox_draft_event("generated")
    for _ in range(6):
        m2.record_inbox_draft_event("memory_hit")
    wd2 = HealthWatchdog(app=_fake_app(), config_manager=_qa_cm(), interval_sec=60)
    wd2._check_draft_quality()
    data2 = pub2[0][1]
    assert data2["light"] == "yellow"
    assert any(p["id"] == "memory_hit_low" and p["status"] == "warn"
               for p in data2["problems"])


def test_draft_quality_event_alias_and_message():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert "draft_quality" in _EVENT_ALIASES
    title, text = _build_message("draft_quality_alert", {
        "light": "red",
        "problems": [{"name": "草稿记忆命中率", "detail": "近窗口记忆命中率 10% < 阈值 30%"}]})
    assert "草稿质量告警" in title
    assert "记忆命中率" in text
    t2, x2 = _build_message("draft_quality_alert", {"recovered": True})
    assert "恢复" in t2


# ── 记忆 key 漂移告警闭环（裸 key 复发自我守护）────────────────────────────

def _kd_cm(**over):
    kd = {"enabled": True, "bare_keys_max": 0, "bare_keys_severe": 50,
          "interval_sec": 60}
    kd.update(over)
    return _CM({"ai": {"provider": "openai", "api_key": "sk-real-123"},
                "inbox": {"auto_draft": {"key_drift_alert": kd}}})


def _kh(bare, *, facts=None):
    """构造 episodic_key_health 返回值（bare 个裸 key）。"""
    return {
        "enabled": True, "total_keys": bare + 5, "canonical_keys": 5,
        "bare_keys": bare, "bare_facts": facts if facts is not None else bare * 2,
        "bare_ratio": 0.0,
        "bare_samples": [{"key": str(i), "facts": 1} for i in range(min(bare, 5))],
    }


def _app_with_sm(health):
    """_fake_app 叠加 state.skill_manager.episodic_key_health（可经 holder 改值）。"""
    app = _fake_app()
    holder = {"h": health}
    app.state.skill_manager = types.SimpleNamespace(
        episodic_key_health=lambda sample=5: holder["h"])
    return app, holder


def test_memory_key_drift_alert_yellow(monkeypatch):
    pub = []
    _bus(monkeypatch, pub)
    app, _ = _app_with_sm(_kh(3))
    wd = HealthWatchdog(app=app, config_manager=_kd_cm(), interval_sec=60)
    wd._check_memory_key_drift(now=1000)
    assert pub and pub[0][0] == "memory_key_drift_alert"
    d = pub[0][1]
    assert d["recovered"] is False and d["light"] == "yellow"
    assert any(p["id"] == "memory_key_drift" and p["status"] == "warn"
               for p in d["problems"])
    assert wd.total_memory_key_drift_alerts == 1


def test_memory_key_drift_red_when_severe(monkeypatch):
    pub = []
    _bus(monkeypatch, pub)
    app, _ = _app_with_sm(_kh(60))
    wd = HealthWatchdog(app=app, config_manager=_kd_cm(), interval_sec=60)
    wd._check_memory_key_drift(now=1000)
    assert pub[0][1]["light"] == "red"
    assert any(p["status"] == "fail" for p in pub[0][1]["problems"])


def test_memory_key_drift_dedup_then_recover(monkeypatch):
    pub = []
    _bus(monkeypatch, pub)
    app, holder = _app_with_sm(_kh(3))
    wd = HealthWatchdog(app=app, config_manager=_kd_cm(), interval_sec=60)
    wd._check_memory_key_drift(now=1000)
    wd._check_memory_key_drift(now=1100)  # 签名不变 → 不重复发
    assert len(pub) == 1
    holder["h"] = _kh(0)                   # 漂移清零 → 恢复
    wd._check_memory_key_drift(now=1200)
    assert len(pub) == 2
    assert pub[1][1]["recovered"] is True


def test_memory_key_drift_throttled_within_interval(monkeypatch):
    pub = []
    _bus(monkeypatch, pub)
    app, _ = _app_with_sm(_kh(3))
    wd = HealthWatchdog(app=app, config_manager=_kd_cm(interval_sec=3600),
                        interval_sec=60)
    wd._check_memory_key_drift(now=1000)
    wd._check_memory_key_drift(now=1500)  # 距上次<3600 → 节流跳过
    assert len(pub) == 1


def test_memory_key_drift_disabled_no_alert(monkeypatch):
    pub = []
    _bus(monkeypatch, pub)
    app, _ = _app_with_sm(_kh(3))
    wd = HealthWatchdog(app=app, config_manager=_kd_cm(enabled=False),
                        interval_sec=60)
    wd._check_memory_key_drift(now=1000)
    assert pub == []


def test_memory_key_drift_clean_no_alert(monkeypatch):
    pub = []
    _bus(monkeypatch, pub)
    app, _ = _app_with_sm(_kh(0))         # 迁移后干净 → 不告警
    wd = HealthWatchdog(app=app, config_manager=_kd_cm(), interval_sec=60)
    wd._check_memory_key_drift(now=1000)
    assert pub == []


def test_memory_key_drift_event_alias_and_message():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert "memory_key_drift" in _EVENT_ALIASES
    title, text = _build_message("memory_key_drift_alert", {
        "light": "red",
        "problems": [{"name": "记忆 key 漂移", "detail": "检测到 60 个裸 key"}]})
    assert "记忆 key 漂移" in title
    assert "裸 key" in text
    t2, _ = _build_message("memory_key_drift_alert", {"recovered": True})
    assert "恢复" in t2


# ─── F1：AI 回复质量退化告警（复用 ops_incidents 闭环，默认关）─────────────

def _ai_quality_cm(**over):
    aq = {"enabled": True, "window_days": 7, "min_samples": 10}
    aq.update(over)
    return _CM({"ai": {"provider": "openai", "api_key": "sk-real-123"},
                "inbox": {"ai_quality_alert": aq}})


class _AiQualityInbox:
    """可控 ai_safety_summary 的假 inbox + 事件闭环桩（until_ts 非空=上一窗口 prev）。"""

    def __init__(self, cur, prev=None):
        self._cur, self._prev = cur, (prev or {})
        self.opened, self.resolved = [], []

    def ai_safety_summary(self, since_ts=0.0, until_ts=None, include_trend=False):
        return dict(self._prev) if until_ts is not None else dict(self._cur)

    def open_or_update_incident(self, **kw):
        self.opened.append(kw)
        return len(self.opened)

    def resolve_open_incidents(self, *, kind="", ts=None):
        self.resolved.append(kind)
        return 1


def test_ai_quality_alert_dedup_then_recover(monkeypatch):
    published = []
    _bus(monkeypatch, published)
    inbox = _AiQualityInbox(cur={"reviewed": 50, "adopt_rate": 0.10,
                                 "reject_rate": 0.0, "high_risk": 0})
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    wd = HealthWatchdog(app=app, config_manager=_ai_quality_cm(), interval_sec=60)

    wd._check_ai_quality()   # adopt 0.10 < severe 0.20 → fail(red) 告警
    wd._check_ai_quality()   # 签名不变 → 不重发
    assert wd.total_ai_quality_alerts == 1
    assert inbox.opened and inbox.opened[-1]["kind"] == "ai_quality"
    assert inbox.opened[0]["light"] == "red"
    assert [t for t, _ in published].count("ai_quality_alert") == 1
    assert published[0][1]["recovered"] is False

    inbox._cur = {"reviewed": 50, "adopt_rate": 0.85, "reject_rate": 0.0, "high_risk": 0}
    wd._check_ai_quality()   # 采纳率回健康 → resolve
    assert inbox.resolved == ["ai_quality"]
    assert published[-1][1]["recovered"] is True
    assert wd._last_ai_quality_sig is None


def test_ai_quality_disabled_by_default(monkeypatch):
    published = []
    _bus(monkeypatch, published)
    inbox = _AiQualityInbox(cur={"reviewed": 50, "adopt_rate": 0.0,
                                 "reject_rate": 1.0, "high_risk": 0})
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    # 未配置 ai_quality_alert → 默认关，不评不发不落表
    wd = HealthWatchdog(app=app,
                        config_manager=_CM({"ai": {"provider": "openai", "api_key": "x"}}),
                        interval_sec=60)
    wd._check_ai_quality()
    assert published == [] and inbox.opened == []


def test_ai_quality_event_alias_and_message():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert "ai_quality" in _EVENT_ALIASES
    title, text = _build_message("ai_quality_alert", {
        "light": "red",
        "problems": [{"name": "草稿采纳率", "detail": "采纳率 10% < 阈值 40%"}]})
    assert "AI 质量" in title
    assert "采纳率" in text
    t2, _ = _build_message("ai_quality_alert", {"recovered": True})
    assert "恢复" in t2


# ─── B 线：实时语音退化告警（复用 ops_incidents 闭环，默认关）────────────────

def _rtv_cm(**over):
    alert = {"enabled": True}
    alert.update(over.pop("alert", {}))
    rtv = {"enabled": True, "alert": alert}
    rtv.update(over)
    return _CM({"realtime_voice": rtv,
                "ai": {"provider": "openai", "api_key": "sk-real-123"}})


class _RtvInbox:
    def __init__(self):
        self.opened, self.resolved = [], []

    def open_or_update_incident(self, **kw):
        self.opened.append(kw)
        return len(self.opened)

    def resolve_open_incidents(self, *, kind="", ts=None):
        self.resolved.append(kind)
        return 1


def _seed_bad_rtv_stats():
    from src.ai.realtime_voice_stats import get_realtime_voice_stats
    s = get_realtime_voice_stats()
    s.reset()
    for _ in range(5):
        s.attempt()
        s.ended("host_unreachable")
    for _ in range(5):
        s.health_probe(False)
    return s


def _seed_good_rtv_stats():
    from src.ai.realtime_voice_stats import get_realtime_voice_stats
    s = get_realtime_voice_stats()
    s.reset()
    for _ in range(5):
        s.attempt()
        s.connected()
        s.ended("normal", was_connected=True, duration_sec=12.0)
    for _ in range(5):
        s.health_probe(True)
    return s


def test_realtime_voice_alert_dedup_then_recover(monkeypatch):
    _seed_bad_rtv_stats()
    published = []
    _bus(monkeypatch, published)
    inbox = _RtvInbox()
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    wd = HealthWatchdog(app=app, config_manager=_rtv_cm(), interval_sec=60)

    wd._check_realtime_voice()
    wd._check_realtime_voice()
    assert wd.total_realtime_voice_alerts == 1
    assert inbox.opened and inbox.opened[-1]["kind"] == "realtime_voice"
    assert [t for t, _ in published].count("realtime_voice_alert") == 1
    assert published[0][1]["recovered"] is False

    _seed_good_rtv_stats()
    wd._check_realtime_voice()
    assert inbox.resolved == ["realtime_voice"]
    assert published[-1][1]["recovered"] is True
    assert wd._last_realtime_voice_sig is None


def test_realtime_voice_disabled_by_default(monkeypatch):
    _seed_bad_rtv_stats()
    published = []
    _bus(monkeypatch, published)
    inbox = _RtvInbox()
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    wd = HealthWatchdog(app=app,
                        config_manager=_CM({"ai": {"provider": "openai", "api_key": "x"}}),
                        interval_sec=60)
    wd._check_realtime_voice()
    assert published == [] and inbox.opened == []


def test_realtime_voice_feature_off_skips_alert(monkeypatch):
    _seed_bad_rtv_stats()
    published = []
    _bus(monkeypatch, published)
    inbox = _RtvInbox()
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    wd = HealthWatchdog(app=app,
                        config_manager=_rtv_cm(enabled=False),
                        interval_sec=60)
    wd._check_realtime_voice()
    assert published == [] and inbox.opened == []


def test_realtime_voice_event_alias_and_message():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert "realtime_voice" in _EVENT_ALIASES
    title, text = _build_message("realtime_voice_alert", {
        "light": "red",
        "problems": [{"name": "语音主机健康率", "detail": "主机健康率 20% < 阈值 80%"}]})
    assert "实时语音" in title
    assert "语音主机" in text
    t2, _ = _build_message("realtime_voice_alert", {"recovered": True})
    assert "恢复" in t2


def test_realtime_voice_reconciles_stale_incident_on_startup(monkeypatch):
    """重启后 stats 已恢复、内存签名为空，但 DB 仍挂 open 事件 → 首次巡检静默 resolve。"""
    from src.ai.realtime_voice_stats import get_realtime_voice_stats
    get_realtime_voice_stats().reset()
    published = []
    _bus(monkeypatch, published)
    inbox = _RtvInbox()
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=inbox))
    wd = HealthWatchdog(app=app, config_manager=_rtv_cm(), interval_sec=60)
    wd._check_realtime_voice()
    assert inbox.resolved == ["realtime_voice"]
    assert all(t != "realtime_voice_alert" for t, _ in published)
