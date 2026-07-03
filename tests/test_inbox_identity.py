"""会话身份画像（真实昵称 / @username / 电话 / 头像）落库与采集回归。

覆盖「以后所有用户都有真实昵称、头像、资料面板」这条链路的关键不变量：
- store 昵称优先级覆盖：真实昵称不被空名/裸号码冲掉（修「列表显示数字 id」根因）；
- 裸号码占位可被后续真名升级；
- 身份列 username/phone/avatar_url 落库、空值不覆盖、并经 store_row_to_chat 上桌；
- Telegram peer 身份抽取（first+last / @username / 电话）；
- ingest_incoming 落身份 + 号码补名收口（进程内 sink 与 HTTP 桥一致）；
- update_conversation_identity 惰性回填。
"""

import types

from src.inbox.models import InboxConversation
from src.inbox.normalizer import store_row_to_chat
from src.inbox.store import InboxStore
from src.integrations import protocol_bridge as pb


def _conv(cid="telegram:a:111", **kw):
    base = dict(
        conversation_id=cid, platform="telegram", account_id="a", chat_key="111",
        display_name="", last_ts=100,
    )
    base.update(kw)
    return InboxConversation(**base)


# ── 昵称优先级覆盖（核心：修「采到真名后被空名冲成数字 id」）──────────────────

def test_real_name_not_clobbered_by_empty_or_numeric(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.ingest_batch(_conv(display_name="Alice", last_ts=100), [])
    # 空名 / 裸 chat_key 名进来，都不得把真名冲掉
    store.ingest_batch(_conv(display_name="", last_ts=101), [])
    store.ingest_batch(_conv(display_name="111", last_ts=102), [])
    assert store.get_conversation("telegram:a:111")["display_name"] == "Alice"
    store.close()


def test_numeric_placeholder_upgraded_to_real_name(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.ingest_batch(_conv(display_name="111", last_ts=100), [])
    assert store.get_conversation("telegram:a:111")["display_name"] == "111"
    store.ingest_batch(_conv(display_name="Bob", last_ts=101), [])
    assert store.get_conversation("telegram:a:111")["display_name"] == "Bob"
    store.close()


# ── 身份列落库 / surface / 空值保护 ───────────────────────────────────────────

def test_identity_columns_persist_and_surface(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.ingest_batch(_conv(display_name="Alice", username="alice_tg",
                             phone="+8613800001234", avatar_url="/static/x.jpg"), [])
    row = store.get_conversation("telegram:a:111")
    assert row["username"] == "alice_tg"
    assert row["phone"] == "+8613800001234"
    assert row["avatar_url"] == "/static/x.jpg"
    assert row["first_seen"] > 0
    chat = store_row_to_chat(row)
    assert chat["name"] == "Alice"
    assert chat["username"] == "alice_tg"
    assert chat["phone"] == "+8613800001234"
    assert chat["avatar_url"] == "/static/x.jpg"
    store.close()


def test_identity_empty_does_not_clobber(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.ingest_batch(_conv(username="alice_tg", avatar_url="/static/x.jpg"), [])
    store.ingest_batch(_conv(username="", avatar_url="", last_ts=200), [])
    row = store.get_conversation("telegram:a:111")
    assert row["username"] == "alice_tg"
    assert row["avatar_url"] == "/static/x.jpg"
    store.close()


def test_first_seen_is_monotonic(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.ingest_batch(_conv(display_name="Alice", last_ts=100), [])
    first = store.get_conversation("telegram:a:111")["first_seen"]
    assert first > 0
    store.ingest_batch(_conv(display_name="Alice", last_ts=200), [])
    assert store.get_conversation("telegram:a:111")["first_seen"] == first
    store.close()


# ── Telegram peer 身份抽取 ────────────────────────────────────────────────────

def test_tg_peer_identity_full_name_username_phone():
    peer = types.SimpleNamespace(first_name="John", last_name="Doe",
                                 username="@johnd", phone_number="+123456789")
    ident = pb.tg_peer_identity(peer)
    assert ident["name"] == "John Doe"
    assert ident["username"] == "johnd"   # 前导 @ 去掉
    assert ident["phone"] == "+123456789"


def test_tg_peer_identity_username_fallback_when_no_name():
    ident = pb.tg_peer_identity(types.SimpleNamespace(username="onlyhandle"))
    assert ident["name"] == "@onlyhandle"
    assert ident["username"] == "onlyhandle"


def test_tg_peer_identity_none():
    ident = pb.tg_peer_identity(None)
    assert ident == {"name": "", "username": "", "phone": ""}


def test_tg_message_payload_carries_identity():
    chat = types.SimpleNamespace(id=111, first_name="John", last_name="Doe",
                                 username="johnd", phone_number="")
    msg = types.SimpleNamespace(chat=chat, text="hi", caption=None, id="9",
                                date=None, outgoing=False)
    p = pb.tg_message_payload(msg, "acc1")
    assert p["name"] == "John Doe"
    assert p["username"] == "johnd"
    assert p["chat_key"] == "111"


# ── ingest_incoming：落身份 + 号码补名收口 ────────────────────────────────────

def test_ingest_incoming_persists_identity(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = pb.ingest_incoming(store, platform="telegram", account_id="a",
                             chat_key="111", name="Alice", text="hi", ts=100,
                             username="alice_tg", phone="+8613800001234")
    assert cid == "telegram:a:111"
    row = store.get_conversation(cid)
    assert row["display_name"] == "Alice"
    assert row["username"] == "alice_tg"
    assert row["phone"] == "+8613800001234"
    store.close()


def test_ingest_incoming_backfills_number_from_contacts(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_protocol_contacts("whatsapp", "a",
                                   [{"chat_key": "123", "name": "Carol"}])
    # 来显名就是裸号码 → 进程内 sink 也应用通讯录名补齐（收口于 ingest_incoming）
    cid = pb.ingest_incoming(store, platform="whatsapp", account_id="a",
                             chat_key="123", name="123", text="hi", ts=100)
    assert store.get_conversation(cid)["display_name"] == "Carol"
    store.close()


# ── 惰性回填 ──────────────────────────────────────────────────────────────────

def test_update_conversation_identity(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.ingest_batch(_conv(display_name="111"), [])
    ok = store.update_conversation_identity(
        "telegram:a:111", display_name="Dora", username="dora",
        avatar_url="/static/d.jpg")
    assert ok
    row = store.get_conversation("telegram:a:111")
    assert row["display_name"] == "Dora"
    assert row["username"] == "dora"
    assert row["avatar_url"] == "/static/d.jpg"
    store.close()


def test_update_conversation_identity_keeps_real_name(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.ingest_batch(_conv(display_name="Alice"), [])
    # 传裸 chat_key 作名不得覆盖真名；但 username 仍可补
    store.update_conversation_identity("telegram:a:111", display_name="111",
                                       username="alice_tg")
    row = store.get_conversation("telegram:a:111")
    assert row["display_name"] == "Alice"
    assert row["username"] == "alice_tg"
    store.close()
