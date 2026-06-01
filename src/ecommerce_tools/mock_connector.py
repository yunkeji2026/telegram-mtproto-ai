"""MockEcommerceConnector — 内存 mock connector（Phase D）。

用于本地演示、测试与「无真实电商接口时」的端到端跑通。数据确定性，
可由 config 注入种子，也可用内置示例。绝不联网。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .models import OrderInfo, ShipmentInfo


_DEFAULT_ORDERS: Dict[str, Dict[str, Any]] = {
    "1001": {
        "status": "shipped", "currency": "USD", "total": "59.90",
        "customer_name": "Alice", "created_at": "2026-05-20",
        "items": [{"sku": "TS-RED-M", "name": "T-Shirt Red M", "qty": 2}],
        "shipment": {
            "tracking_no": "LP001234567CN", "carrier": "YunExpress",
            "status": "in_transit", "last_event": "Departed facility",
            "last_event_at": "2026-05-22", "eta": "2026-05-30",
        },
    },
    "1002": {
        "status": "delivered", "currency": "USD", "total": "120.00",
        "customer_name": "Bob", "created_at": "2026-05-10",
        "items": [{"sku": "SHOE-42", "name": "Running Shoe 42", "qty": 1}],
        "shipment": {
            "tracking_no": "LP009876543CN", "carrier": "4PX",
            "status": "delivered", "last_event": "Delivered",
            "last_event_at": "2026-05-18", "eta": "2026-05-18",
        },
    },
}


class MockEcommerceConnector:
    name = "mock"

    def __init__(self, *, orders: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._orders = dict(_DEFAULT_ORDERS)
        if orders:
            self._orders.update(orders)
        # 建物流单号 → 订单号 反查表
        self._by_tracking: Dict[str, str] = {}
        for ono, o in self._orders.items():
            tn = (o.get("shipment") or {}).get("tracking_no")
            if tn:
                self._by_tracking[str(tn)] = ono

    @staticmethod
    def _to_shipment(s: Optional[Dict[str, Any]]) -> Optional[ShipmentInfo]:
        if not s:
            return None
        return ShipmentInfo(
            tracking_no=str(s.get("tracking_no") or ""),
            carrier=str(s.get("carrier") or ""),
            status=str(s.get("status") or ""),
            last_event=str(s.get("last_event") or ""),
            last_event_at=str(s.get("last_event_at") or ""),
            eta=str(s.get("eta") or ""),
        )

    async def get_order(self, order_no: str) -> Optional[OrderInfo]:
        key = str(order_no or "").lstrip("#").strip()
        o = self._orders.get(key)
        if not o:
            return None
        return OrderInfo(
            order_no=key,
            status=str(o.get("status") or ""),
            currency=str(o.get("currency") or ""),
            total=str(o.get("total") or ""),
            items=list(o.get("items") or []),
            customer_name=str(o.get("customer_name") or ""),
            customer_email=str(o.get("customer_email") or ""),
            created_at=str(o.get("created_at") or ""),
            shipment=self._to_shipment(o.get("shipment")),
        )

    async def track_shipment(self, tracking_no: str) -> Optional[ShipmentInfo]:
        tn = str(tracking_no or "").strip()
        ono = self._by_tracking.get(tn)
        if ono:
            return self._to_shipment(self._orders[ono].get("shipment"))
        return None
