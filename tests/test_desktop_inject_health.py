"""桌面壳注入健康信标（D1b）后端模块单测。

覆盖 src/web/desktop_inject_health.py：分类纯函数（与渲染层 deriveInjectState 同口径）、
最新态存储（每账号去重/覆盖、ts 倒序、stale 标注、概览计数、软上限）。
"""

from __future__ import annotations

import time

from src.web.desktop_inject_health import (
    InjectHealthStore,
    MISMATCH_STATUSES,
    classify_inject_health,
)


def test_classify_ok():
    assert classify_inject_health(
        {"supported": True, "composer": True, "bubbles": 5, "chatOpen": True}
    ) == "ok"


def test_classify_unsupported():
    assert classify_inject_health({"supported": False}) == "unsupported"


def test_classify_no_chat():
    assert classify_inject_health(
        {"supported": True, "composer": False, "bubbles": 0, "chatOpen": False}
    ) == "no_chat"


def test_classify_mismatch_composer():
    # 会话已开（有气泡）但抓不到输入框 → 输入框选择器失配
    assert classify_inject_health(
        {"supported": True, "composer": False, "bubbles": 3, "chatOpen": True}
    ) == "mismatch_composer"


def test_classify_mismatch_bubble():
    # 会话已开 + 有输入框，但抓不到任何气泡 → 气泡选择器失配
    assert classify_inject_health(
        {"supported": True, "composer": True, "bubbles": 0, "chatOpen": True}
    ) == "mismatch_bubble"


def test_classify_empty_record():
    assert classify_inject_health({}) == "no_chat"


def test_record_dedups_by_account():
    store = InjectHealthStore()
    store.record({"platform": "instagram", "account_id": "ig1", "supported": True,
                  "composer": True, "bubbles": 2, "chatOpen": True})
    store.record({"platform": "instagram", "account_id": "ig1", "supported": True,
                  "composer": False, "bubbles": 2, "chatOpen": True})
    rows = store.latest()
    assert len(rows) == 1
    assert rows[0]["status"] == "mismatch_composer"  # 最新一条覆盖


def test_record_multiple_accounts_sorted_desc():
    store = InjectHealthStore()
    store.record({"platform": "x", "account_id": "a", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": 100})
    store.record({"platform": "x", "account_id": "b", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": 200})
    rows = store.latest()
    assert [r["account_id"] for r in rows] == ["b", "a"]  # ts 倒序


def test_no_key_not_stored_but_classified():
    store = InjectHealthStore()
    rec = store.record({"supported": True, "composer": True, "bubbles": 1, "chatOpen": True})
    assert rec["status"] == "ok"
    assert store.latest() == []  # 无主键不入库


def test_stale_flag():
    store = InjectHealthStore()
    store.record({"platform": "zalo", "account_id": "z1", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": time.time() - 200})
    rows = store.latest(stale_after=90.0)
    assert rows[0]["stale"] is True
    rows2 = store.latest(stale_after=None)
    assert "stale" not in rows2[0]


def test_summary_counts():
    store = InjectHealthStore()
    store.record({"platform": "instagram", "account_id": "1", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True})  # ok
    store.record({"platform": "instagram", "account_id": "2", "supported": True,
                  "composer": False, "bubbles": 1, "chatOpen": True})  # mismatch_composer
    store.record({"platform": "x", "account_id": "3", "supported": True,
                  "composer": True, "bubbles": 0, "chatOpen": True})  # mismatch_bubble
    s = store.summary()
    assert s["total"] == 3
    assert s["mismatch"] == 2
    assert s["ok"] == 1


def test_selectors_normalized():
    store = InjectHealthStore()
    rec = store.record({"platform": "messenger", "account_id": "m1", "supported": True,
                        "composer": True, "bubbles": 1, "chatOpen": True,
                        "selectors": {"bubble": True, "composer": True}})
    assert rec["selectors"] == {"bubble": True, "composer": True, "sendBtn": False, "peerTitle": False}


def test_soft_cap_evicts_oldest():
    store = InjectHealthStore(cap=2)
    store.record({"platform": "p", "account_id": "old", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": 1})
    store.record({"platform": "p", "account_id": "mid", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": 2})
    store.record({"platform": "p", "account_id": "new", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": 3})
    ids = {r["account_id"] for r in store.latest()}
    assert "old" not in ids and len(ids) == 2


def test_mismatch_statuses_constant():
    assert "mismatch_composer" in MISMATCH_STATUSES
    assert "mismatch_bubble" in MISMATCH_STATUSES
