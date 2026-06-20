"""Phase O2：主动关怀待办持久层单测（:memory:）。

覆盖：add 往返 + 置信度阈值过滤 + 同主题去重 + add_from_text 接线 + list_due/pending +
状态流转（sent/skipped/cancel，仅 pending 可转）+ expire_overdue + count。
"""
from datetime import datetime

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_schedule import CareScheduleStore

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()


def _commit(due_offset_days=1.0, topic="面试", conf=0.85):
    due = NOW + due_offset_days * 86400
    return CareCommitment(
        due_at=due, event_at=due - 36000, topic=topic, sentiment="negative",
        anchor_text="明天", source_text="明天面试好紧张", confidence=conf,
    )


def _store():
    return CareScheduleStore(":memory:")


def test_add_and_list_pending():
    s = _store()
    rid = s.add_commitment(_commit(), contact_key="tg:u1", platform="telegram",
                           account_id="default", chat_key="u1")
    assert rid
    pend = s.list_pending()
    assert len(pend) == 1 and pend[0]["topic"] == "面试"
    assert pend[0]["status"] == "pending"
    assert s.count() == 1 and s.count(status="pending") == 1


def test_low_confidence_filtered():
    s = _store()
    rid = s.add_commitment(_commit(conf=0.5), contact_key="tg:u1")
    assert rid is None
    assert s.count() == 0


def test_dedup_same_topic_window():
    s = _store()
    a = s.add_commitment(_commit(due_offset_days=1.0, topic="面试"), contact_key="tg:u1")
    # 同 contact + 同主题 + due 邻近（1 天内）→ 去重
    b = s.add_commitment(_commit(due_offset_days=1.5, topic="面试"), contact_key="tg:u1")
    assert a and b is None
    assert s.count() == 1


def test_dedup_allows_different_topic():
    s = _store()
    a = s.add_commitment(_commit(topic="面试"), contact_key="tg:u1")
    b = s.add_commitment(_commit(topic="复查"), contact_key="tg:u1")
    assert a and b and a != b
    assert s.count() == 2


def test_dedup_allows_far_due():
    s = _store()
    a = s.add_commitment(_commit(due_offset_days=1.0, topic="面试"), contact_key="tg:u1")
    # 同主题但 due 相距 10 天（超 3 天窗口）→ 允许
    b = s.add_commitment(_commit(due_offset_days=11.0, topic="面试"), contact_key="tg:u1")
    assert a and b
    assert s.count() == 2


def test_dedup_scoped_per_contact():
    s = _store()
    a = s.add_commitment(_commit(topic="面试"), contact_key="tg:u1")
    b = s.add_commitment(_commit(topic="面试"), contact_key="tg:u2")
    assert a and b  # 不同 contact 不互相去重
    assert s.count() == 2


def test_add_from_text():
    s = _store()
    ids = s.add_from_text("明天面试好紧张", contact_key="tg:u1", platform="telegram", now=NOW)
    assert len(ids) == 1
    # 无锚点 → 不入库
    assert s.add_from_text("今天好累", contact_key="tg:u1", now=NOW) == []


def test_list_due():
    s = _store()
    s.add_commitment(_commit(due_offset_days=1.0, topic="面试"), contact_key="tg:u1")
    s.add_commitment(_commit(due_offset_days=5.0, topic="复查"), contact_key="tg:u1")
    # now+2 天：只有第一条到期
    due = s.list_due(now=NOW + 2 * 86400)
    assert len(due) == 1 and due[0]["topic"] == "面试"


def test_mark_sent_only_pending():
    s = _store()
    rid = s.add_commitment(_commit(), contact_key="tg:u1")
    assert s.mark_sent(rid) is True
    assert s.count(status="sent") == 1 and s.count(status="pending") == 0
    # 已 sent 不能再转
    assert s.mark_sent(rid) is False
    assert s.mark_skipped(rid) is False
    row = s.list_recent(status="sent")[0]
    assert row["sent_at"] is not None


def test_mark_skipped_and_cancel():
    s = _store()
    r1 = s.add_commitment(_commit(topic="面试"), contact_key="tg:u1")
    r2 = s.add_commitment(_commit(topic="复查"), contact_key="tg:u1")
    assert s.mark_skipped(r1, note="无上下文") is True
    assert s.cancel(r2) is True
    assert s.count(status="skipped") == 1 and s.count(status="cancelled") == 1


def test_expire_overdue():
    s = _store()
    # due 在很久以前
    old = CareCommitment(due_at=NOW - 10 * 86400, event_at=NOW - 10 * 86400,
                         topic="面试", sentiment="neutral", anchor_text="x",
                         source_text="y", confidence=0.85)
    rid = s.add_commitment(old, contact_key="tg:u1")
    assert rid
    n = s.expire_overdue(now=NOW, grace_days=1.0)
    assert n == 1
    assert s.count(status="expired") == 1 and s.count(status="pending") == 0


def test_expire_keeps_recent_pending():
    s = _store()
    s.add_commitment(_commit(due_offset_days=1.0), contact_key="tg:u1")
    # 未到期项不应被 expire
    assert s.expire_overdue(now=NOW, grace_days=1.0) == 0
    assert s.count(status="pending") == 1


def test_bring_forward_makes_due():
    s = _store()
    rid = s.add_commitment(_commit(due_offset_days=3.0), contact_key="tg:u1")
    # 提前前：3 天后到期，now 时不 due
    assert len(s.list_due(now=NOW)) == 0
    assert s.bring_forward(rid, now=NOW) is True
    assert len(s.list_due(now=NOW)) == 1


def test_bring_forward_only_pending():
    s = _store()
    rid = s.add_commitment(_commit(), contact_key="tg:u1")
    s.cancel(rid)
    # 非 pending → 不可提前
    assert s.bring_forward(rid, now=NOW) is False


def test_list_by_contact_and_count_pending():
    s = _store()
    s.add_commitment(_commit(topic="面试"), contact_key="tg:u1")
    s.add_commitment(_commit(topic="复查"), contact_key="tg:u1")
    s.add_commitment(_commit(topic="生日"), contact_key="tg:u2")
    assert len(s.list_by_contact("tg:u1")) == 2
    assert len(s.list_by_contact("tg:u2")) == 1
    assert len(s.list_by_contact("tg:nope")) == 0
    assert s.count_pending_by_contact("tg:u1") == 2
    assert s.count_pending_by_contact("tg:u2") == 1
    # 取消一条后 pending 计数下降，list_by_contact(status=pending) 也下降
    one = s.list_by_contact("tg:u1")[0]
    s.cancel(one["id"])
    assert s.count_pending_by_contact("tg:u1") == 1
    assert len(s.list_by_contact("tg:u1", status="pending")) == 1
    assert len(s.list_by_contact("tg:u1")) == 2  # 全状态仍 2


def test_pending_counts_by_contacts_batch():
    s = _store()
    s.add_commitment(_commit(topic="面试"), contact_key="tg:u1")
    s.add_commitment(_commit(topic="复查"), contact_key="tg:u1")
    s.add_commitment(_commit(topic="生日"), contact_key="tg:u2")
    counts = s.pending_counts_by_contacts(["tg:u1", "tg:u2", "tg:u3"])
    assert counts == {"tg:u1": 2, "tg:u2": 1}  # u3 无 pending → 不出现
    assert s.pending_counts_by_contacts([]) == {}
