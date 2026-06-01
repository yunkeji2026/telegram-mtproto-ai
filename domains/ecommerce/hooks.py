"""Ecommerce domain hooks.

电商域的轻量 hook：意图关键词补充、订单号/物流号识别（供 Phase D 工具层用）、
模糊 token、升级话术。保持与其它域包一致结构，逻辑全部域内自洽，核心零硬编码。
"""

from __future__ import annotations

from typing import Dict, List, Set

from src.hooks.base import DomainHook

# 订单号/物流号正则与抽取已收敛到单一真源（避免跨文件正则漂移）
from src.ecommerce_tools.extract import (
    _ORDER_KW,
    _SHIPPING_KW,
    extract_order_no,
    extract_tracking_no,
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


# extract_order_no / extract_tracking_no 见 src.ecommerce_tools.extract（单一真源），
# 此处通过模块顶部 import 暴露，外部 `from domains.ecommerce.hooks import extract_order_no` 仍可用。
__all__ = ["EcommerceDomainHook", "extract_order_no", "extract_tracking_no"]
