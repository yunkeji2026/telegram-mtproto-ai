"""Phase Q1：跨域身份桥纯函数单测。

覆盖：对象/dict 两种入参、account_id 缺省回落 default、缺字段跳过、去重保序、
格式与 inbox.normalizer.conv_id 一致（防漂移）。
"""
from dataclasses import dataclass

from src.contacts.identity_bridge import (
    conversation_ids_for_identities,
    external_id_lookup_candidates,
    resolve_contact_id,
)


@dataclass
class _CI:
    channel: str = ""
    account_id: str = ""
    external_id: str = ""


def test_basic_object_mapping():
    cis = [_CI("line", "acc1", "line:user:abc"), _CI("telegram", "acc2", "12345")]
    out = conversation_ids_for_identities(cis)
    assert out == ["line:acc1:line:user:abc", "telegram:acc2:12345"]


def test_dict_input():
    cis = [{"channel": "messenger", "account_id": "a", "external_id": "Bob"}]
    assert conversation_ids_for_identities(cis) == ["messenger:a:Bob"]


def test_account_id_defaults_to_default():
    cis = [_CI("line", "", "u1")]
    assert conversation_ids_for_identities(cis) == ["line:default:u1"]


def test_skips_missing_fields():
    cis = [_CI("", "a", "x"), _CI("line", "a", ""), _CI("line", "a", "u1")]
    assert conversation_ids_for_identities(cis) == ["line:a:u1"]


def test_dedup_preserves_order():
    cis = [_CI("line", "a", "u1"), _CI("line", "a", "u1"), _CI("line", "a", "u2")]
    assert conversation_ids_for_identities(cis) == ["line:a:u1", "line:a:u2"]


def test_empty():
    assert conversation_ids_for_identities([]) == []
    assert conversation_ids_for_identities(None) == []


def test_format_matches_inbox_conv_id():
    # 钉死格式与 inbox.normalizer.conv_id 一致（权威源），防双边漂移
    from src.inbox.normalizer import conv_id
    cis = [_CI("line", "acc1", "u1")]
    assert conversation_ids_for_identities(cis)[0] == conv_id("line", "acc1", "u1")


def test_external_id_lookup_candidates_strips_prefix():
    out = external_id_lookup_candidates("messenger", "a", "messenger_rpa:Bob")
    assert out == ["messenger_rpa:Bob", "Bob"]


def test_resolve_contact_id_prefix_hit(tmp_path):
    """Q 延伸：inbox chat_key 带前缀，CI external_id 裸名 → 仍能反查。"""
    from src.contacts.gateway import ContactGateway
    from src.contacts.handoff import HandoffTokenService
    from src.contacts.merge import MergeService
    from src.contacts.models import CHANNEL_MESSENGER
    from src.contacts.store import ContactStore

    store = ContactStore(db_path=tmp_path / "contacts.db")
    gw = ContactGateway(
        store, HandoffTokenService(store, ttl_seconds=3600), MergeService(store))
    ctx = gw.on_peer_seen(
        channel=CHANNEL_MESSENGER, account_id="a", external_id="Bob")
    got = resolve_contact_id(
        store, platform="messenger", account_id="a", chat_key="messenger_rpa:Bob")
    assert got == ctx.contact.contact_id
    store.close()
