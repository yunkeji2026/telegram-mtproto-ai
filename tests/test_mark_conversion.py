"""阶段 E：人工标记成交/转化（修复"空心漏斗"终点不可达）。

覆盖：FSM 终点可达性 + /api/unified-inbox/mark-conversion 端点契约
（contacts 未启用 / 非法 stage / 未关联客户 / 成功推进 / FSM 守卫拦非法前驱）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.journey_fsm import is_transition_allowed
from src.contacts.models import STAGE_BONDED, STAGE_CONVERTED, STAGE_LINE_ENGAGED
from src.contacts.store import ContactStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


# ── FSM 终点可达性（纯函数）──────────────────────────────

def test_bonded_reachable_from_line_engaged():
    assert is_transition_allowed(STAGE_LINE_ENGAGED, STAGE_BONDED) is True


def test_bonded_not_reachable_from_initial():
    assert is_transition_allowed("INITIAL", STAGE_BONDED) is False


def test_converted_reachable_from_anywhere():
    # CONVERTED 未在转移表中限制前驱 → 任意阶段可达（人工确认转化）
    assert is_transition_allowed(STAGE_BONDED, STAGE_CONVERTED) is True
    assert is_transition_allowed("INITIAL", STAGE_CONVERTED) is True


# ── 路由契约 ──────────────────────────────────────────────

class _Templates:
    def TemplateResponse(self, *a, **k):
        raise AssertionError("not used")


class _ContactsHolder:
    def __init__(self, store):
        self.store = store
        self.gateway = None


def _client(contacts_store=None, conv_meta=None):
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_unified_inbox_routes(app, page_auth=_auth, api_auth=_auth, templates=_Templates())
    if contacts_store is not None:
        app.state.contacts = _ContactsHolder(contacts_store)
    if conv_meta is not None:
        class _Inbox:
            def get_conv_meta(self, cid):
                return conv_meta
        app.state.inbox_store = _Inbox()
    return TestClient(app)


def _post(client, body):
    return client.post("/api/unified-inbox/mark-conversion", json=body).json()


def _seed_journey(store, stage):
    contact, _ci, _new = store.ensure_channel_identity(
        channel="web", account_id="default", external_id="u1",
    )
    j = store.get_journey_by_contact(contact.contact_id)
    store.update_journey(j.journey_id, funnel_stage=stage)
    return contact.contact_id, j.journey_id


def test_contacts_disabled():
    r = _post(_client(), {"conversation_id": "c1", "contact_id": "x", "stage": "BONDED"})
    assert r["ok"] is False and r["reason"] == "contacts_disabled"


def test_bad_stage(tmp_path):
    store = ContactStore(db_path=tmp_path / "c.db")
    try:
        r = _post(_client(contacts_store=store),
                  {"contact_id": "x", "stage": "WARMING"})
        assert r["ok"] is False and r["reason"] == "bad_stage"
    finally:
        store.close()


def test_no_contact(tmp_path):
    store = ContactStore(db_path=tmp_path / "c.db")
    try:
        # 无 contact_id 且无会话 meta → no_contact
        r = _post(_client(contacts_store=store), {"conversation_id": "c1", "stage": "BONDED"})
        assert r["ok"] is False and r["reason"] == "no_contact"
    finally:
        store.close()


def test_mark_bonded_happy(tmp_path):
    store = ContactStore(db_path=tmp_path / "c.db")
    try:
        contact_id, journey_id = _seed_journey(store, STAGE_LINE_ENGAGED)
        r = _post(_client(contacts_store=store),
                  {"contact_id": contact_id, "stage": "BONDED"})
        assert r["ok"] is True
        assert r["funnel_stage"] == "BONDED"
        assert r["funnel_stage_label"] == "成交"
        # 落库 + 事件
        assert store.get_journey(journey_id).funnel_stage == "BONDED"
        evts = store.list_events(journey_id, limit=20)
        assert any(e.get("event_type") == "stage_change" for e in evts)
    finally:
        store.close()


def test_mark_blocked_from_initial(tmp_path):
    store = ContactStore(db_path=tmp_path / "c.db")
    try:
        contact_id, journey_id = _seed_journey(store, "INITIAL")
        r = _post(_client(contacts_store=store),
                  {"contact_id": contact_id, "stage": "BONDED"})
        assert r["ok"] is False and r["reason"] == "transition_blocked"
        assert r["current_stage"] == "INITIAL"
        # 未污染：仍停在 INITIAL
        assert store.get_journey(journey_id).funnel_stage == "INITIAL"
    finally:
        store.close()


def test_contact_resolved_via_conversation_meta(tmp_path):
    store = ContactStore(db_path=tmp_path / "c.db")
    try:
        contact_id, journey_id = _seed_journey(store, STAGE_LINE_ENGAGED)
        client = _client(contacts_store=store, conv_meta={"contact_id": contact_id})
        # 不传 contact_id，靠会话 meta 解析
        r = _post(client, {"conversation_id": "telegram:default:c1", "stage": "CONVERTED"})
        assert r["ok"] is True and r["funnel_stage"] == "CONVERTED"
        assert store.get_journey(journey_id).funnel_stage == "CONVERTED"
    finally:
        store.close()
