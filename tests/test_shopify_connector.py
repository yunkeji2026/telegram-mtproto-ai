"""ShopifyConnector 单测 — 全部注入 fake http_get，不联网（P1-a）。"""

from __future__ import annotations

from src.ecommerce_tools.shopify_connector import ShopifyConnector
from src.ecommerce_tools.service import build_connector, EcommerceToolService
from src.ecommerce_tools.mock_connector import MockEcommerceConnector


class _FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


_ORDER_PAYLOAD = {
    "orders": [
        {
            "name": "#1001",
            "order_number": 1001,
            "financial_status": "paid",
            "fulfillment_status": "shipped",
            "currency": "USD",
            "total_price": "59.90",
            "email": "jane@example.com",
            "created_at": "2026-05-01T10:00:00Z",
            "customer": {"first_name": "Jane", "last_name": "Doe"},
            "line_items": [{"sku": "SKU-1", "title": "T-Shirt", "quantity": 2}],
            "fulfillments": [
                {
                    "tracking_company": "DHL",
                    "tracking_number": "TRK999",
                    "shipment_status": "in_transit",
                    "status": "success",
                    "updated_at": "2026-05-02T08:00:00Z",
                }
            ],
        }
    ]
}


def _make(payload, status=200, capture=None):
    async def _http_get(url, headers):
        if capture is not None:
            capture["url"] = url
            capture["headers"] = headers
        return _FakeResp(status, payload)

    return ShopifyConnector(
        shop="demo", access_token="shpat_x", http_get=_http_get
    )


async def test_get_order_maps_fields():
    cap = {}
    conn = _make(_ORDER_PAYLOAD, capture=cap)
    order = await conn.get_order("1001")
    assert order is not None
    assert order.order_no == "1001"           # name 去 # 前缀
    assert order.status == "shipped"           # fulfillment_status 优先
    assert order.currency == "USD"
    assert order.total == "59.90"
    assert order.customer_name == "Jane Doe"
    assert order.customer_email == "jane@example.com"
    assert order.items and order.items[0]["sku"] == "SKU-1"
    assert order.shipment is not None
    assert order.shipment.tracking_no == "TRK999"
    assert order.shipment.carrier == "DHL"
    assert order.shipment.status == "in_transit"
    # shop 归一 + 鉴权头注入
    assert "demo.myshopify.com" in cap["url"]
    assert "%231001" in cap["url"]             # # 被 url-encode
    assert cap["headers"]["X-Shopify-Access-Token"] == "shpat_x"


async def test_get_order_not_found_empty_orders():
    conn = _make({"orders": []})
    assert await conn.get_order("9999") is None


async def test_get_order_non_200_returns_none():
    conn = _make({"orders": [{"name": "#1"}]}, status=401)
    assert await conn.get_order("1") is None


async def test_missing_credentials_returns_none():
    conn = ShopifyConnector(shop="", access_token="", http_get=None)
    assert await conn.get_order("1001") is None


async def test_track_shipment_unsupported_returns_none():
    conn = _make(_ORDER_PAYLOAD)
    assert await conn.track_shipment("TRK999") is None


def test_build_connector_shopify_ok():
    conn = build_connector({
        "provider": "shopify",
        "shopify": {"shop": "demo", "access_token": "shpat_x"},
    })
    assert isinstance(conn, ShopifyConnector)


def test_build_connector_shopify_missing_creds_falls_back_mock():
    conn = build_connector({"provider": "shopify", "shopify": {"shop": "demo"}})
    assert isinstance(conn, MockEcommerceConnector)


async def test_service_with_shopify_connector_lookup_order():
    cap = {}
    conn = _make(_ORDER_PAYLOAD, capture=cap)
    svc = EcommerceToolService(connector=conn, audit_store=None)
    res = await svc.lookup_order("1001")
    assert res.ok and res.found
    assert res.source == "shopify"
    assert res.data["order_no"] == "1001"
    facts = res.to_context_facts()
    assert "1001" in facts and "shipped" in facts


async def test_service_shopify_order_not_found():
    conn = _make({"orders": []})
    svc = EcommerceToolService(connector=conn, audit_store=None)
    res = await svc.lookup_order("9999")
    assert res.ok and not res.found
    facts = res.to_context_facts()
    assert "勿编造" in facts
