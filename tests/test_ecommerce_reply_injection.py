"""P1-b — 电商真实事实注入回复生成链路 + 订单号抽取单一真源收敛。

覆盖：
- AIClient._maybe_inject_ecommerce_facts 的门槛逻辑（命中/未命中/无意图/无单号/无服务）
- _build_system_instruction 在 context 带 _ecommerce_facts 时输出高优先级事实块
- domains.ecommerce.hooks 的抽取函数与 src.ecommerce_tools.extract 为同一真源
"""

from __future__ import annotations

from src.ai.ai_client import AIClient
from src.ecommerce_tools.service import EcommerceToolService
from src.ecommerce_tools.mock_connector import MockEcommerceConnector


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


_MOCK_ORDERS = {
    "1001": {
        "order_no": "1001",
        "status": "shipped",
        "currency": "USD",
        "total": "59.90",
        "shipment": {"carrier": "DHL", "status": "in_transit",
                     "last_event": "出库", "tracking_no": "TRK1"},
    }
}


def _svc_with_order() -> EcommerceToolService:
    conn = MockEcommerceConnector(orders=_MOCK_ORDERS)
    return EcommerceToolService(conn, audit_store=None)


def _svc_empty() -> EcommerceToolService:
    return EcommerceToolService(MockEcommerceConnector(orders={}), audit_store=None)


async def test_inject_when_order_found():
    client = AIClient(_Cfg())
    client.set_ecommerce_tools(_svc_with_order())
    ctx: dict = {}
    await client._maybe_inject_ecommerce_facts("我的订单 #1001 到哪了？", ctx)
    facts = ctx.get("_ecommerce_facts") or ""
    assert "1001" in facts
    assert "shipped" in facts


async def test_inject_not_found_with_order_intent_emits_guard():
    client = AIClient(_Cfg())
    client.set_ecommerce_tools(_svc_empty())
    ctx: dict = {}
    await client._maybe_inject_ecommerce_facts("订单号 #9999 查不到吗", ctx)
    facts = ctx.get("_ecommerce_facts") or ""
    assert "勿编造" in facts          # 反幻觉守卫已注入
    assert "9999" in facts


async def test_no_inject_when_number_without_order_intent():
    """随机数字（如金额）且无订单/物流意图 → 不注入，避免噪声。"""
    client = AIClient(_Cfg())
    client.set_ecommerce_tools(_svc_empty())
    ctx: dict = {}
    await client._maybe_inject_ecommerce_facts("我转了 12345 块给你", ctx)
    assert "_ecommerce_facts" not in ctx


async def test_no_inject_when_no_order_no():
    client = AIClient(_Cfg())
    client.set_ecommerce_tools(_svc_with_order())
    ctx: dict = {}
    await client._maybe_inject_ecommerce_facts("在吗在吗", ctx)
    assert "_ecommerce_facts" not in ctx


async def test_no_inject_when_service_absent():
    client = AIClient(_Cfg())  # 未注入 ecommerce_tools
    ctx: dict = {}
    await client._maybe_inject_ecommerce_facts("订单 #1001", ctx)
    assert "_ecommerce_facts" not in ctx


async def test_respects_upstream_facts():
    client = AIClient(_Cfg())
    client.set_ecommerce_tools(_svc_with_order())
    ctx = {"_ecommerce_facts": "上游已注入"}
    await client._maybe_inject_ecommerce_facts("订单 #1001", ctx)
    assert ctx["_ecommerce_facts"] == "上游已注入"


def test_system_instruction_includes_facts_block():
    client = AIClient(_Cfg())
    prompt = client._build_system_instruction(
        {"reply_lang": "zh", "_ecommerce_facts": "[事实] 订单 1001 状态=shipped"}
    )
    assert "电商实时事实" in prompt
    assert "1001" in prompt
    assert "严禁编造" in prompt


def test_system_instruction_no_facts_block_when_absent():
    client = AIClient(_Cfg())
    prompt = client._build_system_instruction({"reply_lang": "zh"})
    assert "电商实时事实" not in prompt


def _svc_default() -> EcommerceToolService:
    # 用内置默认单：1001 运单 LP001234567CN（YunExpress in_transit）
    return EcommerceToolService(MockEcommerceConnector(), audit_store=None)


async def test_inject_shipment_facts_when_distinct_tracking_found():
    """消息同时含订单号 + 独立运单号 → 订单与物流事实都注入。"""
    client = AIClient(_Cfg())
    client.set_ecommerce_tools(_svc_default())  # 默认单 1001 运单 LP001234567CN
    ctx: dict = {}
    await client._maybe_inject_ecommerce_facts(
        "订单 #1001 的运单 LP001234567CN 到哪了", ctx
    )
    facts = ctx.get("_ecommerce_facts") or ""
    assert "1001" in facts
    assert "LP001234567CN" in facts


async def test_no_shipment_guard_when_tracking_not_found():
    """运单查不到不注入「查不到」守卫（避免每条长数字误报），仅保留订单事实。"""
    client = AIClient(_Cfg())
    client.set_ecommerce_tools(_svc_default())
    ctx: dict = {}
    await client._maybe_inject_ecommerce_facts(
        "订单 #1001 运单 LP999999999XX 呢", ctx
    )
    facts = ctx.get("_ecommerce_facts") or ""
    assert "1001" in facts
    assert "LP999999999XX" not in facts


async def test_classified_intent_authoritative_suppresses_false_positive():
    """消息含 order 字样但已分类意图是闲聊 → 不注入「查不到」守卫(降误报)。"""
    client = AIClient(_Cfg())
    client.set_ecommerce_tools(_svc_empty())
    ctx: dict = {"intent": "small_talk"}
    await client._maybe_inject_ecommerce_facts("my order 9999 lol", ctx)
    assert "_ecommerce_facts" not in ctx


async def test_classified_ecom_intent_opens_guard():
    """已分类电商意图 → 查不到也注入守卫(即便关键词正则可能漏)。"""
    client = AIClient(_Cfg())
    client.set_ecommerce_tools(_svc_empty())
    ctx: dict = {"intent": "order_query"}
    await client._maybe_inject_ecommerce_facts("#9999", ctx)
    facts = ctx.get("_ecommerce_facts") or ""
    assert "勿编造" in facts


async def test_found_order_injects_regardless_of_intent():
    """查得到订单始终注入，与意图门槛无关。"""
    client = AIClient(_Cfg())
    client.set_ecommerce_tools(_svc_with_order())
    ctx: dict = {"intent": "small_talk"}
    await client._maybe_inject_ecommerce_facts("#1001", ctx)
    assert "1001" in (ctx.get("_ecommerce_facts") or "")


def test_is_ecom_intent_labels():
    from src.ecommerce_tools.extract import is_ecom_intent
    assert is_ecom_intent("order_query") is True
    assert is_ecom_intent("status_check") is True
    assert is_ecom_intent("物流") is True
    assert is_ecom_intent("order_status") is True   # 子串兜底
    assert is_ecom_intent("small_talk") is False
    assert is_ecom_intent("") is False


def test_extractor_single_source_of_truth():
    """domains.ecommerce.hooks 的抽取函数即 src.ecommerce_tools.extract 同一实现。"""
    from domains.ecommerce.hooks import extract_order_no as h_order
    from domains.ecommerce.hooks import extract_tracking_no as h_track
    from src.ecommerce_tools.extract import extract_order_no, extract_tracking_no
    assert h_order is extract_order_no
    assert h_track is extract_tracking_no
    assert extract_order_no("订单 #1001 呢") == "1001"
