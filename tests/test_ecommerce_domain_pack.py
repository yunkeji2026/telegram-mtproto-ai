"""ecommerce 域包完整性 + hook 抽号测试（Phase D0）。"""

from pathlib import Path

import yaml

from domains.ecommerce.hooks import (
    EcommerceDomainHook,
    extract_order_no,
    extract_tracking_no,
)

_ROOT = Path(__file__).resolve().parent.parent / "domains" / "ecommerce"


def test_ecommerce_now_has_persona():
    p = _ROOT / "persona.yaml"
    assert p.exists()
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["name"] == "小乐"
    assert data["identity"]["deny_ai"] is True


def test_ecommerce_manifest_declares_hooks():
    m = yaml.safe_load((_ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    assert m.get("hooks") is True


def test_hook_extra_intent_keywords():
    hook = EcommerceDomainHook()
    kw = hook.get_extra_intent_keywords()
    assert "shipping_track" in kw
    assert "return_refund" in kw


def test_extract_order_no():
    assert extract_order_no("我的订单 #1001 还没发货") == "1001"
    assert extract_order_no("order SP-20240601-001 status") == "SP-20240601-001"
    assert extract_order_no("你好啊") == ""


def test_extract_tracking_no():
    assert extract_tracking_no("运单号 LP001234567CN") == "LP001234567CN"
    assert extract_tracking_no("没有单号") == ""


def test_is_domain_metrics_query_true_with_order():
    hook = EcommerceDomainHook()
    assert hook.is_domain_metrics_query("订单 1001 到哪了") is True
    # 没有具体单号的泛问不算实时查询
    assert hook.is_domain_metrics_query("怎么查物流") is False
