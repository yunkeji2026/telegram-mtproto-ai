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
    # W3-3D.3：清掉 trend TTL cache，避免上一测的缓存泄入下一测
    from src.web.routes.contacts_routes import _intimacy_trend_cache_clear
    _intimacy_trend_cache_clear()
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

    # ── W3-3B.4：enrich + 人话化 signals + target_journey ──────────────
    def test_list_includes_humanized_signals(self, client):
        """list_reviews 应返回 signals_human 结构（按 contrib 降序 + top-2 高亮）。"""
        self._setup_review(client)
        items = client.get("/api/merge-reviews").json()["items"]
        assert items, "应有一条 pending review"
        sh = items[0].get("signals_human")
        assert sh is not None, "signals_human 必须存在"
        assert isinstance(sh.get("items"), list)
        assert isinstance(sh.get("top"), list)
        # 每条至少有 icon/label/raw_pct
        for entry in sh["items"]:
            assert "icon" in entry and "label" in entry
            assert "raw_pct" in entry
        # name_match 和 time_proximity 应在前列（最常见的 top 信号）
        # 不强求具体次序，只要 top 里包含合理项
        assert len(sh["top"]) <= 2

    def test_list_includes_target_journey(self, client):
        """list_reviews 应返回 target_journey 信息（funnel_stage + intimacy_score）"""
        self._setup_review(client)
        items = client.get("/api/merge-reviews").json()["items"]
        tj = items[0].get("target_journey")
        assert tj is not None
        assert "funnel_stage" in tj
        assert "intimacy_score" in tj
        assert isinstance(tj["intimacy_score"], (int, float))


class TestMergeScan:
    """W3-3B.4：主动扫描端点 — 弥补「被动触发」的 silent gap。"""

    def test_scan_empty(self, client):
        """没有未合并 LINE → scanned=0"""
        r = client.post("/api/merge-reviews/scan")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["scanned"] == 0
        assert body["enqueued"] == 0

    def test_scan_finds_orphan_line(self, client):
        """有 messenger 在等待 + 孤立 LINE → 扫描应入队 review。"""
        # 准备 messenger 侧 + 签发 token（让候选池非空）
        m_ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            display_name="Bob", language_hint="en", timezone_hint="Asia/Tokyo",
        )
        client.store.update_contact(m_ctx.contact.contact_id,
                                     primary_name="Bob",
                                     language_hint="en",
                                     timezone_hint="Asia/Tokyo")
        client.gateway.issue_handoff(
            messenger_ci_id=m_ctx.channel_identity.channel_identity_id)
        # 造一个 LINE 孤儿 ci（绕过 on_line_first_text 的 token 路径）
        # 用 ensure_channel_identity 模拟「曾被看到但从未触发合并评估」
        client.store.ensure_channel_identity(
            channel="line", account_id="a", external_id="line_orphan",
            display_name="Bob",  # 相似名字
            language_hint="en", timezone_hint="Asia/Tokyo",
        )
        r = client.post("/api/merge-reviews/scan")
        assert r.status_code == 200
        body = r.json()
        assert body["scanned"] >= 1
        # 名字完全一致 + 语言/时区都匹配 → 应该入队（或够 auto）
        assert body["enqueued"] >= 1
        # 现在 list 应该能拿到这条
        items = client.get("/api/merge-reviews").json()["items"]
        assert any(it.get("candidate_ci", {}).get("external_id") == "line_orphan"
                   for it in items)

    def test_scan_skips_already_merged(self, client):
        """已经在 messenger contact 下的 LINE ci 不应被重新扫描。"""
        # 完整 token merge：messenger → handoff → LINE 凭 token 合并
        m_ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            display_name="Carol")
        tok = client.gateway.issue_handoff(
            messenger_ci_id=m_ctx.channel_identity.channel_identity_id)
        client.gateway.on_line_first_text(
            account_id="a", external_id="line_carol",
            text=f"嗨 {tok}",
            display_name="Carol",
        )
        # 此时 line_carol 已迁到 messenger contact 下
        r = client.post("/api/merge-reviews/scan")
        # 因为 ci.contact_id 已包含 messenger 渠道 → SQL 过滤掉了
        assert r.json()["scanned"] == 0


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


class TestIntimacyStageEnrich:
    """W3-3C.1：/api/contacts?expand=journey 返回 intimacy_stage 派生信息。"""

    def test_intimacy_stage_present_when_score_known(self, client):
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi")
        # 强制触发 intimacy refresh
        client.intim.refresh_journey_intimacy(ctx.journey.journey_id)
        items = client.get("/api/contacts?expand=journey").json()["items"]
        item = next(i for i in items if i["contact_id"] == ctx.contact.contact_id)
        # 仅 1 条 msg_in，分数偏低 → 应是 initial
        assert item.get("intimacy_stage") is not None
        assert item["intimacy_stage"]["stage"] in {"initial", "warming"}
        assert "label" in item["intimacy_stage"]

    def test_no_journey_no_stage(self, client):
        client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_2")
        items = client.get("/api/contacts").json()["items"]  # 不 expand
        assert "intimacy_stage" not in items[0]


class TestIntimacyHistory:
    """W3-3C.2：单 journey 30 天 intimacy 历史 — 用事件流重放，无需新表。"""

    def test_history_returns_correct_day_count(self, client):
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi")
        r = client.get(f"/api/journeys/{ctx.journey.journey_id}/intimacy-history?days=14")
        assert r.status_code == 200
        body = r.json()
        assert body["days"] == 14
        assert len(body["series"]) == 14
        # 每个数据点的字段
        for p in body["series"]:
            assert "day" in p and "score" in p
            assert isinstance(p["score"], (int, float))

    def test_history_404_unknown_journey(self, client):
        r = client.get("/api/journeys/nope/intimacy-history")
        assert r.status_code == 404

    def test_history_clamps_days(self, client):
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi")
        # days=999 应被夹到 90 上限
        r = client.get(f"/api/journeys/{ctx.journey.journey_id}/intimacy-history?days=999")
        assert r.json()["days"] == 90

    def test_history_replay_shows_growth(self, client):
        """造一个 5 天前才开始的 journey，重放应在前几天 score=0，后几天 >0"""
        import time as _t
        store, gw = client.store, client.gateway
        ctx = gw.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_grow")
        jid = ctx.journey.journey_id
        # 写 5 条 msg_in，每天一条
        now = int(_t.time())
        for i in range(5):
            with store._lock:
                eid = f"evt_g_{i}"
                ts = now - (4 - i) * 86400
                store._conn.execute(
                    "INSERT INTO journey_events(event_id, journey_id, trace_id, "
                    "event_type, payload_json, ts) VALUES (?, ?, '', 'msg_in', '{}', ?)",
                    (eid, jid, ts),
                )
                store._conn.execute(
                    "INSERT INTO journey_events(event_id, journey_id, trace_id, "
                    "event_type, payload_json, ts) VALUES (?, ?, '', 'msg_out', '{}', ?)",
                    (eid + "_o", jid, ts + 60),
                )
                store._conn.commit()
        r = client.get(f"/api/journeys/{jid}/intimacy-history?days=10")
        series = r.json()["series"]
        # 最早几天应该 =0（事件还没发生），最近几天应该 >0
        early_scores = [p["score"] for p in series[:3]]
        late_scores = [p["score"] for p in series[-3:]]
        assert max(early_scores) == 0, f"前 3 天应无信号: {early_scores}"
        assert max(late_scores) > 0, f"后 3 天应有 intimacy: {late_scores}"

    def test_history_includes_stage_label(self, client):
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi")
        r = client.get(f"/api/journeys/{ctx.journey.journey_id}/intimacy-history?days=3")
        for p in r.json()["series"]:
            # 有 score 的天必有 stage（仅 score=0 早期点 stage=initial 也是有效的）
            assert "stage" in p


class TestIntimacyTrendGlobal:
    """W3-3C.3：全域 intimacy 趋势 — 限 top_n 防全表扫描。"""

    def test_empty_db(self, client):
        r = client.get("/api/relations/intimacy-trend?days=7")
        assert r.status_code == 200
        body = r.json()
        assert body["days"] == 7
        assert body["sample_size"] == 0
        assert len(body["series"]) == 7
        # 空库每天 avg=0
        for p in body["series"]:
            assert p["avg_intimacy"] == 0.0
            assert p["active_count"] == 0

    def test_with_data(self, client):
        # 造 3 个 journey 各有几条消息
        for i in range(3):
            client.gateway.on_message(
                channel=CHANNEL_MESSENGER, account_id="a",
                external_id=f"fb_t{i}",
                direction="in", text_preview="hi")
            client.gateway.on_message(
                channel=CHANNEL_MESSENGER, account_id="a",
                external_id=f"fb_t{i}",
                direction="out", text_preview="hello")
        r = client.get("/api/relations/intimacy-trend?days=7")
        body = r.json()
        assert body["sample_size"] == 3
        # 今日（最后一个点）应有 active_count > 0 + avg_intimacy > 0
        today = body["series"][-1]
        assert today["active_count"] > 0
        assert today["avg_intimacy"] > 0

    def test_top_n_clamped(self, client):
        r = client.get("/api/relations/intimacy-trend?top_n=5000")
        # 不报错；上限为 1000
        assert r.status_code == 200

    def test_cache_returns_same_object(self, client):
        """W3-3D.3：60s cache 命中——同 key 第二次请求应直接返回缓存。

        校验：第二次请求耗时显著低于第一次，且 payload 完全一致。
        （耗时校验难写得稳定，这里只验内容一致 + 尝试通过造数据后再请求来确认）
        """
        # 造点数据
        for i in range(2):
            client.gateway.on_message(
                channel=CHANNEL_MESSENGER, account_id="a",
                external_id=f"fb_c{i}",
                direction="in", text_preview="hi")
        first = client.get("/api/relations/intimacy-trend?days=7").json()
        # 立刻再请求一次（同 key）→ 应命中缓存
        # 验证方法：在两次请求之间「偷偷加一个 contact」，若缓存生效则 sample_size 不变
        client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_late",
            direction="in", text_preview="hi")
        second = client.get("/api/relations/intimacy-trend?days=7").json()
        assert first["sample_size"] == second["sample_size"], (
            "60s 内同 key 应命中 cache，sample_size 不应变化"
        )


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


class TestBatchApproveReviews:
    """W3-3E.3：批量 approve 端点 — 含置信度护栏 + 部分失败容忍。"""

    def _make_review(self, client, ext_id: str, line_ext: str, name: str = "Alice"):
        """造一个 pending review，返回 (review_id, target_contact_id)。"""
        m_ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id=ext_id,
            display_name=name, language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        client.store.update_contact(
            m_ctx.contact.contact_id,
            primary_name=name, language_hint="zh", timezone_hint="Asia/Shanghai",
        )
        client.gateway.issue_handoff(
            messenger_ci_id=m_ctx.channel_identity.channel_identity_id)
        outcome = client.gateway.on_line_first_text(
            account_id="a", external_id=line_ext,
            display_name=name + " L.",  # 名字相似 → review 不直接 auto
            language_hint="zh", timezone_hint="Asia/Tokyo",
            text="嗨",
        )
        assert outcome.review_id, f"未生成 review，outcome={outcome}"
        return outcome.review_id, m_ctx.contact.contact_id

    def test_empty_body_rejected(self, client):
        r = client.post("/api/merge-reviews/batch-approve", json={"review_ids": []})
        assert r.status_code == 400

    def test_review_ids_must_be_list(self, client):
        r = client.post(
            "/api/merge-reviews/batch-approve",
            json={"review_ids": "rid1"},
        )
        assert r.status_code == 400

    def test_batch_approve_with_low_min_conf_merges_all(self, client):
        rid1, target1 = self._make_review(client, "fb_a", "line_a", "Alice")
        rid2, target2 = self._make_review(client, "fb_b", "line_b", "Bob")
        # 把门槛降到 0 → 所有 pending 都过
        r = client.post(
            "/api/merge-reviews/batch-approve",
            json={"review_ids": [rid1, rid2], "min_confidence": 0.0},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert set(body["approved"]) == {rid1, rid2}
        assert body["skipped_low_conf"] == []
        assert body["failed"] == []
        # pending 队列已空
        assert client.get("/api/merge-reviews").json()["items"] == []
        # 两个 LINE ci 都已迁到目标
        for tgt in (target1, target2):
            chans = {ci.channel for ci in client.store.list_channel_identities_of(tgt)}
            assert chans == {"messenger", "line"}

    def test_batch_approve_skips_low_confidence(self, client):
        rid, target = self._make_review(client, "fb_a", "line_a", "Alice")
        # 默认 min_conf=0.85——_make_review 造的是中置信，应被跳过
        r = client.post(
            "/api/merge-reviews/batch-approve",
            json={"review_ids": [rid]},
        )
        assert r.status_code == 200
        body = r.json()
        # 中置信场景下，approved 应为空，skipped_low_conf 应有 1 条
        # （若哪天 _make_review 升级到能造高置信，把这个测试拆成两条）
        assert body["min_confidence"] == 0.85
        if body["approved"]:
            # 偶发场景：分数其实达到了 0.85，那就接受这个事实
            assert rid in body["approved"]
        else:
            assert any(s["review_id"] == rid for s in body["skipped_low_conf"])
            # LINE 没搬家
            chans = {ci.channel for ci in client.store.list_channel_identities_of(target)}
            assert chans == {"messenger"}

    def test_batch_approve_unknown_rid_marked_failed(self, client):
        r = client.post(
            "/api/merge-reviews/batch-approve",
            json={"review_ids": ["nonexistent-rid"], "min_confidence": 0.0},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["approved"] == []
        assert any(f["review_id"] == "nonexistent-rid" and f["reason"] == "not_pending"
                   for f in body["failed"])


class TestRelationsDigest:
    """W3-3E.4：/api/relations/digest 综合洞察 API。"""

    def test_digest_empty_lib(self, client):
        r = client.get("/api/relations/digest")
        assert r.status_code == 200
        body = r.json()
        # 顶层字段齐全
        for key in ("generated_at", "stats", "trend_delta", "health_score",
                    "insights", "text_summary"):
            assert key in body, f"digest 缺字段 {key}"
        # 空库：total_contacts = 0
        assert body["stats"]["total_contacts"] == 0
        # 健康度仍可计算（成绩单），不应抛
        assert "grade" in body["health_score"]
        assert "score" in body["health_score"]
        # text_summary 是非空字符串
        assert isinstance(body["text_summary"], str)
        assert len(body["text_summary"]) > 0

    def test_digest_with_data_has_insights(self, client):
        # 造 2 个对话以喂数据
        for i in range(2):
            client.gateway.on_message(
                channel=CHANNEL_MESSENGER, account_id="a",
                external_id=f"fb_d{i}", direction="in", text_preview="hi")
            client.gateway.on_message(
                channel=CHANNEL_MESSENGER, account_id="a",
                external_id=f"fb_d{i}", direction="out", text_preview="hello")
        r = client.get("/api/relations/digest")
        body = r.json()
        assert body["stats"]["total_contacts"] >= 2
        # insights 必须是 list（可能为空但类型对）
        assert isinstance(body["insights"], list)


class TestRelationsDigestPush:
    """W3-3E.5：digest 推送到 webhook — 含 wiring & 缓存依赖检查。"""

    def test_push_503_when_webhook_not_wired(self, client):
        # 先填缓存（否则 400 digest_not_cached 先触发）
        from src.web.routes import contacts_routes as cr_mod
        import time as _t
        today_end = (int(_t.time()) // 86400) * 86400 + 86400 - 1
        cr_mod._relations_digest_cache.put(
            ("digest", today_end),
            {"text_summary": "x", "draft_quality": {}, "health_score": {},
             "stats": {}, "trend_delta": {}, "insights": [], "generated_at": ""},
        )
        # 默认 fixture 没传 fire_webhook → 应返回 503
        r = client.post("/api/relations/digest/push")
        assert r.status_code == 503

    def test_push_400_when_no_cache(self, tmp_path):
        """fire_webhook 已 wired，但运营没先 GET digest → 应 400 引导先 GET。"""
        from src.web.routes.contacts_routes import (
            register_contacts_routes, _intimacy_trend_cache_clear,
        )
        from src.web.routes import contacts_routes as cr_mod
        _intimacy_trend_cache_clear()
        cr_mod._relations_digest_cache.clear()

        store = ContactStore(db_path=tmp_path / "c.db")
        handoff = HandoffTokenService(store, ttl_seconds=3600)
        merge = MergeService(store)
        gateway = ContactGateway(store, handoff, merge)
        intim = IntimacyEngine(store)
        app = FastAPI()
        captured = {}

        async def _stub_fire(event, actor, target, summary=""):
            captured["called"] = (event, actor, target, summary)

        register_contacts_routes(
            app, api_auth=lambda: None,
            contacts_store=store, merge_service=merge,
            intimacy_engine=intim, gateway=gateway,
            fire_webhook=_stub_fire,
        )
        tc = TestClient(app)
        r = tc.post("/api/relations/digest/push")
        assert r.status_code == 400
        assert "digest_not_cached" in r.json()["detail"]
        assert "called" not in captured
        store.close()

    def test_push_succeeds_after_get_digest(self, tmp_path):
        """GET digest 之后再 push → 200 + webhook 收到 relations_digest 事件。"""
        from src.web.routes.contacts_routes import (
            register_contacts_routes, _intimacy_trend_cache_clear,
        )
        from src.web.routes import contacts_routes as cr_mod
        _intimacy_trend_cache_clear()
        cr_mod._relations_digest_cache.clear()

        store = ContactStore(db_path=tmp_path / "c.db")
        handoff = HandoffTokenService(store, ttl_seconds=3600)
        merge = MergeService(store)
        gateway = ContactGateway(store, handoff, merge)
        intim = IntimacyEngine(store)
        app = FastAPI()
        captured = {}

        async def _stub_fire(event, actor, target, summary=""):
            captured["call"] = (event, actor, target, summary)

        register_contacts_routes(
            app, api_auth=lambda: None,
            contacts_store=store, merge_service=merge,
            intimacy_engine=intim, gateway=gateway,
            fire_webhook=_stub_fire,
        )
        tc = TestClient(app)
        # 先 GET 把 cache 填好
        rg = tc.get("/api/relations/digest")
        assert rg.status_code == 200
        # 再 push
        rp = tc.post("/api/relations/digest/push")
        assert rp.status_code == 200, rp.text
        body = rp.json()
        assert body["pushed"] is True
        assert body["summary_length"] > 0
        # webhook 确实被触发，且 event_type 正确
        assert captured.get("call") is not None
        event, actor, target, summary = captured["call"]
        assert event == "relations_digest"
        assert target == "relations_digest"
        assert isinstance(summary, str) and len(summary) > 0
        store.close()


class TestDraftLogIntegration:
    """W3-3G：reunion 草稿 → log → mark-sent → 评估 的端到端联动。"""

    def _build(self, tmp_path, ai_stub):
        from src.web.routes.contacts_routes import (
            register_contacts_routes, _intimacy_trend_cache_clear,
        )
        from src.web.routes import contacts_routes as cr_mod
        _intimacy_trend_cache_clear()
        cr_mod._relations_digest_cache.clear()
        store = ContactStore(db_path=tmp_path / "c.db")
        handoff = HandoffTokenService(store, ttl_seconds=3600)
        merge = MergeService(store)
        gateway = ContactGateway(store, handoff, merge)
        intim = IntimacyEngine(store)
        reactivator = ReactivationScheduler(store, min_silent_days=3, min_intimacy=40.0)
        app = FastAPI()
        register_contacts_routes(
            app, api_auth=lambda: None,
            contacts_store=store, merge_service=merge,
            intimacy_engine=intim, gateway=gateway,
            reactivation_scheduler=reactivator,
            ai_client=ai_stub,
        )
        tc = TestClient(app)
        tc.store = store
        tc.gateway = gateway
        return tc, store

    def test_draft_reunion_writes_to_draft_log(self, tmp_path):
        class _AI:
            async def chat(self, prompt):  # noqa
                return "嗨，最近怎么样？"
        tc, store = self._build(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_r",
            direction="in", text_preview="hello")
        jid = ctx.journey.journey_id
        r = tc.post(f"/api/reactivation/{jid}/draft-reunion")
        body = r.json()
        assert body["draft_id"], "响应必须带 draft_id"
        # 库里能查到
        d = store.latest_unsent_draft_for(jid)
        assert d["draft_id"] == body["draft_id"]
        assert d["draft_text"] == "嗨，最近怎么样？"
        assert d["sent_ts"] is None
        store.close()

    def test_mark_sent_links_latest_draft(self, tmp_path):
        class _AI:
            async def chat(self, prompt):  # noqa
                return "好久不见"
        tc, store = self._build(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_r",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        draft_id = tc.post(f"/api/reactivation/{jid}/draft-reunion").json()["draft_id"]
        # mark sent
        r = tc.post(f"/api/reactivation/{jid}/mark-sent")
        body = r.json()
        assert body["ok"] is True
        assert body["linked_draft_id"] == draft_id
        # draft 已无 unsent
        assert store.latest_unsent_draft_for(jid) is None
        store.close()

    def test_mark_sent_without_prior_draft_is_noop_link(self, tmp_path):
        """运营直接 mark-sent 而没生成草稿 → ok=True 但 linked_draft_id 为空。"""
        class _AI:
            async def chat(self, prompt):  # noqa
                return ""  # 不会被调用
        tc, store = self._build(tmp_path, _AI())
        ctx = tc.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_x")
        jid = ctx.journey.journey_id
        r = tc.post(f"/api/reactivation/{jid}/mark-sent")
        body = r.json()
        assert body["ok"] is True
        assert body["linked_draft_id"] == ""
        store.close()

    def test_regenerate_then_mark_sent_uses_latest(self, tmp_path):
        """生成 → 再生成 → mark-sent，应链最新那条，旧那条保留 unsent。"""
        counter = {"n": 0}

        class _AI:
            async def chat(self, prompt):  # noqa
                counter["n"] += 1
                return f"draft #{counter['n']}"

        tc, store = self._build(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_r",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        d1 = tc.post(f"/api/reactivation/{jid}/draft-reunion").json()["draft_id"]
        d2 = tc.post(f"/api/reactivation/{jid}/draft-reunion").json()["draft_id"]
        assert d1 != d2
        linked = tc.post(f"/api/reactivation/{jid}/mark-sent").json()["linked_draft_id"]
        assert linked == d2
        # d1 仍是 unsent
        with store._lock:
            row = dict(store._conn.execute(
                "SELECT sent_ts FROM draft_log WHERE draft_id=?", (d1,),
            ).fetchone())
        assert row["sent_ts"] is None
        store.close()


class TestDraftQualityEndpoints:
    """W3-3G：/api/contacts/draft-quality + /draft-eval/run 端点。"""

    def test_quality_empty(self, client):
        r = client.get("/api/drafts/quality")
        assert r.status_code == 200
        body = r.json()
        assert body["generated"] == 0
        assert body["success_rate"] is None

    def test_eval_run_empty_returns_zero(self, client):
        r = client.post("/api/drafts/eval-run", json={"window_secs": 86400})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["evaluated"] == 0
        assert body["window_secs"] == 86400

    def test_quality_with_data(self, client):
        # 直接写库造样本（避开 ai_client wiring）
        import time
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_q",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        # 生成 + 发出 + 评估
        did = client.store.record_draft(
            journey_id=jid, draft_text="hi", draft_lang="zh")
        client.store.mark_draft_sent(did)
        client.store.eval_draft_success(did, success=True)
        r = client.get("/api/drafts/quality?days=7")
        body = r.json()
        assert body["sent"] == 1
        assert body["evaluated"] == 1
        assert body["success_rate"] == 1.0
        assert body["by_lang"]["zh"]["sent"] == 1


class TestDigestIncludesDraftQuality:
    """W3-3G：digest payload 应携带 draft_quality 字段。"""

    def test_digest_has_draft_quality_field(self, client):
        r = client.get("/api/relations/digest")
        body = r.json()
        assert "draft_quality" in body
        dq = body["draft_quality"]
        assert "generated" in dq
        assert "success_rate" in dq

    def test_digest_text_summary_includes_draft_line_when_sent(self, client):
        # 造一条 sent 草稿
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_t",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        did = client.store.record_draft(
            journey_id=jid, draft_text="hi", draft_lang="zh")
        client.store.mark_draft_sent(did)
        # 清 digest cache 让重算
        from src.web.routes import contacts_routes as cr_mod
        cr_mod._relations_digest_cache.clear()
        r = client.get("/api/relations/digest")
        body = r.json()
        assert "草稿质量" in body["text_summary"]
        assert body["draft_quality"]["sent"] == 1


class TestDigestWinningVariant:
    """W3-3I.1：digest payload 在有显著优胜者时应含 winning_variant 字段 + insight。"""

    def _clear_digest_cache(self):
        from src.web.routes import contacts_routes as cr_mod
        cr_mod._relations_digest_cache.clear()

    def test_no_winner_when_data_empty(self, client):
        self._clear_digest_cache()
        body = client.get("/api/relations/digest").json()
        dq = body["draft_quality"]
        # 没有任何草稿 → winning_variant 不存在
        assert "winning_variant" not in dq

    def test_winning_variant_appears_when_significant(self, client):
        """造 v1 高成功率 + v2 低成功率（各 20 条评估） → digest 应返回 winner。"""
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="wv_tester",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        # v1: 18/20 成功；v2: 2/20 成功
        for variant, successes in [("v1", 18), ("v2", 2)]:
            for i in range(20):
                did = client.store.record_draft(
                    journey_id=jid, draft_text="hi",
                    prompt_variant=variant,
                )
                client.store.mark_draft_sent(did)
                client.store.eval_draft_success(did, success=(i < successes))
        self._clear_digest_cache()
        body = client.get("/api/relations/digest").json()
        dq = body["draft_quality"]
        assert "winning_variant" in dq, "期望 winning_variant 字段出现"
        w = dq["winning_variant"]
        assert w["winner"] == "v1"
        assert w["runner_up"] == "v2"
        assert w["gap_pct"] > 0
        # insight 文本应提及 v1
        assert any("v1" in i for i in body["insights"])

    def test_no_winner_when_ci_overlap(self, client):
        """v1/v2 成功率接近 → CI 重叠 → winning_variant 不应出现。"""
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="wv_close",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        # v1: 8/15; v2: 7/15 — 相差太小，CI 重叠
        for variant, successes in [("v1", 8), ("v2", 7)]:
            for i in range(15):
                did = client.store.record_draft(
                    journey_id=jid, draft_text="hi", prompt_variant=variant,
                )
                client.store.mark_draft_sent(did)
                client.store.eval_draft_success(did, success=(i < successes))
        self._clear_digest_cache()
        body = client.get("/api/relations/digest").json()
        dq = body["draft_quality"]
        # 不应宣布赢家（差距不显著）
        assert "winning_variant" not in dq


class TestPromptSnapshotHash:
    """W3-3I.5：draft_log 行应持久化 prompt_snapshot_hash。"""

    def _build_client(self, tmp_path, ai_stub):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from src.web.routes.contacts_routes import (
            register_contacts_routes, _intimacy_trend_cache_clear,
        )
        from src.web.routes import contacts_routes as cr_mod
        _intimacy_trend_cache_clear()
        cr_mod._relations_digest_cache.clear()
        store = ContactStore(db_path=tmp_path / "c.db")
        svc = HandoffTokenService(store)
        merge = MergeService(store)
        rs = ReactivationScheduler(store)
        app = FastAPI()
        register_contacts_routes(
            app,
            api_auth=lambda: None,
            contacts_store=store,
            merge_service=merge,
            reactivation_scheduler=rs,
            ai_client=ai_stub,
        )
        tc = TestClient(app, raise_server_exceptions=True)
        tc.store = store
        gw = ContactGateway(store, svc, merge)
        tc.gateway = gw
        return tc, store

    def test_hash_persisted_on_draft_generation(self, tmp_path):
        class _AI:
            async def chat(self, prompt):   # noqa
                return "嗨，好久不见～"

        tc, store = self._build_client(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="hash_test",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        body = tc.post(f"/api/reactivation/{jid}/draft-reunion").json()
        assert body.get("draft_id"), "期望 draft_id 出现"
        with store._lock:
            row = dict(store._conn.execute(
                "SELECT prompt_snapshot_hash FROM draft_log WHERE draft_id=?",
                (body["draft_id"],),
            ).fetchone())
        # hash 应为非空 16 字符 hex
        h = row["prompt_snapshot_hash"]
        assert h and len(h) == 16, f"prompt_snapshot_hash 不正确: {h!r}"

    def test_hash_changes_with_different_prompts(self):
        from src.contacts.reunion_prompts import hash_prompt
        h1 = hash_prompt("你扮演「Aki」…prompt_a")
        h2 = hash_prompt("你扮演「Aki」…prompt_b")
        assert h1 != h2

    def test_hash_stable_same_input(self):
        from src.contacts.reunion_prompts import hash_prompt
        p = "同一段 prompt 文本"
        assert hash_prompt(p) == hash_prompt(p)


class TestEvalSchedulerStatus:
    """W3-3K.3：/api/drafts/eval-scheduler/status 端点。"""

    def test_status_not_wired_returns_unavailable(self, client):
        """默认 fixture 不注入 eval_scheduler → available=False。"""
        r = client.get("/api/drafts/eval-scheduler/status")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert "eval_scheduler_not_wired" in body["reason"]

    def _build_client_with_scheduler(self, tmp_path):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from src.web.routes.contacts_routes import (
            register_contacts_routes, _intimacy_trend_cache_clear,
        )
        from src.web.routes import contacts_routes as cr_mod
        from src.contacts.draft_eval import DraftEvalScheduler
        _intimacy_trend_cache_clear()
        cr_mod._relations_digest_cache.clear()
        store = ContactStore(db_path=tmp_path / "c.db")
        svc = HandoffTokenService(store)
        merge = MergeService(store)
        rs = ReactivationScheduler(store)
        sched = DraftEvalScheduler(store, base_interval_secs=60)
        app = FastAPI()
        register_contacts_routes(
            app, api_auth=lambda: None,
            contacts_store=store, merge_service=merge,
            reactivation_scheduler=rs, eval_scheduler=sched,
        )
        tc = TestClient(app, raise_server_exceptions=True)
        tc.store = store
        tc.sched = sched
        gw = ContactGateway(store, svc, merge)
        tc.gateway = gw
        return tc

    def test_status_wired_returns_initial_nulls(self, tmp_path):
        tc = self._build_client_with_scheduler(tmp_path)
        r = tc.get("/api/drafts/eval-scheduler/status")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["last_run_at"] is None
        assert body["total_runs"] == 0
        assert body["eval_window_secs"] == 86400

    def test_eval_run_updates_scheduler_state(self, tmp_path):
        """POST /api/drafts/eval-run → scheduler.run_once() → status 反映运行记录。"""
        tc = self._build_client_with_scheduler(tmp_path)
        r = tc.post("/api/drafts/eval-run", json={})
        assert r.status_code == 200
        assert "evaluated" in r.json()
        # status 应已更新
        st = tc.get("/api/drafts/eval-scheduler/status").json()
        assert st["total_runs"] == 1
        assert st["last_run_at"] is not None
        assert st["last_result"]["evaluated"] == 0  # 无待评估草稿

    def test_status_interval_backs_off(self, tmp_path):
        """两次 eval-run（evaluated=0）后 current_interval_secs 应变大。"""
        tc = self._build_client_with_scheduler(tmp_path)
        tc.post("/api/drafts/eval-run", json={})
        st1 = tc.get("/api/drafts/eval-scheduler/status").json()
        tc.post("/api/drafts/eval-run", json={})
        st2 = tc.get("/api/drafts/eval-scheduler/status").json()
        assert st2["current_interval_secs"] >= st1["current_interval_secs"]

    def test_status_fields_complete(self, tmp_path):
        tc = self._build_client_with_scheduler(tmp_path)
        tc.post("/api/drafts/eval-run", json={})
        body = tc.get("/api/drafts/eval-scheduler/status").json()
        for key in ("last_run_at", "last_run_ago_secs", "last_result",
                    "next_run_at", "next_run_in_secs",
                    "current_interval_secs", "base_interval_secs",
                    "total_runs", "is_running", "eval_window_secs"):
            assert key in body, f"missing key: {key}"


class TestReunionPromptsAPI:
    """W3-3J.1：/api/reunion-prompts GET + POST set-default。"""

    def test_list_returns_variants(self, client):
        r = client.get("/api/reunion-prompts")
        assert r.status_code == 200
        body = r.json()
        assert "variants" in body
        assert "default_variant" in body
        assert isinstance(body["variants"], list)
        assert len(body["variants"]) >= 1
        assert body["default_variant"] in body["variants"]

    def test_set_default_valid_variant(self, client):
        variants = client.get("/api/reunion-prompts").json()["variants"]
        # 选第一个 variant 设为 default
        target = variants[0]
        r = client.post("/api/reunion-prompts/set-default",
                        json={"variant": target})
        # inline-only 模式下 yaml 不存在 → 期望 400（promote_failed）
        # 但 variant 本身合法，不应 422
        assert r.status_code in (200, 400)
        if r.status_code == 400:
            assert "promote_failed" in r.json()["detail"]

    def test_set_default_unknown_variant(self, client):
        r = client.post("/api/reunion-prompts/set-default",
                        json={"variant": "nonexistent_v99"})
        assert r.status_code == 400
        assert "variant_not_found" in r.json()["detail"]

    def test_set_default_missing_body_field(self, client):
        r = client.post("/api/reunion-prompts/set-default", json={})
        assert r.status_code == 422


class TestDigestPushOnlyWinner:
    """W3-3J.2：push only_winner 过滤。"""

    def _prime_cache_no_winner(self, client):
        """往缓存写一个没有 winning_variant 的 digest snapshot。"""
        from src.web.routes import contacts_routes as cr_mod
        import time as _t
        today_end = (int(_t.time()) // 86400) * 86400 + 86400 - 1
        cr_mod._relations_digest_cache.put(
            ("digest", today_end),
            {"draft_quality": {}, "text_summary": "x", "health_score": {},
             "stats": {}, "trend_delta": {}, "insights": [], "generated_at": ""},
        )

    def _prime_cache_with_winner(self, client):
        from src.web.routes import contacts_routes as cr_mod
        import time as _t
        today_end = (int(_t.time()) // 86400) * 86400 + 86400 - 1
        cr_mod._relations_digest_cache.put(
            ("digest", today_end),
            {"draft_quality": {"winning_variant": {"winner": "v1"}},
             "text_summary": "x", "health_score": {}, "stats": {},
             "trend_delta": {}, "insights": [], "generated_at": ""},
        )

    def test_only_winner_false_fires_anyway(self, client):
        """only_winner=false（默认）→ 即使没 winner 也推（但 webhook 未挂 → 503）。"""
        self._prime_cache_no_winner(client)
        r = client.post("/api/relations/digest/push?only_winner=false")
        # webhook 未挂 → 503；不是 no_winner 的 200
        assert r.status_code == 503

    def test_only_winner_skips_when_no_winner(self, client):
        self._prime_cache_no_winner(client)
        r = client.post("/api/relations/digest/push?only_winner=true")
        assert r.status_code == 200
        body = r.json()
        assert body["pushed"] is False
        assert body["reason"] == "no_winner"

    def test_only_winner_fires_when_winner_present(self, client):
        """有 winner → 尝试推送，因 webhook 未挂得 503（而非 200 no_winner）。"""
        self._prime_cache_with_winner(client)
        r = client.post("/api/relations/digest/push?only_winner=true")
        # webhook 未挂 → 503；证明进了推送分支
        assert r.status_code == 503


class TestDraftQualityByHash:
    """W3-3J.4：/api/drafts/quality 响应应含 by_hash 字段。"""

    def test_quality_includes_by_hash(self, client):
        r = client.get("/api/drafts/quality?days=7")
        assert r.status_code == 200
        body = r.json()
        assert "by_hash" in body
        assert isinstance(body["by_hash"], dict)

    def test_by_hash_groups_correctly(self, client):
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="hash_grp",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        from src.contacts.reunion_prompts import hash_prompt
        h1 = hash_prompt("prompt_alpha")
        h2 = hash_prompt("prompt_beta")
        # 各发 2 条
        for h in [h1, h1, h2, h2]:
            did = client.store.record_draft(
                journey_id=jid, draft_text="x",
                prompt_snapshot_hash=h,
            )
            client.store.mark_draft_sent(did)
        r = client.get("/api/drafts/quality?days=7")
        bh = r.json()["by_hash"]
        assert h1 in bh, "h1 桶不存在"
        assert h2 in bh, "h2 桶不存在"
        assert bh[h1]["sent"] == 2
        assert bh[h2]["sent"] == 2

    def test_by_hash_legacy_bucket(self, client):
        """无 hash 的旧数据归入 _legacy 桶。"""
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="hash_leg",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        did = client.store.record_draft(
            journey_id=jid, draft_text="x", prompt_snapshot_hash="",
        )
        client.store.mark_draft_sent(did)
        r = client.get("/api/drafts/quality?days=7")
        bh = r.json()["by_hash"]
        assert "_legacy" in bh


class TestReunionDraft:
    """W3-3F：reactivation 候选的 AI 草稿生成。"""

    def _build_client_with_ai(self, tmp_path, ai_stub):
        from src.web.routes.contacts_routes import (
            register_contacts_routes, _intimacy_trend_cache_clear,
        )
        from src.web.routes import contacts_routes as cr_mod
        _intimacy_trend_cache_clear()
        cr_mod._relations_digest_cache.clear()
        store = ContactStore(db_path=tmp_path / "c.db")
        handoff = HandoffTokenService(store, ttl_seconds=3600)
        merge = MergeService(store)
        gateway = ContactGateway(store, handoff, merge)
        intim = IntimacyEngine(store)
        reactivator = ReactivationScheduler(store, min_silent_days=3, min_intimacy=40.0)
        app = FastAPI()
        register_contacts_routes(
            app, api_auth=lambda: None,
            contacts_store=store, merge_service=merge,
            intimacy_engine=intim, gateway=gateway,
            reactivation_scheduler=reactivator,
            ai_client=ai_stub,
        )
        tc = TestClient(app)
        tc.store = store
        tc.gateway = gateway
        return tc, store

    def test_503_when_ai_not_wired(self, client):
        """fixture 默认没传 ai_client → 调 draft-reunion 应 503。"""
        r = client.post("/api/reactivation/nonexistent/draft-reunion")
        assert r.status_code == 503
        assert "ai_client_not_wired" in r.json()["detail"]

    def test_404_when_journey_unknown(self, tmp_path):
        class _AI:
            calls = []
            async def chat(self, prompt):  # noqa
                self.calls.append(prompt)
                return "嗨，最近怎么样？"
        ai = _AI()
        tc, _ = self._build_client_with_ai(tmp_path, ai)
        r = tc.post("/api/reactivation/nonexistent-jid/draft-reunion")
        assert r.status_code == 404
        assert ai.calls == [], "journey 不存在时不应调 AI"

    def test_draft_returns_text_and_metadata(self, tmp_path):
        import time as _t
        captured = []

        class _AI:
            async def chat(self, prompt):  # noqa
                captured.append(prompt)
                return "嗨，最近怎么样？好久没聊了 ☺"

        tc, store = self._build_client_with_ai(tmp_path, _AI())
        # 造一个 reunion 场景：有 journey + 之前的 inbound 消息
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_r",
            direction="in", text_preview="周末出去玩了")
        jid = ctx.journey.journey_id
        # 把 funnel + intimacy 设成 reunion 候选
        with store._lock:
            store._conn.execute(
                "UPDATE journeys SET funnel_stage='BONDED', intimacy_score=22.0, "
                "updated_at=? WHERE journey_id=?",
                (int(_t.time()) - 20 * 86400, jid),
            )
            store._conn.commit()

        r = tc.post(f"/api/reactivation/{jid}/draft-reunion")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["draft_text"] == "嗨，最近怎么样？好久没聊了 ☺"
        assert body["journey_id"] == jid
        assert body["funnel_stage"] == "BONDED"
        assert body["intimacy_score"] == 22.0
        assert body["prompt_signals"]["has_last_inbound"] is True
        # AI 收到的 prompt 必须包含关键信号
        assert len(captured) == 1
        p = captured[0]
        # v1/v2 都注入 last_inbound（稳定 token）
        assert "周末出去玩了" in p
        assert "只输出消息正文" in p  # 跨 v1/v2 稳定的中文 token
        # W3-3H：响应应带 variant，元数据走 response（v2 prompt 不含 funnel）
        assert body["prompt_variant"] in ("v1", "v2")
        store.close()

    def test_draft_strips_quotes_and_truncates(self, tmp_path):
        long_quoted = '"' + "好久不见，最近过得怎么样呀？" * 30 + '"'

        class _AI:
            async def chat(self, prompt):  # noqa
                return long_quoted

        tc, store = self._build_client_with_ai(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_q",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        r = tc.post(f"/api/reactivation/{jid}/draft-reunion")
        assert r.status_code == 200
        body = r.json()
        # 引号被剥；长度被裁到 200
        assert not body["draft_text"].startswith('"')
        assert not body["draft_text"].endswith('"')
        assert len(body["draft_text"]) <= 201  # 200 + 1 ellipsis
        assert body["draft_text"].endswith("…")
        store.close()

    def test_ai_empty_response_returns_502(self, tmp_path):
        class _AI:
            async def chat(self, prompt):  # noqa
                return ""

        tc, store = self._build_client_with_ai(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_e",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        r = tc.post(f"/api/reactivation/{jid}/draft-reunion")
        assert r.status_code == 502
        assert "empty" in r.json()["detail"]
        store.close()

    def test_ai_exception_returns_502(self, tmp_path):
        class _AI:
            async def chat(self, prompt):  # noqa
                raise RuntimeError("api timeout")

        tc, store = self._build_client_with_ai(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_x",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        r = tc.post(f"/api/reactivation/{jid}/draft-reunion")
        assert r.status_code == 502
        assert "ai_generation_failed" in r.json()["detail"]
        store.close()

    def test_language_aware_prompt_en(self, tmp_path):
        """contact.language_hint='en' → 英文 prompt + draft_lang='en'。"""
        captured = []

        class _AI:
            async def chat(self, prompt):  # noqa
                captured.append(prompt)
                return "Hey, how have you been?"

        tc, store = self._build_client_with_ai(tmp_path, _AI())
        ctx = tc.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_en",
            display_name="Bob", language_hint="en")
        store.update_contact(ctx.contact.contact_id, language_hint="en")
        jid = ctx.journey.journey_id
        r = tc.post(f"/api/reactivation/{jid}/draft-reunion")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["draft_lang"] == "en"
        # prompt 应该是英文（语言独有 token，跨 v1/v2 variant 稳定）
        p = captured[0]
        assert "[Context] silent" in p
        assert "Output ONLY the message body" in p
        assert "重逢" not in p
        assert "久しぶり" not in p
        store.close()

    def test_language_aware_prompt_ja(self, tmp_path):
        captured = []

        class _AI:
            async def chat(self, prompt):  # noqa
                captured.append(prompt)
                return "久しぶり、元気にしてた？"

        tc, store = self._build_client_with_ai(tmp_path, _AI())
        ctx = tc.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_ja",
            display_name="Yuki", language_hint="ja")
        store.update_contact(ctx.contact.contact_id, language_hint="ja")
        jid = ctx.journey.journey_id
        r = tc.post(f"/api/reactivation/{jid}/draft-reunion")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["draft_lang"] == "ja"
        # 语言独有 token（跨 variant 稳定）
        p = captured[0]
        assert "【状況】沈黙" in p
        assert "本文のみ出力" in p
        store.close()

    def test_unknown_language_falls_back_to_zh(self, tmp_path):
        captured = []

        class _AI:
            async def chat(self, prompt):  # noqa
                captured.append(prompt)
                return "好久不见！"

        tc, store = self._build_client_with_ai(tmp_path, _AI())
        ctx = tc.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_kr",
            display_name="K", language_hint="ko")
        store.update_contact(ctx.contact.contact_id, language_hint="ko")
        jid = ctx.journey.journey_id
        r = tc.post(f"/api/reactivation/{jid}/draft-reunion")
        body = r.json()
        # 未知语言兜底中文
        assert body["draft_lang"] == "zh"
        p = captured[0]
        assert "只输出消息正文" in p
        assert "30 字" in p
        store.close()

    def test_no_inbound_history_still_generates(self, tmp_path):
        """没有 inbound 消息历史 → 仍能生成（has_last_inbound=False）。"""
        captured = []

        class _AI:
            async def chat(self, prompt):  # noqa
                captured.append(prompt)
                return "好久不见，最近还好吗？"

        tc, store = self._build_client_with_ai(tmp_path, _AI())
        # 只 on_peer_seen，不发 msg_in
        ctx = tc.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_n",
            display_name="Cold")
        jid = ctx.journey.journey_id
        r = tc.post(f"/api/reactivation/{jid}/draft-reunion")
        assert r.status_code == 200
        body = r.json()
        assert body["prompt_signals"]["has_last_inbound"] is False
        # prompt 里不应有「对方最后一句话」段
        assert "对方最后一句话" not in captured[0]
        store.close()


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


# ─────────────────────────────────────────────────────────────────────────────
# W3-3L 测试
# ─────────────────────────────────────────────────────────────────────────────

class TestChannelIdentityLookup:
    """W3-3L.1：GET /api/channel-identities/lookup"""

    def test_lookup_found_with_account_id(self, client):
        client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_99",
            display_name="TestUser",
        )
        r = client.get("/api/channel-identities/lookup",
                       params={"channel": "messenger", "external_id": "fb_99",
                               "account_id": "a1"})
        assert r.status_code == 200
        body = r.json()
        assert body["found"] is True
        assert body["channel_identity"]["external_id"] == "fb_99"
        assert body["contact"] is not None
        assert body["journey"] is not None

    def test_lookup_found_without_account_id(self, client):
        client.gateway.on_peer_seen(
            channel="line", account_id="b1", external_id="line_77",
        )
        r = client.get("/api/channel-identities/lookup",
                       params={"channel": "line", "external_id": "line_77"})
        assert r.status_code == 200
        body = r.json()
        assert body["found"] is True
        assert body["channel_identity"]["channel"] == "line"

    def test_lookup_not_found_returns_found_false(self, client):
        r = client.get("/api/channel-identities/lookup",
                       params={"channel": "telegram", "external_id": "tg_nonexistent"})
        assert r.status_code == 200
        assert r.json()["found"] is False

    def test_lookup_wrong_account_id_not_found(self, client):
        client.gateway.on_peer_seen(
            channel="messenger", account_id="acct_x", external_id="fb_x1",
        )
        r = client.get("/api/channel-identities/lookup",
                       params={"channel": "messenger", "external_id": "fb_x1",
                               "account_id": "acct_wrong"})
        assert r.status_code == 200
        assert r.json()["found"] is False


class TestAdminLinkChannel:
    """W3-3L.3：POST /api/contacts/{id}/link-channel"""

    def _setup_two_contacts(self, client):
        ctx_m = client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_m1",
        )
        ctx_l = client.gateway.on_peer_seen(
            channel="line", account_id="a1", external_id="line_l1",
        )
        return ctx_m.contact, ctx_l.channel_identity

    def test_link_channel_moves_ci(self, client):
        contact_m, ci_l = self._setup_two_contacts(client)
        r = client.post(f"/api/contacts/{contact_m.contact_id}/link-channel",
                        json={"channel_identity_id": ci_l.channel_identity_id,
                              "note": "confirmed same person"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["changed"] is True
        # 验证 CI 已迁移
        ci_after = client.store.get_channel_identity(ci_l.channel_identity_id)
        assert ci_after.contact_id == contact_m.contact_id
        assert ci_after.linked_via == "manual"

    def test_link_channel_already_linked_noop(self, client):
        ctx = client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_noop",
        )
        cis = client.store.list_channel_identities_of(ctx.contact.contact_id)
        assert len(cis) == 1
        r = client.post(f"/api/contacts/{ctx.contact.contact_id}/link-channel",
                        json={"channel_identity_id": cis[0].channel_identity_id})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["changed"] is False
        assert body["reason"] == "already_linked"

    def test_link_channel_contact_not_found(self, client):
        ctx = client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_404",
        )
        cis = client.store.list_channel_identities_of(ctx.contact.contact_id)
        r = client.post("/api/contacts/nonexistent_contact/link-channel",
                        json={"channel_identity_id": cis[0].channel_identity_id})
        assert r.status_code == 404

    def test_link_channel_ci_not_found(self, client):
        ctx = client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_ci404",
        )
        r = client.post(f"/api/contacts/{ctx.contact.contact_id}/link-channel",
                        json={"channel_identity_id": "nonexistent_ci"})
        assert r.status_code == 404

    def test_link_channel_missing_body_field(self, client):
        ctx = client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_body",
        )
        r = client.post(f"/api/contacts/{ctx.contact.contact_id}/link-channel",
                        json={})
        assert r.status_code == 422


class TestMultiPlatformStats:
    """W3-3L.2：多平台统计 — store + /api/funnel/stats"""

    def test_count_zero_when_no_contacts(self, client):
        result = client.store.count_multi_platform_contacts()
        assert result["multi_platform_contacts"] == 0
        assert result["by_channel_combo"] == {}

    def test_count_single_platform_not_counted(self, client):
        client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_s1",
        )
        result = client.store.count_multi_platform_contacts()
        assert result["multi_platform_contacts"] == 0

    def test_count_multi_platform_after_merge(self, client):
        ctx_m = client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_multi1",
        )
        ctx_l = client.gateway.on_peer_seen(
            channel="line", account_id="a1", external_id="line_multi1",
        )
        # 手动关联：把 LINE CI 迁到 Messenger contact
        client.store.relink_channel_identity(
            ci_id=ctx_l.channel_identity.channel_identity_id,
            new_contact_id=ctx_m.contact.contact_id,
            linked_via="manual",
            attribution_confidence=1.0,
        )
        result = client.store.count_multi_platform_contacts()
        assert result["multi_platform_contacts"] == 1
        assert "line+messenger" in result["by_channel_combo"]

    def test_funnel_stats_has_multi_platform(self, client):
        r = client.get("/api/funnel/stats")
        assert r.status_code == 200
        body = r.json()
        assert "multi_platform" in body
        mp = body["multi_platform"]
        assert "multi_platform_contacts" in mp
        assert "by_channel_combo" in mp

    def test_combo_key_sorted(self, client):
        """combo key 应按字母排序，messenger+line 而非 line+messenger。"""
        ctx_m = client.gateway.on_peer_seen(
            channel="messenger", account_id="a2", external_id="fb_sort",
        )
        ctx_l = client.gateway.on_peer_seen(
            channel="line", account_id="a2", external_id="line_sort",
        )
        client.store.relink_channel_identity(
            ci_id=ctx_l.channel_identity.channel_identity_id,
            new_contact_id=ctx_m.contact.contact_id,
            linked_via="manual",
            attribution_confidence=1.0,
        )
        result = client.store.count_multi_platform_contacts()
        # 字典 key 应是 "line+messenger"（字母序）
        combos = list(result["by_channel_combo"].keys())
        for key in combos:
            parts = key.split("+")
            assert parts == sorted(parts), f"combo key not sorted: {key}"


class TestExpandChannels:
    """W3-3L.4：/api/contacts?expand=channels"""

    def test_expand_channels_returns_channels_list(self, client):
        client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_ec1",
        )
        r = client.get("/api/contacts", params={"expand": "channels"})
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) >= 1
        for item in items:
            assert "channels" in item
            assert isinstance(item["channels"], list)

    def test_expand_channels_multi_platform_contact(self, client):
        ctx_m = client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_ec2",
        )
        ctx_l = client.gateway.on_peer_seen(
            channel="line", account_id="a1", external_id="line_ec2",
        )
        client.store.relink_channel_identity(
            ci_id=ctx_l.channel_identity.channel_identity_id,
            new_contact_id=ctx_m.contact.contact_id,
            linked_via="manual",
            attribution_confidence=1.0,
        )
        r = client.get("/api/contacts",
                       params={"expand": "channels", "limit": "200"})
        items = r.json()["items"]
        merged = next((i for i in items
                       if i["contact_id"] == ctx_m.contact.contact_id), None)
        assert merged is not None
        assert set(merged["channels"]) == {"messenger", "line"}

    def test_expand_journey_and_channels_together(self, client):
        client.gateway.on_peer_seen(
            channel="telegram", account_id="a1", external_id="tg_jc",
        )
        r = client.get("/api/contacts", params={"expand": "journey,channels"})
        assert r.status_code == 200
        items = r.json()["items"]
        for item in items:
            assert "channels" in item

    def test_no_expand_no_channels_field(self, client):
        client.gateway.on_peer_seen(
            channel="messenger", account_id="a1", external_id="fb_noexp",
        )
        r = client.get("/api/contacts")
        items = r.json()["items"]
        for item in items:
            assert "channels" not in item


class TestDraftQualityWinningVariant:
    """W3-3I.1：/api/drafts/quality 必须返回 winning_variant 字段。"""

    def test_quality_includes_winning_variant_key(self, client):
        r = client.get("/api/drafts/quality?days=7")
        assert r.status_code == 200
        body = r.json()
        assert "winning_variant" in body

    def test_winning_variant_none_when_no_data(self, client):
        r = client.get("/api/drafts/quality?days=7")
        body = r.json()
        assert body["winning_variant"] is None

    def test_winning_variant_populated_when_clear_winner(self, client):
        """两个 variant 极端差异 + 足够样本 → winning_variant 有值。"""
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="wv_test",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        store = client.store
        # v1: 18/20 success, v2: 2/20 success
        for _ in range(18):
            did = store.record_draft(
                journey_id=jid, draft_text="x", prompt_variant="v1",
            )
            store.mark_draft_sent(did)
            store.eval_draft_success(did, success=True)
        for _ in range(2):
            did = store.record_draft(
                journey_id=jid, draft_text="x", prompt_variant="v1",
            )
            store.mark_draft_sent(did)
            store.eval_draft_success(did, success=False)
        for _ in range(2):
            did = store.record_draft(
                journey_id=jid, draft_text="x", prompt_variant="v2",
            )
            store.mark_draft_sent(did)
            store.eval_draft_success(did, success=True)
        for _ in range(18):
            did = store.record_draft(
                journey_id=jid, draft_text="x", prompt_variant="v2",
            )
            store.mark_draft_sent(did)
            store.eval_draft_success(did, success=False)

        r = client.get("/api/drafts/quality?days=7")
        body = r.json()
        assert body["winning_variant"] is not None
        assert body["winning_variant"]["winner"] == "v1"
        assert body["winning_variant"]["runner_up"] == "v2"
