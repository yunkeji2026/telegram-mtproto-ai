"""订单号/物流号抽取 + 电商意图判定 —— 全仓单一真源（P1-b 收敛）。

此前 domains/ecommerce/hooks.py 与 src/web/routes/unified_inbox_routes.py 各有一份
相同正则，改一处易漏（Shopify 4 位单号兼容曾要同时改两处）。统一收敛到此模块，
纯函数、无副作用、零外部依赖，便于单测与跨层复用（含 src/ai 回复链路）。
"""

from __future__ import annotations

import re

# 订单号：4~24 位字母数字（含 # 前缀、连字符），如 #1001（Shopify 默认）/ SP-20240601-001
_ORDER_NO_RE = re.compile(r"#?\b([A-Z]{0,4}[-_]?\d[\dA-Z\-_]{3,23})\b", re.IGNORECASE)
# 物流单号：10~24 位字母数字（更长，常全大写），如 LP00123456789CN
_TRACKING_RE = re.compile(r"\b([A-Z]{2}\d{6,}[A-Z]{0,2}|\d{10,24})\b")

_ORDER_KW = re.compile(r"订单|下单|order|订单号|order\s*(no|number|id)", re.IGNORECASE)
_SHIPPING_KW = re.compile(
    r"物流|快递|运单|包裹|查件|发货|到货|tracking|shipment|parcel|courier",
    re.IGNORECASE,
)


def extract_order_no(text: str) -> str:
    """从文本里抽取首个疑似订单号。无则返回空串。"""
    m = _ORDER_NO_RE.search(str(text or ""))
    return m.group(1) if m else ""


def extract_tracking_no(text: str) -> str:
    """从文本里抽取首个疑似物流单号。无则返回空串。"""
    m = _TRACKING_RE.search(str(text or ""))
    return m.group(1) if m else ""


def has_order_intent(text: str) -> bool:
    """是否含订单/物流意图关键词（用于给「查不到」事实注入加门槛，降低误报）。"""
    t = str(text or "")
    return bool(_ORDER_KW.search(t) or _SHIPPING_KW.search(t))


# 已分类意图里属于电商范畴的标签（skill_manager / 域包 hook / LLM 产出）
_ECOM_INTENTS = {
    "order_query", "status_check", "price_check",
    "shipping_track", "return_refund", "product_query",
}
# 兜底子串匹配（兼容中文/LLM 自由文本意图，如「物流」「退款」「order_status」）
_ECOM_INTENT_SUBSTR = (
    "order", "ship", "track", "refund", "return",
    "物流", "订单", "快递", "运单", "退款", "退货", "发货", "到货",
)


def is_ecom_intent(intent_label: str) -> bool:
    """已分类意图是否属于电商范畴。

    比关键词正则更准：调用方有分类意图时应以此为权威门槛，
    可避免「消息恰好含 order 字样但意图是闲聊」的误报。
    """
    s = str(intent_label or "").strip().lower()
    if not s:
        return False
    if s in _ECOM_INTENTS:
        return True
    return any(tok in s for tok in _ECOM_INTENT_SUBSTR)
