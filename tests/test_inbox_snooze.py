"""P0-companion：会话搁置（snooze）单测。

覆盖不变量：
- set/clear/snoozed_ids/list_snoozed 基本读写；
- 到点自动重浮（读时按 now 过滤，无需扫表）；
- 过去时间 = 取消搁置（不误挂）；
- clear_snooze 对「未搁置/无 meta 行」是 no-op（不建行、不报错）——入站热路径安全；
- 客户再次来消息（ingest_collected_chats 新入站）→ 立即取消搁置；
- _snoozed_set 读侧助手正确过滤（待接管/SLA 队列据此排除）。
"""

import time

from src.inbox.ingest import ingest_collected_chats
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_sla import _snoozed_set


def _conv(store, cid="line:a:room1"):
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="line", account_id="a", chat_key="room1",
        display_name="User", language="ja", last_text="hi", last_ts=100, unread=1,
    ))


def test_set_and_clear_snooze_roundtrip(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    until = time.time() + 3600
    assert store.set_snooze(cid, until) is True
    assert cid in store.snoozed_ids()
    meta = store.get_conv_meta(cid)
    assert abs(float(meta["snooze_until"]) - until) < 1.0
    store.clear_snooze(cid)
    assert cid not in store.snoozed_ids()
    store.close()


def test_snooze_auto_wakes_at_deadline(tmp_path):
    """到点自动重浮：snoozed_ids 传入晚于 until 的 now 即不再包含（无需定时扫表）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    now = time.time()
    store.set_snooze(cid, now + 60)
    assert cid in store.snoozed_ids(now=now + 30)      # 窗口内 → 搁置中
    assert cid not in store.snoozed_ids(now=now + 90)   # 已过点 → 自动重浮
    store.close()


def test_set_snooze_past_time_is_cancel(tmp_path):
    """until<=now 视为取消，不会把会话「搁置到过去」。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    store.set_snooze(cid, time.time() + 3600)
    assert store.set_snooze(cid, time.time() - 10) is False
    assert cid not in store.snoozed_ids()
    store.close()


def test_clear_snooze_noop_without_meta_row(tmp_path):
    """未搁置/无 meta 行时 clear 是 no-op：不报错、不凭空建 meta 行（入站热路径安全）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    store.clear_snooze(cid)              # 不应抛
    assert store.get_conv_meta(cid) is None  # 未被凭空建行
    store.close()


def test_list_snoozed_reports_remaining(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    store.set_snooze(cid, time.time() + 120)
    items = store.list_snoozed()
    assert len(items) == 1
    it = items[0]
    assert it["conversation_id"] == cid
    assert it["name"] == "User"
    assert 0 < it["remaining_sec"] <= 120
    store.close()


def test_customer_reply_wakes_snooze_via_ingest(tmp_path):
    """客户再次来消息（新入站）→ ingest 侧 clear_snooze，立即重浮。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    store.set_snooze(cid, time.time() + 3600)
    assert cid in store.snoozed_ids()
    chat = {
        "conversation_id": cid, "platform": "line", "account_id": "a",
        "chat_key": "room1", "name": "User", "last_ts": 200,
        "last_message": {"text": "在吗？", "direction": "in", "ts": 200},
    }
    inserted = ingest_collected_chats(store, [chat])
    assert inserted == 1
    assert cid not in store.snoozed_ids()   # 客户回复已唤醒
    store.close()


def test_snoozed_set_helper_filters(tmp_path):
    """_snoozed_set(inbox, ids)：待接管/SLA 快照据此把搁置会话排除出队列。"""
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "line:a:room1")
    _conv(store, "line:a:room2")
    store.set_snooze("line:a:room1", time.time() + 600)
    got = _snoozed_set(store, ["line:a:room1", "line:a:room2"])
    assert got == {"line:a:room1"}
    store.close()
