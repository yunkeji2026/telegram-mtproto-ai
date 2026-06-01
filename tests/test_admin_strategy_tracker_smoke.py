"""冒烟测试：admin 端点对 _get_strategy_tracker 的调用不再 NameError。

背景：strategy_routes 抽出时 _get_strategy_tracker 闭包未在 admin.py 保留，
导致 data-purge/session-stats/export-strategy-events/report/daily 一旦被调用即
NameError 500（无测试覆盖、启动不触发，故长期潜伏）。本测试把这 4 个端点钉死：
telegram_client=None → tracker 为 None → 必须优雅降级，绝不 500。

conftest 的 client 用 raise_server_exceptions=True：若再次出现 NameError，
请求期会直接抛出使本测试失败。
"""

from __future__ import annotations


def test_data_purge_no_nameerror(auth_client):
    r = auth_client.post("/api/data-purge", json={})
    assert r.status_code != 500           # 关键：不再 NameError
    assert r.status_code in (200, 403)


def test_session_stats_no_tracker(auth_client):
    r = auth_client.get("/api/session-stats")
    assert r.status_code == 200
    # 无 tracker（telegram_client=None）→ 优雅返回空
    assert r.json().get("total_sessions") == 0


def test_export_strategy_events_no_tracker(auth_client):
    r = auth_client.get("/api/export-strategy-events")
    # 无 tracker → 404（Tracker not available），关键是不 500
    assert r.status_code != 500
    assert r.status_code in (200, 404)


def test_daily_report_no_tracker(auth_client):
    r = auth_client.get("/api/report/daily")
    assert r.status_code == 200
    body = r.json()
    assert "generated_at" in body
    # 策略段在无 tracker 时被跳过，但报表整体仍生成
    assert "text_summary" in body


def test_weekly_report_smoke(auth_client):
    r = auth_client.get("/api/report/weekly")
    assert r.status_code == 200
    assert r.json().get("type") == "weekly"
