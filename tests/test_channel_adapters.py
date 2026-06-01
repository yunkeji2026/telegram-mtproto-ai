"""Channel Adapter 契约 + 隔离单测（Phase A2）。"""

from __future__ import annotations

from types import SimpleNamespace

from src.inbox.channel_adapters import (
    ChannelAdapter,
    LineInboxAdapter,
    WhatsAppInboxAdapter,
    MessengerInboxAdapter,
    TelegramInboxAdapter,
    collect_chats_via_adapters,
    default_inbox_adapters,
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


def test_default_registry_has_four_platforms():
    adapters = default_inbox_adapters()
    assert [a.platform for a in adapters] == [
        "line", "whatsapp", "messenger", "telegram",
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
