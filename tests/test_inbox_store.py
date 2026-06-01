"""InboxStore 单元测试（Phase A）。"""

from src.inbox.models import InboxConversation, InboxMessage, MessageAnalysis
from src.inbox.store import InboxStore, _message_pk


def _conv(cid="line:a:room1", **kw):
    base = dict(
        conversation_id=cid, platform="line", account_id="a", chat_key="room1",
        display_name="User", language="ja", last_text="こんにちは", last_ts=100, unread=2,
    )
    base.update(kw)
    return InboxConversation(**base)


def test_ddl_creates_tables_and_basic_roundtrip(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(_conv())
    rows = store.list_conversations()
    assert len(rows) == 1
    assert rows[0]["conversation_id"] == "line:a:room1"
    assert rows[0]["language"] == "ja"
    store.close()


def test_message_dedup_with_platform_id(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    m = InboxMessage(conversation_id="line:a:room1", platform_msg_id="m1", text="hi", ts=1)
    assert store.ingest_message(m) is True
    # 同 platform_msg_id 再 ingest → 不重复
    assert store.ingest_message(m) is False
    assert store.count_messages("line:a:room1") == 1
    store.close()


def test_message_dedup_without_platform_id_uses_hash(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    m1 = InboxMessage(conversation_id="c1", platform_msg_id="", text="same", ts=5)
    m2 = InboxMessage(conversation_id="c1", platform_msg_id="", text="same", ts=5)
    m3 = InboxMessage(conversation_id="c1", platform_msg_id="", text="different", ts=5)
    assert store.ingest_message(m1) is True
    assert store.ingest_message(m2) is False  # 同 text|ts → 同 hash 主键 → 去重
    assert store.ingest_message(m3) is True
    assert store.count_messages("c1") == 2
    store.close()


def test_message_pk_distinct_for_empty_platform_id():
    # 无 platform_msg_id 时不应折叠成 (conv, '')，靠 hash 区分
    a = _message_pk("c1", "", "hello", 1)
    b = _message_pk("c1", "", "world", 1)
    assert a != b
    assert a.startswith("c1:h:")


def test_last_ts_monotonic_no_regression(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(_conv(last_text="new", last_ts=200))
    # 旧 fetch（更小 ts）不应覆盖更新的 last_text/last_ts
    store.upsert_conversation(_conv(last_text="old", last_ts=50))
    row = store.get_conversation("line:a:room1")
    assert row["last_text"] == "new"
    assert row["last_ts"] == 200
    store.close()


def test_ingest_batch_returns_inserted_count(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    msgs = [
        InboxMessage(conversation_id="line:a:room1", platform_msg_id="m1", text="a", ts=1),
        InboxMessage(conversation_id="line:a:room1", platform_msg_id="m2", text="b", ts=2),
    ]
    assert store.ingest_batch(_conv(), msgs) == 2
    # 重放整批 → 0 新增（幂等）
    assert store.ingest_batch(_conv(), msgs) == 0
    store.close()


def test_automation_mode_persists_across_restart(tmp_path):
    db = tmp_path / "inbox.db"
    store = InboxStore(db)
    assert store.get_automation_mode("line:a:room1") == "review"  # 默认
    store.set_automation_mode("line:a:room1", "auto_ai")
    store.close()

    # 模拟重启：新实例指向同一 db
    store2 = InboxStore(db)
    assert store2.get_automation_mode("line:a:room1") == "auto_ai"
    store2.close()


def test_automation_mode_rejects_invalid(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.set_automation_mode("c1", "nonsense")
    assert store.get_automation_mode("c1") == "review"
    store.close()


def test_analysis_save_and_latest(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.save_analysis(MessageAnalysis(
        message_id="m1", conversation_id="c1", intent="提问", emotion="平稳",
        risk_level="high", risk_reasons=["money"], analyzer="llm", confidence=0.9,
    ))
    latest = store.latest_analysis("c1")
    assert latest["intent"] == "提问"
    assert latest["risk_level"] == "high"
    assert latest["risk_reasons"] == ["money"]
    assert latest["analyzer"] == "llm"
    store.close()


def test_migration_idempotent_reopen(tmp_path):
    db = tmp_path / "inbox.db"
    InboxStore(db).close()
    # 二次打开不应报错（CREATE TABLE IF NOT EXISTS 幂等）
    store = InboxStore(db)
    assert store.list_conversations() == []
    store.close()
