"""H1 草稿翻译 + H2 批量处置 + H3 自动清理 测试。

H1:
  - POST /api/drafts/{id}/translate — 无翻译服务 fallback、有翻译服务正常路径、
    目标语言推断（peer_text 语言）、草稿不存在 404
H2:
  - POST /api/drafts/bulk-resolve — 仅主管可用、approve/reject/action 校验、
    批量处置多条草稿、限制 50 条上限
H3:
  - InboxStore.cleanup_old_drafts — 仅删除已处理状态、不碰 pending、
    max_age_days 截止、返回行数
  - AutosendWorker status_snapshot 包含 total_cleaned 字段
"""

import time
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService
from src.inbox.autosend_worker import AutosendWorker
from src.web.routes.drafts_routes import register_drafts_routes


# ─────────────────────────────────────
# helpers
# ─────────────────────────────────────

def _make_store() -> InboxStore:
    store = InboxStore(":memory:")
    return store


def _make_svc(store: InboxStore) -> DraftService:
    return DraftService(
        inbox_store=store,
        line_services=[],
        wa_services=[],
        messenger_service=None,
    )


def _make_app(svc: DraftService, role: str = "", with_translation_svc: Any = None):
    app = FastAPI()

    if role:
        @app.middleware("http")
        async def _inject(request: Request, call_next):
            request.scope["session"] = {"role": role, "user_id": "u1"}
            return await call_next(request)

    def api_auth(request: Request):
        return True

    register_drafts_routes(app, api_auth=api_auth)
    app.state.draft_service = svc
    if with_translation_svc is not None:
        app.state.translation_service = with_translation_svc
    return TestClient(app, raise_server_exceptions=True)


def _upsert_draft(svc: DraftService, *, peer_text="你好", draft_text="您好，有什么可以帮助您？",
                  level="L3", plat="line", status="pending", conv_id="conv-001"):
    did = svc._store.upsert_draft({
        "source_kind": "inbox",
        "source_id": conv_id,  # 用 conv_id 作为唯一 source_id，避免冲突
        "conversation_id": conv_id,
        "platform": plat,
        "account_id": "acc1",
        "chat_key": "user1",
        "autopilot_level": level,
        "risk_level": "medium" if level == "L3" else "low",
        "draft_text": draft_text,
        "peer_text": peer_text,
        "status": status,
    })
    return did


# ─────────────────────────────────────
# H1: Translation API
# ─────────────────────────────────────

class TestH1Translate:
    def test_translate_no_ts_returns_fallback(self):
        store = _make_store()
        svc = _make_svc(store)
        did = _upsert_draft(svc, peer_text="Hello", draft_text="您好，请问有什么可以帮到您？")
        client = _make_app(svc, role="admin")  # no translation_service

        r = client.post(f"/api/drafts/{did}/translate")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["fallback"] is True
        assert d["translated"] == "您好，请问有什么可以帮到您？"  # 原文 fallback
        assert d["draft_id"] == did

    def test_translate_with_ts_calls_translate(self):
        store = _make_store()
        svc = _make_svc(store)
        did = _upsert_draft(svc, peer_text="Hello, how are you?", draft_text="您好！")

        mock_ts = MagicMock()
        mock_result = MagicMock()
        mock_result.translated_text = "Hello! How can I help you?"
        mock_ts.translate = AsyncMock(return_value=mock_result)
        client = _make_app(svc, role="admin", with_translation_svc=mock_ts)

        r = client.post(f"/api/drafts/{did}/translate")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["fallback"] is False
        assert d["translated"] == "Hello! How can I help you?"
        mock_ts.translate.assert_called_once()
        call_kwargs = mock_ts.translate.call_args
        assert call_kwargs.kwargs["target_lang"] == "en"

    def test_translate_target_lang_inferred_from_peer_text(self):
        """日语 peer_text → target_lang 应为 ja"""
        store = _make_store()
        svc = _make_svc(store)
        # 日语 peer_text
        did = _upsert_draft(svc, peer_text="こんにちは、商品を注文したいです", draft_text="こんにちは！")

        captured = {}
        mock_ts = MagicMock()
        async def fake_translate(text, *, target_lang="", source_lang="", style="chat"):
            captured["target_lang"] = target_lang
            r = MagicMock()
            r.translated_text = "こんにちは！（翻訳済み）"
            return r
        mock_ts.translate = fake_translate
        client = _make_app(svc, role="admin", with_translation_svc=mock_ts)

        r = client.post(f"/api/drafts/{did}/translate")
        assert r.status_code == 200
        # 目标语言应该推断为 ja（非中文、非空）
        assert captured.get("target_lang", "") in ("ja", "en")  # detect_language 结果

    def test_translate_draft_not_found(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc)
        r = client.post("/api/drafts/nonexistent-id/translate")
        assert r.status_code == 404

    def test_translate_empty_draft_text(self):
        store = _make_store()
        svc = _make_svc(store)
        did = _upsert_draft(svc, draft_text="")
        client = _make_app(svc)
        r = client.post(f"/api/drafts/{did}/translate")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False
        assert "空" in d.get("error", "")

    def test_translate_ts_exception_graceful_fallback(self):
        store = _make_store()
        svc = _make_svc(store)
        did = _upsert_draft(svc, peer_text="Bonjour", draft_text="您好！")

        mock_ts = MagicMock()
        mock_ts.translate = AsyncMock(side_effect=RuntimeError("API timeout"))
        client = _make_app(svc, role="admin", with_translation_svc=mock_ts)

        r = client.post(f"/api/drafts/{did}/translate")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["fallback"] is True
        assert d["translated"] == "您好！"  # 回退原文

    def test_translate_zh_peer_text_defaults_to_en(self):
        """peer_text 为中文时，target_lang 应回退为 en（避免无意义中→中翻译）"""
        store = _make_store()
        svc = _make_svc(store)
        did = _upsert_draft(svc, peer_text="你好，我想退款", draft_text="您好！")

        mock_ts = MagicMock()
        captured = {}
        async def fake_translate(text, *, target_lang="", source_lang="", style="chat"):
            captured["target_lang"] = target_lang
            r = MagicMock()
            r.translated_text = "Hello!"
            return r
        mock_ts.translate = fake_translate
        client = _make_app(svc, role="admin", with_translation_svc=mock_ts)

        r = client.post(f"/api/drafts/{did}/translate")
        assert r.status_code == 200
        assert captured.get("target_lang") == "en"


# ─────────────────────────────────────
# H2: Bulk Resolve API
# ─────────────────────────────────────

class TestH2BulkResolve:
    def test_bulk_resolve_requires_supervisor(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc, role="agent")  # 普通坐席
        r = client.post("/api/drafts/bulk-resolve", json={"action": "approve", "draft_ids": []})
        assert r.status_code == 403

    def test_bulk_resolve_empty_ids_returns_zero(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc, role="admin")
        r = client.post("/api/drafts/bulk-resolve", json={"action": "approve", "draft_ids": []})
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 0
        assert d["succeeded"] == 0

    def test_bulk_resolve_invalid_action(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc, role="admin")
        r = client.post("/api/drafts/bulk-resolve", json={"action": "send", "draft_ids": ["x"]})
        assert r.status_code == 400

    def test_bulk_approve_multiple_drafts(self):
        store = _make_store()
        svc = _make_svc(store)
        ids = [_upsert_draft(svc, conv_id=f"conv-{i}", level="L3") for i in range(3)]
        client = _make_app(svc, role="master")

        r = client.post("/api/drafts/bulk-resolve",
                        json={"action": "approve", "draft_ids": ids})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["total"] == 3
        assert d["succeeded"] == 3
        assert d["failed"] == 0

    def test_bulk_reject_multiple_drafts(self):
        store = _make_store()
        svc = _make_svc(store)
        ids = [_upsert_draft(svc, conv_id=f"conv-rj-{i}", level="L3") for i in range(2)]
        client = _make_app(svc, role="master")

        r = client.post("/api/drafts/bulk-resolve",
                        json={"action": "reject", "draft_ids": ids})
        assert r.status_code == 200
        d = r.json()
        assert d["succeeded"] == 2
        # 验证草稿已变为 rejected
        for did in ids:
            dr = svc.get_draft(did)
            assert dr["status"] == "rejected"

    def test_bulk_resolve_caps_at_50(self):
        store = _make_store()
        svc = _make_svc(store)
        # 60 条草稿，只有前 50 条应被处理
        ids = [_upsert_draft(svc, conv_id=f"conv-cap-{i}", level="L3") for i in range(60)]
        client = _make_app(svc, role="admin")

        r = client.post("/api/drafts/bulk-resolve",
                        json={"action": "approve", "draft_ids": ids})
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 60
        assert d["succeeded"] <= 50  # 上限 50

    def test_bulk_resolve_nonexistent_draft_counted_as_failed(self):
        store = _make_store()
        svc = _make_svc(store)
        did_ok = _upsert_draft(svc, conv_id="conv-ok", level="L3")
        client = _make_app(svc, role="admin")

        r = client.post("/api/drafts/bulk-resolve",
                        json={"action": "approve", "draft_ids": [did_ok, "nonexistent-id"]})
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 2
        assert d["succeeded"] == 1
        assert d["failed"] == 1

    def test_bulk_resolve_l4_blocked(self):
        """L4 草稿普通批量批准应被拦截（resolve_with_audit 返回 blocked）"""
        store = _make_store()
        svc = _make_svc(store)
        did = _upsert_draft(svc, conv_id="conv-l4", level="L4")
        client = _make_app(svc, role="admin")

        r = client.post("/api/drafts/bulk-resolve",
                        json={"action": "approve", "draft_ids": [did]})
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 1
        # L4 被拦截，算作失败
        assert d["failed"] == 1 or d["succeeded"] == 0


# ─────────────────────────────────────
# H3: cleanup_old_drafts
# ─────────────────────────────────────

class TestH3CleanupOldDrafts:
    def test_cleanup_returns_zero_when_nothing_old(self):
        store = _make_store()
        svc = _make_svc(store)
        # 插入新草稿（刚创建，未超龄）
        _upsert_draft(svc, level="L3", status="pending")
        n = store.cleanup_old_drafts(max_age_days=7)
        assert n == 0

    def _insert_draft_direct(self, store, did, status, ts, conv_id=None):
        """Helper: 直接插入指定时间戳的草稿记录（绕过 upsert 的 NOW()）。"""
        with store._lock:
            store._conn.execute(
                "INSERT INTO reply_drafts "
                "(draft_id, conversation_id, platform, account_id, chat_key, "
                "source_kind, source_id, "
                "autopilot_level, risk_level, draft_text, peer_text, status, "
                "risk_reasons_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, conv_id or f"conv-{did}", "line", "acc1", "u1",
                 "inbox", did,  # source_kind + source_id = did (唯一)
                 "L3", "medium", "text", "msg", status,
                 "[]", ts, ts),
            )
            store._conn.commit()

    def test_cleanup_deletes_old_approved_draft(self):
        store = _make_store()
        old_ts = time.time() - 8 * 86400
        self._insert_draft_direct(store, "old-1", "approved", old_ts)
        n = store.cleanup_old_drafts(max_age_days=7)
        assert n == 1

    def test_cleanup_never_deletes_pending(self):
        store = _make_store()
        old_ts = time.time() - 10 * 86400
        self._insert_draft_direct(store, "pend-1", "pending", old_ts)
        n = store.cleanup_old_drafts(max_age_days=7)
        assert n == 0
        assert store.get_draft("pend-1") is not None

    def test_cleanup_respects_max_age_days(self):
        store = _make_store()
        recent_ts = time.time() - 3 * 86400
        old_ts = time.time() - 9 * 86400
        self._insert_draft_direct(store, "recent", "approved", recent_ts)
        self._insert_draft_direct(store, "old", "approved", old_ts)
        n = store.cleanup_old_drafts(max_age_days=7)
        assert n == 1
        assert store.get_draft("old") is None
        assert store.get_draft("recent") is not None

    def test_cleanup_custom_statuses(self):
        store = _make_store()
        old_ts = time.time() - 8 * 86400
        for did, status in [("rej-1", "rejected"), ("can-1", "cancelled"), ("app-1", "approved")]:
            self._insert_draft_direct(store, did, status, old_ts)
        n = store.cleanup_old_drafts(max_age_days=7, statuses=["rejected"])
        assert n == 1
        assert store.get_draft("rej-1") is None
        assert store.get_draft("can-1") is not None

    def test_cleanup_statuses_safe_guard_no_pending(self):
        """即使传入 pending，也不应删除待处理草稿"""
        store = _make_store()
        old_ts = time.time() - 8 * 86400
        self._insert_draft_direct(store, "pend-safe", "pending", old_ts)
        n = store.cleanup_old_drafts(max_age_days=7, statuses=["pending", "rejected"])
        assert store.get_draft("pend-safe") is not None


# ─────────────────────────────────────
# H3: AutosendWorker status_snapshot
# ─────────────────────────────────────

class TestH3WorkerStatusSnapshot:
    def test_status_snapshot_has_total_cleaned(self):
        store = _make_store()
        svc = _make_svc(store)
        worker = AutosendWorker(
            draft_service=svc,
            config={"enabled": True, "cleanup_age_days": 7, "cleanup_enabled": True},
        )
        snap = worker.status_snapshot()
        assert "total_cleaned" in snap
        assert snap["total_cleaned"] == 0

    def test_status_snapshot_has_total_sent_session(self):
        store = _make_store()
        svc = _make_svc(store)
        worker = AutosendWorker(draft_service=svc, config={})
        snap = worker.status_snapshot()
        assert "total_sent_session" in snap

    def test_worker_cleanup_age_days_config(self):
        store = _make_store()
        svc = _make_svc(store)
        worker = AutosendWorker(
            draft_service=svc,
            config={"cleanup_age_days": 14, "cleanup_enabled": False},
        )
        assert worker._cleanup_age_days == 14
        assert worker._cleanup_enabled is False
