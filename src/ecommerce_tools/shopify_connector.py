"""ShopifyConnector — 真实 Shopify Admin REST 订单/物流查询（Phase D / P1-a）。

设计要点：
- **注入式 http**：构造可传 ``http_get(url, headers) -> resp``（resp 有 .status_code / .json()）；
  不传则惰性用 httpx.AsyncClient。单测注入 fake http_get 即可，**不联网**。
- 只读：仅 GET orders，绝不写回 Shopify。
- get_order 按订单号(name)查；track_shipment Shopify Admin 无「按运单号直查」端点 →
  返回 None（上层 ToolResult.found=False → 如实告知查不到，勿编造）。
- 任何异常/非 200 → 返回 None（service 层包成 not_found / error，不崩）。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from .models import OrderInfo, ShipmentInfo

logger = logging.getLogger(__name__)

HttpGet = Callable[[str, dict], Awaitable[Any]]


class ShopifyConnector:
    name = "shopify"

    def __init__(
        self,
        *,
        shop: str,
        access_token: str,
        api_version: str = "2024-01",
        http_get: Optional[HttpGet] = None,
        timeout: float = 15.0,
    ) -> None:
        # 归一 shop：去协议/尾斜杠，补 .myshopify.com（若给的是裸 handle）
        s = str(shop or "").strip().replace("https://", "").replace("http://", "").rstrip("/")
        if s and "." not in s:
            s = f"{s}.myshopify.com"
        self._shop = s
        self._token = str(access_token or "")
        self._api_version = str(api_version or "2024-01")
        self._http_get = http_get
        self._timeout = float(timeout or 15.0)

    def _base(self) -> str:
        return f"https://{self._shop}/admin/api/{self._api_version}"

    def _headers(self) -> dict:
        return {"X-Shopify-Access-Token": self._token,
                "Content-Type": "application/json"}

    async def _get(self, url: str) -> Optional[dict]:
        if not self._shop or not self._token:
            return None
        try:
            if self._http_get is not None:
                resp = await self._http_get(url, self._headers())
            else:
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.get(url, headers=self._headers())
            if getattr(resp, "status_code", 0) != 200:
                return None
            return resp.json()
        except Exception as ex:
            logger.debug("Shopify GET 失败 %s: %s", url, ex)
            return None

    @staticmethod
    def _map_shipment(fulfillments: list) -> Optional[ShipmentInfo]:
        if not fulfillments:
            return None
        f = fulfillments[0] or {}
        tn = f.get("tracking_number") or ""
        if not tn and isinstance(f.get("tracking_numbers"), list) and f["tracking_numbers"]:
            tn = f["tracking_numbers"][0]
        return ShipmentInfo(
            tracking_no=str(tn or ""),
            carrier=str(f.get("tracking_company") or ""),
            status=str(f.get("shipment_status") or f.get("status") or ""),
            last_event=str(f.get("status") or ""),
            last_event_at=str(f.get("updated_at") or ""),
            eta="",
        )

    @staticmethod
    def _customer_name(order: dict) -> str:
        c = order.get("customer") or {}
        name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x).strip()
        return name or str(order.get("email") or "")

    def _map_order(self, order: dict) -> OrderInfo:
        items = [
            {"sku": li.get("sku", ""), "name": li.get("title", ""),
             "qty": li.get("quantity", 0)}
            for li in (order.get("line_items") or [])
        ]
        status = (order.get("fulfillment_status")
                  or order.get("financial_status") or "")
        return OrderInfo(
            order_no=str(order.get("name") or order.get("order_number") or "").lstrip("#"),
            status=str(status),
            currency=str(order.get("currency") or ""),
            total=str(order.get("total_price") or ""),
            items=items,
            customer_name=self._customer_name(order),
            customer_email=str(order.get("email") or ""),
            created_at=str(order.get("created_at") or ""),
            shipment=self._map_shipment(order.get("fulfillments") or []),
        )

    async def get_order(self, order_no: str) -> Optional[OrderInfo]:
        key = str(order_no or "").strip()
        if not key:
            return None
        # Shopify 订单号 name 带 # 前缀；按 name 查
        name = key if key.startswith("#") else f"#{key}"
        from urllib.parse import quote
        url = f"{self._base()}/orders.json?status=any&name={quote(name)}"
        data = await self._get(url)
        if not data:
            return None
        orders = data.get("orders") or []
        if not orders:
            return None
        return self._map_order(orders[0])

    async def track_shipment(self, tracking_no: str) -> Optional[ShipmentInfo]:
        # Shopify Admin 无「按运单号直查物流」端点；返回 None → 上层如实告知查不到。
        return None
