"""Phase P3：/api/relations/health* 端点集成测试（裸 FastAPI + 真 ContactStore）。

覆盖：单人健康卡（含 pending_care 关联）、流失预警榜排序（value_at_risk 优先 + 健康分升序）、
risk 过滤、min_intimacy 过滤、未知 journey 404。
"""
from __future__ import annotations

import sys
import time as _t
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from starlette.testclient import TestClient

from src.contacts.care_schedule import CareScheduleStore
from src.contacts.merge import MergeService
from src.contacts.models import CHANNEL_MESSENGER
from src.contacts.store import ContactStore
from src.skills.intimacy_engine import IntimacyEngine
from src.web.routes.contacts_routes import register_contacts_routes


def _seed_journey(store, gw, ext, *, in_n, out_n, last_offset_days, start_days_ago=30):
    """造一个 journey + 一串 msg_in/msg_out 事件。最后一条在 last_offset_days 天前。"""
    ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id=ext)
    jid = ctx.journey.journey_id
    now = int(_t.time())
    last_ts = now - int(last_offset_days * 86400)
    # 把消息铺在 [start_days_ago 天前, last_ts] 区间内，均匀分布
    span = max(1, start_days_ago * 86400 - int(last_offset_days * 86400))
    with store._lock:
        for i in range(in_n):
            ts = last_ts - int(span * i / max(1, in_n))
            store._conn.execute(
                "INSERT INTO journey_events(event_id, journey_id, trace_id, "
                "event_type, payload_json, ts) VALUES (?, ?, '', 'msg_in', '{}', ?)",
                (f"{ext}_in_{i}", jid, ts),
            )
        for i in range(out_n):
            ts = last_ts - int(span * i / max(1, out_n)) - 30
            store._conn.execute(
                "INSERT INTO journey_events(event_id, journey_id, trace_id, "
                "event_type, payload_json, ts) VALUES (?, ?, '', 'msg_out', '{}', ?)",
                (f"{ext}_out_{i}", jid, ts),
            )
        store._conn.commit()
    return jid


@pytest.fixture
def client(tmp_path):
    from src.contacts.handoff import HandoffTokenService
    from src.contacts.gateway import ContactGateway

    store = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(store, ttl_seconds=3600)
    merge = MergeService(store)
    gw = ContactGateway(store, handoff, merge)
    intim = IntimacyEngine(store)

    app = FastAPI()

    def noop_auth():
        return None

    register_contacts_routes(
        app, api_auth=noop_auth, contacts_store=store, merge_service=merge,
        intimacy_engine=intim,
    )
    care = CareScheduleStore(":memory:")
    app.state.care_schedule_store = care
    from src.inbox.store import InboxStore
    inbox = InboxStore(":memory:")
    app.state.inbox_store = inbox

    tc = TestClient(app)
    tc.store = store          # type: ignore[attr-defined]
    tc.gateway = gw           # type: ignore[attr-defined]
    tc.care = care            # type: ignore[attr-defined]
    tc.inbox = inbox          # type: ignore[attr-defined]
    yield tc
    store.close()


def test_health_card_active_is_healthy(client):
    jid = _seed_journey(client.store, client.gateway, "fb_active",
                        in_n=20, out_n=20, last_offset_days=0.2)
    r = client.get(f"/api/relations/health/{jid}")
    d = r.json()
    assert d["ok"] is True
    assert d["card"]["risk_level"] in ("healthy", "watch")
    assert d["intimacy"]["score"] > 0


def test_health_card_unknown_journey_404(client):
    r = client.get("/api/relations/health/nope")
    assert r.status_code == 404


def test_health_card_pending_care_via_contact_key(client):
    jid = _seed_journey(client.store, client.gateway, "fb_care",
                        in_n=15, out_n=14, last_offset_days=10)
    # 给该 contact_key 排两条 pending 关怀
    from src.contacts.care_commitment import CareCommitment
    for topic in ("面试", "复查"):
        c = CareCommitment(due_at=_t.time() + 3600, event_at=_t.time() + 3600,
                           topic=topic, sentiment="neutral", anchor_text="x",
                           source_text="s", confidence=1.0)
        client.care.add_commitment(c, contact_key="conv-care",
                                   min_confidence=0.0, dedup_window_days=0.0)
    r = client.get(f"/api/relations/health/{jid}?contact_key=conv-care")
    card = r.json()["card"]
    assert any("已排" in x for x in card["reasons"])
    # 有 pending 且处于需干预区间 → care_pending
    assert card["action"] in ("care_pending", "reactivate", "schedule_care")


def _add_care(client, contact_key, n=1):
    from src.contacts.care_commitment import CareCommitment
    for i in range(n):
        c = CareCommitment(due_at=_t.time() + 3600, event_at=_t.time() + 3600,
                           topic=f"事{i}", sentiment="neutral", anchor_text="x",
                           source_text="s", confidence=1.0)
        client.care.add_commitment(c, contact_key=contact_key,
                                   min_confidence=0.0, dedup_window_days=0.0)


def test_health_card_auto_aggregates_pending_care(client):
    """Phase Q：不传 contact_key，也能从 channel_identities 反查到 care pending。"""
    from src.inbox.normalizer import conv_id
    jid = _seed_journey(client.store, client.gateway, "fb_autocare",
                        in_n=15, out_n=14, last_offset_days=10)
    # care 用 CI 反推的 conversation_id 作 key（messenger:a:fb_autocare）
    _add_care(client, conv_id("messenger", "a", "fb_autocare"), n=2)
    r = client.get(f"/api/relations/health/{jid}")  # 注意：无 contact_key
    card = r.json()["card"]
    assert any("已排" in x for x in card["reasons"])
    assert "已排 2" in "".join(card["reasons"])


def test_health_board_auto_aggregates_pending_care(client):
    from src.inbox.normalizer import conv_id
    _seed_journey(client.store, client.gateway, "fb_boardcare",
                  in_n=30, out_n=30, last_offset_days=18, start_days_ago=120)
    _add_care(client, conv_id("messenger", "a", "fb_boardcare"), n=1)
    r = client.get("/api/relations/health-board?limit=10&scan=100")
    items = r.json()["items"]
    mine = next((it for it in items if it["journey_id"]), None)
    # 该沉默强关系应在榜上且建议 care_pending（已排关怀，待发）
    target = next(it for it in items if "已排" in "".join(it["reasons"]))
    assert target["action"] == "care_pending"


def test_health_card_inbox_enrichment(client):
    """Phase R：单卡带回 inbox 跨域语境（情绪趋势/意图/最近文本），复用 CI 反推会话。"""
    from src.inbox.normalizer import conv_id
    from src.inbox.models import InboxConversation
    jid = _seed_journey(client.store, client.gateway, "fb_inbox",
                        in_n=12, out_n=12, last_offset_days=6)
    cid = conv_id("messenger", "a", "fb_inbox")
    client.inbox.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="messenger", account_id="a",
        chat_key="fb_inbox", display_name="Bob", last_text="最近有点累",
        last_ts=_t.time(), unread=2))
    # 两次 update 造情绪历史 → emotion_trend 可计算；并写 last_intent
    client.inbox.update_conv_meta(cid, platform="messenger", intent="complaint", emotion="anger")
    client.inbox.update_conv_meta(cid, platform="messenger", intent="chitchat", emotion="anger")
    r = client.get(f"/api/relations/health/{jid}")
    ibx = r.json()["inbox"]
    assert ibx is not None
    assert ibx["conversation_id"] == cid
    assert ibx["last_intent"] == "chitchat"
    assert ibx["unread"] == 2
    assert ibx["last_text"] == "最近有点累"


def test_health_card_suffix_match_without_writeback(client):
    """Q 延伸前向：单卡用后缀匹配命中带前缀的真实 conv_id（不开 writeback / 不回写 contact_id）。"""
    from src.inbox.models import InboxConversation
    jid = _seed_journey(client.store, client.gateway, "Bob",
                        in_n=15, out_n=14, last_offset_days=10)
    # inbox 真实 conv（chat_key 带前缀，contact_id 留空——未 writeback）
    prefixed = "messenger:a:messenger_rpa:Bob"
    client.inbox.upsert_conversation(InboxConversation(
        conversation_id=prefixed, platform="messenger", account_id="a",
        chat_key="messenger_rpa:Bob", last_ts=_t.time(),
        last_text="最近忙吗", unread=1))
    client.inbox.update_conv_meta(prefixed, platform="messenger",
                                  intent="chitchat", emotion="neutral")
    _add_care(client, prefixed, n=2)
    r = client.get(f"/api/relations/health/{jid}")  # 不传 contact_key
    d = r.json()
    # care 经后缀匹配聚合
    assert any("已排" in x for x in d["card"]["reasons"])
    # inbox 富集也命中真实 conv
    assert d["inbox"] is not None
    assert d["inbox"]["conversation_id"] == prefixed


def test_health_card_inbox_none_when_no_match(client):
    jid = _seed_journey(client.store, client.gateway, "fb_noinbox",
                        in_n=10, out_n=10, last_offset_days=3)
    # 未在 inbox 建任何会话 → inbox 富集为 None（不报错）
    r = client.get(f"/api/relations/health/{jid}")
    assert r.json()["inbox"] is None


def test_health_board_inbox_columns(client):
    """Phase R2：预警榜上榜行带回 compact inbox（情绪趋势/流失风险）。"""
    from src.inbox.normalizer import conv_id
    from src.inbox.models import InboxConversation
    _seed_journey(client.store, client.gateway, "fb_boardinbox",
                  in_n=30, out_n=30, last_offset_days=18, start_days_ago=120)
    cid = conv_id("messenger", "a", "fb_boardinbox")
    client.inbox.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="messenger", account_id="a",
        chat_key="fb_boardinbox", last_ts=_t.time()))
    client.inbox.update_conv_meta(cid, platform="messenger", intent="complaint", emotion="anger")
    client.inbox.update_conv_meta(cid, platform="messenger", intent="complaint", emotion="anger")
    with client.inbox._lock:
        client.inbox._conn.execute(
            "UPDATE conversation_meta SET churn_risk=? WHERE conversation_id=?",
            ("high", cid),
        )
        client.inbox._conn.commit()
    r = client.get("/api/relations/health-board?limit=10&scan=100")
    target = next(
        (it for it in r.json()["items"] if it.get("inbox")), None)
    assert target is not None
    assert target["inbox"]["churn_risk"] == "high"
    assert target["inbox"]["last_intent"] == "complaint"
    assert "emotion_trend" in target["inbox"]
    # compact 块不含单卡专用字段
    assert "last_text" not in target["inbox"]


def test_health_board_inbox_sort_tiebreak(client):
    """R3：同分时高流失+情绪恶化优先上榜（需 config inbox_sort_tiebreak）。"""
    import json
    from types import SimpleNamespace
    from src.inbox.normalizer import conv_id
    from src.inbox.models import InboxConversation

    client.app.state.config_manager = SimpleNamespace(config={
        "companion": {"relations_health": {"health_board": {
            "inbox_sort_tiebreak": True,
        }}},
    })
    _seed_journey(client.store, client.gateway, "fb_sort_a",
                  in_n=30, out_n=30, last_offset_days=18, start_days_ago=120)
    _seed_journey(client.store, client.gateway, "fb_sort_b",
                  in_n=30, out_n=30, last_offset_days=18, start_days_ago=120)
    cid_a = conv_id("messenger", "a", "fb_sort_a")
    cid_b = conv_id("messenger", "a", "fb_sort_b")
    for cid, churn, n_updates in (
        (cid_a, "high", 3),
        (cid_b, "low", 1),
    ):
        client.inbox.upsert_conversation(InboxConversation(
            conversation_id=cid, platform="messenger", account_id="a",
            chat_key=cid.split(":")[-1], last_ts=_t.time()))
        for _ in range(n_updates):
            client.inbox.update_conv_meta(
                cid, platform="messenger", intent="complaint", emotion="anger")
        with client.inbox._lock:
            client.inbox._conn.execute(
                "UPDATE conversation_meta SET churn_risk=? WHERE conversation_id=?",
                (json.dumps({"level": churn}), cid),
            )
            client.inbox._conn.commit()
    r = client.get("/api/relations/health-board?limit=5&scan=100")
    d = r.json()
    assert d["inbox_sort_tiebreak"] is True
    with_inbox = [it for it in d["items"] if it.get("inbox")]
    assert len(with_inbox) >= 2
    scores = [it["score"] for it in with_inbox[:2]]
    if scores[0] == scores[1]:
        assert with_inbox[0]["inbox"]["churn_risk"] == "high"


def test_health_board_inbox_contact_id_fallback(client):
    """Q 延伸：ingest 回写的 conversations.contact_id 反查补 CI 桥漏匹配。"""
    from src.inbox.models import InboxConversation
    jid = _seed_journey(client.store, client.gateway, "Bob",
                        in_n=15, out_n=14, last_offset_days=10)
    j = client.store.get_journey(jid)
    assert j is not None
    # inbox 真实 conv_id（chat_key 带前缀）≠ CI 桥镜像的 messenger:a:Bob
    prefixed = "messenger:a:messenger_rpa:Bob"
    client.inbox.upsert_conversation(InboxConversation(
        conversation_id=prefixed, platform="messenger", account_id="a",
        chat_key="messenger_rpa:Bob", contact_id=j.contact_id, last_ts=_t.time()))
    _add_care(client, prefixed, n=1)
    r = client.get("/api/relations/health-board?limit=10&scan=100")
    target = next(
        (it for it in r.json()["items"] if it.get("action") == "care_pending"), None)
    assert target is not None
    assert any("已排" in x for x in target.get("reasons", []))


def test_health_board_inbox_sort_disabled_by_default(client):
    r = client.get("/api/relations/health-board?limit=5&scan=50")
    assert r.json().get("inbox_sort_tiebreak") is False


def test_backfill_status_not_run(client):
    r = client.get("/api/relations/backfill-status")
    d = r.json()
    assert d["ok"] is True
    assert d["status"] == "not_run"
    assert d["result"] is None


def test_backfill_run_dry_run_then_status(client):
    """回填端点：dry_run 评估命中率不写库，结果回填 status。"""
    from src.inbox.models import InboxConversation
    jid = _seed_journey(client.store, client.gateway, "Bob",
                        in_n=5, out_n=5, last_offset_days=3)
    j = client.store.get_journey(jid)
    # 造一条缺 contact_id 的历史会话，chat_key 带前缀（后缀候选可命中 CI external_id=Bob）
    client.inbox.upsert_conversation(InboxConversation(
        conversation_id="messenger:a:messenger_rpa:Bob", platform="messenger",
        account_id="a", chat_key="messenger_rpa:Bob", last_ts=_t.time()))
    r = client.post("/api/relations/backfill-run?dry_run=true&limit=50")
    res = r.json()["result"]
    assert res["dry_run"] is True
    assert res["resolved"] >= 1
    assert res["written"] == 0
    assert res["trigger"] == "manual"
    # dry_run 不写库
    assert client.inbox.get_conversation(
        "messenger:a:messenger_rpa:Bob")["contact_id"] == ""
    # status 端点现在能读到这次结果
    st = client.get("/api/relations/backfill-status").json()
    assert st["status"] == "ok"
    assert st["result"]["resolved"] >= 1


def test_backfill_run_real_write(client):
    from src.inbox.models import InboxConversation
    jid = _seed_journey(client.store, client.gateway, "Eve",
                        in_n=5, out_n=5, last_offset_days=3)
    client.inbox.upsert_conversation(InboxConversation(
        conversation_id="messenger:a:messenger_rpa:Eve", platform="messenger",
        account_id="a", chat_key="messenger_rpa:Eve", last_ts=_t.time()))
    r = client.post("/api/relations/backfill-run?dry_run=false&limit=50")
    res = r.json()["result"]
    assert res["written"] >= 1
    conv = client.inbox.get_conversation("messenger:a:messenger_rpa:Eve")
    assert conv["contact_id"] != ""


def test_health_board_ranks_value_at_risk_first(client):
    # 1) 强关系但长沉默（value_at_risk）  2) 强关系且活跃（healthy）  3) 弱关系
    client.store_jid_silent = _seed_journey(
        client.store, client.gateway, "fb_silent",
        in_n=30, out_n=30, last_offset_days=20, start_days_ago=120)
    _seed_journey(client.store, client.gateway, "fb_alive",
                  in_n=30, out_n=30, last_offset_days=0.1, start_days_ago=120)
    r = client.get("/api/relations/health-board?limit=10&scan=100")
    d = r.json()
    assert d["ok"] is True and d["count"] >= 2
    # 第一名应是 value_at_risk 的沉默强关系
    top = d["items"][0]
    assert top["value_at_risk"] is True
    assert top["action"] in ("reactivate", "schedule_care", "care_pending")
    # 列表健康分整体升序（最不健康在前）
    scores = [it["score"] for it in d["items"]]
    assert scores == sorted(scores)


def test_health_board_risk_filter(client):
    _seed_journey(client.store, client.gateway, "fb_alive2",
                  in_n=25, out_n=25, last_offset_days=0.1, start_days_ago=60)
    r = client.get("/api/relations/health-board?risk=healthy&scan=50")
    d = r.json()
    assert all(it["risk_level"] == "healthy" for it in d["items"])


def test_health_board_min_intimacy_filter(client):
    # 弱关系（极少消息）应被 min_intimacy 过滤掉
    _seed_journey(client.store, client.gateway, "fb_weak",
                  in_n=1, out_n=0, last_offset_days=1, start_days_ago=2)
    r = client.get("/api/relations/health-board?min_intimacy=90&scan=50")
    assert r.json()["count"] == 0


# ── K2c：变现×关系健康聚合 ──────────────────────────────────

def _attach_entitlements(client):
    from src.utils.entitlement_store import EntitlementStore
    es = EntitlementStore(":memory:")
    client.app.state.entitlement_store = es
    client.entitlements = es  # type: ignore[attr-defined]
    return es


def test_health_card_monetization_payer(client):
    """K2c：单卡聚合该 contact 的 LTV + 当前会员档（复用 CI 反推 conv_id 作 key）。"""
    from src.inbox.normalizer import conv_id
    es = _attach_entitlements(client)
    jid = _seed_journey(client.store, client.gateway, "fb_payer",
                        in_n=15, out_n=14, last_offset_days=10)
    ck = conv_id("messenger", "a", "fb_payer")
    es.record_gift(ck, "rose", amount=5.0)
    es.grant_subscription(ck, "vip", active_until=_t.time() + 86400,
                          record_ledger=False)
    r = client.get(f"/api/relations/health/{jid}")
    mon = r.json()["monetization"]
    assert mon is not None
    assert mon["is_payer"] is True
    assert mon["ltv"] == 5.0
    assert mon["tier"] == "vip"
    assert mon["is_member"] is True


def test_health_card_monetization_none_for_free(client):
    """非付费用户（无流水无会员）→ monetization 为 None 减噪。"""
    _attach_entitlements(client)
    jid = _seed_journey(client.store, client.gateway, "fb_free",
                        in_n=10, out_n=10, last_offset_days=3)
    r = client.get(f"/api/relations/health/{jid}")
    assert r.json()["monetization"] is None


def test_health_card_monetization_absent_when_store_missing(client):
    """变现未启用（无 entitlement_store）→ monetization 字段 None，不报错。"""
    jid = _seed_journey(client.store, client.gateway, "fb_nostore",
                        in_n=10, out_n=10, last_offset_days=3)
    r = client.get(f"/api/relations/health/{jid}")
    assert r.json()["monetization"] is None


def test_health_board_monetization_columns(client):
    """K2c：预警榜上榜行带回变现信号 + payer_count 汇总。"""
    from src.inbox.normalizer import conv_id
    es = _attach_entitlements(client)
    _seed_journey(client.store, client.gateway, "fb_boardpay",
                  in_n=30, out_n=30, last_offset_days=18, start_days_ago=120)
    ck = conv_id("messenger", "a", "fb_boardpay")
    es.record_gift(ck, "rose", amount=12.5)
    es.grant_subscription(ck, "svip", active_until=_t.time() + 86400,
                          record_ledger=False)
    r = client.get("/api/relations/health-board?limit=10&scan=100")
    d = r.json()
    assert d["payer_count"] >= 1
    target = next((it for it in d["items"] if it.get("monetization")), None)
    assert target is not None
    assert target["monetization"]["ltv"] == 12.5
    assert target["monetization"]["tier"] == "svip"
    assert target["monetization"]["is_payer"] is True


def test_health_board_payer_sort_priority(client):
    """K2c①：开 payer_sort_priority 后，付费用户正流失绝对置顶（盖过普通高价值流失）。"""
    from types import SimpleNamespace
    from src.inbox.normalizer import conv_id
    es = _attach_entitlements(client)
    client.app.state.config_manager = SimpleNamespace(config={
        "companion": {"relations_health": {"health_board": {
            "payer_sort_priority": True,
        }}},
    })
    # 关系 A：非付费、强关系长沉默 → value_at_risk（默认会置顶）
    _seed_journey(client.store, client.gateway, "fb_vrisk",
                  in_n=40, out_n=40, last_offset_days=22, start_days_ago=160)
    # 关系 B：付费用户、中度沉默 → at_risk 但非 value_at_risk 顶格
    _seed_journey(client.store, client.gateway, "fb_payerisk",
                  in_n=20, out_n=18, last_offset_days=14, start_days_ago=90)
    ck = conv_id("messenger", "a", "fb_payerisk")
    es.record_gift(ck, "rose", amount=30.0)
    es.grant_subscription(ck, "vip", active_until=_t.time() + 86400,
                          record_ledger=False)
    r = client.get("/api/relations/health-board?limit=10&scan=100")
    d = r.json()
    assert d["payer_sort_priority"] is True
    # 若付费用户确实落入 at_risk/critical，应排在榜首
    payer_item = next(
        (it for it in d["items"]
         if (it.get("monetization") or {}).get("is_payer")), None)
    if payer_item and payer_item.get("risk_level") in ("at_risk", "critical"):
        assert d["items"][0]["journey_id"] == payer_item["journey_id"]


def test_health_board_payer_sort_disabled_by_default(client):
    _attach_entitlements(client)
    r = client.get("/api/relations/health-board?limit=5&scan=50")
    assert r.json().get("payer_sort_priority") is False
