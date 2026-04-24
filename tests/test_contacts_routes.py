"""contacts_routes HTTP endpoint 测试（裸 FastAPI + TestClient）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from starlette.testclient import TestClient

from src.contacts.gateway import ContactGateway
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.contacts.models import CHANNEL_MESSENGER
from src.contacts.store import ContactStore
from src.skills.intimacy_engine import IntimacyEngine
from src.skills.reactivation_scheduler import ReactivationScheduler
from src.skills.account_limiter import AccountLimiter
from src.skills.handoff_renderer import HandoffRenderer
from src.skills.handoff_compliance import HandoffComplianceChecker
from src.skills.handoff_readiness import HandoffReadinessScorer
from src.web.routes.contacts_routes import register_contacts_routes


@pytest.fixture
def client(tmp_path):
    store = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(store, ttl_seconds=3600)
    merge = MergeService(store)
    gateway = ContactGateway(store, handoff, merge)

    app = FastAPI()

    def noop_auth():
        return None

    intim = IntimacyEngine(store)
    reactivator = ReactivationScheduler(store, min_silent_days=3, min_intimacy=40.0)
    limiter = AccountLimiter(store, daily_cap=5)
    # 注入完整业务栈到 gateway
    scorer = HandoffReadinessScorer(store, intim, turn_saturation=3, open_threshold=70.0)
    renderer = HandoffRenderer(
        Path(__file__).resolve().parent.parent / "config" / "handoff_scripts.yaml")
    compliance = HandoffComplianceChecker(
        config_path=Path(__file__).resolve().parent.parent / "config" / "handoff_compliance.yaml")
    gateway = ContactGateway(
        store, handoff, merge,
        renderer=renderer, limiter=limiter, compliance=compliance,
        readiness_scorer=scorer,
        line_id_provider=lambda acc: f"@line_{acc}",
    )

    register_contacts_routes(
        app, api_auth=noop_auth, contacts_store=store, merge_service=merge,
        intimacy_engine=intim, reactivation_scheduler=reactivator,
        gateway=gateway, account_limiter=limiter,
    )

    tc = TestClient(app)
    tc.store = store          # type: ignore[attr-defined]
    tc.gateway = gateway      # type: ignore[attr-defined]
    tc.handoff = handoff      # type: ignore[attr-defined]
    tc.merge = merge          # type: ignore[attr-defined]
    tc.intim = intim          # type: ignore[attr-defined]
    tc.reactivator = reactivator  # type: ignore[attr-defined]
    tc.limiter = limiter      # type: ignore[attr-defined]
    yield tc
    store.close()


class TestListContacts:
    def test_empty(self, client):
        r = client.get("/api/contacts")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["items"] == []

    def test_after_ensure(self, client):
        client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            display_name="Alice",
        )
        r = client.get("/api/contacts")
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["primary_name"] == "Alice"

    def test_pagination(self, client):
        for i in range(5):
            client.gateway.on_peer_seen(
                channel=CHANNEL_MESSENGER, account_id="a", external_id=f"fb_{i}")
        r = client.get("/api/contacts?limit=2&offset=0")
        assert len(r.json()["items"]) == 2

    def test_expand_journey_embeds_stage_and_intimacy(self, client):
        client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi")
        r = client.get("/api/contacts?expand=journey")
        item = r.json()["items"][0]
        assert "funnel_stage" in item
        assert item["funnel_stage"] == "ENGAGED"
        assert "intimacy_score" in item
        assert "journey_id" in item

    def test_without_expand_no_journey_field(self, client):
        client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        r = client.get("/api/contacts")
        item = r.json()["items"][0]
        assert "funnel_stage" not in item  # 默认不包含


class TestGetContact:
    def test_404(self, client):
        r = client.get("/api/contacts/nonexistent")
        assert r.status_code == 404

    def test_ok(self, client):
        ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            display_name="Alice")
        r = client.get(f"/api/contacts/{ctx.contact.contact_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["contact"]["primary_name"] == "Alice"
        assert body["journey"]["funnel_stage"] == "INITIAL"
        assert len(body["channel_identities"]) == 1


class TestTimeline:
    def test_404_when_no_journey(self, client):
        r = client.get("/api/contacts/no/timeline")
        assert r.status_code == 404

    def test_events_listed(self, client):
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="你好",
        )
        r = client.get(f"/api/contacts/{ctx.contact.contact_id}/timeline")
        assert r.status_code == 200
        body = r.json()
        types = {e["event_type"] for e in body["events"]}
        assert "msg_in" in types
        assert "contact_created" in types


class TestMergeReviews:
    def test_empty(self, client):
        r = client.get("/api/merge-reviews")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def _setup_review(self, client):
        """造一个 pending review 场景。"""
        m_ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            display_name="Alice Liu", language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        client.store.update_contact(m_ctx.contact.contact_id,
                                     primary_name="Alice Liu",
                                     language_hint="zh",
                                     timezone_hint="Asia/Shanghai")
        client.gateway.issue_handoff(
            messenger_ci_id=m_ctx.channel_identity.channel_identity_id)
        # LINE 侧进来 → 中置信
        outcome = client.gateway.on_line_first_text(
            account_id="a", external_id="line_1",
            display_name="Alice L.",
            language_hint="zh", timezone_hint="Asia/Tokyo",
            text="嗨",
        )
        assert outcome.review_id
        return outcome.review_id, m_ctx.contact.contact_id

    def test_list_shows_review(self, client):
        rid, _ = self._setup_review(client)
        r = client.get("/api/merge-reviews")
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["review_id"] == rid
        # 丰富字段有 candidate_ci + target_contact
        assert items[0]["candidate_ci"] is not None
        assert items[0]["target_contact"] is not None

    def test_approve_merges(self, client):
        rid, target = self._setup_review(client)
        r = client.post(f"/api/merge-reviews/{rid}/approve")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # pending 队列已空
        assert client.get("/api/merge-reviews").json()["items"] == []
        # ci 已迁移
        cis = client.store.list_channel_identities_of(target)
        channels = {ci.channel for ci in cis}
        assert channels == {"messenger", "line"}

    def test_reject_keeps_ci_isolated(self, client):
        rid, target = self._setup_review(client)
        r = client.post(f"/api/merge-reviews/{rid}/reject")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # LINE ci 没搬家
        cis = client.store.list_channel_identities_of(target)
        channels = {ci.channel for ci in cis}
        assert channels == {"messenger"}  # LINE 独立 contact

    def test_approve_unknown_review_returns_400(self, client):
        r = client.post("/api/merge-reviews/nonexistent/approve")
        assert r.status_code == 400


class TestFunnelStats:
    def test_empty_stats(self, client):
        r = client.get("/api/funnel/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["total_contacts"] == 0
        assert body["by_stage"] == {}
        assert body["by_channel"] == {}

    def test_stats_with_data(self, client):
        client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi")
        r = client.get("/api/funnel/stats")
        body = r.json()
        assert body["total_contacts"] == 1
        assert body["by_stage"].get("ENGAGED", 0) >= 1
        assert body["by_channel"].get("messenger", 0) >= 1


class TestJourneyDetail:
    def test_404(self, client):
        r = client.get("/api/journeys/nonexistent")
        assert r.status_code == 404

    def test_ok(self, client):
        ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        r = client.get(f"/api/journeys/{ctx.journey.journey_id}")
        body = r.json()
        assert body["journey"]["journey_id"] == ctx.journey.journey_id
        assert body["journey"]["funnel_stage"] == "INITIAL"


class TestIntimacyRefresh:
    def test_refresh_returns_breakdown(self, client):
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi")
        r = client.post(f"/api/journeys/{ctx.journey.journey_id}/intimacy/refresh")
        assert r.status_code == 200
        body = r.json()
        assert body["intimacy"]["score"] > 0
        # Journey 上 intimacy_score 已写回
        j = client.store.get_journey(ctx.journey.journey_id)
        assert j.intimacy_score == body["intimacy"]["score"]

    def test_refresh_404(self, client):
        r = client.post("/api/journeys/nonexistent/intimacy/refresh")
        assert r.status_code == 404


class TestAccountLimit:
    def test_get_limit(self, client):
        r = client.get("/api/accounts/acc-A/limit")
        assert r.status_code == 200
        body = r.json()
        assert body["account_count"] == 0
        assert body["account_remaining"] == 5

    def test_reset_limit(self, client):
        client.limiter.check_and_reserve("acc-A")
        client.limiter.check_and_reserve("acc-A")
        r = client.post("/api/accounts/acc-A/limit/reset")
        assert r.status_code == 200
        after = client.get("/api/accounts/acc-A/limit").json()
        assert after["account_count"] == 0


class TestHandoffPreview:
    def test_preview_with_warm_chat(self, client):
        import time, uuid
        # 造 warm chat + 模拟 goodbye
        ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="acc-A", external_id="fb_1",
            display_name="Alice", language_hint="zh")
        client.store.update_contact(ctx.contact.contact_id,
                                     language_hint="zh", timezone_hint="Asia/Shanghai")
        jid = ctx.journey.journey_id
        now = int(time.time())
        with client.store._lock:
            for d in range(5):
                for i in range(4):
                    for et in ("msg_in", "msg_out"):
                        client.store._conn.execute(
                            "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
                            "VALUES (?, ?, '', ?, '{}', ?)",
                            (uuid.uuid4().hex, jid, et, now - d * 86400 - i * 60),
                        )
            client.store._conn.commit()
        r = client.get(
            "/api/handoff/preview?messenger_ci_id="
            + ctx.channel_identity.channel_identity_id
            + "&latest_in_text=" + "晚安 去睡了",
        )
        body = r.json()
        assert body["success"] is True
        assert body["reason"] == "dry_run_ok"
        assert "@line_acc-A" in body["text"]
        # dry_run 的 token 是占位
        assert body["details"]["dry_run"] is True
        # Cap 没被扣
        counts = client.get("/api/accounts/acc-A/limit").json()
        assert counts["account_count"] == 0

    def test_preview_not_ready(self, client):
        ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="acc-A", external_id="fb_cold")
        r = client.get(
            "/api/handoff/preview?messenger_ci_id="
            + ctx.channel_identity.channel_identity_id
            + "&latest_in_text=hi",
        )
        body = r.json()
        assert body["success"] is False
        assert body["reason"] == "not_ready"


class TestOpsUI:
    def test_contacts_page_loads(self, client):
        r = client.get("/ops/contacts")
        assert r.status_code == 200
        assert "Contacts" in r.text
        assert "/api/contacts" in r.text    # 页面 JS 引用

    def test_merge_reviews_page_loads(self, client):
        r = client.get("/ops/merge-reviews")
        assert r.status_code == 200
        assert "合并审核队列" in r.text
        assert "/api/merge-reviews" in r.text

    def test_contacts_page_has_funnel_chart(self, client):
        """W4-Ops：contacts 页里有漏斗图（inline SVG，无外部 CDN）。"""
        r = client.get("/ops/contacts")
        html = r.text
        assert "funnel-wrap" in html
        assert "renderFunnel" in html
        assert "FUNNEL_FORWARD" in html
        # 正向漏斗必须按业务顺序排列（关键断言：开头是 INITIAL）
        fwd_idx = html.index("FUNNEL_FORWARD")
        window = html[fwd_idx: fwd_idx + 400]
        assert window.index("'INITIAL'") < window.index("'ENGAGED'")
        assert window.index("'ENGAGED'") < window.index("'HANDOFF_READY'")
        assert window.index("'LINE_ENGAGED'") < window.index("'BONDED'")
        # 不能依赖外部 CDN（符合"零新依赖"约束）
        assert "cdn." not in html.lower(), "不应引入 CDN"
        assert "chart.js" not in html.lower(), "不应引入 Chart.js"


class TestReactivation:
    def test_empty_list(self, client):
        r = client.get("/api/reactivation/candidates")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_list_then_mark(self, client):
        import time as _time
        # 造一个 eligible journey
        ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        jid = ctx.journey.journey_id
        with client.store._lock:
            client.store._conn.execute(
                "UPDATE journeys SET funnel_stage='LINE_ENGAGED', intimacy_score=60.0, "
                "updated_at=? WHERE journey_id=?",
                (int(_time.time()) - 5 * 86400, jid),
            )
            client.store._conn.commit()
        # list
        r = client.get("/api/reactivation/candidates")
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["journey_id"] == jid
        # mark
        r2 = client.post(f"/api/reactivation/{jid}/mark-sent")
        assert r2.status_code == 200
        # 再 list：cooldown 排除
        r3 = client.get("/api/reactivation/candidates")
        assert r3.json()["items"] == []
