"""D1 运行时健康测试：聚合逻辑 / 红绿灯阈值 / store.ping / 路由。"""

from src.inbox.store import InboxStore
from src.utils.health import build_health, is_placeholder


def _base(**kw):
    """全绿基线，便于单点改写测某一维度。"""
    args = dict(
        db_ok=True, ai_provider="openai", ai_key_ok=True,
        license_state="active", license_read_only=False, license_plan="pro",
        channels_ready=1, channels_configured=1, channels_total=3,
        workers=[{"id": "autosend", "name": "Autosend", "present": True,
                  "running": True, "circuit_open": False}],
        pending_drafts=10,
    )
    args.update(kw)
    return build_health(**args)


def test_all_green():
    h = _base()
    assert h["light"] == "green"
    assert h["healthy"] is True
    assert h["summary"]["fail"] == 0


def test_db_down_is_red():
    h = _base(db_ok=False)
    assert h["light"] == "red"
    assert h["healthy"] is False
    db = next(c for c in h["components"] if c["id"] == "db")
    assert db["status"] == "fail"


def test_ai_missing_key_is_red():
    h = _base(ai_provider="openai", ai_key_ok=False)
    ai = next(c for c in h["components"] if c["id"] == "ai")
    assert ai["status"] == "fail"
    assert h["light"] == "red"


def test_no_channel_is_yellow_not_red():
    h = _base(channels_ready=0, channels_configured=0)
    ch = next(c for c in h["components"] if c["id"] == "channels")
    assert ch["status"] == "warn"
    assert h["light"] == "yellow"  # 渠道是软性，不致命


def test_worker_present_not_running_is_red():
    h = _base(workers=[{"id": "autosend", "name": "Autosend", "present": True,
                        "running": False, "circuit_open": False}])
    w = next(c for c in h["components"] if c["id"] == "worker_autosend")
    assert w["status"] == "fail"
    assert h["light"] == "red"


def test_worker_circuit_open_is_yellow():
    h = _base(workers=[{"id": "autosend", "name": "Autosend", "present": True,
                        "running": True, "circuit_open": True,
                        "last_error": "boom"}])
    w = next(c for c in h["components"] if c["id"] == "worker_autosend")
    assert w["status"] == "warn"
    assert "boom" in w["detail"]


def test_absent_worker_skipped():
    h = _base(workers=[{"id": "autoclaim", "name": "AutoClaim", "present": False}])
    assert not any(c["id"] == "worker_autoclaim" for c in h["components"])


def test_queue_backlog_warns():
    h = _base(pending_drafts=500, pending_threshold=200)
    q = next(c for c in h["components"] if c["id"] == "queue")
    assert q["status"] == "warn"


def test_license_expired_readonly_is_fail():
    h = _base(license_state="expired", license_read_only=True)
    lic = next(c for c in h["components"] if c["id"] == "license")
    assert lic["status"] == "fail"


def test_license_expired_not_enforced_is_warn():
    h = _base(license_state="expired", license_read_only=False)
    lic = next(c for c in h["components"] if c["id"] == "license")
    assert lic["status"] == "warn"


def test_community_license_is_ok():
    h = _base(license_state="unlicensed", license_plan="community")
    lic = next(c for c in h["components"] if c["id"] == "license")
    assert lic["status"] == "ok"


def test_is_placeholder():
    assert is_placeholder("") is True
    assert is_placeholder("your_api_key") is True
    assert is_placeholder("sk-real-key-123") is False


def test_store_ping(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    assert store.ping() is True
    store.close()


def test_health_route_registered():
    import inspect
    from src.web.routes import runtime_health_routes
    src = inspect.getsource(runtime_health_routes.register_runtime_health_routes)
    assert "/api/admin/health" in src
