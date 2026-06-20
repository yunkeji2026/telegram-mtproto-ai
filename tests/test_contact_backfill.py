"""Phase Q 延伸·存量回填单测：store 写回方法 + backfill 编排（含 dry_run）。"""
from __future__ import annotations

import sys
import time as _t
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.contact_backfill import backfill_contact_ids
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore


def _seed_conv(store, cid, *, chat_key, contact_id="", ts=None):
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="messenger", account_id="a",
        chat_key=chat_key, contact_id=contact_id,
        last_ts=ts if ts is not None else _t.time()))


def test_list_missing_contact_id(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _seed_conv(store, "messenger:a:x", chat_key="x")
    _seed_conv(store, "messenger:a:y", chat_key="y", contact_id="c-1")
    rows = store.list_conversations_missing_contact_id(limit=10)
    ids = {r["conversation_id"] for r in rows}
    assert "messenger:a:x" in ids
    assert "messenger:a:y" not in ids
    store.close()


def test_set_conversation_contact_id_updates_both_tables(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "messenger:a:x"
    _seed_conv(store, cid, chat_key="x")
    # 先造 conv_meta 行（contact_id 空）
    store.update_conv_meta(cid, platform="messenger", intent="chitchat", emotion="neutral")
    assert store.set_conversation_contact_id(cid, "c-99") is True
    assert store.get_conversation(cid)["contact_id"] == "c-99"
    assert store.get_conv_meta(cid)["contact_id"] == "c-99"
    # 不存在的会话 → False
    assert store.set_conversation_contact_id("nope", "c-1") is False
    store.close()


def test_backfill_writes_and_reports(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _seed_conv(store, "messenger:a:Bob", chat_key="messenger_rpa:Bob")
    _seed_conv(store, "messenger:a:Eve", chat_key="messenger_rpa:Eve")
    _seed_conv(store, "messenger:a:has", chat_key="z", contact_id="c-existing")

    def resolver(platform, account_id, chat_key):
        return {"messenger_rpa:Bob": "c-bob"}.get(chat_key, "")

    res = backfill_contact_ids(store, resolver, limit=50)
    d = res.as_dict()
    assert d["scanned"] == 2  # 只扫缺失的两条
    assert d["resolved"] == 1
    assert d["written"] == 1
    assert d["dry_run"] is False
    assert store.get_conversation("messenger:a:Bob")["contact_id"] == "c-bob"
    assert store.get_conversation("messenger:a:Eve")["contact_id"] == ""
    store.close()


def test_backfill_dry_run_does_not_write(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _seed_conv(store, "messenger:a:Bob", chat_key="messenger_rpa:Bob")

    res = backfill_contact_ids(
        store, lambda p, a, c: "c-bob", limit=10, dry_run=True)
    d = res.as_dict()
    assert d["resolved"] == 1
    assert d["written"] == 0
    assert d["dry_run"] is True
    assert d["hit_rate"] == 1.0
    # dry_run 不落库
    assert store.get_conversation("messenger:a:Bob")["contact_id"] == ""
    store.close()


def test_backfill_none_resolver_safe(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _seed_conv(store, "messenger:a:x", chat_key="x")
    res = backfill_contact_ids(store, None, limit=10)
    assert res.as_dict()["scanned"] == 0
    store.close()


def test_find_conversation_ids_by_external_suffix(tmp_path):
    """Q 延伸前向：external_id 'Bob' 命中带前缀的真实 conv_id。"""
    store = InboxStore(tmp_path / "inbox.db")
    _seed_conv(store, "messenger:a:messenger_rpa:Bob", chat_key="messenger_rpa:Bob")
    _seed_conv(store, "messenger:a:Bob", chat_key="Bob")  # 裸名也命中
    _seed_conv(store, "messenger:a:acc_1:Bobby", chat_key="acc_1:Bobby")  # 不应误中
    got = store.find_conversation_ids_by_external("messenger", "a", "Bob")
    assert set(got) == {"messenger:a:messenger_rpa:Bob", "messenger:a:Bob"}
    store.close()


def test_find_conversation_ids_by_external_escapes_wildcards(tmp_path):
    """external_id 含 LIKE 通配符 _ 不应误匹配。"""
    store = InboxStore(tmp_path / "inbox.db")
    _seed_conv(store, "messenger:a:p:a_b", chat_key="p:a_b")
    _seed_conv(store, "messenger:a:p:axb", chat_key="p:axb")  # _ 当字面，不匹配 axb
    got = store.find_conversation_ids_by_external("messenger", "a", "a_b")
    assert got == ["messenger:a:p:a_b"]
    store.close()
