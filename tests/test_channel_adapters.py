"""Channel Adapter 契约 + 隔离单测（Phase A2）。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.inbox.channel_adapters import (
    ChannelAdapter,
    ChannelSendError,
    LineInboxAdapter,
    WhatsAppInboxAdapter,
    MessengerInboxAdapter,
    TelegramInboxAdapter,
    WebInboxAdapter,
    collect_chats_via_adapters,
    default_inbox_adapters,
    send_via_adapters,
    status_via_adapters,
)


class _FakeLineSvc:
    account_id = "line1"
    _merged_cfg = {"label": "LINE One"}

    def list_chats(self, limit):
        return [{"chat_key": "u1", "name": "Alice",
                 "last_peer_text": "hi", "last_ts": 10, "unread_count": 2}]


class _FakeWaSvc:
    account_id = "wa1"
    _merged_cfg = {}

    def list_pending(self, status, limit):
        return [{"chat_key": "w1", "peer_name": "Bob",
                 "peer_text": "yo", "ts": 20}]


class _FakeMsgrSvc:
    def list_approvals(self, status, limit):
        return [{"account_id": "m1", "chat_key": "c1",
                 "name": "Carol", "peer_text": "hello", "ts": 30}]


class _FakeTgClient:
    _recent_messages = [{"chat_id": 99, "user_name": "Dave",
                         "text": "sup", "ts": 40}]


def _req(**state):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def test_default_registry_has_all_platforms():
    adapters = default_inbox_adapters()
    assert [a.platform for a in adapters] == [
        "line", "whatsapp", "messenger", "telegram", "web", "protocol",
    ]
    for a in adapters:
        assert isinstance(a, ChannelAdapter)  # runtime_checkable Protocol


def test_line_adapter_normalizes():
    req = _req(line_rpa_services=[_FakeLineSvc()])
    chats = LineInboxAdapter().collect_chats(req, 20)
    assert len(chats) == 1
    c = chats[0]
    assert c["platform"] == "line"
    assert c["account_label"] == "LINE One"
    assert c["conversation_id"] == "line:line1:u1"
    assert c["unread"] == 2
    assert c["last_message"]["text"] == "hi"


def test_whatsapp_adapter_maps_pending():
    req = _req(whatsapp_rpa_services=[_FakeWaSvc()])
    chats = WhatsAppInboxAdapter().collect_chats(req, 20)
    assert len(chats) == 1 and chats[0]["platform"] == "whatsapp"
    assert chats[0]["name"] == "Bob"


def test_messenger_adapter_maps_approvals():
    req = _req(messenger_rpa_service=_FakeMsgrSvc())
    chats = MessengerInboxAdapter().collect_chats(req, 20)
    assert len(chats) == 1 and chats[0]["account_id"] == "m1"


def test_telegram_adapter_maps_recent():
    req = _req(telegram_client=_FakeTgClient())
    chats = TelegramInboxAdapter().collect_chats(req, 20)
    assert len(chats) == 1 and chats[0]["chat_key"] == "99"


def test_missing_services_yield_empty():
    req = _req()  # 无任何平台 service
    for a in default_inbox_adapters():
        assert a.collect_chats(req, 20) == []


def test_collect_via_adapters_aggregates_all():
    req = _req(
        line_rpa_services=[_FakeLineSvc()],
        whatsapp_rpa_services=[_FakeWaSvc()],
        messenger_rpa_service=_FakeMsgrSvc(),
        telegram_client=_FakeTgClient(),
    )
    chats = collect_chats_via_adapters(req, 20, default_inbox_adapters())
    platforms = {c["platform"] for c in chats}
    assert platforms == {"line", "whatsapp", "messenger", "telegram"}


def test_collect_isolates_failing_adapter():
    class _Boom:
        platform = "boom"

        def collect_chats(self, request, limit):
            raise RuntimeError("down")

    req = _req(line_rpa_services=[_FakeLineSvc()])
    chats = collect_chats_via_adapters(req, 20, [_Boom(), LineInboxAdapter()])
    # 失败适配器被隔离，其它仍产出
    assert len(chats) == 1 and chats[0]["platform"] == "line"


# ── A2 写路径：status / send 适配器对称 ─────────────────────────────

class _SvcWithStatusSend:
    account_id = "line1"
    _merged_cfg = {"label": "LINE One"}

    def status(self):
        return {"running": True, "serial": "S1"}

    async def send_to_chat(self, chat_key, text):
        return {"chat_key": chat_key, "text": text, "sent": True}


def test_status_via_adapters_merges_keys():
    req = _req(
        line_rpa_services=[_SvcWithStatusSend()],
        telegram_client=_FakeTgClient(),
    )
    st = status_via_adapters(req, default_inbox_adapters())
    assert st["line_line1"]["running"] is True
    assert st["line_line1"]["label"] == "LINE One"
    # telegram 始终上报（无 client 时 running=False）
    assert "telegram" in st and st["telegram"]["platform"] == "telegram"


def test_status_isolates_failing_adapter():
    class _BoomStatus:
        platform = "boom"

        def status(self, request):
            raise RuntimeError("x")

    req = _req(line_rpa_services=[_SvcWithStatusSend()])
    st = status_via_adapters(req, [_BoomStatus(), LineInboxAdapter()])
    assert "line_line1" in st  # 失败适配器被隔离


def test_send_via_adapters_routes_to_platform():
    req = _req(line_rpa_services=[_SvcWithStatusSend()])
    result = asyncio.run(send_via_adapters(req, "line", "line1", "u1", "hi",
                                           default_inbox_adapters()))
    assert result["sent"] is True and result["chat_key"] == "u1"


def test_send_unknown_platform_raises_400():
    req = _req()
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(send_via_adapters(req, "nope", "a", "k", "t",
                                      default_inbox_adapters()))
    assert ei.value.status_code == 400


def test_send_no_service_raises_503():
    req = _req()  # 无 line service
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(send_via_adapters(req, "line", "x", "k", "t",
                                      default_inbox_adapters()))
    assert ei.value.status_code == 503


def test_telegram_send_awaits_coroutine():
    class _Tg:
        running = True

        async def send_message(self, chat_key, text):
            return {"ok_tg": True, "chat_key": chat_key}

    req = _req(telegram_client=_Tg())
    result = asyncio.run(send_via_adapters(req, "telegram", "default", "r", "hi",
                                           default_inbox_adapters()))
    assert result["ok_tg"] is True


def test_web_send_delivers_records_and_sets_manual():
    from src.inbox.store import InboxStore
    d = tempfile.mkdtemp()
    store = InboxStore(Path(d) / "inbox.db")
    cm = SimpleNamespace(config={"web_chat": {"enabled": True, "account_id": "web"}})
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        inbox_store=store, config_manager=cm, contacts=None)))
    result = asyncio.run(WebInboxAdapter().send(req, "web", "visitor7", "你好"))
    cid = result["conversation_id"]
    assert result["delivered"] is True
    assert cid.startswith("web:")
    # 出站落库 + 人工接管后停 AI
    assert store.count_messages(cid) >= 1
    assert store.get_automation_mode(cid) == "manual"
    store.close()
