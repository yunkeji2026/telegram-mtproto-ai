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


class TestFollowUpTasks:
    def test_task_lifecycle_recompute(self, cstore, gateway):
        c = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id=" task1".strip(), display_name="A")
        cid = c.contact.contact_id
        t1 = cstore.add_follow_up_task(cid, due_at=5000, note="电话回访")
        t2 = cstore.add_follow_up_task(cid, due_at=3000, note="发资料")
        assert t1 and t2
        # follow_up_at 缓存 = 最近未完成到期 = 3000
        assert cstore.get_contact(cid).follow_up_at == 3000
        tasks = cstore.list_follow_up_tasks(cid)
        assert len(tasks) == 2 and tasks[0]["due_at"] == 3000  # 未完成按到期升序
        # 完成最早的，缓存重算为 5000
        assert cstore.complete_follow_up_task(t2, done_by="op") is True
        assert cstore.get_contact(cid).follow_up_at == 5000
        # 已完成不能再次完成
        assert cstore.complete_follow_up_task(t2) is False
        # 完成全部 → 缓存 0
        cstore.complete_follow_up_task(t1)
        assert cstore.get_contact(cid).follow_up_at == 0

    def test_set_follow_up_dedupes_via_task(self, cstore, gateway):
        c = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="task2", display_name="A")
        cid = c.contact.contact_id
        cstore.set_follow_up(cid, 1000)
        cstore.set_follow_up(cid, 2000)  # 更新同一未完成任务，而非新增
        assert len([t for t in cstore.list_follow_up_tasks(cid) if not t["done_at"]]) == 1
        assert cstore.get_contact(cid).follow_up_at == 2000
        cstore.set_follow_up(cid, 0)  # 清除 → 完成全部
        assert cstore.get_contact(cid).follow_up_at == 0

    def test_count_due_tasks_by_assignee(self, cstore, gateway):
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="dt1", display_name="A")
        b = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                 external_id="dt2", display_name="B")
        cstore.add_follow_up_task(a.contact.contact_id, due_at=1000, assignee="alice")
        cstore.add_follow_up_task(b.contact.contact_id, due_at=1000, assignee="bob")
        cstore.add_follow_up_task(b.contact.contact_id, due_at=9e9, assignee="alice")  # 未到期
        assert cstore.count_due_tasks(now_ts=2000) == 2
        assert cstore.count_due_tasks(assignee="alice", now_ts=2000) == 1

    def test_gateway_add_and_done_task(self, gateway):
        c = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="gt1", display_name="A")
        out = gateway.add_follow_up_task(c.contact.contact_id, due_at=4000,
                                         note="回访", operator="op")
        assert out["ok"] and out["task_id"]
        ov = gateway.contact_overview(c.contact.contact_id)
        assert len(ov["follow_up_tasks"]) == 1 and ov["follow_up_at"] == 4000
        assert gateway.complete_follow_up_task(out["task_id"], operator="op")["ok"]
        assert gateway.contact_overview(c.contact.contact_id)["follow_up_at"] == 0


class TestTagLibrary:
    def test_upsert_list_delete(self, cstore):
        assert cstore.upsert_tag_library("VIP", color="#dc2626", sort_order=1)
        assert cstore.upsert_tag_library("意向", color="#f59e0b", sort_order=2)
        lib = cstore.list_tag_library()
        assert [x["tag"] for x in lib] == ["VIP", "意向"]
        assert lib[0]["color"] == "#dc2626"
        # 更新颜色
        cstore.upsert_tag_library("VIP", color="#000000", sort_order=1)
        assert cstore.list_tag_library()[0]["color"] == "#000000"
        assert cstore.delete_tag_library("VIP")
        assert [x["tag"] for x in cstore.list_tag_library()] == ["意向"]

    def test_list_all_tags_merges_library_color_and_unused(self, cstore, gateway):
        c = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="lt1", display_name="A")
        cstore.set_contact_tags(c.contact.contact_id, ["VIP"])
        cstore.upsert_tag_library("VIP", color="#dc2626")
        cstore.upsert_tag_library("未使用", color="#10b981")  # 库里有但没人用
        tags = {t["tag"]: t for t in cstore.list_all_tags()}
        assert tags["VIP"]["count"] == 1 and tags["VIP"]["color"] == "#dc2626"
        assert tags["未使用"]["count"] == 0 and tags["未使用"]["color"] == "#10b981"


class TestCrm64Api:
    def _client(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        return _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))

    def test_follow_up_task_endpoints(self, cstore, gateway):
        c = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="ep1", display_name="A")
        cli = self._client(cstore, gateway)
        r = cli.post(f"/api/workspace/contact/{c.contact.contact_id}/follow-up",
                     json={"due_at": 1000, "note": "回访"})
        assert r.json()["ok"] is True
        tid = r.json()["task_id"]
        # 缺 due_at → 400
        assert cli.post(f"/api/workspace/contact/{c.contact.contact_id}/follow-up",
                        json={"note": "x"}).status_code == 400
        # follow-ups 含到期任务计数
        fu = cli.get("/api/workspace/follow-ups").json()
        assert fu["due_tasks"] >= 1
        # 完成任务
        assert cli.post(f"/api/workspace/follow-up/{tid}/done").json()["ok"] is True

    def test_tag_library_endpoints(self, cstore, gateway):
        cli = self._client(cstore, gateway)
        r = cli.post("/api/workspace/tag-library", json={"tag": "VIP", "color": "#dc2626"})
        assert r.json()["ok"] and any(x["tag"] == "VIP" for x in r.json()["library"])
        assert cli.get("/api/workspace/tag-library").json()["library"][0]["tag"] == "VIP"
        assert cli.post("/api/workspace/tag-library", json={"tag": ""}).status_code == 400
        d = cli.delete("/api/workspace/tag-library/VIP").json()
        assert d["ok"] and d["library"] == []

    def test_export_csv(self, cstore, gateway):
        gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                             external_id="ex1", display_name="导出客户")
        cli = self._client(cstore, gateway)
        r = cli.get("/api/workspace/contacts/export.csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "attachment" in r.headers.get("content-disposition", "")
        body = r.content.decode("utf-8-sig")
        assert "contact_id" in body and "导出客户" in body


class TestTaskCollaboration:
    def test_list_open_tasks_filters(self, cstore, gateway):
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="ot1", display_name="客户甲")
        b = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                 external_id="ot2", display_name="客户乙")
        cstore.add_follow_up_task(a.contact.contact_id, due_at=1000, assignee="alice", note="回访")
        cstore.add_follow_up_task(b.contact.contact_id, due_at=5000, assignee="bob")
        # 全部未完成
        allt = cstore.list_open_tasks()
        assert len(allt) == 2
        assert allt[0]["due_at"] == 1000 and allt[0]["name"] == "客户甲"
        assert "web" in allt[0]["channels"]
        # 按 assignee
        assert len(cstore.list_open_tasks(assignee="alice")) == 1
        # 按到期
        assert len(cstore.list_open_tasks(due_before=2000)) == 1

    def test_reassign_and_snooze(self, cstore, gateway):
        c = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="rs1", display_name="A")
        tid = cstore.add_follow_up_task(c.contact.contact_id, due_at=1000, assignee="alice")
        # 改派
        assert cstore.reassign_task(tid, "bob") == c.contact.contact_id
        assert cstore.list_open_tasks(assignee="bob")[0]["task_id"] == tid
        # 延期 +2 天（从 max(now, due)）
        cid = cstore.snooze_task(tid, days=2)
        assert cid == c.contact.contact_id
        t = cstore.list_open_tasks()[0]
        assert t["due_at"] > 1000
        # 缓存随之更新
        assert cstore.get_contact(c.contact.contact_id).follow_up_at == t["due_at"]
        # 直设 due_at
        cstore.snooze_task(tid, due_at=9000)
        assert cstore.list_open_tasks()[0]["due_at"] == 9000
        # 已完成任务不能改派/延期
        cstore.complete_follow_up_task(tid)
        assert cstore.reassign_task(tid, "carol") is None
        assert cstore.snooze_task(tid, days=1) is None

    def test_gateway_reassign_snooze(self, gateway):
        c = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="grs1", display_name="A")
        out = gateway.add_follow_up_task(c.contact.contact_id, due_at=1000, operator="op")
        tid = out["task_id"]
        assert gateway.reassign_follow_up_task(tid, assignee="bob", operator="op")["ok"]
        assert gateway.snooze_follow_up_task(tid, days=3, operator="op")["ok"]
        assert gateway.reassign_follow_up_task("nope", assignee="x")["ok"] is False


class TestTaskApi:
    def _client(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        return _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))

    def test_my_tasks_and_assign_snooze(self, cstore, gateway):
        c = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="mt1", display_name="客户")
        cli = self._client(cstore, gateway)
        add = cli.post(f"/api/workspace/contact/{c.contact.contact_id}/follow-up",
                       json={"due_at": 1000, "note": "回访"}).json()
        tid = add["task_id"]
        # my-tasks scope=all due=all 含该任务
        mt = cli.get("/api/workspace/my-tasks", params={"scope": "all", "due": "all"}).json()
        assert mt["ok"] and any(t["task_id"] == tid for t in mt["tasks"])
        assert mt["tasks"][0]["overdue"] is True  # due 1000 已逾期
        # 改派
        assert cli.post(f"/api/workspace/follow-up/{tid}/assign",
                        json={"assignee": "bob"}).json()["ok"]
        # 改派缺 assignee → 400
        assert cli.post(f"/api/workspace/follow-up/{tid}/assign", json={}).status_code == 400
        # 延期
        assert cli.post(f"/api/workspace/follow-up/{tid}/snooze",
                        json={"days": 3}).json()["ok"]
        # 延期缺参数 → 400
        assert cli.post(f"/api/workspace/follow-up/{tid}/snooze", json={}).status_code == 400


class TestConvCrmBridge:
    def test_resolve_and_overdue(self, cstore, gateway):
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="rv_a", display_name="A")
        b = gateway.on_peer_seen(channel=CHANNEL_LINE, account_id="line1",
                                 external_id="rv_b", display_name="B")
        # 批量解析 (channel, external_id) → contact_id
        m = cstore.resolve_contacts_by_external([("web", "rv_a"), ("line", "rv_b"),
                                                 ("web", "missing")])
        assert m[("web", "rv_a")] == a.contact.contact_id
        assert m[("line", "rv_b")] == b.contact.contact_id
        assert ("web", "missing") not in m
        # 逾期集合
        cstore.set_follow_up(a.contact.contact_id, 1000)
        cstore.set_follow_up(b.contact.contact_id, 9e9)
        od = cstore.overdue_contact_ids(now_ts=2000)
        assert a.contact.contact_id in od and b.contact.contact_id not in od

    def test_dashboard_counts(self, cstore, gateway):
        import time as _t
        now = int(_t.time())
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="db_a", display_name="A")
        # created_at 应 >= 今日 0 点
        midnight = now - (now % 86400)
        assert cstore.count_contacts_created_since(midnight) >= 1
        cstore.add_follow_up_task(a.contact.contact_id, due_at=1000, assignee="alice")
        cstore.add_follow_up_task(a.contact.contact_id, due_at=9e9, assignee="bob")
        load = {x["assignee"]: x for x in cstore.agent_task_load()}
        assert load["alice"]["open"] == 1 and load["alice"]["overdue"] == 1
        assert load["bob"]["open"] == 1 and load["bob"]["overdue"] == 0


class TestConvCrmApi:
    def _client(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        return _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))

    def test_contact_tasks_endpoint(self, cstore, gateway):
        c = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="ct_ep", display_name="A")
        cstore.add_follow_up_task(c.contact.contact_id, due_at=1000)
        cli = self._client(cstore, gateway)
        r = cli.get(f"/api/workspace/contact/{c.contact.contact_id}/tasks").json()
        assert r["ok"] and len(r["tasks"]) == 1

    def test_dashboard_endpoint(self, cstore, gateway):
        gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                             external_id="db_ep", display_name="A")
        cli = self._client(cstore, gateway)
        r = cli.get("/api/workspace/dashboard").json()
        assert r["ok"] and "today" in r and "agent_load" in r
        assert r["today"]["new_contacts"] >= 1


class TestSlaTrendStore:
    """Phase 6-7：SLA last_message_dirs + 趋势/多事件聚合。"""

    def test_last_message_dirs(self, tmp_path):
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        store = InboxStore(tmp_path / "i.db")
        cid = "web:web:sla1"
        store.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key="sla1", display_name="V", language="zh",
                              last_text="m1", last_ts=5.0, unread=1),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                          text="hi", original_text="hi", translated_text="hi",
                          source_lang="zh", ts=5.0)])
        # 末条为入站
        d = store.last_message_dirs([cid])
        assert d[cid]["direction"] == "in" and d[cid]["ts"] == 5.0
        # 再来一条出站 → 末条变 out
        store.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key="sla1", display_name="V", language="zh",
                              last_text="reply", last_ts=9.0, unread=0),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="out",
                          text="reply", original_text="reply", translated_text="reply",
                          source_lang="zh", ts=9.0)])
        d2 = store.last_message_dirs([cid])
        assert d2[cid]["direction"] == "out" and d2[cid]["ts"] == 9.0
        # 空集合 / None
        assert store.last_message_dirs([]) == {}
        assert cid in store.last_message_dirs(None)
        store.close()

    def test_events_multi_and_by_day(self, cstore, gateway):
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="trend_a", display_name="A")
        jid = a.journey.journey_id
        cstore.append_event(journey_id=jid, event_type="lead_captured")
        cstore.append_event(journey_id=jid, event_type="lead_captured")
        cstore.append_event(journey_id=jid, event_type="handoff_sent")
        multi = cstore.count_events_since_multi(
            ["lead_captured", "handoff_sent"], 0)
        assert multi["lead_captured"] == 2 and multi["handoff_sent"] == 1
        assert cstore.count_events_since_multi([], 0) == {}
        # 按天聚合（至少今日一格有值）
        by_day = cstore.count_events_by_day("lead_captured", 0)
        assert sum(by_day.values()) == 2
        by_new = cstore.count_contacts_by_day(0)
        assert sum(by_new.values()) >= 1


class TestSlaTrendApi:
    def _client(self, cstore, gateway, inbox=None):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        return _client_with_inbox(
            cstore, gateway, inbox or InboxStore(Path(d) / "i.db"))

    def test_dashboard_has_trend_and_sla(self, cstore, gateway):
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        cid = "web:web:dbsla"
        inbox.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key="dbsla", display_name="V", language="zh",
                              last_text="hi", last_ts=1.0, unread=1),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                          text="hi", original_text="hi", translated_text="hi",
                          source_lang="zh", ts=1.0)])
        cli = self._client(cstore, gateway, inbox)
        r = cli.get("/api/workspace/dashboard").json()
        assert r["ok"]
        assert isinstance(r["trend"], list) and len(r["trend"]) == 7
        assert "new_contacts" in r["trend"][0] and "leads" in r["trend"][0]
        # 末条入站 → SLA 计为等待中且超时（ts=1.0 远早于现在）
        assert r["sla"]["waiting"] >= 1 and r["sla"]["breaching"] >= 1


class TestFirstResponseStore:
    """Phase 6-8：首响原始数据 first_response_rows。"""

    def _ingest(self, store, cid, direction, ts):
        from src.inbox.models import InboxConversation, InboxMessage
        store.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key=cid.split(":")[-1], display_name="V",
                              language="zh", last_text="x", last_ts=float(ts), unread=0),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction=direction,
                          text="x", original_text="x", translated_text="x",
                          source_lang="zh", ts=float(ts))])

    def test_first_response_rows(self, tmp_path):
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "i.db")
        # 会话 A：100 进线，130 回复 → 首响 30
        self._ingest(store, "web:web:A", "in", 100)
        self._ingest(store, "web:web:A", "out", 130)
        # 会话 B：200 进线，未回复 → t_out None
        self._ingest(store, "web:web:B", "in", 200)
        # 会话 C：先 50 出站（主动），80 进线，95 回复 → 首响以首条入站 80 起 = 15
        self._ingest(store, "web:web:C", "out", 50)
        self._ingest(store, "web:web:C", "in", 80)
        self._ingest(store, "web:web:C", "out", 95)
        rows = {r["cid"]: r for r in store.first_response_rows(0)}
        assert rows["web:web:A"]["t_out"] - rows["web:web:A"]["t_in"] == 30
        assert rows["web:web:B"]["t_out"] is None
        assert rows["web:web:C"]["t_in"] == 80
        assert rows["web:web:C"]["t_out"] - rows["web:web:C"]["t_in"] == 15
        # since 窗口过滤：仅 t_in>=150 → 只剩 B
        rows2 = {r["cid"]: r for r in store.first_response_rows(150)}
        assert set(rows2) == {"web:web:B"}
        store.close()


class TestSlaConfig:
    """Phase 6-8：SLA 阈值可配置 + 分级。"""

    def _client(self, cstore, gateway, inbox, cfg):
        from types import SimpleNamespace as NS
        app = FastAPI()
        register_unified_inbox_routes(
            app, page_auth=lambda r: True, api_auth=lambda r: True,
            templates=_Templates(), config_manager=NS(config=cfg))
        app.state.contacts = NS(store=cstore, gateway=gateway)
        app.state.inbox_store = inbox
        return TestClient(app)

    def test_chats_sla_levels(self, cstore, gateway):
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        import time as _t
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = _t.time()
        # 三个会话：刚来(0s)、warn(1h)、crit(3h)，均末条入站
        for key, age in [("fresh", 1), ("warnc", 3700), ("critc", 10900)]:
            cid = f"web:web:{key}"
            inbox.ingest_batch(
                InboxConversation(conversation_id=cid, platform="web", account_id="web",
                                  chat_key=key, display_name="V", language="zh",
                                  last_text="hi", last_ts=now - age, unread=1),
                [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                              text="hi", original_text="hi", translated_text="hi",
                              source_lang="zh", ts=now - age)])
        cfg = {"inbox": {"sla_warn_sec": 1800, "sla_crit_sec": 7200}}
        cli = self._client(cstore, gateway, inbox, cfg)
        chats = {c["chat_key"]: c for c in cli.get("/api/unified-inbox/chats").json()["chats"]}
        assert chats["fresh"]["sla_level"] == ""
        assert chats["warnc"]["sla_level"] == "warn" and chats["warnc"]["sla_breach"]
        assert chats["critc"]["sla_level"] == "crit"

    def test_dashboard_days_and_frt(self, cstore, gateway):
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        import time as _t
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = _t.time()
        cid = "web:web:frt"
        inbox.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key="frt", display_name="V", language="zh",
                              last_text="hi", last_ts=now, unread=0),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                          text="hi", original_text="hi", translated_text="hi",
                          source_lang="zh", ts=now - 60)])
        inbox.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key="frt", display_name="V", language="zh",
                              last_text="re", last_ts=now, unread=0),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="out",
                          text="re", original_text="re", translated_text="re",
                          source_lang="zh", ts=now - 30)])
        cfg = {"inbox": {}}
        cli = self._client(cstore, gateway, inbox, cfg)
        r7 = cli.get("/api/workspace/dashboard").json()
        assert r7["days"] == 7 and len(r7["trend"]) == 7
        assert "conversions" in r7["trend"][0]
        assert r7["first_response"]["today_responded"] == 1
        assert r7["first_response"]["today_attain_rate"] == 100.0
        assert len(r7["frt_trend"]) == 7
        r30 = cli.get("/api/workspace/dashboard?days=30").json()
        assert r30["days"] == 30 and len(r30["trend"]) == 30


class TestSlaAlerts:
    """Phase 6-9：SLA 告警端点 — 严重超时清单 + 分级计数。"""

    def _client(self, cstore, gateway, inbox, cfg):
        from types import SimpleNamespace as NS
        app = FastAPI()
        register_unified_inbox_routes(
            app, page_auth=lambda r: True, api_auth=lambda r: True,
            templates=_Templates(), config_manager=NS(config=cfg))
        app.state.contacts = NS(store=cstore, gateway=gateway)
        app.state.inbox_store = inbox
        return TestClient(app)

    def test_sla_alerts_snapshot(self, cstore, gateway):
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        import time as _t
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = _t.time()
        plan = [("fresh", 1, "in"), ("warnc", 3700, "in"),
                ("critc", 10900, "in"), ("answered", 99999, "out")]
        for key, age, direction in plan:
            cid = f"web:web:{key}"
            inbox.ingest_batch(
                InboxConversation(conversation_id=cid, platform="web", account_id="web",
                                  chat_key=key, display_name="N_" + key, language="zh",
                                  last_text="x", last_ts=now - age, unread=1),
                [InboxMessage(conversation_id=cid, platform_msg_id="", direction=direction,
                              text="x", original_text="x", translated_text="x",
                              source_lang="zh", ts=now - age)])
        cfg = {"inbox": {"sla_warn_sec": 1800, "sla_crit_sec": 7200}}
        cli = self._client(cstore, gateway, inbox, cfg)
        r = cli.get("/api/workspace/sla-alerts").json()
        assert r["ok"]
        # fresh+warnc+critc 末条入站 → waiting=3；warnc+critc 超 warn → breaching=2；critc 超 crit → critical=1
        assert r["waiting"] == 3
        assert r["breaching"] == 2
        assert r["critical"] == 1
        assert len(r["items"]) == 1
        assert r["items"][0]["chat_key"] == "critc"
        assert r["items"][0]["name"] == "N_critc"
        assert r["items"][0]["conversation_id"] == "web:web:critc"


class TestSlaByAgent:
    """Phase 6-10：坐席维度 SLA 归属（按活跃 claim）。"""

    def _client(self, cstore, gateway, inbox, cfg):
        from types import SimpleNamespace as NS
        app = FastAPI()
        register_unified_inbox_routes(
            app, page_auth=lambda r: True, api_auth=lambda r: True,
            templates=_Templates(), config_manager=NS(config=cfg))
        app.state.contacts = NS(store=cstore, gateway=gateway)
        app.state.inbox_store = inbox
        return TestClient(app)

    def test_sla_by_agent_attribution(self, cstore, gateway):
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        import time as _t
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = _t.time()
        # alice 认领两个：一个 crit(3h) 一个 warn(40m)；bob 认领一个 warn(40m)；
        # 一个无人认领 crit(3h)
        plan = [("a_crit", 10900, "alice", "Alice"),
                ("a_warn", 2400, "alice", "Alice"),
                ("b_warn", 2400, "bob", "Bob"),
                ("u_crit", 10900, None, None)]
        for key, age, aid, aname in plan:
            cid = f"web:web:{key}"
            inbox.ingest_batch(
                InboxConversation(conversation_id=cid, platform="web", account_id="web",
                                  chat_key=key, display_name=key, language="zh",
                                  last_text="x", last_ts=now - age, unread=1),
                [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                              text="x", original_text="x", translated_text="x",
                              source_lang="zh", ts=now - age)])
            if aid:
                inbox.set_conversation_claim(cid, aid, agent_name=aname, ttl_sec=3600)
        cfg = {"inbox": {"sla_warn_sec": 1800, "sla_crit_sec": 7200}}
        cli = self._client(cstore, gateway, inbox, cfg)
        d2 = cli.get("/api/workspace/dashboard").json()
        sba = {x["agent_id"]: x for x in d2["sla_by_agent"]}
        assert sba["alice"]["waiting"] == 2
        assert sba["alice"]["critical"] == 1 and sba["alice"]["breaching"] == 2
        assert sba["bob"]["waiting"] == 1 and sba["bob"]["critical"] == 0
        assert sba[""]["agent_name"] == "(未认领)" and sba[""]["critical"] == 1
        # 排序：critical 多者靠前 → alice 先于 未认领(同 crit=1 但 breaching 更高) 与 bob
        assert d2["sla_by_agent"][0]["agent_id"] == "alice"


class TestSlaDetail:
    """Phase 6-11：SLA/首响明细下钻端点。"""

    def _client(self, cstore, gateway, inbox, cfg):
        from types import SimpleNamespace as NS
        app = FastAPI()
        register_unified_inbox_routes(
            app, page_auth=lambda r: True, api_auth=lambda r: True,
            templates=_Templates(), config_manager=NS(config=cfg))
        app.state.contacts = NS(store=cstore, gateway=gateway)
        app.state.inbox_store = inbox
        return TestClient(app)

    def _ingest_in(self, inbox, key, age, now):
        from src.inbox.models import InboxConversation, InboxMessage
        cid = f"web:web:{key}"
        inbox.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key=key, display_name="N_" + key, language="zh",
                              last_text="x", last_ts=now - age, unread=1),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                          text="x", original_text="x", translated_text="x",
                          source_lang="zh", ts=now - age)])
        return cid

    def test_scopes_and_agent_filter(self, cstore, gateway):
        from src.inbox.store import InboxStore
        import time as _t
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = _t.time()
        self._ingest_in(inbox, "fresh", 60, now)
        self._ingest_in(inbox, "warnc", 2400, now)
        ccid = self._ingest_in(inbox, "critc", 10900, now)
        inbox.set_conversation_claim(ccid, "alice", agent_name="Alice", ttl_sec=3600)
        cfg = {"inbox": {"sla_warn_sec": 1800, "sla_crit_sec": 7200}}
        cli = self._client(cstore, gateway, inbox, cfg)
        # waiting=3, breaching(>=warn)=2, critical=1
        assert cli.get("/api/workspace/sla-detail?scope=waiting").json()["count"] == 3
        assert cli.get("/api/workspace/sla-detail?scope=breaching").json()["count"] == 2
        crit = cli.get("/api/workspace/sla-detail?scope=critical").json()
        assert crit["count"] == 1 and crit["items"][0]["agent_name"] == "Alice"
        assert crit["items"][0]["level"] == "crit"
        # agent 过滤："alice" 只命中 critc；"" 未认领命中其余 waiting
        assert cli.get("/api/workspace/sla-detail?scope=waiting&agent=alice").json()["count"] == 1
        assert cli.get("/api/workspace/sla-detail?scope=waiting&agent=").json()["count"] == 2
        # 非法 scope 回落 critical
        assert cli.get("/api/workspace/sla-detail?scope=bogus").json()["scope"] == "critical"

    def test_unresponded_scope(self, cstore, gateway):
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        import time as _t
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = _t.time()
        # A：今日进线未回复 → unresponded 命中
        self._ingest_in(inbox, "A", 300, now)
        # B：今日进线已回复 → 不命中
        cidB = "web:web:B"
        inbox.ingest_batch(
            InboxConversation(conversation_id=cidB, platform="web", account_id="web",
                              chat_key="B", display_name="N_B", language="zh",
                              last_text="r", last_ts=now, unread=0),
            [InboxMessage(conversation_id=cidB, platform_msg_id="", direction="in",
                          text="x", original_text="x", translated_text="x",
                          source_lang="zh", ts=now - 200),
             InboxMessage(conversation_id=cidB, platform_msg_id="", direction="out",
                          text="r", original_text="r", translated_text="r",
                          source_lang="zh", ts=now - 100)])
        cfg = {"inbox": {}}
        cli = self._client(cstore, gateway, inbox, cfg)
        r = cli.get("/api/workspace/sla-detail?scope=unresponded").json()
        keys = {it["chat_key"] for it in r["items"]}
        assert "A" in keys and "B" not in keys


class TestAgentFirstResponseStore:
    """Phase 6-12：agent_sends 归属 + agent_first_responses 查询。"""

    def _in(self, inbox, cid, ts):
        from src.inbox.models import InboxConversation, InboxMessage
        inbox.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key=cid.split(":")[-1], display_name="V",
                              language="zh", last_text="x", last_ts=float(ts), unread=0),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                          text="x", original_text="x", translated_text="x",
                          source_lang="zh", ts=float(ts))])

    def test_agent_first_responses(self, tmp_path):
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "i.db")
        # A：100 进线，alice 130 发送 → 归属 alice，首响 30
        self._in(store, "web:web:A", 100)
        store.record_agent_send("web:web:A", "alice", agent_name="Alice", ts=130)
        # 早于进线的发送不算（50<100）
        store.record_agent_send("web:web:A", "bob", agent_name="Bob", ts=50)
        # B：200 进线，无坐席发送 → resp None
        self._in(store, "web:web:B", 200)
        rows = {r["cid"]: r for r in store.agent_first_responses(0)}
        assert rows["web:web:A"]["agent_id"] == "alice"
        assert rows["web:web:A"]["resp_ts"] == 130
        assert rows["web:web:A"]["resp_ts"] - rows["web:web:A"]["t_in"] == 30
        assert rows["web:web:B"]["resp_ts"] is None
        assert rows["web:web:B"]["agent_id"] is None
        # 最早其后发送优先：A 再加一条 alice 200（应仍取 130）
        store.record_agent_send("web:web:A", "alice", ts=200)
        rows2 = {r["cid"]: r for r in store.agent_first_responses(0)}
        assert rows2["web:web:A"]["resp_ts"] == 130
        store.close()


class TestAgentFrtApi:
    def _client(self, cstore, gateway, inbox, cfg):
        from types import SimpleNamespace as NS

        def page_auth(request: Request):
            return True

        def api_auth(request: Request):
            return True

        app = FastAPI()
        register_unified_inbox_routes(
            app, page_auth=page_auth, api_auth=api_auth,
            templates=_Templates(), config_manager=NS(config=cfg))
        app.state.contacts = NS(store=cstore, gateway=gateway)
        app.state.inbox_store = inbox
        return TestClient(app)

    def test_dashboard_agent_frt(self, cstore, gateway):
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        import time as _t
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = _t.time()
        cid = "web:web:af"
        inbox.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key="af", display_name="V", language="zh",
                              last_text="x", last_ts=now, unread=0),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                          text="x", original_text="x", translated_text="x",
                          source_lang="zh", ts=now - 60)])
        inbox.record_agent_send(cid, "alice", agent_name="Alice", ts=now - 30)
        cfg = {"inbox": {"sla_warn_sec": 1800}}
        cli = self._client(cstore, gateway, inbox, cfg)
        r = cli.get("/api/workspace/dashboard").json()
        af = {x["agent_id"]: x for x in r["agent_frt"]}
        assert af["alice"]["responded"] == 1
        assert af["alice"]["attain_rate"] == 100.0
        assert af["alice"]["avg_sec"] == 30

    def test_send_records_agent_marker(self, cstore, gateway):
        # web 发送应打 agent_sends 点（坐席首响归属基建）
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        import time as _t
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = _t.time()
        cid = "web:web:visitorX"
        inbox.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key="visitorX", display_name="V", language="zh",
                              last_text="hi", last_ts=now - 60, unread=1),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                          text="hi", original_text="hi", translated_text="hi",
                          source_lang="zh", ts=now - 60)])
        cfg = {"web_chat": {"account_id": "web"}, "inbox": {}}
        cli = self._client(cstore, gateway, inbox, cfg)
        resp = cli.post("/api/unified-inbox/send", json={
            "platform": "web", "account_id": "web",
            "chat_key": "visitorX", "text": "你好"})
        assert resp.status_code == 200
        rows = inbox.agent_first_responses(0)
        byc = {r["cid"]: r for r in rows}
        assert byc[cid]["agent_id"] is not None


class TestAgentFrtDetail:
    """Phase 6-14：坐席首响明细下钻端点（agent + days 窗口）。"""

    def _client(self, cstore, gateway, inbox, cfg):
        from types import SimpleNamespace as NS
        app = FastAPI()
        register_unified_inbox_routes(
            app, page_auth=lambda r: True, api_auth=lambda r: True,
            templates=_Templates(), config_manager=NS(config=cfg))
        app.state.contacts = NS(store=cstore, gateway=gateway)
        app.state.inbox_store = inbox
        return TestClient(app)

    def test_agent_frt_detail(self, cstore, gateway):
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        import time as _t
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = _t.time()
        # alice：会话 A 首响 30s（达标），会话 B 首响 3000s（不达标）
        for key, in_age, send_age in [("A", 1000, 970), ("B", 5000, 2000)]:
            cid = f"web:web:{key}"
            inbox.ingest_batch(
                InboxConversation(conversation_id=cid, platform="web", account_id="web",
                                  chat_key=key, display_name="N_" + key, language="zh",
                                  last_text="x", last_ts=now - in_age, unread=0),
                [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                              text="x", original_text="x", translated_text="x",
                              source_lang="zh", ts=now - in_age)])
            inbox.record_agent_send(cid, "alice", agent_name="Alice", ts=now - send_age)
        # bob 的会话不应出现在 alice 明细
        cidc = "web:web:C"
        inbox.ingest_batch(
            InboxConversation(conversation_id=cidc, platform="web", account_id="web",
                              chat_key="C", display_name="N_C", language="zh",
                              last_text="x", last_ts=now - 800, unread=0),
            [InboxMessage(conversation_id=cidc, platform_msg_id="", direction="in",
                          text="x", original_text="x", translated_text="x",
                          source_lang="zh", ts=now - 800)])
        inbox.record_agent_send(cidc, "bob", agent_name="Bob", ts=now - 790)
        cfg = {"inbox": {"sla_warn_sec": 1800}}
        cli = self._client(cstore, gateway, inbox, cfg)
        r = cli.get("/api/workspace/agent-frt-detail?agent=alice&days=7").json()
        assert r["ok"] and r["agent"] == "alice" and r["days"] == 7
        keys = {it["chat_key"] for it in r["items"]}
        assert keys == {"A", "B"}
        byk = {it["chat_key"]: it for it in r["items"]}
        assert byk["A"]["attained"] is True
        assert byk["B"]["attained"] is False
        # 倒序：B(3000s) 在 A(30s) 之前
        assert r["items"][0]["chat_key"] == "B"
        # days=30 仍可用
        r30 = cli.get("/api/workspace/agent-frt-detail?agent=alice&days=30").json()
        assert r30["days"] == 30


class TestResolutionStats:
    """Phase 6-15：解决(引流)时长 — 首条 msg_in → handoff_sent。"""

    def _ev(self, cstore, jid, etype, ts, n=0):
        with cstore._lock:
            cstore._conn.execute(
                "INSERT INTO journey_events (event_id, journey_id, trace_id, "
                "event_type, payload_json, ts) VALUES (?,?,?,?,?,?)",
                (f"{jid}-{etype}-{n}", jid, "", etype, "{}", int(ts)))
            cstore._conn.commit()

    def test_resolution_stats(self, cstore, gateway):
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="res_a", display_name="A")
        b = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="res_b", display_name="B")
        ja, jb = a.journey.journey_id, b.journey.journey_id
        # A：msg_in 1000，handoff_sent 1300 → 300；早于进线的 handoff 不算
        self._ev(cstore, ja, "msg_in", 1000)
        self._ev(cstore, ja, "handoff_sent", 1300)
        self._ev(cstore, ja, "handoff_sent", 500, n=1)
        # B：仅 msg_in 2000 → 未解决
        self._ev(cstore, jb, "msg_in", 2000)
        rows = {r["journey_id"]: r for r in cstore.resolution_stats(0)}
        assert rows[ja]["resolved_ts"] == 1300
        assert rows[ja]["resolved_ts"] - rows[ja]["t_in"] == 300
        assert rows[jb]["resolved_ts"] is None
        # since 过滤：since=1500 → 只剩 B
        rows2 = {r["journey_id"]: r for r in cstore.resolution_stats(1500)}
        assert set(rows2) == {jb}

    def test_dashboard_resolution(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="res_dash", display_name="A")
        jid = a.journey.journey_id
        import time as _t
        now = int(_t.time())
        self._ev(cstore, jid, "msg_in", now - 120)
        self._ev(cstore, jid, "handoff_sent", now - 60)
        cli = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = cli.get("/api/workspace/dashboard").json()
        assert r["resolution"]["today_resolved"] >= 1
        assert r["resolution"]["today_avg_sec"] >= 0
        assert len(r["res_trend"]) == 7


class TestDailyReport:
    """Phase 6-16：坐席经营日报 CSV 导出。"""

    def _ev(self, cstore, jid, etype, ts, n=0):
        with cstore._lock:
            cstore._conn.execute(
                "INSERT INTO journey_events (event_id, journey_id, trace_id, "
                "event_type, payload_json, ts) VALUES (?,?,?,?,?,?)",
                (f"{jid}-{etype}-{n}", jid, "", etype, "{}", int(ts)))
            cstore._conn.commit()

    def _ingest(self, store, cid, direction, ts):
        from src.inbox.models import InboxConversation, InboxMessage
        store.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key=cid.split(":")[-1], display_name="V",
                              language="zh", last_text="x", last_ts=float(ts), unread=0),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction=direction,
                          text="x", original_text="x", translated_text="x",
                          source_lang="zh", ts=float(ts))])

    def test_daily_report_csv(self, cstore, gateway):
        from src.inbox.store import InboxStore
        import time as _t
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = int(_t.time())
        # 今日一笔：进线 → 60s 回复（首响 60，达标）
        self._ingest(inbox, "web:web:R1", "in", now - 120)
        self._ingest(inbox, "web:web:R1", "out", now - 60)
        # 今日解决：journey msg_in → handoff_sent
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="rep1", display_name="A")
        jid = a.journey.journey_id
        self._ev(cstore, jid, "msg_in", now - 120)
        self._ev(cstore, jid, "handoff_sent", now - 60)
        cli = _client_with_inbox(cstore, gateway, inbox)
        r = cli.get("/api/workspace/daily-report.csv?days=7")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]
        body = r.text.lstrip("\ufeff")
        lines = [ln for ln in body.splitlines() if ln.strip()]
        # 表头 + 7 天 + 合计
        assert lines[0].startswith("date,new_contacts,leads,conversions")
        assert len(lines) == 1 + 7 + 1
        assert lines[-1].startswith("合计")
        tot = lines[-1].split(",")
        # 合计 conversions(解决) >=1，frt_responded >=1
        assert int(tot[3]) >= 1   # conversions
        assert int(tot[5]) >= 1   # frt_responded
        assert int(tot[8]) >= 1   # resolved
        inbox.close()

    def test_daily_report_days_30(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        cli = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = cli.get("/api/workspace/daily-report.csv?days=30")
        lines = [ln for ln in r.text.lstrip("\ufeff").splitlines() if ln.strip()]
        assert len(lines) == 1 + 30 + 1


class TestSlaCreateTask:
    """Phase 6-17：SLA 超时会话一键生成跟进任务（告警→行动闭环）。"""

    def test_create_task_from_conversation(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="sla_t1", display_name="Tom")
        contact_id = a.contact.contact_id
        cli = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = cli.post("/api/workspace/sla/create-task", json={
            "platform": "web", "chat_key": "sla_t1", "wait_sec": 3660})
        body = r.json()
        assert body["ok"] is True
        assert body["task_id"]
        assert body["contact_id"] == contact_id
        tasks = cstore.list_follow_up_tasks(contact_id)
        assert len(tasks) == 1
        assert "61 分钟" in tasks[0]["note"]

    def test_create_task_via_conversation_id(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="sla_t2", display_name="Ann")
        cli = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = cli.post("/api/workspace/sla/create-task", json={
            "conversation_id": "web:web:sla_t2", "note": "VIP"})
        body = r.json()
        assert body["ok"] is True
        tasks = cstore.list_follow_up_tasks(a.contact.contact_id)
        assert "VIP" in tasks[0]["note"]

    def test_create_task_contact_not_found(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        cli = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = cli.post("/api/workspace/sla/create-task", json={
            "platform": "web", "chat_key": "no_such"})
        assert r.json()["error"] == "contact_not_found"

    def test_create_task_missing_keys(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        cli = _client_with_inbox(cstore, gateway, InboxStore(Path(d) / "i.db"))
        r = cli.post("/api/workspace/sla/create-task", json={})
        assert r.status_code == 400


class TestAgentPrefs:
    """Phase 6-18：告警个性化 — 每坐席 SLA 阈值 + 免打扰 + 静音。"""

    def test_store_get_set_prefs(self, tmp_path):
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "i.db")
        d = store.get_agent_prefs("a1")
        assert d["warn_sec"] == 0 and d["dnd_start"] == -1
        store.set_agent_prefs("a1", warn_sec=600, crit_sec=1200,
                              muted=1, dnd_start=1320, dnd_end=480)
        d = store.get_agent_prefs("a1")
        assert d["warn_sec"] == 600 and d["crit_sec"] == 1200
        assert d["muted"] == 1 and d["dnd_start"] == 1320 and d["dnd_end"] == 480
        # 覆盖写
        store.set_agent_prefs("a1", warn_sec=0, crit_sec=0)
        assert store.get_agent_prefs("a1")["warn_sec"] == 0
        store.close()

    def test_dnd_active(self):
        import time as _t
        from src.web.routes.unified_inbox_routes import _dnd_active
        # 构造一个跨午夜 22:00-08:00，取 23:00 应 active
        base = _t.struct_time((2026, 6, 6, 23, 0, 0, 5, 157, -1))
        now = _t.mktime(base)
        assert _dnd_active({"dnd_start": 1320, "dnd_end": 480}, now) is True
        # 12:00 不在 22:00-08:00
        noon = _t.mktime(_t.struct_time((2026, 6, 6, 12, 0, 0, 5, 157, -1)))
        assert _dnd_active({"dnd_start": 1320, "dnd_end": 480}, noon) is False
        # 关闭
        assert _dnd_active({"dnd_start": -1, "dnd_end": -1}, now) is False

    def _crit_conv(self, inbox, cid="web:web:Z"):
        import time
        from src.inbox.models import InboxConversation, InboxMessage
        old = time.time() - 99999
        inbox.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key=cid.split(":")[-1], display_name="Z",
                              language="zh", last_text="x", last_ts=old, unread=1),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                          text="x", original_text="x", translated_text="x",
                          source_lang="zh", ts=old)])

    def test_prefs_endpoint_and_mute(self, cstore, gateway):
        import time
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        self._crit_conv(inbox)
        cli = _client_with_inbox(cstore, gateway, inbox)
        # 默认：有严重超时
        snap = cli.get("/api/workspace/sla-alerts").json()
        assert snap["critical"] >= 1 and snap["quiet"] is False
        assert len(snap["items"]) >= 1
        # 设置静音 → quiet=True，items 清空，但 critical 计数仍在
        r = cli.post("/api/workspace/prefs", json={"muted": 1})
        assert r.json()["ok"] is True
        snap2 = cli.get("/api/workspace/sla-alerts").json()
        assert snap2["quiet"] is True
        assert snap2["items"] == []
        assert snap2["critical"] >= 1
        inbox.close()

    def test_prefs_threshold_override(self, cstore, gateway):
        import time
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        # 一条等待 ~50 分钟的会话：全局 crit=120 分不触发严重，个人 crit=30 分则触发
        from src.inbox.models import InboxConversation, InboxMessage
        ts = time.time() - 3000  # 50 分钟
        inbox.ingest_batch(
            InboxConversation(conversation_id="web:web:T", platform="web",
                              account_id="web", chat_key="T", display_name="T",
                              language="zh", last_text="x", last_ts=ts, unread=1),
            [InboxMessage(conversation_id="web:web:T", platform_msg_id="",
                          direction="in", text="x", original_text="x",
                          translated_text="x", source_lang="zh", ts=ts)])
        cli = _client_with_inbox(cstore, gateway, inbox)
        snap = cli.get("/api/workspace/sla-alerts").json()
        assert snap["critical"] == 0  # 全局默认 crit=2h 未到
        cli.post("/api/workspace/prefs", json={"crit_sec": 1800})  # 个人 30 分
        snap2 = cli.get("/api/workspace/sla-alerts").json()
        assert snap2["critical"] >= 1
        inbox.close()


class TestAgentDailyReport:
    """Phase 6-19：坐席个人日报 CSV（首响/发送量/完成任务）。"""

    def test_store_by_day_aggregations(self, cstore, gateway, tmp_path):
        import time
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "i.db")
        now = time.time()
        store.record_agent_send("web:web:c1", "ag1", agent_name="A", ts=now)
        store.record_agent_send("web:web:c2", "ag1", agent_name="A", ts=now)
        store.record_agent_send("web:web:c3", "ag2", agent_name="B", ts=now)
        by = store.count_agent_sends_by_day("ag1", 0)
        assert sum(by.values()) == 2
        assert store.count_agent_sends_by_day("zzz", 0) == {}
        # tasks done by agent
        a = gateway.on_peer_seen(channel=CHANNEL_WEB, account_id="web",
                                 external_id="adr", display_name="X")
        cid = a.contact.contact_id
        tid = cstore.add_follow_up_task(cid, due_at=int(now), created_by="ag1")
        cstore.complete_follow_up_task(tid, done_by="ag1")
        byt = cstore.count_tasks_done_by_day("ag1", 0)
        assert sum(byt.values()) == 1
        assert cstore.count_tasks_done_by_day("none", 0) == {}
        store.close()

    def test_agent_report_csv(self, cstore, gateway):
        import time
        from src.inbox.store import InboxStore
        from src.inbox.models import InboxConversation, InboxMessage
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = time.time()
        # 进线 → 60s 后该坐席发送（首响 60，达标）
        inbox.ingest_batch(
            InboxConversation(conversation_id="web:web:P", platform="web",
                              account_id="web", chat_key="P", display_name="P",
                              language="zh", last_text="x", last_ts=now - 120, unread=1),
            [InboxMessage(conversation_id="web:web:P", platform_msg_id="",
                          direction="in", text="x", original_text="x",
                          translated_text="x", source_lang="zh", ts=now - 120)])
        inbox.record_agent_send("web:web:P", "ag1", agent_name="A", ts=now - 60)
        cli = _client_with_inbox(cstore, gateway, inbox)
        r = cli.get("/api/workspace/daily-report.csv?days=7&agent=ag1")
        assert r.status_code == 200
        assert "agent-report-ag1" in r.headers["content-disposition"]
        lines = [ln for ln in r.text.lstrip("\ufeff").splitlines() if ln.strip()]
        assert lines[0].startswith("date,first_responded,frt_avg_sec")
        assert len(lines) == 1 + 7 + 1
        tot = lines[-1].split(",")
        assert tot[0] == "合计"
        assert int(tot[1]) >= 1   # first_responded
        assert int(tot[4]) >= 1   # sends
        inbox.close()


class TestEscalation:
    """Phase 6-20：告警升级 — 无人有效处理的严重超时（全局口径）。"""

    def _crit(self, inbox, cid):
        import time
        from src.inbox.models import InboxConversation, InboxMessage
        old = time.time() - 99999
        inbox.ingest_batch(
            InboxConversation(conversation_id=cid, platform="web", account_id="web",
                              chat_key=cid.split(":")[-1], display_name=cid.split(":")[-1],
                              language="zh", last_text="x", last_ts=old, unread=1),
            [InboxMessage(conversation_id=cid, platform_msg_id="", direction="in",
                          text="x", original_text="x", translated_text="x",
                          source_lang="zh", ts=old)])

    def test_unclaimed_escalates(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        self._crit(inbox, "web:web:U")
        cli = _client_with_inbox(cstore, gateway, inbox)
        snap = cli.get("/api/workspace/escalations").json()
        assert snap["count"] == 1
        assert snap["items"][0]["reason"] == "unclaimed"
        inbox.close()

    def test_online_claim_no_escalate(self, cstore, gateway):
        import time
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        self._crit(inbox, "web:web:C")
        inbox.set_conversation_claim("web:web:C", "ag1", agent_name="A")
        inbox.upsert_agent_presence("ag1", display_name="A", status="online")
        cli = _client_with_inbox(cstore, gateway, inbox)
        snap = cli.get("/api/workspace/escalations").json()
        assert snap["count"] == 0
        inbox.close()

    def test_offline_holder_escalates(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        self._crit(inbox, "web:web:O")
        inbox.set_conversation_claim("web:web:O", "ag2", agent_name="B")
        # 无 presence 记录 → 视为离线
        cli = _client_with_inbox(cstore, gateway, inbox)
        snap = cli.get("/api/workspace/escalations").json()
        assert snap["count"] == 1
        assert snap["items"][0]["reason"] == "holder_offline"
        inbox.close()

    def test_quiet_holder_escalates(self, cstore, gateway):
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        self._crit(inbox, "web:web:Q")
        inbox.set_conversation_claim("web:web:Q", "ag3", agent_name="C")
        inbox.upsert_agent_presence("ag3", display_name="C", status="online")
        inbox.set_agent_prefs("ag3", muted=1)
        cli = _client_with_inbox(cstore, gateway, inbox)
        snap = cli.get("/api/workspace/escalations").json()
        assert snap["count"] == 1
        assert snap["items"][0]["reason"] == "holder_quiet"
        inbox.close()


class TestEscalationAudit:
    """Phase 6-21：升级问责审计 — record/dedup/count + snapshot today_count。"""

    def test_record_dedup_and_count(self, tmp_path):
        import time
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "i.db")
        now = time.time()
        assert store.record_escalation("web:web:A", reason="unclaimed",
                                       wait_sec=9000, ts=now) is True
        # 同会话 1h 内去重
        assert store.record_escalation("web:web:A", reason="unclaimed",
                                       wait_sec=9100, ts=now + 10) is False
        # 不同会话照记
        assert store.record_escalation("web:web:B", reason="holder_offline",
                                       wait_sec=8000, ts=now) is True
        assert store.count_escalations_since(0) == 2
        # 超出 dedup 窗口可再记
        assert store.record_escalation("web:web:A", reason="unclaimed",
                                       wait_sec=9999, ts=now + 4000) is True
        assert store.count_escalations_since(0) == 3
        rows = store.list_escalations(0)
        assert len(rows) == 3 and rows[0]["ts"] >= rows[-1]["ts"]
        store.close()

    def test_snapshot_today_count(self, cstore, gateway):
        import time
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        inbox.record_escalation("web:web:Z", reason="unclaimed", ts=time.time())
        cli = _client_with_inbox(cstore, gateway, inbox)
        snap = cli.get("/api/workspace/escalations").json()
        assert snap["today_count"] >= 1
        inbox.close()


class TestEscalationLog:
    """Phase 6-22：升级历史 + 接管时延。"""

    def test_takeovers_store(self, tmp_path):
        import time
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "i.db")
        now = time.time()
        # 升级 A 在 now-300，接管在 now-240 → 时延 60
        store.record_escalation("web:web:A", reason="unclaimed", ts=now - 300)
        store.record_agent_send("web:web:A", "ag1", agent_name="A", ts=now - 240)
        # 升级 B 无接管
        store.record_escalation("web:web:B", reason="holder_offline", ts=now - 100)
        rows = {r["conversation_id"]: r for r in store.escalation_takeovers(0)}
        assert int(rows["web:web:A"]["taken_ts"] - rows["web:web:A"]["ts"]) == 60
        assert rows["web:web:A"]["taken_by"] == "ag1"
        assert rows["web:web:B"]["taken_ts"] is None
        # 接管必须晚于升级：A 升级前的发送不算
        store.record_escalation("web:web:C", reason="unclaimed", ts=now)
        store.record_agent_send("web:web:C", "ag2", ts=now - 9999)
        rows2 = {r["conversation_id"]: r for r in store.escalation_takeovers(0)}
        assert rows2["web:web:C"]["taken_ts"] is None
        store.close()

    def test_escalation_log_endpoint(self, cstore, gateway):
        import time
        from src.inbox.store import InboxStore
        d = tempfile.mkdtemp()
        inbox = InboxStore(Path(d) / "i.db")
        now = time.time()
        inbox.record_escalation("web:web:X", reason="unclaimed", ts=now - 200)
        inbox.record_agent_send("web:web:X", "ag1", ts=now - 140)  # 时延 60
        inbox.record_escalation("web:web:Y", reason="holder_offline", ts=now - 50)
        cli = _client_with_inbox(cstore, gateway, inbox)
        d2 = cli.get("/api/workspace/escalation-log?days=7").json()
        assert d2["ok"] is True
        st = d2["stats"]
        assert st["total"] == 2
        assert st["taken"] == 1
        assert st["taken_rate"] == 50.0
        assert st["avg_takeover_sec"] == 60
        inbox.close()


class TestCrmWidgetsAsset:
    """Phase 6-13：共享前端组件抽取的静态资产与挂载守卫。"""

    def _root(self):
        import src.web.routes.unified_inbox_routes as m
        return Path(m.__file__).resolve().parents[3]

    def test_widget_file_exposes_api(self):
        js = (self._root() / "src" / "web" / "static" / "workspace"
              / "crm-widgets.js").read_text(encoding="utf-8")
        assert "window.CRMW" in js
        for fn in ["esc", "fmtDur", "fmtWait", "fmtWaitMin", "spark",
                   "sparkPct", "toast"]:
            assert (fn + ":") in js or ("function " + fn) in js

    def test_base_template_includes_widget(self):
        tpl = (self._root() / "src" / "web" / "templates"
               / "workspace_base.html").read_text(encoding="utf-8")
        assert "/static/workspace/crm-widgets.js" in tpl


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
