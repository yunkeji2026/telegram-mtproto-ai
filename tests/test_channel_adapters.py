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
    ProtocolInboxAdapter,
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


def test_telegram_adapter_preserves_outbound_direction():
    class _Tg:
        _recent_messages = [{
            "chat_id": 99, "user_name": "Dave", "text": "sent by me",
            "ts": 41, "direction": "out", "id": 123,
        }]

    chats = TelegramInboxAdapter().collect_chats(_req(telegram_client=_Tg()), 20)

    assert chats[0]["unread"] == 0
    assert chats[0]["last_message"]["direction"] == "out"


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


def test_send_via_adapters_records_orchestrator_route(monkeypatch):
    """编排器接管时记 route=orchestrator（观测「happy path」占比）。"""
    import src.integrations.account_orchestrator as ao
    from src.inbox.send_route_stats import get_send_route_stats

    class _FakeOrch:
        def owns(self, platform, account_id):
            return True

        async def send(self, platform, account_id, chat_key, text,
                       *, reply_to=None, mentions=None):
            return {"delivered": True}

    monkeypatch.setattr(ao, "get_orchestrator", lambda *a, **k: _FakeOrch())
    get_send_route_stats().reset()
    asyncio.run(send_via_adapters(_req(), "telegram", "accX", "555", "hey",
                                  default_inbox_adapters()))
    d = get_send_route_stats().dump()
    assert d["by_platform"]["telegram"]["orchestrator"] == 1
    assert d["orchestrator_total"] == 1 and d["adapter_total"] == 0


def test_send_via_adapters_records_adapter_route(monkeypatch):
    """编排器未接管、回落适配器时记 route=adapter（观测「编排器漏接」的信号源）。"""
    import src.integrations.account_orchestrator as ao
    from src.inbox.send_route_stats import get_send_route_stats

    class _FakeOrch:
        def owns(self, platform, account_id):
            return False

        async def send(self, *a, **k):
            raise AssertionError("owns=False 不应走编排器")

    monkeypatch.setattr(ao, "get_orchestrator", lambda *a, **k: _FakeOrch())
    get_send_route_stats().reset()
    req = _req(line_rpa_services=[_SvcWithStatusSend()])
    res = asyncio.run(send_via_adapters(req, "line", "line1", "u1", "hi",
                                        default_inbox_adapters()))
    assert res["sent"] is True
    d = get_send_route_stats().dump()
    assert d["by_platform"]["line"]["adapter"] == 1
    assert d["adapter_total"] == 1 and d["orchestrator_total"] == 0


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


# ── Messenger 出站分流：网页号→网页微服务 / RPA 号→按名找人（修 AttributeError） ──

class _FakeAcctRegistry:
    """account_registry.get(platform, account_id) → mode 行（供 web/device 分流判定）。"""

    def __init__(self, rows):
        self._rows = rows  # {(platform, account_id): {"mode": ...}}

    def get(self, platform, account_id):
        return self._rows.get((str(platform), str(account_id)))


def test_messenger_send_web_account_routes_to_web_service(monkeypatch):
    """网页号（mode=web）：按 thread id 直发 :8791 微服务（chat_key 即 jid），不落 RPA。"""
    import src.integrations.account_registry as ar
    import src.integrations.messenger_web_login as mgw
    monkeypatch.setattr(ar, "get_account_registry",
                        lambda: _FakeAcctRegistry({("messenger", "100"): {"mode": "web"}}))
    monkeypatch.setattr(mgw, "web_enabled", lambda cfg: True)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")
    sent = {}

    async def _fake_post(url, payload, timeout=20.0):
        sent["url"] = url
        sent["payload"] = payload
        return {"ok": True, "message_id": "mid-1"}

    monkeypatch.setattr(mgw, "_post_json", _fake_post)
    req = _req(config_manager=SimpleNamespace(config={}))  # 无 messenger_rpa_service
    res = asyncio.run(MessengerInboxAdapter().send(
        req, "100", "9159931534093766", "hi"))
    assert res["delivered"] is True and res["message_id"] == "mid-1"
    assert res["conversation_id"] == "messenger:100:9159931534093766"
    assert sent["url"] == "http://svc/accounts/100/send"
    assert sent["payload"] == {"jid": "9159931534093766", "text": "hi"}


def test_messenger_send_web_unreachable_raises_503(monkeypatch):
    import src.integrations.account_registry as ar
    import src.integrations.messenger_web_login as mgw
    monkeypatch.setattr(ar, "get_account_registry",
                        lambda: _FakeAcctRegistry({("messenger", "100"): {"mode": "web"}}))
    monkeypatch.setattr(mgw, "web_enabled", lambda cfg: True)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")

    async def _boom(url, payload, timeout=20.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mgw, "_post_json", _boom)
    req = _req(config_manager=SimpleNamespace(config={}))
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(MessengerInboxAdapter().send(req, "100", "555", "hi"))
    assert ei.value.status_code == 503


class _MsgrRpaSvc:
    """RPA service：暴露正确的 send_to_chat_name_for_account（account_id + reply_text）。"""

    def __init__(self):
        self.calls = []

    async def send_to_chat_name_for_account(self, account_id, *, chat_name, reply_text):
        self.calls.append((account_id, chat_name, reply_text))
        return {"ok": True, "step": "sent"}

    def list_approvals(self, status, limit):
        return []


class _MsgrStore:
    def get_conversation(self, cid):
        assert cid == "messenger:vwnj:555"
        return {"display_name": "Carol", "chat_key": "555"}


def test_messenger_send_rpa_resolves_display_name(monkeypatch):
    """RPA 号（mode=device）：把数字 thread id 解析成显示名后按名发送（reply_text 口径）。"""
    import src.integrations.account_registry as ar
    monkeypatch.setattr(ar, "get_account_registry",
                        lambda: _FakeAcctRegistry({("messenger", "vwnj"): {"mode": "device"}}))
    svc = _MsgrRpaSvc()
    req = _req(messenger_rpa_service=svc, inbox_store=_MsgrStore(),
               config_manager=SimpleNamespace(config={}))
    res = asyncio.run(MessengerInboxAdapter().send(req, "vwnj", "555", "hello"))
    assert res["ok"] is True
    assert svc.calls == [("vwnj", "Carol", "hello")]  # 解析成 Carol，非数字 id


def test_messenger_send_rpa_missing_method_raises_clean(monkeypatch):
    """回归：service 无 send_to_chat_name_for_account 时报干净的 501，而非 AttributeError。

    历史 bug：MessengerInboxAdapter.send 曾调 service 上不存在的 send_to_chat_name，
    任何 Messenger 出站（AutosendWorker 全自动回复）都必崩。"""
    import src.integrations.account_registry as ar
    monkeypatch.setattr(ar, "get_account_registry", lambda: _FakeAcctRegistry({}))

    class _LegacySvc:  # 模拟历史 service：没有正确的发送方法
        def list_approvals(self, status, limit):
            return []

    req = _req(messenger_rpa_service=_LegacySvc(),
               config_manager=SimpleNamespace(config={}))
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(MessengerInboxAdapter().send(req, "vwnj", "555", "x"))
    assert ei.value.status_code == 501


def test_messenger_send_no_service_raises_503(monkeypatch):
    import src.integrations.account_registry as ar
    import src.integrations.messenger_web_login as mgw
    monkeypatch.setattr(ar, "get_account_registry", lambda: _FakeAcctRegistry({}))
    monkeypatch.setattr(mgw, "web_enabled", lambda cfg: False)  # 网页未开
    req = _req(config_manager=SimpleNamespace(config={}))  # 无任何 messenger service
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(MessengerInboxAdapter().send(req, "x", "k", "t"))
    assert ei.value.status_code == 503


def _capture_emits(monkeypatch):
    """截获 protocol_bridge.emit_incoming 的出站回写（返回收集列表）。"""
    import src.integrations.protocol_bridge as pb
    emitted = []
    monkeypatch.setattr(pb, "emit_incoming", lambda m: emitted.append(m))
    return emitted


def test_messenger_send_web_writes_back_outbound(monkeypatch):
    """网页号发送成功后，乐观回写会话线程（direction=out，platform_msg_id=服务返回 id）。

    补齐「自动回复/坐席发送成功但会话线程要等下次轮询才显示」的缝：回落适配器路径也
    与 orch.send 同口径 emit_incoming(out)。"""
    import src.integrations.account_registry as ar
    import src.integrations.messenger_web_login as mgw
    monkeypatch.setattr(ar, "get_account_registry",
                        lambda: _FakeAcctRegistry({("messenger", "100"): {"mode": "web"}}))
    monkeypatch.setattr(mgw, "web_enabled", lambda cfg: True)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")

    async def _fake_post(url, payload, timeout=20.0):
        return {"ok": True, "message_id": "mid-1"}

    monkeypatch.setattr(mgw, "_post_json", _fake_post)
    emitted = _capture_emits(monkeypatch)
    req = _req(config_manager=SimpleNamespace(config={}))
    asyncio.run(MessengerInboxAdapter().send(req, "100", "9159931534093766", "hi"))
    assert len(emitted) == 1  # 恰好回写一条，不重复
    m = emitted[0]
    assert m["direction"] == "out" and m["text"] == "hi"
    assert m["platform"] == "messenger" and m["account_id"] == "100"
    assert m["chat_key"] == "9159931534093766"
    assert m["msg_id"] == "mid-1"  # 带平台 id → 与日后回显同键幂等去重


def test_messenger_send_rpa_writes_back_outbound(monkeypatch):
    """RPA 号发送成功后同样回写；无平台 id → hash 键乐观落库（由 store 去重护栏收敛）。"""
    import src.integrations.account_registry as ar
    monkeypatch.setattr(ar, "get_account_registry",
                        lambda: _FakeAcctRegistry({("messenger", "vwnj"): {"mode": "device"}}))
    emitted = _capture_emits(monkeypatch)
    svc = _MsgrRpaSvc()
    req = _req(messenger_rpa_service=svc, inbox_store=_MsgrStore(),
               config_manager=SimpleNamespace(config={}))
    asyncio.run(MessengerInboxAdapter().send(req, "vwnj", "555", "hello"))
    assert len(emitted) == 1
    m = emitted[0]
    assert m["direction"] == "out" and m["text"] == "hello"
    assert m["account_id"] == "vwnj" and m["chat_key"] == "555"
    assert m["msg_id"] == ""  # RPA 无平台 id → 走 hash 键


def test_messenger_send_failure_does_not_write_back(monkeypatch):
    """发送抛错（如网页服务不可达）时不得回写——否则会话里出现「幽灵已发」。"""
    import src.integrations.account_registry as ar
    import src.integrations.messenger_web_login as mgw
    monkeypatch.setattr(ar, "get_account_registry",
                        lambda: _FakeAcctRegistry({("messenger", "100"): {"mode": "web"}}))
    monkeypatch.setattr(mgw, "web_enabled", lambda cfg: True)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")

    async def _boom(url, payload, timeout=20.0):
        raise RuntimeError("refused")

    monkeypatch.setattr(mgw, "_post_json", _boom)
    emitted = _capture_emits(monkeypatch)
    req = _req(config_manager=SimpleNamespace(config={}))
    with pytest.raises(ChannelSendError):
        asyncio.run(MessengerInboxAdapter().send(req, "100", "555", "hi"))
    assert emitted == []  # 未送达 → 不回写


# ── LINE/WA 回落发送：enqueue_send + 出站回写（P3） ─────────────────────

class _LineEnqueueSvc:
    account_id = "line1"
    _merged_cfg = {"label": "LINE One"}

    def __init__(self):
        self.calls = []

    def enqueue_send(self, *, chat_key, peer_name, text, created_by=""):
        self.calls.append((chat_key, peer_name, text))
        return 42


class _WaEnqueueSvc:
    account_id = "wa1"
    _merged_cfg = {}

    def __init__(self):
        self.calls = []

    def enqueue_send(self, chat_key, peer_name, text):
        self.calls.append((chat_key, peer_name, text))
        return 99


class _LineWaStore:
    def get_conversation(self, cid):
        rows = {
            "line:line1:u1": {"display_name": "Alice", "chat_key": "u1"},
            "whatsapp:wa1:w1": {"display_name": "Bob", "chat_key": "w1"},
        }
        return rows.get(cid)


def test_line_send_enqueue_writes_back_outbound(monkeypatch):
    """真实 LINE service 走 enqueue_send；入队成功后乐观回写会话线程。"""
    emitted = _capture_emits(monkeypatch)
    svc = _LineEnqueueSvc()
    req = _req(line_rpa_services=[svc], inbox_store=_LineWaStore())
    res = asyncio.run(LineInboxAdapter().send(req, "line1", "u1", "hi"))
    assert res["queued"] is True and res["item_id"] == 42
    assert svc.calls == [("u1", "Alice", "hi")]
    assert len(emitted) == 1
    m = emitted[0]
    assert m["platform"] == "line" and m["direction"] == "out"
    assert m["text"] == "hi" and m["chat_key"] == "u1"


def test_wa_send_enqueue_writes_back_outbound(monkeypatch):
    """真实 WA service 走 enqueue_send；入队成功后乐观回写会话线程。"""
    emitted = _capture_emits(monkeypatch)
    svc = _WaEnqueueSvc()
    req = _req(whatsapp_rpa_services=[svc], inbox_store=_LineWaStore())
    res = asyncio.run(WhatsAppInboxAdapter().send(req, "wa1", "w1", "yo"))
    assert res["queued"] is True and res["item_id"] == 99
    assert svc.calls == [("w1", "Bob", "yo")]
    assert len(emitted) == 1 and emitted[0]["platform"] == "whatsapp"


def test_line_send_enqueue_failure_does_not_write_back(monkeypatch):
    """入队失败（如空 text）时不回写，避免会话里出现幽灵消息。"""

    class _BoomEnqueue:
        account_id = "line1"

        def enqueue_send(self, *, chat_key, peer_name, text, created_by=""):
            raise ValueError("chat_key 和 text 不能为空")

    emitted = _capture_emits(monkeypatch)
    req = _req(line_rpa_services=[_BoomEnqueue()])
    with pytest.raises(ChannelSendError) as ei:
        asyncio.run(LineInboxAdapter().send(req, "line1", "", "hi"))
    assert ei.value.status_code == 400
    assert emitted == []


def test_line_send_fake_send_to_chat_still_works(monkeypatch):
    """测试 fake（仅有 send_to_chat、无 enqueue_send）仍走旧路径并回写。"""
    emitted = _capture_emits(monkeypatch)
    req = _req(line_rpa_services=[_SvcWithStatusSend()])
    res = asyncio.run(LineInboxAdapter().send(req, "line1", "u1", "hi"))
    assert res["sent"] is True
    assert len(emitted) == 1 and emitted[0]["platform"] == "line"


# ── 已移除账号：只读历史展示（看不到之前聊天记录的修复） ──────────────

class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def list(self):
        return self._rows


class _FakeProtoStore:
    """按 platform 返回会话；两个账号各一条会话。"""

    def __init__(self):
        self._rows = {
            "telegram": [
                {"platform": "telegram", "account_id": "acc_on", "chat_key": "u1",
                 "conversation_id": "telegram:acc_on:u1", "last_text": "在线对话",
                 "last_ts": 100, "display_name": "Active"},
                {"platform": "telegram", "account_id": "acc_off", "chat_key": "u2",
                 "conversation_id": "telegram:acc_off:u2", "last_text": "历史对话",
                 "last_ts": 50, "display_name": "Archived"},
            ]
        }

    def list_conversations(self, limit, platform):
        return list(self._rows.get(platform, []))

    def get_automation_mode(self, cid):
        return "review"

    def count_messages(self, cid):
        return 3


def _proto_req(rows, *, show_removed=True):
    store = _FakeProtoStore()
    cm = SimpleNamespace(config={"inbox": {"show_removed_history": show_removed}})
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        inbox_store=store, config_manager=cm)))


def _patch_registry(monkeypatch, rows):
    import src.integrations.account_registry as ar
    monkeypatch.setattr(ar, "get_account_registry", lambda: _FakeRegistry(rows))


def test_protocol_adapter_marks_removed_account_readonly(monkeypatch):
    rows = [
        {"platform": "telegram", "account_id": "acc_on",
         "mode": "protocol", "status": "online"},
        {"platform": "telegram", "account_id": "acc_off",
         "mode": "protocol", "status": "removed"},
    ]
    _patch_registry(monkeypatch, rows)
    chats = ProtocolInboxAdapter().collect_chats(_proto_req(rows), 20)
    by_acc = {c["account_id"]: c for c in chats}
    # 在线账号：可发送、非只读
    assert by_acc["acc_on"]["can_send"] is True
    assert by_acc["acc_on"]["read_only"] is False
    assert by_acc["acc_on"]["account_status"] == ""
    # 已移除账号：只读历史可见、禁止发送
    assert by_acc["acc_off"]["read_only"] is True
    assert by_acc["acc_off"]["can_send"] is False
    assert by_acc["acc_off"]["account_status"] == "removed"


def test_protocol_adapter_hides_removed_when_flag_off(monkeypatch):
    rows = [
        {"platform": "telegram", "account_id": "acc_on",
         "mode": "protocol", "status": "online"},
        {"platform": "telegram", "account_id": "acc_off",
         "mode": "protocol", "status": "removed"},
    ]
    _patch_registry(monkeypatch, rows)
    chats = ProtocolInboxAdapter().collect_chats(
        _proto_req(rows, show_removed=False), 20)
    accs = {c["account_id"] for c in chats}
    assert accs == {"acc_on"}  # 关闭开关回到旧行为：removed 账号被隐藏


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
