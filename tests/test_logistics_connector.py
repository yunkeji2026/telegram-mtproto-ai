"""物流连接器 + service 集成单测（注入 fake http，不联网）。"""

from __future__ import annotations

from src.ecommerce_tools.logistics import (
    LogisticsConnector,
    MockLogisticsConnector,
    AfterShipConnector,
    build_logistics_connector,
)
from src.ecommerce_tools.service import EcommerceToolService
from src.ecommerce_tools.mock_connector import MockEcommerceConnector


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


_AFTERSHIP_OK = {
    "data": {
        "tracking": {
            "slug": "dhl",
            "tag": "InTransit",
            "expected_delivery": "2026-06-10",
            "updated_at": "2026-06-02T08:00:00Z",
            "checkpoints": [
                {"message": "Picked up", "checkpoint_time": "2026-06-01T10:00:00Z"},
                {"message": "Departed facility", "checkpoint_time": "2026-06-02T07:00:00Z"},
            ],
        }
    }
}


# ── Mock logistics ──────────────────────────────────────────────────────

async def test_mock_logistics_track_found():
    conn = MockLogisticsConnector()
    s = await conn.track("LP001234567CN")
    assert s is not None and s.carrier == "YunExpress" and s.status == "in_transit"


async def test_mock_logistics_not_found():
    assert await MockLogisticsConnector().track("NOPE") is None


def test_mock_logistics_satisfies_protocol():
    assert isinstance(MockLogisticsConnector(), LogisticsConnector)


# ── AfterShip (fake http) ───────────────────────────────────────────────

def _aftership(payload, status=200, cap=None):
    async def _http_get(url, headers):
        if cap is not None:
            cap["url"] = url
            cap["headers"] = headers
        return _FakeResp(status, payload)
    return AfterShipConnector(api_key="key1", http_get=_http_get)


async def test_aftership_maps_latest_checkpoint():
    cap = {}
    conn = _aftership(_AFTERSHIP_OK, cap=cap)
    s = await conn.track("TRK1", carrier="dhl")
    assert s is not None
    assert s.tracking_no == "TRK1"
    assert s.carrier == "dhl"
    assert s.status == "InTransit"
    assert s.last_event == "Departed facility"          # 取最后一个 checkpoint
    assert s.eta == "2026-06-10"
    assert "/trackings/dhl/TRK1" in cap["url"]
    assert cap["headers"]["aftership-api-key"] == "key1"


async def test_aftership_detect_slug_when_no_carrier():
    cap = {}
    conn = _aftership(_AFTERSHIP_OK, cap=cap)
    await conn.track("TRK1")
    assert "/trackings/detect/TRK1" in cap["url"]


async def test_aftership_non_200_returns_none():
    conn = _aftership(_AFTERSHIP_OK, status=404)
    assert await conn.track("TRK1") is None


async def test_aftership_missing_key_returns_none():
    conn = AfterShipConnector(api_key="", http_get=None)
    assert await conn.track("TRK1") is None


# ── build factory ───────────────────────────────────────────────────────

def test_build_disabled_returns_none():
    assert build_logistics_connector({"enabled": False}) is None
    assert build_logistics_connector({}) is None


def test_build_mock():
    assert isinstance(build_logistics_connector(
        {"enabled": True, "provider": "mock"}), MockLogisticsConnector)


def test_build_aftership_ok():
    conn = build_logistics_connector(
        {"enabled": True, "provider": "aftership", "aftership": {"api_key": "k"}})
    assert isinstance(conn, AfterShipConnector)


def test_build_aftership_missing_key_none():
    assert build_logistics_connector(
        {"enabled": True, "provider": "aftership"}) is None


# ── service 集成：物流优先 + 回退 ────────────────────────────────────────

async def test_service_uses_logistics_first():
    svc = EcommerceToolService(
        MockEcommerceConnector(),
        logistics_connector=MockLogisticsConnector(),
    )
    res = await svc.track_shipment("1ZTEST999")  # 仅物流 mock 有
    assert res.ok and res.found
    assert res.source == "mock_logistics"
    assert res.data["status"] == "delivered"


class _EmptyLogistics:
    name = "empty_logi"

    async def track(self, tracking_no, carrier=""):
        return None


async def test_service_falls_back_to_ecommerce_connector():
    """物流连接器查不到 → 回退电商 connector（其默认单含 LP001234567CN）。"""
    svc = EcommerceToolService(
        MockEcommerceConnector(),
        logistics_connector=_EmptyLogistics(),
    )
    res = await svc.track_shipment("LP001234567CN")
    assert res.ok and res.found
    assert res.source == "mock"               # 回退到电商 connector 名
    assert res.data["carrier"] == "YunExpress"


async def test_service_without_logistics_uses_ecommerce():
    svc = EcommerceToolService(MockEcommerceConnector())
    res = await svc.track_shipment("LP001234567CN")
    assert res.ok and res.found and res.source == "mock"


async def test_service_logistics_facts_for_reply():
    svc = EcommerceToolService(
        MockEcommerceConnector(),
        logistics_connector=MockLogisticsConnector(),
    )
    res = await svc.track_shipment("LP001234567CN")
    facts = res.to_context_facts()
    assert "LP001234567CN" in facts
