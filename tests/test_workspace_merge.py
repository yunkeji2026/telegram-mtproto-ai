"""Phase 5-5 — 坐席手动合并 / 拆分 / 审核队列 测试。

覆盖：
- ContactStore.split_channel_identity（拆分孤岛保护 + 正常拆出）
- Gateway: contact_overview / merge_candidates_for / manual_merge_identity / split_identity
            / 审核队列 approve/reject
- 工作台 API：overview / merge / split / merge-reviews 列表 + 裁决
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.contacts import (
    ContactGateway,
    ContactStore,
    HandoffTokenService,
    MergeService,
)
from src.contacts.models import CHANNEL_LINE, CHANNEL_WEB
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


@pytest.fixture()
def cstore():
    d = tempfile.mkdtemp()
    return ContactStore(Path(d) / "contacts.db")


@pytest.fixture()
def gateway(cstore):
    return ContactGateway(cstore, HandoffTokenService(cstore), MergeService(cstore))


# ── store.split_channel_identity ─────────────────────────────────────────────

class TestSplit:
    def test_split_isolated_returns_none(self, cstore):
        _, ci, _ = cstore.ensure_channel_identity(
            channel=CHANNEL_WEB, account_id="web", external_id="solo")
        assert cstore.split_channel_identity(ci_id=ci.channel_identity_id) is None

    def test_split_detaches_to_new_contact(self, cstore):
        # 两个渠道身份并到同一 Contact
        _, web_ci, _ = cstore.ensure_channel_identity(
            channel=CHANNEL_WEB, account_id="web", external_id="wv1")
        line_c, line_ci, _ = cstore.ensure_channel_identity(
            channel=CHANNEL_LINE, account_id="line1", external_id="lu1")
        cstore.relink_channel_identity(
            ci_id=web_ci.channel_identity_id, new_contact_id=line_c.contact_id,
            linked_via="manual", attribution_confidence=1.0)
        assert cstore.get_channel_identity(web_ci.channel_identity_id).contact_id == line_c.contact_id

        new_cid = cstore.split_channel_identity(ci_id=web_ci.channel_identity_id)
        assert new_cid and new_cid != line_c.contact_id
        moved = cstore.get_channel_identity(web_ci.channel_identity_id)
        assert moved.contact_id == new_cid
        assert moved.linked_via == "manual_split"
        # 新 contact 有独立 journey
        assert cstore.get_journey_by_contact(new_cid) is not None


# ── gateway 合并/拆分/审核 ───────────────────────────────────────────────────

class TestGatewayMergeOps:
    def test_overview_and_candidates(self, gateway, cstore):
        a, _, _ = cstore.ensure_channel_identity(
            channel=CHANNEL_WEB, account_id="web", external_id="wa")
        b = cstore.create_contact(primary_name="老客户B")
        cstore.set_contact_attribute(a.contact_id, "phone", "+100")
        cstore.set_contact_attribute(b.contact_id, "phone", "+100")
        ov = gateway.contact_overview(a.contact_id)
        assert ov["contact_id"] == a.contact_id
        assert ov["attributes"]["phone"] == "+100"
        cands = gateway.merge_candidates_for(a.contact_id)
        assert [c["contact_id"] for c in cands] == [b.contact_id]
        assert cands[0]["match_on"] == "phone"

    def test_manual_merge_and_split(self, gateway, cstore):
        ctx_w = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                     external_id="wv2", display_name="访客")
        ctx_l = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                     external_id="lu2", display_name="老张")
        web_ci_id = ctx_w.channel_identity.channel_identity_id
        ok = gateway.manual_merge_identity(
            ci_id=web_ci_id, target_contact_id=ctx_l.contact.contact_id, operator="op1")
        assert ok
        ci = gateway.find_channel_identity(channel=CHANNEL_WEB, account_id="web",
                                           external_id="wv2")
        assert ci.contact_id == ctx_l.contact.contact_id

        new_cid = gateway.split_identity(ci_id=web_ci_id, operator="op1")
        assert new_cid
        ci2 = gateway.find_channel_identity(channel=CHANNEL_WEB, account_id="web",
                                            external_id="wv2")
        assert ci2.contact_id == new_cid

    def test_review_approve(self, gateway, cstore):
        ctx = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                   external_id="wv3", display_name="V")
        # 目标 Contact 须有 Journey（真实场景下均经 ensure_channel_identity 创建）
        target_ctx = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                          external_id="luT", display_name="T")
        rid = cstore.enqueue_merge_review(
            candidate_ci_id=ctx.channel_identity.channel_identity_id,
            target_contact_id=target_ctx.contact.contact_id, confidence=0.7,
            breakdown={"email": 1.0})
        assert len(gateway.list_pending_merge_reviews()) == 1
        assert gateway.approve_merge_review(rid, resolved_by="op")
        # 已批准 → 队列清空，ci 迁到 target
        assert gateway.list_pending_merge_reviews() == []
        ci = gateway.find_channel_identity(channel=CHANNEL_WEB, account_id="web",
                                           external_id="wv3")
        assert ci.contact_id == target_ctx.contact.contact_id


# ── 工作台 API ───────────────────────────────────────────────────────────────

class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page not used")


@pytest.fixture()
def api_client(cstore, gateway):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=page_auth, api_auth=api_auth,
        templates=_Templates(), config_manager=SimpleNamespace(config={}),
    )
    app.state.contacts = SimpleNamespace(store=cstore, gateway=gateway)
    return TestClient(app)


class TestMergeApi:
    def test_overview_endpoint(self, api_client, gateway, cstore):
        ctx = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                   external_id="api1", display_name="V")
        other = cstore.create_contact(primary_name="同人")
        cstore.set_contact_attribute(ctx.contact.contact_id, "email", "z@z.com")
        cstore.set_contact_attribute(other.contact_id, "email", "z@z.com")
        r = api_client.get("/api/workspace/contacts/overview",
                           params={"platform": "web", "account_id": "web", "chat_key": "api1"})
        d = r.json()
        assert d["ok"] and d["contact"]["contact_id"] == ctx.contact.contact_id
        assert [c["contact_id"] for c in d["candidates"]] == [other.contact_id]

    def test_merge_then_split_endpoints(self, api_client, gateway, cstore):
        ctx_w = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                     external_id="api2", display_name="V")
        ctx_l = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                     external_id="apiL", display_name="L")
        ci_id = ctx_w.channel_identity.channel_identity_id
        rm = api_client.post("/api/workspace/contacts/merge",
                             json={"ci_id": ci_id, "target_contact_id": ctx_l.contact.contact_id})
        assert rm.json()["ok"] is True
        rs = api_client.post("/api/workspace/contacts/split", json={"ci_id": ci_id})
        assert rs.json()["ok"] is True and rs.json()["new_contact_id"]

    def test_split_nothing_to_split(self, api_client, gateway):
        ctx = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                   external_id="api3", display_name="V")
        r = api_client.post("/api/workspace/contacts/split",
                            json={"ci_id": ctx.channel_identity.channel_identity_id})
        assert r.json()["ok"] is False

    def test_reviews_list_and_resolve(self, api_client, gateway, cstore):
        ctx = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                   external_id="api4", display_name="V")
        target = cstore.create_contact(primary_name="T")
        rid = cstore.enqueue_merge_review(
            candidate_ci_id=ctx.channel_identity.channel_identity_id,
            target_contact_id=target.contact_id, confidence=0.7, breakdown={})
        lst = api_client.get("/api/workspace/merge-reviews").json()
        assert lst["count"] == 1
        assert lst["reviews"][0]["target"]["contact_id"] == target.contact_id
        res = api_client.post(f"/api/workspace/merge-reviews/{rid}", json={"action": "reject"})
        assert res.json()["ok"] is True
        assert api_client.get("/api/workspace/merge-reviews").json()["count"] == 0

    def test_contacts_disabled_graceful(self):
        app = FastAPI()
        register_unified_inbox_routes(
            app, page_auth=lambda r: True, api_auth=lambda r: True,
            templates=_Templates(), config_manager=SimpleNamespace(config={}),
        )
        c = TestClient(app)
        r = c.get("/api/workspace/contacts/overview",
                  params={"platform": "web", "chat_key": "x"})
        assert r.json()["ok"] is False


# ── Phase 6-1：Contact 360 ────────────────────────────────────────────────────

def _client_with_inbox(cstore, gateway, inbox):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=page_auth, api_auth=api_auth,
        templates=_Templates(), config_manager=SimpleNamespace(config={}),
    )
    app.state.contacts = SimpleNamespace(store=cstore, gateway=gateway)
    app.state.inbox_store = inbox
    return TestClient(app)


class TestContact360:
    def test_detail_aggregates_cross_channel_timeline(self, cstore, gateway):
        from src.inbox.store import InboxStore
        from src.integrations.web_chat.service import WebChatService
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "inbox.db")
        svc = WebChatService(account_id="web")
        # web 身份 + 一条 web 消息
        ctx_w = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                     external_id="v360", display_name="V")
        svc.record_message(inbox, "v360", text="网页你好", direction="in")
        # line 身份并入同一 contact + 一条 line 消息
        ctx_l = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                     external_id="lu360", display_name="V")
        gateway.manual_merge_identity(
            ci_id=ctx_w.channel_identity.channel_identity_id,
            target_contact_id=ctx_l.contact.contact_id, operator="op")
        from src.inbox.normalizer import conv_id
        from src.inbox.models import InboxConversation, InboxMessage
        lcid = conv_id("line", "line1", "lu360")
        inbox.ingest_batch(
            InboxConversation(conversation_id=lcid, platform="line", account_id="line1",
                              chat_key="lu360", display_name="V", language="zh",
                              last_text="LINE你好", last_ts=200.0, unread=0),
            [InboxMessage(conversation_id=lcid, platform_msg_id="", direction="in",
                          text="LINE你好", original_text="LINE你好", translated_text="LINE你好",
                          source_lang="zh", ts=200.0)])

        c = _client_with_inbox(cstore, gateway, inbox)
        r = c.get(f"/api/workspace/contact/{ctx_l.contact.contact_id}")
        d2 = r.json()
        assert d2["ok"]
        channels = {m["channel"] for m in d2["timeline"]}
        assert channels == {"web", "line"}
        # 按 ts 升序
        ts = [m["ts"] for m in d2["timeline"]]
        assert ts == sorted(ts)

    def test_detail_404_for_unknown(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        c = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        assert c.get("/api/workspace/contact/nope").status_code == 404

    def test_search_endpoint(self, cstore, gateway):
        from src.inbox.store import InboxStore
        gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                             external_id="sx", display_name="搜索目标")
        d = tempfile.mkdtemp()
        c = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = c.get("/api/workspace/contacts/search", params={"q": "搜索目标"})
        body = r.json()
        assert body["ok"] and body["total"] >= 1
        assert any(x["primary_name"] == "搜索目标" for x in body["contacts"])

    def test_merge_contact_endpoint(self, cstore, gateway):
        from src.inbox.store import InboxStore
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="ma", display_name="A")
        b = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                 external_id="mb", display_name="B")
        d = tempfile.mkdtemp()
        c = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = c.post("/api/workspace/contacts/merge-contact",
                   json={"source_contact_id": a.contact.contact_id,
                         "target_contact_id": b.contact.contact_id})
        assert r.json()["ok"] is True
        # a 的 web 身份已迁到 b
        ci = gateway.find_channel_identity(channel=CHANNEL_WEB, account_id="web",
                                           external_id="ma")
        assert ci.contact_id == b.contact.contact_id


# ── Phase 6-2：CRM 客户列表 + 时间线游标 ──────────────────────────────────────

class TestContactsListStore:
    def test_overview_filters(self, cstore, gateway):
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="L1", display_name="有留资")
        gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                             external_id="L2", display_name="无留资")
        cstore.set_contact_attribute(a.contact.contact_id, "phone", "+1")
        rows, total = cstore.list_contacts_overview(limit=10)
        assert total == 2
        # has_lead 过滤
        leads, n = cstore.list_contacts_overview(has_lead=True, limit=10)
        assert n == 1 and leads[0]["contact_id"] == a.contact.contact_id
        assert leads[0]["has_lead"] is True
        assert "web" in leads[0]["channels"]
        # q 搜索
        found, fn = cstore.list_contacts_overview(q="无留资", limit=10)
        assert fn == 1 and found[0]["primary_name"] == "无留资"

    def test_overview_stage_filter(self, cstore, gateway):
        # web 入站 → ENGAGED
        gateway.on_message(channel=CHANNEL_WEB, account_id="web", external_id="S1",
                           direction="in", text_preview="hi", display_name="X")
        eng, n = cstore.list_contacts_overview(stage="ENGAGED", limit=10)
        assert n == 1 and eng[0]["funnel_stage"] == "ENGAGED"
        assert cstore.list_contacts_overview(stage="CONVERTED", limit=10)[1] == 0


class TestContactsListApi:
    def test_list_endpoint_pagination_and_counts(self, cstore, gateway):
        from src.inbox.store import InboxStore
        for i in range(7):
            gateway.on_message(channel=CHANNEL_WEB, account_id="web",
                               external_id=f"P{i}", direction="in",
                               text_preview="hi", display_name=f"C{i}")
        d = tempfile.mkdtemp()
        c = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = c.get("/api/workspace/contacts/list", params={"limit": 5, "offset": 0}).json()
        assert r["ok"] and r["total"] == 7 and len(r["contacts"]) == 5
        assert r["stage_counts"].get("ENGAGED") == 7
        assert r["contacts"][0].get("funnel_stage_label")
        # 第二页
        r2 = c.get("/api/workspace/contacts/list", params={"limit": 5, "offset": 5}).json()
        assert len(r2["contacts"]) == 2


class TestCrmStore:
    def test_follow_up_and_due_count(self, cstore, gateway):
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="F1", display_name="A")
        b = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                 external_id="F2", display_name="B")
        cstore.set_follow_up(a.contact.contact_id, 1000)   # 已到期（过去）
        cstore.set_follow_up(b.contact.contact_id, 9999999999)  # 未来
        assert cstore.count_due_follow_ups(now_ts=2000) == 1
        # follow_up=due 仅返回 a
        due, n = cstore.list_contacts_overview(follow_up="due", now_ts=2000, limit=10)
        assert n == 1 and due[0]["contact_id"] == a.contact.contact_id
        # follow_up=any 返回两者
        assert cstore.list_contacts_overview(follow_up="any", now_ts=2000, limit=10)[1] == 2
        # 清除
        cstore.set_follow_up(a.contact.contact_id, 0)
        assert cstore.count_due_follow_ups(now_ts=2000) == 0

    def test_tags_crud_and_filter_and_aggregate(self, cstore, gateway):
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="T1", display_name="A")
        b = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                 external_id="T2", display_name="B")
        assert cstore.set_contact_tags(a.contact.contact_id, ["VIP", "VIP", "意向"]) == ["VIP", "意向"]
        cstore.set_contact_tags(b.contact.contact_id, ["VIP"])
        assert cstore.get_contact_tags(a.contact.contact_id) == ["VIP", "意向"]
        # 批量
        m = cstore.get_tags_for_contacts([a.contact.contact_id, b.contact.contact_id])
        assert set(m[a.contact.contact_id]) == {"VIP", "意向"}
        # 聚合
        agg = {t["tag"]: t["count"] for t in cstore.list_all_tags()}
        assert agg["VIP"] == 2 and agg["意向"] == 1
        # 标签过滤
        vip, n = cstore.list_contacts_overview(tag="VIP", limit=10)
        assert n == 2
        intent, ni = cstore.list_contacts_overview(tag="意向", limit=10)
        assert ni == 1 and intent[0]["contact_id"] == a.contact.contact_id

    def test_gateway_update_contact_crm(self, gateway, cstore):
        ctx = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                   external_id="C1", display_name="A")
        out = gateway.update_contact_crm(
            ctx.contact.contact_id, note="重要客户", tags=["A", "B"], follow_up_at=5000,
            operator="op")
        assert out["ok"] and out["tags"] == ["A", "B"]
        ov = gateway.contact_overview(ctx.contact.contact_id)
        assert ov["note"] == "重要客户" and ov["follow_up_at"] == 5000
        assert ov["tags"] == ["A", "B"]

    def test_update_crm_unknown_contact(self, gateway):
        assert gateway.update_contact_crm("nope", note="x")["ok"] is False


class TestCrmApi:
    def test_crm_save_and_followups_endpoint(self, cstore, gateway):
        from src.inbox.store import InboxStore
        ctx = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                   external_id="api_crm", display_name="V")
        d = tempfile.mkdtemp()
        c = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = c.post(f"/api/workspace/contact/{ctx.contact.contact_id}/crm",
                   json={"note": "vip", "tags": ["热", "意向"], "follow_up_at": 1000})
        assert r.json()["ok"] is True and r.json()["tags"] == ["热", "意向"]
        # follow-ups due 列表含该客户
        fu = c.get("/api/workspace/follow-ups", params={"scope": "any"}).json()
        assert fu["ok"] and any(x["contact_id"] == ctx.contact.contact_id for x in fu["contacts"])
        # tags 聚合
        tags = c.get("/api/workspace/tags").json()
        assert any(t["tag"] == "意向" for t in tags["tags"])

    def test_crm_bad_tags(self, cstore, gateway):
        from src.inbox.store import InboxStore
        ctx = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                   external_id="api_bad", display_name="V")
        d = tempfile.mkdtemp()
        c = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = c.post(f"/api/workspace/contact/{ctx.contact.contact_id}/crm",
                   json={"tags": "notalist"})
        assert r.status_code == 400


class TestTimelineCursor:
    def test_list_recent_and_before_ts(self, tmp_path):
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        store = InboxStore(tmp_path / "i.db")
        cid = "web:web:cur"
        for i in range(5):
            store.ingest_batch(
                InboxConversation(conversation_id=cid, platform="web", account_id="web",
                                  chat_key="cur", display_name="V", language="zh",
                                  last_text=f"m{i}", last_ts=float(i), unread=0),
                [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                              text=f"m{i}", original_text=f"m{i}", translated_text=f"m{i}",
                              source_lang="zh", ts=float(i))])
        recent = store.list_recent_messages(cid, limit=2)
        assert [m["text"] for m in recent] == ["m3", "m4"]  # 最近 2 条，升序
        older = store.list_recent_messages(cid, limit=2, before_ts=3.0)
        assert [m["text"] for m in older] == ["m1", "m2"]
        store.close()
