"""R9d 坐席工作台危机入口：crisis_summary_for_user + contact-profile 危机块。

覆盖 SkillManager 概览方法（空/有记录/未处理计数）与 contact-profile 端点在
注入 skill_manager 后挂出 crisis 块、无记录时为 None。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.utils.crisis_event_store import CrisisEventStore


# ── SkillManager.crisis_summary_for_user ────────────────────────────────

class _SM:
    """最小 SkillManager 替身：仅持有 _crisis_store + 真方法。"""

    def __init__(self, store):
        self._crisis_store = store

    crisis_summary_for_user = (
        __import__("src.skills.skill_manager", fromlist=["SkillManager"])
        .SkillManager.crisis_summary_for_user
    )


def _store(tmp_path):
    return CrisisEventStore(tmp_path / "crisis.db")


def test_summary_empty_when_no_store():
    sm = _SM(None)
    out = sm.crisis_summary_for_user("u1")
    assert out == {"total": 0, "unhandled": 0, "has_more": False,
                   "latest": None, "recent": []}


def test_summary_empty_when_no_match(tmp_path):
    st = _store(tmp_path)
    st.record(user_id="other", level="severe")
    sm = _SM(st)
    out = sm.crisis_summary_for_user("u1")
    assert out["total"] == 0
    st.close()


def test_summary_counts_and_latest(tmp_path):
    st = _store(tmp_path)
    st.record(user_id="u1", level="elevated", category="despair")
    sev_id = st.record(user_id="u1", level="severe", category="self_harm",
                       escalated=True)
    sm = _SM(st)
    out = sm.crisis_summary_for_user("u1")
    assert out["total"] == 2
    assert out["unhandled"] == 2
    assert out["latest"]["id"] == sev_id
    assert out["latest"]["level"] == "severe"
    assert out["latest"]["escalated"] is True
    st.close()


def test_summary_unhandled_excludes_handled(tmp_path):
    st = _store(tmp_path)
    eid = st.record(user_id="u1", level="severe")
    st.record(user_id="u1", level="elevated")
    st.mark_handled(eid, handled_by="agent")
    sm = _SM(st)
    out = sm.crisis_summary_for_user("u1")
    assert out["total"] == 2
    assert out["unhandled"] == 1
    st.close()


def test_summary_blank_key(tmp_path):
    st = _store(tmp_path)
    st.record(user_id="u1", level="severe")
    sm = _SM(st)
    assert sm.crisis_summary_for_user("")["total"] == 0
    st.close()


# ── R9e：群聊按 chat_id 匹配 ─────────────────────────────────────────────

def test_summary_matches_group_by_chat_id(tmp_path):
    """群聊：危机 user_id=个人、chat_id=群；以群 key 应能命中。"""
    st = _store(tmp_path)
    st.record(user_id="member_99", level="severe", chat_id="-100200")
    sm = _SM(st)
    out = sm.crisis_summary_for_user("-100200")  # 群 chat_key
    assert out["total"] == 1
    assert out["latest"]["level"] == "severe"
    st.close()


def test_summary_private_still_matches_by_user_id(tmp_path):
    """私聊：chat_id 可能为空/等于 user；以 user key 仍命中。"""
    st = _store(tmp_path)
    st.record(user_id="u1", level="elevated", chat_id="u1")
    sm = _SM(st)
    out = sm.crisis_summary_for_user("u1")
    assert out["total"] == 1
    st.close()


def test_summary_chat_id_exact_not_prefix(tmp_path):
    """chat_id 是精确匹配，不前缀误命中相邻群。"""
    st = _store(tmp_path)
    st.record(user_id="m1", level="severe", chat_id="-100")
    sm = _SM(st)
    # "-10" 不应前缀命中 chat_id="-100"（chat_id 走精确）
    out = sm.crisis_summary_for_user("-10")
    assert out["total"] == 0
    st.close()


def test_list_recent_match_key_or_semantics(tmp_path):
    """store.list_recent(match_key) = user_id 前缀 OR chat_id 精确。"""
    st = _store(tmp_path)
    st.record(user_id="alice", level="severe", chat_id="-555")
    st.record(user_id="bob", level="elevated", chat_id="-555")
    st.record(user_id="carol", level="severe", chat_id="-999")
    # match_key=-555 命中前两条（chat_id 精确）
    rows = st.list_recent(match_key="-555")
    assert len(rows) == 2
    # match_key=alice 命中第一条（user_id 前缀）
    rows2 = st.list_recent(match_key="alice")
    assert len(rows2) == 1 and rows2[0]["user_id"] == "alice"
    st.close()


# ── contact-profile crisis 块 ───────────────────────────────────────────

def _make_app(store, sm=None, role="agent"):
    from src.web.routes.unified_inbox_routes import register_unified_inbox_routes

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.scope["session"] = {"role": role, "user_id": "u1"}
        return await call_next(request)

    def api_auth(r: Request):
        return True

    def page_auth(r: Request):
        return True

    cfg = MagicMock()
    cfg.config = {}
    cfg.get = lambda k, d=None: d

    register_unified_inbox_routes(
        app, config_manager=cfg, api_auth=api_auth,
        page_auth=page_auth, templates=MagicMock(),
    )
    if store:
        app.state.inbox_store = store
    # 注入 telegram_client.skill_manager
    app.state.telegram_client = SimpleNamespace(skill_manager=sm) if sm else None
    return TestClient(app, raise_server_exceptions=True)


def test_contact_profile_includes_crisis_block(tmp_path):
    st = _store(tmp_path)
    st.record(user_id="u1", level="severe", category="self_harm", escalated=True)
    sm = _SM(st)
    client = _make_app(store=None, sm=sm)
    r = client.get("/api/unified-inbox/contact-profile?conversation_id=tg:acc:u1")
    assert r.status_code == 200
    cr = r.json().get("crisis")
    assert cr is not None
    assert cr["total"] == 1
    assert cr["unhandled"] == 1
    assert cr["latest"]["level"] == "severe"
    st.close()


def test_contact_profile_crisis_none_when_no_record(tmp_path):
    st = _store(tmp_path)
    sm = _SM(st)
    client = _make_app(store=None, sm=sm)
    r = client.get("/api/unified-inbox/contact-profile?conversation_id=tg:acc:u1")
    assert r.status_code == 200
    assert r.json().get("crisis") is None
    st.close()


def test_contact_profile_crisis_none_without_skill_manager():
    client = _make_app(store=None, sm=None)
    r = client.get("/api/unified-inbox/contact-profile?conversation_id=tg:acc:u1")
    assert r.status_code == 200
    assert r.json().get("crisis") is None
