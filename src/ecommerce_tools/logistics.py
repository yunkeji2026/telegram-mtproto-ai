"""LogisticsConnector — 物流聚合器连接层（Phase D / 物流增量）。

为什么独立于 EcommerceConnector：
- 电商系统（Shopify/Woo）擅长「按订单号查订单」，但多数**不提供**「按运单号直查物流轨迹」。
- 物流聚合器（AfterShip / 17Track）正相反：以运单号为中心，跨承运商查轨迹。
二者是不同数据源，强塞进一个 connector 会别扭。故抽独立协议，
``EcommerceToolService`` 可选注入：track_shipment 优先用物流连接器，回退电商连接器。

设计同 ShopifyConnector：注入式 http、只读、异常/非 200 → None（上层如实告知查不到）。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, runtime_checkable

from .models import ShipmentInfo

logger = logging.getLogger(__name__)

HttpGet = Callable[[str, dict], Awaitable[Any]]


@runtime_checkable
class LogisticsConnector(Protocol):
    name: str

    async def track(self, tracking_no: str, carrier: str = "") -> Optional[ShipmentInfo]:
        """按运单号（可选承运商）查物流轨迹。查不到返回 None。"""
        ...


class MockLogisticsConnector:
    """内存 mock：本地演示 / 测试 / 无真实物流接口时跑通。绝不联网。"""

    name = "mock_logistics"

    _DEFAULT = {
        "LP001234567CN": {"carrier": "YunExpress", "status": "in_transit",
                          "last_event": "Departed facility",
                          "last_event_at": "2026-05-22", "eta": "2026-05-30"},
        "1ZTEST999": {"carrier": "UPS", "status": "delivered",
                      "last_event": "Delivered", "last_event_at": "2026-05-18",
                      "eta": "2026-05-18"},
    }

    def __init__(self, *, shipments: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._data = dict(self._DEFAULT)
        if shipments:
            self._data.update(shipments)

    async def track(self, tracking_no: str, carrier: str = "") -> Optional[ShipmentInfo]:
        s = self._data.get(str(tracking_no or "").strip())
        if not s:
            return None
        return ShipmentInfo(
            tracking_no=str(tracking_no), carrier=str(s.get("carrier") or carrier or ""),
            status=str(s.get("status") or ""), last_event=str(s.get("last_event") or ""),
            last_event_at=str(s.get("last_event_at") or ""), eta=str(s.get("eta") or ""),
        )


class AfterShipConnector:
    """AfterShip v4 物流查询（注入式 http，只读）。

    端点：GET https://api.aftership.com/v4/trackings/{slug}/{tracking_number}
    鉴权头：aftership-api-key。slug 为承运商代码（可空，AfterShip 自动识别时用 detect）。
    缺 api_key → track 返回 None（上层回落/如实告知）。
    """

    name = "aftership"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.aftership.com/v4",
        http_get: Optional[HttpGet] = None,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = str(api_key or "")
        self._base = str(base_url or "https://api.aftership.com/v4").rstrip("/")
        self._http_get = http_get
        self._timeout = float(timeout or 15.0)

    def _headers(self) -> dict:
        return {"aftership-api-key": self._api_key,
                "Content-Type": "application/json"}

    async def _get(self, url: str) -> Optional[dict]:
        if not self._api_key:
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
            logger.debug("AfterShip GET 失败 %s: %s", url, ex)
            return None

    @staticmethod
    def _latest_checkpoint(tracking: dict) -> dict:
        cps = tracking.get("checkpoints") or []
        return cps[-1] if cps else {}

    async def track(self, tracking_no: str, carrier: str = "") -> Optional[ShipmentInfo]:
        tn = str(tracking_no or "").strip()
        if not tn:
            return None
        from urllib.parse import quote
        slug = quote(str(carrier).strip()) if carrier else "detect"
        url = f"{self._base}/trackings/{slug}/{quote(tn)}"
        data = await self._get(url)
        if not data:
            return None
        tracking = ((data.get("data") or {}).get("tracking")) or {}
        if not tracking:
            return None
        cp = self._latest_checkpoint(tracking)
        return ShipmentInfo(
            tracking_no=tn,
            carrier=str(tracking.get("slug") or carrier or ""),
            status=str(tracking.get("tag") or tracking.get("subtag") or ""),
            last_event=str(cp.get("message") or ""),
            last_event_at=str(cp.get("checkpoint_time") or tracking.get("updated_at") or ""),
            eta=str(tracking.get("expected_delivery") or ""),
        )


def build_logistics_connector(config: Optional[Dict[str, Any]]) -> Optional[Any]:
    """按配置构造物流连接器。未配置/未启用 → None（service 回退电商连接器）。"""
    cfg = config or {}
    if not cfg.get("enabled", False):
        return None
    provider = str(cfg.get("provider") or "mock").lower()
    if provider == "mock":
        return MockLogisticsConnector(shipments=cfg.get("mock_shipments") or None)
    if provider == "aftership":
        api_key = (cfg.get("aftership") or {}).get("api_key") or cfg.get("api_key") or ""
        if api_key:
            return AfterShipConnector(
                api_key=api_key,
                base_url=str((cfg.get("aftership") or {}).get("base_url")
                             or "https://api.aftership.com/v4"),
                timeout=float(cfg.get("timeout_sec") or 15),
            )
        logger.warning("logistics: provider=aftership 但缺 api_key，禁用物流连接器")
        return None
    logger.warning("logistics: provider=%s 暂未实现，禁用物流连接器", provider)
    return None
