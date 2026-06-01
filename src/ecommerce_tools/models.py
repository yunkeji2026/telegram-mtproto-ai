"""电商工具层数据模型（Phase D）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ShipmentInfo:
    tracking_no: str = ""
    carrier: str = ""
    status: str = ""          # 如 in_transit / delivered / pending / exception
    last_event: str = ""
    last_event_at: str = ""
    eta: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tracking_no": self.tracking_no,
            "carrier": self.carrier,
            "status": self.status,
            "last_event": self.last_event,
            "last_event_at": self.last_event_at,
            "eta": self.eta,
        }


@dataclass
class OrderInfo:
    order_no: str
    status: str = ""          # 如 paid / shipped / delivered / refunding / cancelled
    currency: str = ""
    total: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)
    customer_name: str = ""
    customer_email: str = ""
    created_at: str = ""
    shipment: Optional[ShipmentInfo] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_no": self.order_no,
            "status": self.status,
            "currency": self.currency,
            "total": self.total,
            "items": list(self.items),
            "customer_name": self.customer_name,
            "customer_email": self.customer_email,
            "created_at": self.created_at,
            "shipment": self.shipment.to_dict() if self.shipment else None,
        }


@dataclass
class ToolResult:
    """工具调用统一返回。

    found=False 时 data 为空 → 调用方（回复引擎）必须如实告知「查不到」，
    严禁编造订单/物流/库存（Phase D2 事实校验的核心约束）。
    """

    ok: bool
    found: bool
    kind: str                 # order / shipment / stock
    query: str = ""
    data: Optional[Dict[str, Any]] = None
    source: str = ""          # connector 名（mock / shopify / ...）
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "found": self.found,
            "kind": self.kind,
            "query": self.query,
            "data": self.data,
            "source": self.source,
            "error": self.error,
        }

    def to_context_facts(self) -> str:
        """把结构化结果转成可注入 prompt 的事实串；查不到则明确标注未知。"""
        if not self.ok:
            return f"[工具错误] {self.kind} 查询失败：{self.error}（请如实告知系统暂时无法查询，勿编造）"
        if not self.found:
            return f"[事实] 未查到 {self.kind}「{self.query}」的记录（必须如实告知客户查不到，勿编造状态）"
        d = self.data or {}
        if self.kind == "order":
            ship = d.get("shipment") or {}
            ship_str = ""
            if ship:
                _tn = ship.get("tracking_no") or ""
                _tn_str = f"，运单号={_tn}" if _tn else ""
                ship_str = (
                    f"，物流{ship.get('carrier','')} {ship.get('status','')}"
                    f"（{ship.get('last_event','')}）{_tn_str}"
                )
            return (
                f"[事实] 订单 {d.get('order_no','')} 状态={d.get('status','')}，"
                f"金额={d.get('total','')}{d.get('currency','')}{ship_str}。"
                "只可基于以上事实回复，勿编造未列出的信息。"
            )
        if self.kind == "shipment":
            return (
                f"[事实] 物流单 {d.get('tracking_no','')} 承运={d.get('carrier','')}，"
                f"状态={d.get('status','')}，最新={d.get('last_event','')}，预计={d.get('eta','')}。"
                "只可基于以上事实回复。"
            )
        return f"[事实] {self.kind}: {d}"
