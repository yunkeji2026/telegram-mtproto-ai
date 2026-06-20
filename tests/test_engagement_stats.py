"""情感陪聊·关系健康度量测试：get_engagement_stats + get_retention_cohorts + ROI 接线。

口径（陪聊视角，与客服"解决率"相反——回访=好事）：
- active_relationships：有用户入站(in)的会话。
- sticky：跨 ≥2 自然日入站（会回来的关系）。
- retention D1/D7：首次入站落在窗口内的同期群，N 天内是否回访。
"""

import time

from src.inbox.store import InboxStore
from src.inbox.models import InboxConversation, InboxMessage

# 固定基准日（本地正午，避开自然日边界）
BASE = time.mktime((2026, 3, 2, 12, 0, 0, 0, 0, -1))
DAY = 86400.0


def _seed(store, cid):
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="acc", chat_key=cid))


def _msg(store, cid, direction, ts, text):
    store.ingest_message(InboxMessage(
        conversation_id=cid, direction=direction, text=text, ts=ts,
        platform_msg_id=f"{cid}-{direction}-{ts}"))


def test_engagement_active_and_sticky(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # A：当天 in+out → 活跃，非黏性
    _seed(store, "A")
    _msg(store, "A", "in", BASE, "hi")
    _msg(store, "A", "out", BASE + 60, "hello dear")
    # B：两天都有 in → 活跃 + 黏性
    _seed(store, "B")
    _msg(store, "B", "in", BASE, "morning")
    _msg(store, "B", "in", BASE + DAY, "back again")
    _msg(store, "B", "out", BASE + DAY + 60, "missed you")
    # C：只有 out（AI 单方面发，用户没说话）→ 不算活跃关系
    _seed(store, "C")
    _msg(store, "C", "out", BASE, "are you there")

    st = store.get_engagement_stats(since_ts=BASE - 3600)
    assert st["active_relationships"] == 2          # A、B（C 不算）
    assert st["sticky_relationships"] == 1          # 仅 B
    assert st["sticky_rate"] == 0.5
    assert st["messages_in"] == 3                   # A1 + B2
    assert st["messages_out"] == 3                  # A1 + B1 + C1
    assert st["avg_turns"] == round(6 / 2, 1)


def test_engagement_reciprocity(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _seed(store, "A")
    _msg(store, "A", "in", BASE, "q1")
    _msg(store, "A", "out", BASE + 1, "a1")
    _msg(store, "A", "out", BASE + 2, "a1b")
    st = store.get_engagement_stats(since_ts=BASE - 3600)
    assert st["reciprocity"] == 2.0                 # 2 out / 1 in


def test_engagement_empty(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    st = store.get_engagement_stats(since_ts=0)
    assert st["active_relationships"] == 0
    assert st["sticky_rate"] == 0.0
    assert st["avg_turns"] == 0.0


def test_retention_cohorts(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # X：首次入站在窗口内，次日回访 → 命中 d1/d7/d30
    _seed(store, "X")
    _msg(store, "X", "in", BASE, "first")
    _msg(store, "X", "in", BASE + DAY, "i came back")
    # Y：首次入站在窗口内，再不回 → 未留存
    _seed(store, "Y")
    _msg(store, "Y", "in", BASE, "only once")
    # Z：首次入站在窗口之前 → 不进同期群
    _seed(store, "Z")
    _msg(store, "Z", "in", BASE - 10 * DAY, "old user")
    _msg(store, "Z", "in", BASE, "still here")

    ret = store.get_retention_cohorts(since_ts=BASE - 3600, until_ts=BASE + 3600)
    assert ret["cohort_size"] == 2                  # X、Y（Z 首触在窗口前）
    assert ret["retained"]["d1"] == 1               # 仅 X
    assert ret["retention_rate"]["d1"] == 0.5
    assert ret["retention_rate"]["d7"] == 0.5
    assert ret["retention_rate"]["d30"] == 0.5


def test_retention_same_day_not_counted(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # 同一天来回多次，但没有跨天 → 不算留存（留存=隔天还回来）
    _seed(store, "S")
    _msg(store, "S", "in", BASE, "m1")
    _msg(store, "S", "in", BASE + 3600, "m2")
    ret = store.get_retention_cohorts(since_ts=BASE - 3600, until_ts=BASE + 3600)
    assert ret["cohort_size"] == 1
    assert ret["retained"]["d1"] == 0


def test_roi_summary_includes_relationship(tmp_path):
    from types import SimpleNamespace
    from src.web.routes.unified_inbox_roi import build_roi_summary

    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed(store, "A")
    _msg(store, "A", "in", now - 3600, "hi")
    _msg(store, "A", "out", now - 3500, "hello")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(inbox_store=store)))
    out = build_roi_summary(request, config_manager=None, span=7)
    rel = out["relationship"]
    assert rel["active_relationships"] == 1
    assert "sticky_rate_pct" in rel
    assert "retention_d7_pct" in rel
