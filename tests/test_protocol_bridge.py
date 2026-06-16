"""M6①：protocol 账号 ↔ 统一收件箱 桥接的单元测试。

覆盖：
- ingest_incoming 落库（入站/出站）+ conversation_id 形状；
- emit_incoming 经已注册 sink 派发 / 无 sink 时静默；
- orchestrator.owns / send 扇出到 worker，并把出站消息回写收件箱；
- send_via_adapters 在编排器拥有该账号时优先走 worker；
- ProtocolInboxAdapter 仅读 store 中 mode==protocol 的账号会话。
"""

from __future__ import annotations

import types

from src.inbox.store import InboxStore
from src.integrations import account_orchestrator as ao
from src.integrations import account_registry as ar
from src.integrations import protocol_bridge as pb


class FakeWorker:
    def __init__(self) -> None:
        self.sent: list = []
        self.media: list = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def healthy(self) -> bool:
        return True

    def status(self) -> dict:
        return {"type": "fake"}

    async def send(self, chat_key: str, text: str) -> dict:
        self.sent.append((chat_key, text))
        return {"delivered": True, "message_id": "x1"}

    async def send_media(self, chat_key: str, *, media_path: str,
                         media_type: str, caption: str = "") -> dict:
        self.media.append((chat_key, media_path, media_type, caption))
        return {"delivered": True, "message_id": "m1"}


def _running_managed(orch, platform, account_id, worker):
    key = ao.account_key(platform, account_id)
    orch._managed[key] = ao._Managed(
        key=key, platform=platform, account_id=account_id,
        mode="protocol", worker=worker, state="running",
    )
    return key


# ── ingest ──────────────────────────────────────────────────────────────────

def test_ingest_incoming_writes_inbound(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = pb.ingest_incoming(
        store, platform="telegram", account_id="acc1", chat_key="123",
        name="Alice", text="hi", ts=100, msg_id="m1", direction="in",
    )
    assert cid == "telegram:acc1:123"
    convs = store.list_conversations(limit=10, platform="telegram")
    assert any(c["conversation_id"] == cid for c in convs)


def test_ingest_incoming_outbound(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = pb.ingest_incoming(
        store, platform="whatsapp", account_id="a", chat_key="999",
        text="reply", direction="out", msg_id="o1",
    )
    assert cid == "whatsapp:a:999"
    convs = store.list_conversations(limit=10, platform="whatsapp")
    assert any(c["conversation_id"] == cid for c in convs)


def test_ingest_incoming_guards(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    assert pb.ingest_incoming(store, platform="telegram", account_id="a",
                              chat_key="", text="x") is None
    assert pb.ingest_incoming(None, platform="telegram", account_id="a",
                              chat_key="1", text="x") is None


# ── sink ────────────────────────────────────────────────────────────────────

def test_emit_incoming_dispatches_to_sink():
    seen: list = []
    pb.register_inbox_sink(lambda m: seen.append(m))
    try:
        pb.emit_incoming(pb.make_message(
            platform="telegram", account_id="a", chat_key="1", text="x"))
        assert seen and seen[0]["platform"] == "telegram"
    finally:
        pb.register_inbox_sink(None)


def test_emit_incoming_no_sink_is_noop():
    pb.register_inbox_sink(None)
    pb.emit_incoming({"platform": "telegram", "chat_key": "1"})  # 不抛异常即可


def test_emit_incoming_sink_error_swallowed():
    def _boom(_m):
        raise RuntimeError("boom")
    pb.register_inbox_sink(_boom)
    try:
        pb.emit_incoming({"platform": "telegram", "chat_key": "1"})  # 不应冒泡
    finally:
        pb.register_inbox_sink(None)


# ── orchestrator owns / send ──────────────────────────────────────────────────

async def test_orchestrator_owns():
    orch = ao.AccountOrchestrator(config={})
    _running_managed(orch, "telegram", "acc1", FakeWorker())
    assert orch.owns("telegram", "acc1")
    assert orch.owns("Telegram", "acc1")  # 大小写不敏感
    assert not orch.owns("telegram", "nope")
    assert not orch.owns("whatsapp", "acc1")


async def test_orchestrator_send_routes_and_writes_back():
    orch = ao.AccountOrchestrator(config={})
    w = FakeWorker()
    _running_managed(orch, "telegram", "acc1", w)
    seen: list = []
    pb.register_inbox_sink(lambda m: seen.append(m))
    try:
        res = await orch.send("telegram", "acc1", "123", "hello")
    finally:
        pb.register_inbox_sink(None)
    assert res["delivered"] is True
    assert w.sent == [("123", "hello")]
    assert seen and seen[0]["direction"] == "out"
    assert seen[0]["text"] == "hello"


async def test_orchestrator_send_no_worker_raises():
    orch = ao.AccountOrchestrator(config={})
    try:
        await orch.send("telegram", "ghost", "1", "x")
        assert False, "应抛 RuntimeError"
    except RuntimeError:
        pass


# ── send_via_adapters 优先路由到编排器 ─────────────────────────────────────────

async def test_send_via_adapters_prefers_orchestrator(monkeypatch):
    from src.inbox import channel_adapters as ca
    orch = ao.AccountOrchestrator(config={})
    w = FakeWorker()
    _running_managed(orch, "telegram", "accX", w)
    monkeypatch.setattr(ao, "_orchestrator", orch)
    pb.register_inbox_sink(lambda m: None)
    try:
        res = await ca.send_via_adapters(
            None, "telegram", "accX", "555", "hey", ca.default_inbox_adapters())
    finally:
        pb.register_inbox_sink(None)
    assert res["delivered"] is True
    assert w.sent == [("555", "hey")]


# ── ProtocolInboxAdapter 仅读 protocol 账号 ───────────────────────────────────

def test_protocol_inbox_adapter_filters_protocol_accounts(tmp_path, monkeypatch):
    from src.inbox import channel_adapters as ca
    store = InboxStore(tmp_path / "inbox.db")
    reg = ar.AccountRegistry(tmp_path / "reg.db")
    reg.upsert("telegram", "accP", mode="protocol", label="P")
    monkeypatch.setattr(ar, "_registry", reg)

    pb.ingest_incoming(store, platform="telegram", account_id="accP",
                       chat_key="42", text="hello", direction="in")
    pb.ingest_incoming(store, platform="telegram", account_id="other",
                       chat_key="99", text="x", direction="in")

    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=store)))
    adapter = ca.ProtocolInboxAdapter()
    chats = adapter.collect_chats(req, 20)
    keys = {c["chat_key"] for c in chats}
    assert "42" in keys
    assert "99" not in keys


def test_protocol_inbox_adapter_empty_without_protocol(tmp_path, monkeypatch):
    from src.inbox import channel_adapters as ca
    store = InboxStore(tmp_path / "inbox.db")
    reg = ar.AccountRegistry(tmp_path / "reg2.db")
    monkeypatch.setattr(ar, "_registry", reg)
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=store)))
    assert ca.ProtocolInboxAdapter().collect_chats(req, 20) == []


def test_protocol_inbox_adapter_includes_desktop_mode(tmp_path, monkeypatch):
    """P1 同步桥：桌面壳账号以 mode=desktop 落库，也应被收件箱列表读出。"""
    from src.inbox import channel_adapters as ca
    store = InboxStore(tmp_path / "inbox.db")
    reg = ar.AccountRegistry(tmp_path / "reg3.db")
    reg.upsert("telegram", "tg-desktop", mode="desktop", label="桌面1")
    monkeypatch.setattr(ar, "_registry", reg)

    pb.ingest_incoming(store, platform="telegram", account_id="tg-desktop",
                       chat_key="777", text="from desktop", direction="in")

    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=store)))
    chats = ca.ProtocolInboxAdapter().collect_chats(req, 20)
    assert "777" in {c["chat_key"] for c in chats}


def test_desktop_mode_not_orchestrated(tmp_path, monkeypatch):
    """mode=desktop 不在 worker_supported 范围，编排器不会尝试为其拉起 worker。"""
    assert ao.worker_supported("telegram", "desktop") is False


# ── M6②：TG 历史回填 + 消息归一 ──────────────────────────────────────────────

class _FakeChat:
    def __init__(self, cid, title=None, first_name=None):
        self.id = cid
        self.title = title
        self.first_name = first_name


class _FakeDate:
    def __init__(self, ts):
        self._ts = ts

    def timestamp(self):
        return self._ts


class _FakeMsg:
    def __init__(self, chat, text="", mid="", ts=0, outgoing=False, caption=None):
        self.chat = chat
        self.text = text
        self.caption = caption
        self.id = mid
        self.date = _FakeDate(ts) if ts else None
        self.outgoing = outgoing


def test_tg_message_payload_inbound():
    msg = _FakeMsg(_FakeChat(111, title="Group A"), text="hi", mid="9", ts=100)
    p = pb.tg_message_payload(msg, "acc1")
    assert p["platform"] == "telegram"
    assert p["chat_key"] == "111"
    assert p["name"] == "Group A"
    assert p["text"] == "hi"
    assert p["msg_id"] == "9"
    assert p["ts"] == 100
    assert p["direction"] == "in"


def test_tg_message_payload_outgoing_and_caption():
    msg = _FakeMsg(_FakeChat(222, first_name="Bob"), caption="cap", outgoing=True)
    p = pb.tg_message_payload(msg, "acc1")
    assert p["name"] == "Bob"
    assert p["text"] == "cap"
    assert p["direction"] == "out"


def test_tg_message_payload_no_chat_returns_none():
    assert pb.tg_message_payload(_FakeMsg(None), "acc1") is None


class _FakeDialogs:
    def __init__(self, dialogs):
        self._dialogs = dialogs

    async def get_dialogs(self, limit=20):
        for d in self._dialogs[:limit]:
            yield d


async def test_backfill_telegram_emits_recent():
    dialogs = [
        types.SimpleNamespace(top_message=_FakeMsg(_FakeChat(1, title="A"),
                                                   text="m1", mid="1", ts=10)),
        types.SimpleNamespace(top_message=_FakeMsg(_FakeChat(2, title="B"),
                                                   text="", mid="2", ts=20)),
        types.SimpleNamespace(top_message=None),
    ]
    seen = []
    n = await pb.backfill_telegram(_FakeDialogs(dialogs), "acc1", 10,
                                   emit=seen.append)
    assert n == 1          # 仅有文本的会话被推入（空文本/无 top_message 跳过）
    assert seen[0]["chat_key"] == "1"


async def test_backfill_telegram_respects_limit_and_guards():
    assert await pb.backfill_telegram(None, "acc1", 10) == 0
    assert await pb.backfill_telegram(_FakeDialogs([]), "acc1", 0) == 0


# ── M6④：媒体落地 ─────────────────────────────────────────────────────────────

def test_ingest_incoming_with_media_and_text(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = pb.ingest_incoming(
        store, platform="whatsapp", account_id="a", chat_key="55",
        text="看这张", media_type="image",
        media_ref="/static/protocol_media/whatsapp/a_1.jpg", direction="in",
        msg_id="m1",
    )
    rows = store.list_messages(cid)
    assert len(rows) == 1
    assert rows[0]["media_type"] == "image"
    assert rows[0]["media_ref"].endswith("a_1.jpg")
    assert rows[0]["text"] == "看这张"


def test_ingest_incoming_media_only_uses_placeholder(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = pb.ingest_incoming(
        store, platform="telegram", account_id="a", chat_key="77",
        text="", media_type="voice",
        media_ref="/static/protocol_media/telegram/a_2.ogg", direction="in",
        msg_id="m2",
    )
    rows = store.list_messages(cid)
    assert len(rows) == 1          # 无文本媒体也落库（gate 放行 media_ref）
    assert rows[0]["media_type"] == "voice"
    assert rows[0]["text"] == ""   # 正文仍为空，不喂 auto-draft
    convs = store.list_conversations(limit=10, platform="telegram")
    conv = next(c for c in convs if c["conversation_id"] == cid)
    assert conv["last_text"] == "[语音]"   # 会话预览用占位符


def test_tg_media_meta():
    assert pb.tg_media_meta(types.SimpleNamespace(photo=object())) == ("image", ".jpg")
    assert pb.tg_media_meta(types.SimpleNamespace(voice=object())) == ("voice", ".ogg")
    doc = types.SimpleNamespace(document=types.SimpleNamespace(file_name="x.pdf"))
    assert pb.tg_media_meta(doc) == ("document", ".pdf")
    assert pb.tg_media_meta(types.SimpleNamespace()) is None


def test_tg_message_payload_carries_media():
    msg = _FakeMsg(_FakeChat(5, title="X"), text="cap", mid="9", ts=1)
    p = pb.tg_message_payload(msg, "acc1", media_type="image",
                              media_ref="/static/protocol_media/telegram/acc1_9.jpg")
    assert p["media_type"] == "image"
    assert p["media_ref"].endswith("acc1_9.jpg")


def test_media_paths_creates_dir_and_url(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "protocol_media_root", lambda: tmp_path / "pm")
    dest, url = pb.media_paths("telegram", "acc1_9", ".jpg")
    assert dest.parent.is_dir()
    assert url == "/static/protocol_media/telegram/acc1_9.jpg"
    assert dest.name == "acc1_9.jpg"


# ── M6⑤：媒体识别翻译——/static URL 映射回本地文件 ────────────────────────────

def test_static_media_ref_to_path(monkeypatch, tmp_path):
    monkeypatch.setattr(pb, "protocol_media_root", lambda: tmp_path / "pm")
    p = pb.static_media_ref_to_path("/static/protocol_media/telegram/a_1.jpg")
    assert p == str(tmp_path / "pm" / "telegram" / "a_1.jpg")
    assert pb.static_media_ref_to_path("https://cdn/x.jpg") is None
    assert pb.static_media_ref_to_path("/uploads/x.jpg") is None
    assert pb.static_media_ref_to_path("") is None


# ── M6⑥：出站媒体 ─────────────────────────────────────────────────────────────

def test_media_type_from_ext():
    assert pb.media_type_from_ext(".jpg") == "image"
    assert pb.media_type_from_ext("png") == "image"
    assert pb.media_type_from_ext(".ogg") == "voice"
    assert pb.media_type_from_ext(".mp4") == "video"
    assert pb.media_type_from_ext(".pdf") == "document"
    assert pb.media_type_from_ext("") == "document"


def test_save_outbound_media(monkeypatch, tmp_path):
    monkeypatch.setattr(pb, "protocol_media_root", lambda: tmp_path / "pm")
    local, url, mtype = pb.save_outbound_media(
        "telegram", "acc1", "photo.png", b"\x89PNG\r\n")
    assert mtype == "image"
    assert url.startswith("/static/protocol_media/telegram/out_acc1_")
    assert url.endswith(".png")
    from pathlib import Path as _P
    assert _P(local).is_file()
    assert _P(local).read_bytes() == b"\x89PNG\r\n"


async def test_orchestrator_owns_media_and_send_media():
    orch = ao.AccountOrchestrator(config={})
    w = FakeWorker()
    _running_managed(orch, "telegram", "accM", w)
    assert orch.owns_media("telegram", "accM")
    assert not orch.owns_media("telegram", "ghost")
    seen: list = []
    pb.register_inbox_sink(lambda m: seen.append(m))
    try:
        res = await orch.send_media(
            "telegram", "accM", "777",
            media_path="/tmp/a.jpg",
            media_url="/static/protocol_media/telegram/out_accM_x.jpg",
            media_type="image", caption="看图")
    finally:
        pb.register_inbox_sink(None)
    assert res["delivered"] is True
    assert w.media == [("777", "/tmp/a.jpg", "image", "看图")]
    assert seen and seen[0]["direction"] == "out"
    assert seen[0]["media_type"] == "image"
    assert seen[0]["media_ref"].endswith("out_accM_x.jpg")
    assert seen[0]["text"] == "看图"


async def test_orchestrator_send_media_no_worker_raises():
    orch = ao.AccountOrchestrator(config={})
    try:
        await orch.send_media("telegram", "ghost", "1",
                              media_path="/x", media_url="/u",
                              media_type="image")
        assert False, "应抛 RuntimeError"
    except RuntimeError:
        pass


def test_static_media_ref_resolves_for_translate(monkeypatch, tmp_path):
    from src.inbox.media_resolver import resolve_for_translate
    root = tmp_path / "pm"
    monkeypatch.setattr(pb, "protocol_media_root", lambda: root)
    d = root / "telegram"
    d.mkdir(parents=True)
    (d / "acc1_9.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    local = pb.static_media_ref_to_path("/static/protocol_media/telegram/acc1_9.jpg")
    path, kind, reason = resolve_for_translate(
        {"media_type": "image", "media_ref": local})
    assert reason == "ok"
    assert kind == "image"
    assert path and path.endswith("acc1_9.jpg")
