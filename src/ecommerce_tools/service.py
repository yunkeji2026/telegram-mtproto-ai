"""EcommerceToolService — 电商工具服务（Phase D1/D2）。

职责：
- 包装 connector，提供 lookup_order / track_shipment，统一返回 ToolResult。
- 事实校验语义：connector 返回 None → found=False，调用方必须如实告知查不到。
- 审计：每次工具调用写 audit_store（agent_actions 闭环），best-effort。
- connector 异常/超时不抛：返回 ok=False 的 ToolResult，回复引擎据此降级。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from .models import ToolResult
from .mock_connector import MockEcommerceConnector

logger = logging.getLogger(__name__)


class EcommerceToolService:
    def __init__(
        self,
        connector: Any,
        *,
        audit_store: Optional[Any] = None,
        timeout_sec: float = 8.0,
    ) -> None:
        self._connector = connector
        self._audit = audit_store
        self._timeout = float(timeout_sec or 8.0)

    @property
    def connector_name(self) -> str:
        return str(getattr(self._connector, "name", "unknown"))

    async def lookup_order(self, order_no: str, *, by: str = "") -> ToolResult:
        q = str(order_no or "").strip()
        if not q:
            return ToolResult(ok=False, found=False, kind="order", query=q,
                              source=self.connector_name, error="empty_order_no")
        try:
            order = await asyncio.wait_for(self._connector.get_order(q), timeout=self._timeout)
        except asyncio.TimeoutError:
            return self._fail("order", q, "timeout", by)
        except Exception as ex:
            return self._fail("order", q, f"{type(ex).__name__}: {ex}", by)
        if order is None:
            res = ToolResult(ok=True, found=False, kind="order", query=q,
                             source=self.connector_name)
        else:
            res = ToolResult(ok=True, found=True, kind="order", query=q,
                             data=order.to_dict(), source=self.connector_name)
        self._audit_call("ecommerce_order_lookup", q, res, by)
        return res

    async def track_shipment(self, tracking_no: str, *, by: str = "") -> ToolResult:
        q = str(tracking_no or "").strip()
        if not q:
            return ToolResult(ok=False, found=False, kind="shipment", query=q,
                              source=self.connector_name, error="empty_tracking_no")
        try:
            ship = await asyncio.wait_for(
                self._connector.track_shipment(q), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            return self._fail("shipment", q, "timeout", by)
        except Exception as ex:
            return self._fail("shipment", q, f"{type(ex).__name__}: {ex}", by)
        if ship is None:
            res = ToolResult(ok=True, found=False, kind="shipment", query=q,
                             source=self.connector_name)
        else:
            res = ToolResult(ok=True, found=True, kind="shipment", query=q,
                             data=ship.to_dict(), source=self.connector_name)
        self._audit_call("ecommerce_shipment_track", q, res, by)
        return res

    def _fail(self, kind: str, q: str, err: str, by: str) -> ToolResult:
        res = ToolResult(ok=False, found=False, kind=kind, query=q,
                         source=self.connector_name, error=err)
        self._audit_call(f"ecommerce_{kind}_error", q, res, by)
        return res

    def _audit_call(self, action: str, query: str, res: ToolResult, by: str) -> None:
        if self._audit is None or not hasattr(self._audit, "log"):
            return
        try:
            summary = {"found": res.found, "ok": res.ok,
                       "source": res.source, "error": res.error}
            self._audit.log(
                user_id=by or "system",
                action=action,
                target=query,
                new_val=json.dumps(summary, ensure_ascii=False),
            )
        except Exception:
            logger.debug("ecommerce tool 审计写入失败", exc_info=True)


def build_connector(config: Optional[Dict[str, Any]]) -> Any:
    """按配置构造 connector。当前支持 mock；shopify/woocommerce 为后续扩展点。"""
    cfg = config or {}
    provider = str(cfg.get("provider") or "mock").lower()
    if provider == "mock":
        return MockEcommerceConnector(orders=cfg.get("mock_orders") or None)
    # 扩展点：provider == "shopify" / "woocommerce" → 实例化对应 connector
    # 未实现的 provider 一律回落 mock，保证服务可用且不崩
    logger.warning("ecommerce connector provider=%s 暂未实现，回落 mock", provider)
    return MockEcommerceConnector()
