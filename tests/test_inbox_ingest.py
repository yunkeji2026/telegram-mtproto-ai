"""inbox.ingest 映射测试（Phase A）。"""

from unittest.mock import patch

from src.inbox.store import InboxStore
from src.inbox.ingest import ingest_collected_chats, ingest_thread


def _chat(**kw):
    base = {
        "platform": "whatsapp",
        "account_id": "wa-a",
        "chat_key": "room",
        "conversation_id": "whatsapp:wa-a:room",
        "name": "WA User",
        "last_msg": "hello",
        "last_ts": 110,
        "unread": 1,
        "language": "en",
        "last_message": {"text": "hello", "ts": 110, "direction": "in", "language": "en"},
    }
    base.update(kw)
    return base


def test_ingest_collected_chats_populates_store(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    n = ingest_collected_chats(store, [_chat()])
    assert n == 1
    convs = store.list_conversations()
    assert convs[0]["conversation_id"] == "whatsapp:wa-a:room"
    assert convs[0]["display_name"] == "WA User"
    assert store.count_messages("whatsapp:wa-a:room") == 1
    store.close()


def test_ingest_skips_invalid_rows(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # 缺 conversation_id / platform 的行被跳过
    n = ingest_collected_chats(store, [{"name": "x"}, _chat(conversation_id="", platform="")])
    assert n == 0
    assert store.list_conversations() == []
    store.close()


def test_ingest_is_idempotent(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    ingest_collected_chats(store, [_chat()])
    # 同一轮聚合重放 → 消息不重复
    ingest_collected_chats(store, [_chat()])
    assert store.count_messages("whatsapp:wa-a:room") == 1
    store.close()


def test_ingest_thread_persists_history(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    chat = _chat()
    messages = [
        {"text": "m1", "ts": 1, "message_id": "1", "language": "en"},
        {"text": "m2", "ts": 2, "message_id": "2", "language": "en"},
        {"text": "m3", "ts": 3, "message_id": "3", "language": "en"},
    ]
    assert ingest_thread(store, chat, messages) == 3
    rows = store.list_messages("whatsapp:wa-a:room")
    assert [r["text"] for r in rows] == ["m1", "m2", "m3"]
    store.close()


def test_ingest_writes_contact_id_when_resolver_registered(tmp_path):
    """Q 延伸：register_contact_resolver → conversations + conv_meta 回写 contact_id。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.register_contact_resolver(
        lambda platform, account_id, chat_key: (
            "contact-xyz" if chat_key == "messenger_rpa:Bob" else ""
        ),
    )
    chat = _chat(
        platform="messenger",
        account_id="a",
        chat_key="messenger_rpa:Bob",
        conversation_id="messenger:a:messenger_rpa:Bob",
        last_message={"text": "hi", "ts": 110, "direction": "in", "language": "en"},
    )
    with patch(
        "src.ai.chat_assistant_service.quick_analyze",
        return_value={"intent": "chitchat", "emotion": "neutral", "risk_level": "low"},
    ):
        ingest_collected_chats(store, [chat])
    conv = store.get_conversation("messenger:a:messenger_rpa:Bob")
    assert conv is not None
    assert conv["contact_id"] == "contact-xyz"
    meta = store.get_conv_meta("messenger:a:messenger_rpa:Bob")
    assert meta is not None
    assert meta["contact_id"] == "contact-xyz"
    store.close()
