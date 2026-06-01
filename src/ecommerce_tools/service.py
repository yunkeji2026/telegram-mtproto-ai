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
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

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
        cache_ttl_sec: float = 0.0,
        cache_max_entries: int = 512,
        logistics_connector: Optional[Any] = None,
    ) -> None:
        self._connector = connector
        # 物流聚合器（AfterShip/17Track）：按运单号查轨迹，优先于电商连接器；
        # None 则 track_shipment 回退用电商连接器（多数返 None → 如实告知查不到）。
        self._logistics = logistics_connector
        self._audit = audit_store
        self._timeout = float(timeout_sec or 8.0)
        # 短 TTL + 有界 LRU 内存缓存：避免同会话连续追问同一单号重复打 API。
        # ttl<=0 关闭；仅缓存 ok=True 结果（错误/超时不缓存，下次重试）。
        self._cache_ttl = float(cache_ttl_sec or 0.0)
        self._cache_max = max(1, int(cache_max_entries or 512))
        self._cache: "OrderedDict[Tuple[str, str], Tuple[float, ToolResult]]" = OrderedDict()
        self._cache_hits = 0
        self._cache_misses = 0

    @property
    def connector_name(self) -> str:
        return str(getattr(self._connector, "name", "unknown"))

    def _cache_key(self, kind: str, q: str) -> Tuple[str, str]:
        # 键带 connector 名，避免切换 provider 后命中旧数据；单号大小写/＃归一
        norm = q.lstrip("#").strip().lower()
        return (f"{self.connector_name}:{kind}", norm)

    def _cache_get(self, kind: str, q: str) -> Optional[ToolResult]:
        if self._cache_ttl <= 0:
            return None
        key = self._cache_key(kind, q)
        item = self._cache.get(key)
        if not item:
            self._cache_misses += 1
            return None
        exp, res = item
        if exp < time.monotonic():
            self._cache.pop(key, None)
            self._cache_misses += 1
            return None
        self._cache.move_to_end(key)  # LRU：命中即最近使用
        self._cache_hits += 1
        return res

    def cache_stats(self) -> Dict[str, Any]:
        """缓存命中率快照，供观测（hit_rate 仅在缓存启用且有查询时有意义）。"""
        total = self._cache_hits + self._cache_misses
        return {
            "enabled": self._cache_ttl > 0,
            "ttl_sec": self._cache_ttl,
            "max_entries": self._cache_max,
            "size": len(self._cache),
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate": (self._cache_hits / total) if total else 0.0,
        }

    def _cache_put(self, kind: str, q: str, res: ToolResult) -> None:
        if self._cache_ttl <= 0 or not res.ok:
            return
        now = time.monotonic()
        key = self._cache_key(kind, q)
        self._cache[key] = (now + self._cache_ttl, res)
        self._cache.move_to_end(key)
        # 先顺手清掉已过期项（最早插入的在前），再按容量上界 LRU 淘汰
        while self._cache:
            k0 = next(iter(self._cache))
            if self._cache[k0][0] < now:
                self._cache.pop(k0, None)
            else:
                break
        while len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)

    async def lookup_order(self, order_no: str, *, by: str = "") -> ToolResult:
        q = str(order_no or "").strip()
        if not q:
            return ToolResult(ok=False, found=False, kind="order", query=q,
                              source=self.connector_name, error="empty_order_no")
        cached = self._cache_get("order", q)
        if cached is not None:
            self._audit_call("ecommerce_order_lookup", q, cached, by, cache_hit=True)
            return cached
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
        self._cache_put("order", q, res)
        self._audit_call("ecommerce_order_lookup", q, res, by)
        return res

    async def track_shipment(self, tracking_no: str, *, by: str = "") -> ToolResult:
        q = str(tracking_no or "").strip()
        if not q:
            return ToolResult(ok=False, found=False, kind="shipment", query=q,
                              source=self.connector_name, error="empty_tracking_no")
        cached = self._cache_get("shipment", q)
        if cached is not None:
            self._audit_call("ecommerce_shipment_track", q, cached, by, cache_hit=True)
            return cached
        try:
            ship, source = await self._do_track(q)
        except asyncio.TimeoutError:
            return self._fail("shipment", q, "timeout", by)
        except Exception as ex:
            return self._fail("shipment", q, f"{type(ex).__name__}: {ex}", by)
        if ship is None:
            res = ToolResult(ok=True, found=False, kind="shipment", query=q,
                             source=source)
        else:
            res = ToolResult(ok=True, found=True, kind="shipment", query=q,
                             data=ship.to_dict(), source=source)
        self._cache_put("shipment", q, res)
        self._audit_call("ecommerce_shipment_track", q, res, by)
        return res

    async def _do_track(self, q: str):
        """物流查询：优先物流连接器；返回 None 则回退电商连接器。返回 (ship, source)。"""
        if self._logistics is not None:
            lname = str(getattr(self._logistics, "name", "logistics"))
            ship = await asyncio.wait_for(self._logistics.track(q), timeout=self._timeout)
            if ship is not None:
                return ship, lname
            # 物流聚合器查不到 → 回退电商连接器（多数仍 None，但保留语义）
        ship = await asyncio.wait_for(
            self._connector.track_shipment(q), timeout=self._timeout
        )
        return ship, self.connector_name

    def _fail(self, kind: str, q: str, err: str, by: str) -> ToolResult:
        res = ToolResult(ok=False, found=False, kind=kind, query=q,
                         source=self.connector_name, error=err)
        self._audit_call(f"ecommerce_{kind}_error", q, res, by)
        return res

    def _audit_call(self, action: str, query: str, res: ToolResult, by: str,
                    *, cache_hit: bool = False) -> None:
        if self._audit is None or not hasattr(self._audit, "log"):
            return
        try:
            summary = {"found": res.found, "ok": res.ok,
                       "source": res.source, "error": res.error,
                       "cache_hit": cache_hit}
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
    if provider == "shopify":
        sh = cfg.get("shopify") or {}
        shop = sh.get("shop") or sh.get("domain") or ""
        token = sh.get("access_token") or sh.get("token") or ""
        if shop and token:
            from .shopify_connector import ShopifyConnector
            return ShopifyConnector(
                shop=shop, access_token=token,
                api_version=str(sh.get("api_version") or "2024-01"),
                timeout=float(cfg.get("timeout_sec") or 15),
            )
        logger.warning("ecommerce: provider=shopify 但缺 shop/access_token，回落 mock")
        return MockEcommerceConnector()
    # 扩展点：woocommerce / 自有 ERP → 实例化对应 connector
    # 未实现的 provider 一律回落 mock，保证服务可用且不崩
    logger.warning("ecommerce connector provider=%s 暂未实现，回落 mock", provider)
    return MockEcommerceConnector()
