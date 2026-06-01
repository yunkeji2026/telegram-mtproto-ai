"""EcommerceConnector 协议（Phase D）。

connector 是「数据面」：只负责从外部电商系统（Shopify/WooCommerce/ERP/mock）
取只读数据，返回 OrderInfo / ShipmentInfo 或 None（查不到）。绝不写回外部系统。

实现方无需继承（structural typing），满足下列方法即可。
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from .models import OrderInfo, ShipmentInfo


@runtime_checkable
class EcommerceConnector(Protocol):
    name: str

    async def get_order(self, order_no: str) -> Optional[OrderInfo]:
        """按订单号查订单。查不到返回 None。"""
        ...

    async def track_shipment(self, tracking_no: str) -> Optional[ShipmentInfo]:
        """按物流单号查物流。查不到返回 None。"""
        ...
