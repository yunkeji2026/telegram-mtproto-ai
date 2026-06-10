"""Phase 11 海外告警渠道（Telegram/WhatsApp/Messenger）+ webhook 覆盖层单测。"""

from __future__ import annotations

import json

from src.inbox.webhook_notifier import (
    WebhookNotifier,
    _build_chat_body,
    _plainify,
    _resolve_chat_endpoint,
)


def test_plainify_strips_markdown_and_relative_links():
    text = "**平台**: telegram\n[👥 前往账号管理](/workspace/unified-inbox)"
    out = _plainify(text)
    assert "**" not in out
    assert "前往账号管理" in out
    # 相对链接只保留文字
    assert "/workspace/unified-inbox" not in out


def test_plainify_keeps_absolute_links():
    out = _plainify("[文档](https://example.com/d)")
    assert "https://example.com/d" in out


def test_resolve_endpoint_telegram_from_token():
    url = _resolve_chat_endpoint("telegram", "", "123:ABC")
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"


def test_resolve_endpoint_messenger_from_token():
    url = _resolve_chat_endpoint("messenger", "", "PAGETOKEN")
    assert url.startswith("https://graph.facebook.com/")
    assert "access_token=PAGETOKEN" in url


def test_resolve_endpoint_whatsapp_needs_url():
    # whatsapp 无法仅凭 token 推断（需要 phone-id）
    assert _resolve_chat_endpoint("whatsapp", "", "TOKEN") == ""
    assert _resolve_chat_endpoint("whatsapp", "https://x/y", "TOKEN") == "https://x/y"


def test_resolve_endpoint_explicit_url_wins():
    assert _resolve_chat_endpoint("telegram", "https://custom/h", "tok") == "https://custom/h"


def test_build_chat_body_telegram():
    body, headers = _build_chat_body("telegram", "hi", "-100123", "")
    d = json.loads(body)
    assert d["chat_id"] == "-100123"
    assert d["text"] == "hi"
    assert "Authorization" not in headers


def test_build_chat_body_whatsapp_has_bearer():
    body, headers = _build_chat_body("whatsapp", "hi", "8613800000000", "TK")
    d = json.loads(body)
    assert d["messaging_product"] == "whatsapp"
    assert d["to"] == "8613800000000"
    assert d["text"]["body"] == "hi"
    assert headers["Authorization"] == "Bearer TK"


def test_build_chat_body_messenger():
    body, _ = _build_chat_body("messenger", "hi", "PSID1", "")
    d = json.loads(body)
    assert d["recipient"]["id"] == "PSID1"
    assert d["message"]["text"] == "hi"


def test_notifier_reload_rebuilds_matchers():
    n = WebhookNotifier(config=[])
    assert len(n._matchers) == 0
    n.reload([{
        "name": "tg", "format": "telegram", "token": "t", "target": "c",
        "events": ["autoreply_alert"],
    }])
    assert len(n._matchers) == 1
    assert n._matchers[0]["fmt"] == "telegram"
    assert n._matchers[0]["target"] == "c"
    # 禁用项不进匹配器
    n.reload([{"name": "x", "format": "telegram", "enabled": False,
               "events": ["all"]}])
    assert len(n._matchers) == 0


async def test_send_test_missing_target_returns_error():
    n = WebhookNotifier(config=[])
    res = await n.send_test({
        "name": "tg", "format": "telegram", "token": "tok", "target": "",
        "events": ["autoreply_alert"],
    })
    assert res["ok"] is False  # 缺 target → 计为错误


def test_store_sanitize_and_effective(tmp_path):
    from src.integrations import notify_webhooks_store as ws
    ws.set_store_path(tmp_path / "wh.json")
    try:
        # 覆盖层不存在 → 沿用 config.yaml
        base = {"notify": {"webhooks": [{"name": "y", "format": "json",
                                         "events": ["all"]}]}}
        assert ws.effective_webhooks(base)[0]["name"] == "y"
        # 保存覆盖层后整段取代
        saved = ws.save_list([
            {"name": "tg", "format": "telegram", "token": "secret-token",
             "target": "-100", "events": ["autoreply_alert", "不存在的别名"]},
            {"name": "bad", "format": "不存在的格式", "events": []},
        ])
        assert saved[0]["format"] == "telegram"
        assert saved[0]["events"] == ["autoreply_alert"]  # 非法别名剔除
        assert saved[1]["format"] == "json"  # 非法 format 回落
        assert saved[1]["events"] == ["autoreply_alert"]  # 空 events 兜底
        eff = ws.effective_webhooks(base)
        assert len(eff) == 2 and eff[0]["name"] == "tg"
        # 脱敏：token 不明文外泄
        masked = ws.mask(eff)
        assert masked[0]["token"].endswith("***")
        assert masked[0]["token_set"] is True
    finally:
        ws.set_store_path(tmp_path / "wh.json")
