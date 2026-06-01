"""电商工具层测试（Phase D1/D2）。"""

import pytest

from src.ecommerce_tools import (
    EcommerceConnector,
    MockEcommerceConnector,
    EcommerceToolService,
    build_connector,
)
from src.ecommerce_tools.models import ToolResult


def test_mock_connector_satisfies_protocol():
    assert isinstance(MockEcommerceConnector(), EcommerceConnector)


@pytest.mark.asyncio
async def test_lookup_order_found():
    svc = EcommerceToolService(MockEcommerceConnector())
    res = await svc.lookup_order("1001")
    assert res.ok and res.found
    assert res.data["status"] == "shipped"
    assert res.data["shipment"]["carrier"] == "YunExpress"


@pytest.mark.asyncio
async def test_lookup_order_strips_hash_prefix():
    svc = EcommerceToolService(MockEcommerceConnector())
    res = await svc.lookup_order("#1001")
    assert res.found is True


@pytest.mark.asyncio
async def test_lookup_order_not_found_is_ok_but_not_found():
    svc = EcommerceToolService(MockEcommerceConnector())
    res = await svc.lookup_order("9999")
    assert res.ok is True
    assert res.found is False
    assert res.data is None
    # 事实校验：必须明确告知查不到
    assert "未查到" in res.to_context_facts()


@pytest.mark.asyncio
async def test_track_shipment_found():
    svc = EcommerceToolService(MockEcommerceConnector())
    res = await svc.track_shipment("LP001234567CN")
    assert res.found is True
    assert res.data["status"] == "in_transit"


@pytest.mark.asyncio
async def test_empty_query_returns_error():
    svc = EcommerceToolService(MockEcommerceConnector())
    res = await svc.lookup_order("")
    assert res.ok is False
    assert res.error == "empty_order_no"


@pytest.mark.asyncio
async def test_connector_exception_does_not_raise():
    class _Boom:
        name = "boom"

        async def get_order(self, order_no):
            raise RuntimeError("api down")

        async def track_shipment(self, tracking_no):
            raise RuntimeError("api down")

    svc = EcommerceToolService(_Boom())
    res = await svc.lookup_order("1001")
    assert res.ok is False
    assert "RuntimeError" in res.error
    # 事实校验：错误时也提示勿编造
    assert "勿编造" in res.to_context_facts()


@pytest.mark.asyncio
async def test_audit_logged_on_lookup():
    calls = []

    class _Audit:
        def log(self, **kw):
            calls.append(kw)

    svc = EcommerceToolService(MockEcommerceConnector(), audit_store=_Audit())
    await svc.lookup_order("1001", by="op1")
    assert len(calls) == 1
    assert calls[0]["action"] == "ecommerce_order_lookup"
    assert calls[0]["target"] == "1001"
    assert calls[0]["user_id"] == "op1"


def test_build_connector_mock():
    conn = build_connector({"provider": "mock"})
    assert conn.name == "mock"


def test_build_connector_unknown_falls_back_to_mock():
    conn = build_connector({"provider": "shopify"})
    assert conn.name == "mock"  # 未实现 → 回落 mock，不崩


@pytest.mark.asyncio
async def test_custom_mock_orders():
    conn = MockEcommerceConnector(orders={"X1": {"status": "paid", "total": "10"}})
    svc = EcommerceToolService(conn)
    res = await svc.lookup_order("X1")
    assert res.found and res.data["status"] == "paid"


def test_order_facts_only_state_no_fabrication():
    res = ToolResult(ok=True, found=True, kind="order", query="1001",
                     data={"order_no": "1001", "status": "shipped", "total": "59.90",
                           "currency": "USD", "shipment": {}})
    facts = res.to_context_facts()
    assert "1001" in facts and "shipped" in facts
    assert "勿编造" in facts
