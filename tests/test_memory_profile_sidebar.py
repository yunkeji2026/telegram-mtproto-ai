"""R14 记忆画像聚合卡：episodic_profile_summary + contact-profile memory_profile 块。

覆盖 SkillManager 包装（空/有记录）与 contact-profile 端点在注入 skill_manager 后
挂出 memory_profile 块、无记录时为 None。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.utils.episodic_memory_store import EpisodicMemoryStore


class _SM:
    """最小 SkillManager 替身：持有 _episodic_store + 真包装方法。"""

    def __init__(self, store):
        self._episodic_store = store

    episodic_profile_summary = (
        __import__("src.skills.skill_manager", fromlist=["SkillManager"])
        .SkillManager.episodic_profile_summary
    )


def _epi(tmp_path):
    return EpisodicMemoryStore(tmp_path / "epi.db")


# ── SkillManager.episodic_profile_summary ───────────────────────────────

def test_wrapper_empty_when_no_store():
    sm = _SM(None)
    out = sm.episodic_profile_summary("u1")
    assert out["total"] == 0 and out["top_stable"] == []


def test_wrapper_blank_key(tmp_path):
    st = _epi(tmp_path)
    st.add_fact("u1", "用户住在杭州")
    sm = _SM(st)
    assert sm.episodic_profile_summary("")["total"] == 0
    st.close()


def test_wrapper_counts(tmp_path):
    st = _epi(tmp_path)
    st.add_fact("u1", "用户住在杭州", source="user_stated")
    st.add_fact("u1", "用户可能单身", source="ai_inferred")
    sm = _SM(st)
    out = sm.episodic_profile_summary("u1")
    assert out["total"] == 2
    assert out["user_stated"] == 1 and out["ai_inferred"] == 1
    st.close()


# ── contact-profile memory_profile 块 ───────────────────────────────────

def _make_app(sm=None, role="agent"):
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
    app.state.telegram_client = SimpleNamespace(skill_manager=sm) if sm else None
    return TestClient(app, raise_server_exceptions=True)


def test_contact_profile_includes_memory_profile(tmp_path):
    st = _epi(tmp_path)
    rid = st.add_fact("u1", "用户是设计师", source="user_stated")
    st.add_fact("u1", "用户可能喜欢猫", source="ai_inferred")
    st._conn.execute("UPDATE episodic_memory SET tier='stable' WHERE id=?", (rid,))
    st._conn.commit()
    sm = _SM(st)
    client = _make_app(sm=sm)
    r = client.get("/api/unified-inbox/contact-profile?conversation_id=tg:acc:u1")
    assert r.status_code == 200
    mp = r.json().get("memory_profile")
    assert mp is not None
    assert mp["total"] == 2
    assert mp["stable"] == 1
    assert mp["ai_inferred"] == 1
    assert "用户是设计师" in mp["top_stable"]
    # R15：AI 推断挂出待确认列表
    pend = mp.get("pending_inferred") or []
    assert any(p["content"] == "用户可能喜欢猫" for p in pend)
    st.close()


def test_contact_profile_memory_profile_none_when_empty(tmp_path):
    st = _epi(tmp_path)
    sm = _SM(st)
    client = _make_app(sm=sm)
    r = client.get("/api/unified-inbox/contact-profile?conversation_id=tg:acc:u1")
    assert r.status_code == 200
    assert r.json().get("memory_profile") is None
    st.close()


def test_contact_profile_memory_profile_none_without_skill_manager():
    client = _make_app(sm=None)
    r = client.get("/api/unified-inbox/contact-profile?conversation_id=tg:acc:u1")
    assert r.status_code == 200
    assert r.json().get("memory_profile") is None


def test_contact_profile_memory_profile_falls_back_to_cid(tmp_path):
    """私聊键命不中时退回完整 conversation_id 作为存储键。"""
    st = _epi(tmp_path)
    st.add_fact("tg:acc:u1", "用户偏好晚上聊天", source="user_stated")
    sm = _SM(st)
    client = _make_app(sm=sm)
    r = client.get("/api/unified-inbox/contact-profile?conversation_id=tg:acc:u1")
    assert r.status_code == 200
    mp = r.json().get("memory_profile")
    assert mp is not None and mp["total"] == 1
    st.close()
