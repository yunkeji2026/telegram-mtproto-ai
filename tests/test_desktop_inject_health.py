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


# ── #4 失配持续告警升级：跃迁追踪 / 持续时长 / 历史 ──────────────────────────
def _rec(store, status_kwargs, ts):
    base = {"platform": "instagram", "account_id": "ig1", "supported": True}
    base.update(status_kwargs)
    base["ts"] = ts
    return store.record(base)


def test_mismatch_since_set_on_entry_and_persists_across_substatus():
    store = InjectHealthStore()
    _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 100)   # ok
    r1 = _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 110)  # mismatch_composer
    assert r1["mismatch_since"] == 110
    # 切到另一种失配子状态（composer→bubble）→ mismatch_since 不重置（持续在失配）
    r2 = _rec(store, {"composer": True, "bubbles": 0, "chatOpen": True}, 150)  # mismatch_bubble
    assert r2["mismatch_since"] == 110


def test_mismatch_since_cleared_on_recovery():
    store = InjectHealthStore()
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 100)  # mismatch
    r = _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 120)  # ok 恢复
    assert r["mismatch_since"] is None
    # 再次失配 → 重新计起点（非沿用旧的）
    r2 = _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 200)
    assert r2["mismatch_since"] == 200


def test_latest_reports_mismatch_secs():
    store = InjectHealthStore()
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 1000)  # mismatch@1000
    rows = store.latest(now=1180)
    assert rows[0]["mismatch_secs"] == 180


def test_persistent_mismatches_threshold():
    store = InjectHealthStore()
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 1000)  # mismatch@1000
    # 阈值 300s：t=1200 时未到 → 空；t=1400 时已过 → 命中
    assert store.persistent_mismatches(300, now=1200) == []
    hit = store.persistent_mismatches(300, now=1400)
    assert len(hit) == 1 and hit[0]["account_id"] == "ig1"
    assert hit[0]["mismatch_secs"] == 400


def test_summary_persistent_count():
    store = InjectHealthStore()
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 1000)
    s = store.summary(persist_sec=300, now=1400)
    assert s["persistent_mismatch"] == 1
    # 未给 persist_sec → 不含该键（向后兼容）
    assert "persistent_mismatch" not in store.summary()


def test_recovery_excludes_from_persistent():
    store = InjectHealthStore()
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 1000)
    _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 1500)  # 恢复
    assert store.persistent_mismatches(300, now=2000) == []


def test_recent_events_records_transitions():
    store = InjectHealthStore()
    _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 100)   # ok（首次）
    _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 110)   # ok（不变→不记）
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 120)  # →mismatch_composer
    _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 130)   # →ok
    evs = store.recent_events()
    # 最新在前：ok(130) ← mismatch(120) ← ok(100)；中间不变那条不记
    assert [e["status"] for e in evs] == ["ok", "mismatch_composer", "ok"]
    assert evs[0]["from"] == "mismatch_composer"
