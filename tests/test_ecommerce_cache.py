"""P2-a — EcommerceToolService 短 TTL 缓存单测（确定性，无 sleep）。"""

from __future__ import annotations

import time

from src.ecommerce_tools.service import EcommerceToolService
from src.ecommerce_tools.models import OrderInfo, ShipmentInfo


class _CountingConnector:
    name = "counting"

    def __init__(self) -> None:
        self.order_calls = 0
        self.ship_calls = 0

    async def get_order(self, order_no: str):
        self.order_calls += 1
        key = str(order_no).lstrip("#")
        if key == "boom":
            raise RuntimeError("connector down")
        if key == "1001":
            return OrderInfo(order_no="1001", status="shipped",
                             total="10", currency="USD")
        return None

    async def track_shipment(self, tracking_no: str):
        self.ship_calls += 1
        if tracking_no == "LP001234567CN":
            return ShipmentInfo(tracking_no=tracking_no, carrier="DHL",
                                status="in_transit")
        return None


class _CapAudit:
    def __init__(self) -> None:
        self.logs = []

    def log(self, **kw):
        self.logs.append(kw)


async def test_cache_hit_avoids_second_call():
    conn = _CountingConnector()
    svc = EcommerceToolService(conn, cache_ttl_sec=100)
    r1 = await svc.lookup_order("1001")
    r2 = await svc.lookup_order("1001")
    assert r1.found and r2.found
    assert conn.order_calls == 1  # 第二次命中缓存


async def test_cache_disabled_by_default():
    conn = _CountingConnector()
    svc = EcommerceToolService(conn)  # ttl 默认 0 → 关闭
    await svc.lookup_order("1001")
    await svc.lookup_order("1001")
    assert conn.order_calls == 2


async def test_not_found_is_cached():
    """查不到也是 ok=True，短 TTL 内缓存，避免反复打不存在的单号。"""
    conn = _CountingConnector()
    svc = EcommerceToolService(conn, cache_ttl_sec=100)
    await svc.lookup_order("9999")
    await svc.lookup_order("9999")
    assert conn.order_calls == 1


async def test_error_not_cached():
    """连接器异常 ok=False，不缓存，下次重试。"""
    conn = _CountingConnector()
    svc = EcommerceToolService(conn, cache_ttl_sec=100)
    r1 = await svc.lookup_order("boom")
    r2 = await svc.lookup_order("boom")
    assert not r1.ok and not r2.ok
    assert conn.order_calls == 2


async def test_cache_expiry():
    conn = _CountingConnector()
    svc = EcommerceToolService(conn, cache_ttl_sec=100)
    await svc.lookup_order("1001")
    # 手工把过期时间拨到过去 → 下次必 miss（确定性，避免 sleep）
    for k in list(svc._cache.keys()):
        exp, res = svc._cache[k]
        svc._cache[k] = (time.monotonic() - 1, res)
    await svc.lookup_order("1001")
    assert conn.order_calls == 2


async def test_cache_key_normalizes_hash_and_case():
    conn = _CountingConnector()
    svc = EcommerceToolService(conn, cache_ttl_sec=100)
    await svc.lookup_order("#1001")
    await svc.lookup_order("1001")
    assert conn.order_calls == 1  # # 前缀归一后命中同一键


async def test_audit_marks_cache_hit():
    conn = _CountingConnector()
    audit = _CapAudit()
    svc = EcommerceToolService(conn, audit_store=audit, cache_ttl_sec=100)
    await svc.lookup_order("1001")
    await svc.lookup_order("1001")
    assert len(audit.logs) == 2
    import json
    first = json.loads(audit.logs[0]["new_val"])
    second = json.loads(audit.logs[1]["new_val"])
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True


async def test_shipment_cache_hit():
    conn = _CountingConnector()
    svc = EcommerceToolService(conn, cache_ttl_sec=100)
    await svc.track_shipment("LP001234567CN")
    await svc.track_shipment("LP001234567CN")
    assert conn.ship_calls == 1


async def test_cache_key_separates_order_and_shipment():
    """同一字符串作订单 vs 物流，键不串（kind 隔离）。"""
    conn = _CountingConnector()
    svc = EcommerceToolService(conn, cache_ttl_sec=100)
    await svc.lookup_order("LP001234567CN")   # order miss(not found)
    await svc.track_shipment("LP001234567CN")  # shipment 应独立查
    assert conn.order_calls == 1
    assert conn.ship_calls == 1


async def test_lru_eviction_by_maxsize():
    """超容量按最久未用淘汰：被淘汰键再查需重新打 connector。"""
    conn = _CountingConnector()
    svc = EcommerceToolService(conn, cache_ttl_sec=100, cache_max_entries=2)
    await svc.lookup_order("a")  # calls=1
    await svc.lookup_order("b")  # calls=2
    await svc.lookup_order("c")  # calls=3 → 淘汰最旧 "a"
    await svc.lookup_order("c")  # 命中, calls=3
    await svc.lookup_order("a")  # 已被淘汰 → miss, calls=4
    assert conn.order_calls == 4
    assert len(svc._cache) <= 2


async def test_expired_entries_swept_on_insert():
    """插入新项时顺手清理已过期项，防内存泄漏。"""
    conn = _CountingConnector()
    svc = EcommerceToolService(conn, cache_ttl_sec=100, cache_max_entries=512)
    await svc.lookup_order("1001")
    # 把 1001 拨过期
    for k in list(svc._cache.keys()):
        exp, res = svc._cache[k]
        svc._cache[k] = (time.monotonic() - 1, res)
    await svc.lookup_order("9999")  # 触发 put → 清理过期的 1001
    keys = [k for (_, k) in svc._cache.keys()]
    assert "1001" not in keys
    assert "9999" in keys


def test_order_facts_include_tracking_no():
    from src.ecommerce_tools.models import ToolResult
    res = ToolResult(
        ok=True, found=True, kind="order", query="1001",
        data={"order_no": "1001", "status": "shipped", "total": "10",
              "currency": "USD",
              "shipment": {"carrier": "DHL", "status": "in_transit",
                           "last_event": "出库", "tracking_no": "LP001234567CN"}},
    )
    facts = res.to_context_facts()
    assert "运单号=" in facts
    assert "LP001234567CN" in facts
