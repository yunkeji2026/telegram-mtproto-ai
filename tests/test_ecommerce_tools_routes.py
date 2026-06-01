"""/api/tools/ecommerce/* 路由测试（Phase D）。"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ecommerce_tools import EcommerceToolService, MockEcommerceConnector
from src.web.routes.ecommerce_tools_routes import register_ecommerce_tools_routes


def _client(with_service=True):
    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_ecommerce_tools_routes(app, api_auth=api_auth)
    if with_service:
        app.state.ecommerce_tools = EcommerceToolService(MockEcommerceConnector())
    return TestClient(app)


def test_order_endpoint_by_order_no():
    c = _client()
    resp = c.get("/api/tools/ecommerce/order?order_no=1001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["found"] is True
    assert "facts" in data["result"]


def test_order_endpoint_extracts_from_text():
    c = _client()
    resp = c.get("/api/tools/ecommerce/order?text=我的订单%20%231001%20到哪了")
    assert resp.status_code == 200
    assert resp.json()["result"]["data"]["order_no"] == "1001"


def test_order_endpoint_missing_query_400():
    c = _client()
    resp = c.get("/api/tools/ecommerce/order?text=你好")
    assert resp.status_code == 400


def test_track_endpoint():
    c = _client()
    resp = c.get("/api/tools/ecommerce/track?tracking_no=LP001234567CN")
    assert resp.status_code == 200
    assert resp.json()["result"]["data"]["status"] == "in_transit"


def test_resolve_endpoint_auto_extracts():
    c = _client()
    resp = c.post("/api/tools/ecommerce/resolve", json={"text": "订单 #1001 怎么还没到"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert any(r["found"] for r in data["results"])


def test_not_found_order_returns_facts_no_fabrication():
    c = _client()
    resp = c.get("/api/tools/ecommerce/order?order_no=9999")
    data = resp.json()
    assert data["result"]["found"] is False
    assert "未查到" in data["result"]["facts"]


def test_503_without_service():
    c = _client(with_service=False)
    assert c.get("/api/tools/ecommerce/order?order_no=1001").status_code == 503


def test_cache_stats_endpoint():
    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_ecommerce_tools_routes(app, api_auth=api_auth)
    app.state.ecommerce_tools = EcommerceToolService(
        MockEcommerceConnector(), cache_ttl_sec=100,
    )
    c = TestClient(app)
    c.get("/api/tools/ecommerce/order?order_no=1001")  # miss -> 落缓存
    c.get("/api/tools/ecommerce/order?order_no=1001")  # hit
    resp = c.get("/api/tools/ecommerce/cache_stats")
    assert resp.status_code == 200
    s = resp.json()["stats"]
    assert s["enabled"] is True
    assert s["hits"] == 1
    assert s["misses"] == 1
    assert 0 < s["hit_rate"] <= 1
