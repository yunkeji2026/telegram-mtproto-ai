"""D2 运维可靠性测试：worker 错误率归一 / 评分红绿灯 / 趋势累计 / store 时间线 / 路由。"""

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.utils.reliability import (
    build_reliability,
    worker_reliability,
)


def test_worker_reliability_error_rate_and_status():
    snaps = [
        {"id": "autosend", "name": "Autosend", "running": True,
         "total_sent": 90, "total_errors": 10, "circuit_open": False},
        {"id": "webhook", "name": "Webhook", "running": True,
         "total_sent": 50, "total_errors": 0, "circuit_open": False},
    ]
    out = worker_reliability(snaps)
    a = next(w for w in out if w["id"] == "autosend")
    assert a["error_rate"] == 0.1
    assert a["status"] == "ok"  # 10% < 20% 阈值
    b = next(w for w in out if w["id"] == "webhook")
    assert b["error_rate"] == 0.0


def test_worker_not_running_is_fail():
    out = worker_reliability([{"id": "x", "name": "X", "running": False,
                               "total_sent": 0, "total_errors": 0}])
    assert out[0]["status"] == "fail"


def test_worker_high_error_rate_is_warn():
    out = worker_reliability([{"id": "x", "name": "X", "running": True,
                               "total_sent": 5, "total_errors": 5}])
    assert out[0]["error_rate"] == 0.5
    assert out[0]["status"] == "warn"


def test_worker_circuit_open_is_warn():
    out = worker_reliability([{"id": "x", "name": "X", "running": True,
                               "total_sent": 100, "total_errors": 0,
                               "circuit_open": True}])
    assert out[0]["status"] == "warn"


def test_build_reliability_all_green():
    d = build_reliability(
        worker_snapshots=[{"id": "a", "name": "A", "running": True,
                           "total_sent": 100, "total_errors": 0}],
        timeline=[{"bucket_ts": 1000, "total": 10, "autosend": 8,
                   "blocked": 1, "rejected": 1}],
        recent_alerts=[],
    )
    assert d["light"] == "green"
    assert d["score"] == 100
    assert d["totals"]["dispositions"] == 10
    assert d["totals"]["block_rate"] == 0.1
    assert d["totals"]["reject_rate"] == 0.1
    assert len(d["trend"]) == 1


def test_build_reliability_score_deductions():
    # 一个 fail worker(-30) + 2 条告警(-10) → 60 → yellow
    d = build_reliability(
        worker_snapshots=[{"id": "a", "name": "A", "running": False,
                           "total_sent": 0, "total_errors": 0}],
        timeline=[],
        recent_alerts=[{"light": "red"}, {"light": "red"}],
    )
    assert d["score"] == 60
    assert d["light"] == "yellow"
    assert d["alert_count"] == 2


def test_build_reliability_red_when_low():
    d = build_reliability(
        worker_snapshots=[
            {"id": "a", "name": "A", "running": False, "total_sent": 0, "total_errors": 0},
            {"id": "b", "name": "B", "running": False, "total_sent": 0, "total_errors": 0},
        ],
        timeline=[],
        recent_alerts=[],
    )
    assert d["score"] == 40
    assert d["light"] == "red"


def test_store_reliability_timeline_buckets(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    base = 1_700_000_000.0
    # 同一小时桶内多条
    store.record_draft_audit("d1", action="autosend", conversation_id="c1", ts=base + 10)
    store.record_draft_audit("d2", action="blocked", conversation_id="c1", ts=base + 20)
    store.record_draft_audit("d3", action="rejected", conversation_id="c1", ts=base + 30)
    # 下一个小时桶
    store.record_draft_audit("d4", action="autosend", conversation_id="c1", ts=base + 3700)
    tl = store.get_reliability_timeline(base - 100, bucket_sec=3600)
    assert len(tl) == 2
    first = tl[0]
    assert first["total"] == 3
    assert first["autosend"] == 1
    assert first["blocked"] == 1
    assert first["rejected"] == 1
    store.close()


def test_reliability_route_registered():
    import inspect
    from src.web.routes import runtime_health_routes
    src = inspect.getsource(runtime_health_routes.register_runtime_health_routes)
    assert "/api/admin/reliability" in src
