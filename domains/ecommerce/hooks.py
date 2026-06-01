"""Ecommerce domain hooks.

电商域的轻量 hook：意图关键词补充、订单号/物流号识别（供 Phase D 工具层用）、
模糊 token、升级话术。保持与其它域包一致结构，逻辑全部域内自洽，核心零硬编码。
"""

from __future__ import annotations

import re
from typing import Dict, List, Set

from src.hooks.base import DomainHook


# 订单号：4~24 位字母数字（含 # 前缀、连字符），如 #1001（Shopify 默认）/ SP-20240601-001
_ORDER_NO_RE = re.compile(r"#?\b([A-Z]{0,4}[-_]?\d[\dA-Z\-_]{3,23})\b", re.IGNORECASE)
# 物流单号：10~24 位字母数字（更长，常全大写），如 LP00123456789CN
_TRACKING_RE = re.compile(r"\b([A-Z]{2}\d{6,}[A-Z]{0,2}|\d{10,24})\b")

_ORDER_KW = re.compile(
    r"订单|下单|order|订单号|order\s*(no|number|id)", re.IGNORECASE
)
_SHIPPING_KW = re.compile(
    r"物流|快递|运单|包裹|tracking|shipment|parcel|courier", re.IGNORECASE
)


class EcommerceDomainHook(DomainHook):
    """电商域 hook。"""

    def get_extra_intent_keywords(self) -> Dict[str, List[str]]:
        return {
            "order_query": ["订单", "下单", "order", "订单号", "我的订单"],
            "shipping_track": ["物流", "快递", "运单", "包裹", "tracking", "到货", "发货"],
            "return_refund": ["退款", "退货", "退钱", "refund", "return", "换货"],
            "product_query": ["商品", "产品", "库存", "尺码", "颜色", "product", "stock", "size"],
        }

    def get_ambiguous_tokens(self) -> Set[str]:
        # 这些短词在电商语境里可能干扰语言检测
        return {"size", "ok", "no"}

    def get_escalation_line(self) -> str:
        return "\n\n如需更快处理，可帮您转人工专员跟进哈～"

    def is_domain_metrics_query(self, text: str) -> bool:
        # 问具体订单/物流状态属于实时数据查询，应走工具层而非 KB
        t = text or ""
        if _ORDER_KW.search(t) or _SHIPPING_KW.search(t):
            return bool(extract_order_no(t) or extract_tracking_no(t))
        return False


# ── 供 Phase D 工具层复用的纯函数（无副作用，便于单测） ──────────────

def extract_order_no(text: str) -> str:
    """从文本里抽取首个疑似订单号。无则返回空串。"""
    m = _ORDER_NO_RE.search(str(text or ""))
    return m.group(1) if m else ""


def extract_tracking_no(text: str) -> str:
    """从文本里抽取首个疑似物流单号。无则返回空串。"""
    m = _TRACKING_RE.search(str(text or ""))
    return m.group(1) if m else ""
