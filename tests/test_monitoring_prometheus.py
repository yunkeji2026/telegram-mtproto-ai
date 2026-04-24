"""监控 API：Prometheus 文本中含启动配置基线指标。"""

from __future__ import annotations

from starlette.testclient import TestClient

from src.monitoring.metrics_store import get_metrics_store
from src.monitoring.server import create_app


def test_prometheus_includes_startup_advisory_gauges():
    get_metrics_store().set_startup_advisory_counts(2, 1)
    get_metrics_store().set_startup_advisory_audit_logged(1)
    app = create_app(assistant_ref=None, auth_token="")
    with TestClient(app) as client:
        r = client.get("/api/metrics/prometheus")
    assert r.status_code == 200
    body = r.text.replace("\r\n", "\n")
    assert "tg_bot_startup_advisory_total 2" in body
    assert "tg_bot_startup_advisory_warnings 1" in body
    assert "tg_bot_startup_advisory_audit_logged 1" in body
