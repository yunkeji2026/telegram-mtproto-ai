"""P8：出站路由回落率按日趋势落库（send_route_trend_store）+ stats 旁路 sync。"""

from __future__ import annotations

import time

from src.inbox.send_route_stats import get_send_route_stats
from src.inbox.send_route_trend_store import (
    SendRouteTrendStore,
    configure_send_route_trend_store,
    get_send_route_trend_store,
    reset_send_route_trend_store,
    sync_send_route_trend_from_stats,
)


def test_daily_zero_fills_and_fallback_rate():
    store = SendRouteTrendStore(":memory:")
    now = time.time()
    store.add(orchestrator=6, adapter=4, now=now)
    days = store.daily(days=7, now=now)
    assert len(days) == 7
    assert all(d["total"] == 0 for d in days[:-1])  # 历史日补零
    today = days[-1]
    assert today["orchestrator"] == 6
    assert today["adapter"] == 4
    assert today["total"] == 10
    assert today["fallback_rate"] == 0.4


def test_add_upserts_same_day():
    store = SendRouteTrendStore(":memory:")
    now = time.time()
    store.add(orchestrator=1, adapter=1, now=now)
    store.add(orchestrator=2, adapter=0, now=now)
    today = store.daily(days=1, now=now)[-1]
    assert today["orchestrator"] == 3 and today["adapter"] == 1
    assert today["total"] == 4


def test_add_zero_is_noop():
    store = SendRouteTrendStore(":memory:")
    now = time.time()
    store.add(orchestrator=0, adapter=0, now=now)
    assert store.daily(days=1, now=now)[-1]["total"] == 0


def test_prune_removes_old_days():
    store = SendRouteTrendStore(":memory:")
    now = time.time()
    store.add(orchestrator=1, adapter=1, now=now - 100 * 86400)  # 100 天前
    store.add(orchestrator=2, adapter=1, now=now)
    removed = store.prune(retention_days=90.0, now=now)
    assert removed == 1
    assert store.daily(days=1, now=now)[-1]["total"] == 3


def test_sync_noop_until_configured():
    reset_send_route_trend_store()
    sync_send_route_trend_from_stats({"orchestrator_total": 5, "adapter_total": 2})
    assert get_send_route_trend_store() is None  # 未配置 → 不建库
    reset_send_route_trend_store()


def test_sync_writes_incremental_deltas():
    """累计计数器口径：首次 sync 只记基线，之后写增量。"""
    reset_send_route_trend_store()
    store = configure_send_route_trend_store(enabled=True, db_path=":memory:")
    assert store is not None
    # bootstrap 基线（当前累计 3/1）→ 不写
    sync_send_route_trend_from_stats({"orchestrator_total": 3, "adapter_total": 1})
    assert store.daily(days=1)[-1]["total"] == 0
    # 累计涨到 8/4 → 增量 5/3 写入
    sync_send_route_trend_from_stats({"orchestrator_total": 8, "adapter_total": 4})
    today = store.daily(days=1)[-1]
    assert today["orchestrator"] == 5 and today["adapter"] == 3
    assert today["fallback_rate"] == round(3 / 8, 4)
    reset_send_route_trend_store()


def test_sync_ignores_counter_reset():
    """进程重启使累计计数归零：sync 不得写负增量（以新基线重启）。"""
    reset_send_route_trend_store()
    store = configure_send_route_trend_store(enabled=True, db_path=":memory:")
    sync_send_route_trend_from_stats({"orchestrator_total": 10, "adapter_total": 5})  # 基线
    sync_send_route_trend_from_stats({"orchestrator_total": 12, "adapter_total": 6})  # +2/+1
    assert store.daily(days=1)[-1]["total"] == 3
    # 计数器归零（重启）→ delta 为负，忽略；不改当日累计
    sync_send_route_trend_from_stats({"orchestrator_total": 0, "adapter_total": 0})
    assert store.daily(days=1)[-1]["total"] == 3
    # 新基线后重新累计 +4/+1
    sync_send_route_trend_from_stats({"orchestrator_total": 4, "adapter_total": 1})
    assert store.daily(days=1)[-1]["total"] == 8
    reset_send_route_trend_store()


def test_sync_end_to_end_from_real_stats():
    """真实 SendRouteStats.dump() 喂进 sync（口径契约回归）。"""
    reset_send_route_trend_store()
    store = configure_send_route_trend_store(enabled=True, db_path=":memory:")
    stats = get_send_route_stats()
    stats.reset()
    sync_send_route_trend_from_stats(stats.dump())  # 基线（全 0）
    stats.record("telegram", "orchestrator")
    stats.record("line", "adapter")
    stats.record("line", "adapter")
    sync_send_route_trend_from_stats(stats.dump())
    today = store.daily(days=1)[-1]
    assert today["orchestrator"] == 1 and today["adapter"] == 2
    reset_send_route_trend_store()
    stats.reset()


# ── /api/admin/send-route-trend 读端点 ───────────────────────────────────────

def _make_ops_app():
    from fastapi import FastAPI
    from src.web.routes.ops_overview_routes import register_ops_overview_routes

    app = FastAPI()

    class _Ctx:
        def api_auth(self, request):
            return True

        def api_write(self, perm):
            def _dep():
                return True
            return _dep

        def page_auth(self, request):
            return True

        templates = None
        config_manager = None
        audit_store = None
        user_store = None
        token = None

    register_ops_overview_routes(app, _Ctx())
    return app


def test_send_route_trend_endpoint_disabled_returns_empty():
    from fastapi.testclient import TestClient
    reset_send_route_trend_store()  # 未开启
    client = TestClient(_make_ops_app(), raise_server_exceptions=True)
    d = client.get("/api/admin/send-route-trend?days=7").json()
    assert d["enabled"] is False and d["days"] == []


def test_send_route_trend_endpoint_returns_series():
    from fastapi.testclient import TestClient
    reset_send_route_trend_store()
    store = configure_send_route_trend_store(enabled=True, db_path=":memory:")
    store.add(orchestrator=7, adapter=3)
    client = TestClient(_make_ops_app(), raise_server_exceptions=True)
    d = client.get("/api/admin/send-route-trend?days=7").json()
    assert d["enabled"] is True and len(d["days"]) == 7
    assert d["days"][-1]["fallback_rate"] == 0.3
    reset_send_route_trend_store()
