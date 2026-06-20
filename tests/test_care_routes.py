"""Phase O4：/api/care/schedule* 路由契约 + 入站捕获回调。

覆盖：手动加→列表/计数→到期预览→取消；非法入参软失败；
make_care_inbound_cb 在 enabled/capture 开关下的 gated 行为。
"""
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.contacts.care_schedule import CareScheduleStore
from src.web.routes.care_routes import register_care_routes


def _client():
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_care_routes(app, api_auth=_auth, config_manager=None)
    app.state.care_schedule_store = CareScheduleStore(":memory:")
    return TestClient(app), app.state.care_schedule_store


def test_manual_add_then_list_and_summary():
    client, _ = _client()
    r = client.post("/api/care/schedule", json={
        "contact_key": "c1", "platform": "messenger", "chat_key": "fb:1",
        "topic": "面试", "due_in_hours": 48, "source_text": "周五面试",
    })
    assert r.json()["ok"] is True
    rid = r.json()["id"]

    r2 = client.get("/api/care/schedule")
    body = r2.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["items"][0]["id"] == rid
    assert body["summary"]["pending"] == 1


def test_manual_add_requires_fields():
    client, _ = _client()
    r = client.post("/api/care/schedule", json={"contact_key": "c1"})
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "missing"


def test_manual_add_rejects_past_due():
    client, _ = _client()
    r = client.post("/api/care/schedule", json={
        "contact_key": "c1", "topic": "x", "due_at": time.time() - 100,
    })
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "due_in_past"


def test_due_preview_and_cancel():
    client, store = _client()
    # 直接塞一条已到期 pending
    from src.contacts.care_commitment import CareCommitment
    c = CareCommitment(due_at=time.time() - 10, event_at=time.time() - 10,
                       topic="复查", sentiment="neutral", anchor_text="x",
                       source_text="s", confidence=1.0)
    sid = store.add_commitment(c, contact_key="c2", platform="messenger",
                               chat_key="fb:2", min_confidence=0.0, dedup_window_days=0.0)
    assert sid

    due = client.get("/api/care/schedule/due").json()
    assert due["count"] == 1 and due["items"][0]["id"] == sid

    cancel = client.post(f"/api/care/schedule/{sid}/cancel", json={"note": "no"})
    assert cancel.json()["ok"] is True
    assert client.get("/api/care/schedule/due").json()["count"] == 0
    assert client.get("/api/care/schedule").json()["summary"]["cancelled"] == 1


def test_cancel_unknown_returns_not_pending():
    client, _ = _client()
    r = client.post("/api/care/schedule/9999/cancel", json={})
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "not_pending"


def test_send_now_brings_due_forward():
    client, store = _client()
    # 一条 48h 后到期的 pending → send-now 后立刻进 due 列表
    r = client.post("/api/care/schedule", json={
        "contact_key": "c3", "topic": "体检", "due_in_hours": 48,
        "platform": "messenger", "chat_key": "fb:3",
    })
    sid = r.json()["id"]
    assert client.get("/api/care/schedule/due").json()["count"] == 0
    sn = client.post(f"/api/care/schedule/{sid}/send-now", json={})
    assert sn.json()["ok"] is True
    assert client.get("/api/care/schedule/due").json()["count"] == 1


def test_send_now_unknown_returns_not_pending():
    client, _ = _client()
    r = client.post("/api/care/schedule/9999/send-now", json={})
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "not_pending"


# ── Phase O 质量闭环：dry_run 样本审核端点 ──────────────────────────────
def test_care_dry_samples_empty_then_populated():
    from src.monitoring.metrics_store import get_metrics_store
    ms = get_metrics_store()
    ms._care_dry_samples.clear()
    client, _ = _client()
    r = client.get("/api/care/dry-run-samples")
    assert r.json()["ok"] is True and r.json()["count"] == 0

    ms.record_care_dry_run(sample={
        "care_id": 1, "topic": "面试", "platform": "telegram",
        "reply_text": "你之前说的面试怎么样啦？",
    })
    r2 = client.get("/api/care/dry-run-samples").json()
    assert r2["count"] == 1 and r2["samples"][0]["topic"] == "面试"


def test_care_dry_feedback_dislike_adds_blacklist():
    from src.monitoring.metrics_store import get_metrics_store
    ms = get_metrics_store()
    ms._care_dry_samples.clear()
    ms._reactivation_disliked_replies.clear()
    client, _ = _client()
    bad = "你之前说的体检结果出来了吗？"
    ms.record_care_dry_run(sample={"care_id": 9, "topic": "体检", "reply_text": bad})
    ts = ms.care_dry_samples(limit=1)[0]["ts"]

    r = client.post("/api/care/dry-run-feedback",
                    json={"verdict": "dislike", "sample_ts": ts})
    assert r.json()["ok"] is True
    is_sim, _ = ms.is_similar_to_disliked(bad, threshold=0.7)
    assert is_sim is True  # 已进共享黑名单


def test_care_dry_feedback_bad_verdict():
    client, _ = _client()
    r = client.post("/api/care/dry-run-feedback", json={"verdict": "meh"})
    assert r.json()["ok"] is False and r.json()["reason"] == "bad_verdict"


# ── 入站捕获回调 ────────────────────────────────────────────────────────

class _CM:
    def __init__(self, cfg):
        self.config = cfg


def test_capture_cb_gated_off_by_default():
    from src.contacts.care_capture import make_care_inbound_cb
    store = CareScheduleStore(":memory:")
    cb = make_care_inbound_cb(store, _CM({}))  # 无 proactive_care 配置 → 关
    cb({"conversation_id": "c1", "platform": "messenger", "chat_key": "fb:1"}, "周五面试")
    assert store.count() == 0


def test_capture_cb_enabled_captures():
    from src.contacts.care_capture import make_care_inbound_cb
    store = CareScheduleStore(":memory:")
    cm = _CM({"companion": {"proactive_care": {"enabled": True, "capture": True}}})
    cb = make_care_inbound_cb(store, cm)
    cb({"conversation_id": "c1", "platform": "messenger", "chat_key": "fb:1"}, "我下周三要面试")
    assert store.count(status="pending") == 1


def test_capture_cb_capture_flag_off():
    from src.contacts.care_capture import make_care_inbound_cb
    store = CareScheduleStore(":memory:")
    cm = _CM({"companion": {"proactive_care": {"enabled": True, "capture": False}}})
    cb = make_care_inbound_cb(store, cm)
    cb({"conversation_id": "c1", "platform": "messenger"}, "我下周三要面试")
    assert store.count() == 0


def test_capture_cb_never_raises_on_bad_input():
    from src.contacts.care_capture import make_care_inbound_cb
    store = CareScheduleStore(":memory:")
    cm = _CM({"companion": {"proactive_care": {"enabled": True}}})
    cb = make_care_inbound_cb(store, cm)
    cb({}, "")          # 空 conv + 空文本
    cb({"conversation_id": ""}, "周五面试")  # 空 contact_key
    assert store.count() == 0
