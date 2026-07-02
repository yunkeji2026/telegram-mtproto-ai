"""E1 对话上下文 + E2 自动草稿生成 + E3 系统健康 Widget 测试。

覆盖：
  E1 对话历史面板（前端变更通过路由/API 层验证）
    - /api/unified-inbox/history 返回正确结构（conv_id 在 draft 响应中）
    - draft 响应包含 conversation_id 字段

  E2 自动草稿生成
    - InboxStore.register_new_inbound_cb 注册成功
    - ingest_collected_chats 新消息触发 _new_inbound_cbs
    - ingest_collected_chats 非入站消息不触发
    - ingest_collected_chats 空消息不触发
    - DraftService.auto_generate_draft 单元：生成草稿 + 返回 draft_id
    - auto_generate_draft 幂等：已有 pending → 跳过
    - auto_generate_draft 超短文本（如 Hi）→ 正常生成（min_text_len 默认 0）
    - 纯媒体入站（贴纸等）→ 占位符触发 auto-draft
    - auto_generate_draft 无 store → 返回 None
    - auto_generate_draft risk→autopilot 映射正确（low→L2, high→L4）
    - auto_generate_draft 异常路径安全静默

  E3 系统健康 Widget（API 层）
    - GET /api/drafts/autosend-status 可用（200 + ok）
    - GET /api/drafts/risk-summary 返回 sla_overdue 字段
    - 两端点均在 openapi schema 中注册
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.ingest import ingest_collected_chats
from src.inbox.drafts import DraftService
from src.web.routes.drafts_routes import register_drafts_routes


# ──────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────

def _api_auth(request: Request) -> None:
    return None


def _make_app(store=None, role: str = "admin"):
    from src.inbox.drafts import DraftService
    from src.ai.chat_assistant_service import quick_risk
    app = FastAPI()
    if role:
        @app.middleware("http")
        async def _inj(request: Request, call_next):
            request.scope["session"] = {"role": role, "user_id": "u1", "username": "u1"}
            return await call_next(request)
    register_drafts_routes(app, api_auth=_api_auth)
    if store is not None:
        svc = DraftService(inbox_store=store, risk_fn=quick_risk)
        app.state.inbox_store = store
        app.state.draft_service = svc
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def tmp_store(tmp_path):
    s = InboxStore(tmp_path / "test.db")
    yield s
    s.close()


# ──────────────────────────────────────────────────────
# E1 draft API 包含 conversation_id
# ──────────────────────────────────────────────────────

class TestE1DraftConvId:
    """E1 基础验证：draft API 响应包含 conversation_id，供前端发起历史查询。"""

    def test_draft_api_includes_conversation_id(self, tmp_store):
        """upsert_draft + list_drafts 返回 conversation_id 字段。"""
        tmp_store.upsert_draft({
            "source_kind": "inbox",
            "conversation_id": "tg:default:user123",
            "platform": "telegram",
            "account_id": "default",
            "chat_key": "user123",
            "peer_text": "你好",
            "draft_text": "你好，有什么可以帮您？",
            "risk_level": "low",
            "autopilot_level": "L2",
            "status": "pending",
        })
        drafts = tmp_store.list_drafts(status="pending")
        assert drafts, "应有草稿"
        d = drafts[0]
        assert "conversation_id" in d
        assert d["conversation_id"] == "tg:default:user123"

    def test_list_drafts_filter_by_conversation_id(self, tmp_store):
        """list_drafts 支持 conversation_id 过滤（E2 幂等保护需要）。"""
        cid = "wa:default:abc"
        tmp_store.upsert_draft({
            "source_kind": "inbox",
            "conversation_id": cid,
            "platform": "whatsapp",
            "peer_text": "hello",
            "draft_text": "Hi there",
            "risk_level": "low",
            "autopilot_level": "L2",
            "status": "pending",
        })
        # 另一条不同会话
        tmp_store.upsert_draft({
            "source_kind": "inbox",
            "conversation_id": "wa:default:other",
            "platform": "whatsapp",
            "peer_text": "other",
            "draft_text": "other reply",
            "risk_level": "low",
            "autopilot_level": "L1",
            "status": "pending",
        })
        filtered = tmp_store.list_drafts(conversation_id=cid, limit=10)
        assert len(filtered) == 1
        assert filtered[0]["conversation_id"] == cid


# ──────────────────────────────────────────────────────
# E2 InboxStore 回调注册
# ──────────────────────────────────────────────────────

class TestE2StoreCallback:
    def test_register_new_inbound_cb_appends(self, tmp_store):
        """register_new_inbound_cb 将回调加入列表。"""
        cb = MagicMock()
        tmp_store.register_new_inbound_cb(cb)
        assert cb in tmp_store._new_inbound_cbs

    def test_multiple_cbs_registered(self, tmp_store):
        """多个回调均被保存。"""
        cb1, cb2 = MagicMock(), MagicMock()
        tmp_store.register_new_inbound_cb(cb1)
        tmp_store.register_new_inbound_cb(cb2)
        assert len(tmp_store._new_inbound_cbs) == 2


# ──────────────────────────────────────────────────────
# E2 ingest → 回调触发
# ──────────────────────────────────────────────────────

class TestE2IngestCallback:
    def _make_chat(self, *, direction="in", text="你好"):
        return {
            "conversation_id": "tg:default:u1",
            "platform": "telegram",
            "account_id": "default",
            "chat_key": "u1",
            "name": "测试用户",
            "last_msg": text,
            "last_ts": time.time(),
            "unread": 1,
            "language": "zh",
            "last_message": {
                "text": text,
                "direction": direction,
                "ts": time.time(),
            },
        }

    def test_inbound_msg_triggers_cb(self, tmp_store):
        """新入站消息触发 _new_inbound_cbs。"""
        cb = MagicMock()
        tmp_store.register_new_inbound_cb(cb)
        ingest_collected_chats(tmp_store, [self._make_chat(direction="in", text="你好")],
                               publish_events=False)
        cb.assert_called_once()
        # 验证参数：第一个是 conv dict，第二个是消息文本
        args = cb.call_args[0]
        assert args[1] == "你好"
        assert args[0]["conversation_id"] == "tg:default:u1"

    def test_outbound_msg_no_cb(self, tmp_store):
        """出站消息不触发回调。"""
        cb = MagicMock()
        tmp_store.register_new_inbound_cb(cb)
        ingest_collected_chats(tmp_store, [self._make_chat(direction="out", text="我的回复")],
                               publish_events=False)
        cb.assert_not_called()

    def test_empty_text_no_cb(self, tmp_store):
        """纯空文本且无媒体时不触发回调。"""
        cb = MagicMock()
        tmp_store.register_new_inbound_cb(cb)
        ingest_collected_chats(tmp_store, [self._make_chat(direction="in", text="")],
                               publish_events=False)
        cb.assert_not_called()

    def test_media_only_triggers_cb_with_placeholder(self, tmp_store):
        """无正文但有媒体的入站消息用占位符触发 auto-draft 回调。"""
        cb = MagicMock()
        tmp_store.register_new_inbound_cb(cb)
        chat = self._make_chat(direction="in", text="")
        chat["last_message"]["media_type"] = "sticker"
        ingest_collected_chats(tmp_store, [chat], publish_events=False)
        cb.assert_called_once()
        assert cb.call_args[0][1] == "[贴纸]"

    def test_repeat_ingest_no_second_cb(self, tmp_store):
        """同一消息第二次 ingest 不重复触发（幂等去重）。"""
        cb = MagicMock()
        tmp_store.register_new_inbound_cb(cb)
        chat = self._make_chat(direction="in", text="重复")
        ingest_collected_chats(tmp_store, [chat], publish_events=False)
        ingest_collected_chats(tmp_store, [chat], publish_events=False)
        # 第二次 ingest_batch 应返回 n=0 → 不触发
        assert cb.call_count == 1

    def test_cb_exception_does_not_break_ingest(self, tmp_store):
        """回调抛异常时 ingest 正常完成（best-effort）。"""
        def bad_cb(conv, text):
            raise RuntimeError("callback failure")
        tmp_store.register_new_inbound_cb(bad_cb)
        n = ingest_collected_chats(tmp_store, [self._make_chat(direction="in", text="测试")],
                                   publish_events=False)
        assert n >= 1  # ingest 正常完成


# ──────────────────────────────────────────────────────
# 源头止血：群/频道会话识别（auto-draft skip 用）
# ──────────────────────────────────────────────────────

class TestIsGroupConversation:
    def test_private_telegram_positive_peer(self):
        from src.inbox.ingest import is_group_conversation
        conv = {"platform": "telegram", "conversation_id": "telegram:acc:8142915241",
                "chat_key": "8142915241"}
        assert is_group_conversation(conv) is False

    def test_telegram_supergroup_negative_peer(self):
        from src.inbox.ingest import is_group_conversation
        conv = {"platform": "telegram",
                "conversation_id": "telegram:8244899900:-1001560025690",
                "chat_key": "-1001560025690"}
        assert is_group_conversation(conv) is True

    def test_telegram_legacy_group_negative_peer(self):
        from src.inbox.ingest import is_group_conversation
        conv = {"platform": "telegram", "conversation_id": "telegram:acc:-4812",
                "chat_key": "-4812"}
        assert is_group_conversation(conv) is True

    def test_chat_type_group_wins_even_without_negative_id(self):
        from src.inbox.ingest import is_group_conversation
        conv = {"platform": "line", "conversation_id": "line:acc:room1",
                "chat_key": "room1", "chat_type": "group"}
        assert is_group_conversation(conv) is True

    def test_non_telegram_private_not_group(self):
        from src.inbox.ingest import is_group_conversation
        conv = {"platform": "whatsapp", "conversation_id": "whatsapp:acc:u9",
                "chat_key": "u9", "chat_type": "private"}
        assert is_group_conversation(conv) is False

    def test_missing_fields_defaults_false(self):
        from src.inbox.ingest import is_group_conversation
        assert is_group_conversation({}) is False


# ──────────────────────────────────────────────────────
# E2 DraftService.auto_generate_draft
# ──────────────────────────────────────────────────────

class TestE2AutoGenerateDraft:
    def _make_svc(self, store):
        from src.ai.chat_assistant_service import quick_risk
        return DraftService(inbox_store=store, risk_fn=quick_risk)

    def test_generate_low_risk_returns_draft_id(self, tmp_store):
        """低风险消息 → 返回 draft_id，autopilot_level=L2。"""
        svc = self._make_svc(tmp_store)
        conv = {"conversation_id": "tg:default:u1", "platform": "telegram",
                "account_id": "default", "chat_key": "u1", "display_name": "Test"}
        draft_id = svc.auto_generate_draft(conv, "你好，请问有什么可以帮我的吗？",
                                           automation_mode="auto_ai")
        assert draft_id is not None
        draft = tmp_store.get_draft(draft_id)
        assert draft is not None
        assert draft["autopilot_level"] == "L2"
        assert draft["peer_text"] == "你好，请问有什么可以帮我的吗？"
        assert draft["draft_text"]  # 有生成的回复文本

    def test_generate_high_risk_sets_l4(self, tmp_store):
        """高风险消息（含敏感词）→ autopilot_level=L4。"""
        svc = self._make_svc(tmp_store)
        conv = {"conversation_id": "tg:default:risky", "platform": "telegram",
                "account_id": "default", "chat_key": "risky", "display_name": "R"}
        draft_id = svc.auto_generate_draft(conv, "请把密码和银行卡号发给我",
                                           automation_mode="auto_ai")
        assert draft_id is not None
        draft = tmp_store.get_draft(draft_id)
        assert draft["autopilot_level"] == "L4"
        assert draft["risk_level"] == "high"

    def test_short_text_generates_draft(self, tmp_store):
        """极短文本（如 Hi）也生成草稿（陪伴场景默认 min_text_len=0）。"""
        svc = self._make_svc(tmp_store)
        conv = {"conversation_id": "tg:default:s1", "platform": "telegram",
                "account_id": "default", "chat_key": "s1", "display_name": "S"}
        result = svc.auto_generate_draft(conv, "hi")
        assert result is not None

    def test_empty_text_skipped(self, tmp_store):
        """空文本不生成草稿。"""
        svc = self._make_svc(tmp_store)
        conv = {"conversation_id": "tg:default:e1", "platform": "telegram",
                "account_id": "default", "chat_key": "e1", "display_name": "E"}
        result = svc.auto_generate_draft(conv, "")
        assert result is None

    def test_no_store_returns_none(self):
        """无 store 时安全返回 None。"""
        svc = DraftService(inbox_store=None)
        conv = {"conversation_id": "tg:default:x", "platform": "telegram",
                "account_id": "default", "chat_key": "x"}
        result = svc.auto_generate_draft(conv, "你好")
        assert result is None

    def test_idempotent_skips_existing_pending(self, tmp_store):
        """同一会话已有 pending 草稿且 peer_text 相同时跳过（幂等保护）。"""
        svc = self._make_svc(tmp_store)
        conv = {"conversation_id": "tg:default:idem", "platform": "telegram",
                "account_id": "default", "chat_key": "idem", "display_name": "I"}
        tmp_store.upsert_draft({
            "source_kind": "inbox",
            "conversation_id": "tg:default:idem",
            "platform": "telegram",
            "peer_text": "已有消息",
            "draft_text": "已有回复",
            "risk_level": "low",
            "autopilot_level": "L1",
            "status": "pending",
        })
        result = svc.auto_generate_draft(conv, "已有消息")
        assert result is None  # 同 peer_text → 跳过

    def test_stale_pending_cancelled_on_new_peer_text(self, tmp_store):
        """peer_text 变化时作废陈旧 pending 草稿并重新生成。"""
        svc = self._make_svc(tmp_store)
        conv = {"conversation_id": "tg:default:stale", "platform": "telegram",
                "account_id": "default", "chat_key": "stale", "display_name": "S"}
        old_id = tmp_store.upsert_draft({
            "source_kind": "inbox",
            "conversation_id": "tg:default:stale",
            "platform": "telegram",
            "peer_text": "Hello",
            "draft_text": "旧回复",
            "risk_level": "low",
            "autopilot_level": "L1",
            "status": "pending",
        })
        result = svc.auto_generate_draft(conv, "新消息来了")
        assert result is not None
        assert result != old_id
        old = tmp_store.get_draft(old_id)
        assert old["status"] == "cancelled"

    def test_medium_risk_sets_l3(self, tmp_store):
        """中等风险消息 → autopilot_level=L3（review 模式下）。"""
        svc = self._make_svc(tmp_store)
        conv = {"conversation_id": "tg:default:med", "platform": "telegram",
                "account_id": "default", "chat_key": "med", "display_name": "M"}
        draft_id = svc.auto_generate_draft(conv, "我要投诉你们的服务，非常不满意！",
                                           automation_mode="review")
        assert draft_id is not None
        draft = tmp_store.get_draft(draft_id)
        assert draft["autopilot_level"] in ("L3", "L4")  # medium 或 high → L3/L4

    def test_generates_nonempty_draft_text(self, tmp_store):
        """生成的 draft_text 非空（取自规则建议）。"""
        svc = self._make_svc(tmp_store)
        conv = {"conversation_id": "tg:default:dt1", "platform": "telegram",
                "account_id": "default", "chat_key": "dt1"}
        did = svc.auto_generate_draft(conv, "请问你们几点营业？", automation_mode="auto_ai")
        assert did is not None
        draft = tmp_store.get_draft(did)
        assert len(draft["draft_text"]) > 0


# ──────────────────────────────────────────────────────
# E2 端到端：ingest → auto_generate_draft 全链路
# ──────────────────────────────────────────────────────

class TestE2EndToEnd:
    def test_ingest_triggers_auto_draft(self, tmp_store):
        """真实注册 auto_generate_draft 作为回调，ingest 后草稿自动出现。"""
        from src.ai.chat_assistant_service import quick_risk
        svc = DraftService(inbox_store=tmp_store, risk_fn=quick_risk)
        # 注册回调
        tmp_store.register_new_inbound_cb(svc.auto_generate_draft)

        chat = {
            "conversation_id": "wa:default:e2e",
            "platform": "whatsapp",
            "account_id": "default",
            "chat_key": "e2e",
            "name": "E2E User",
            "last_msg": "你好，我想了解产品",
            "last_ts": time.time(),
            "unread": 1,
            "language": "zh",
            "last_message": {
                "text": "你好，我想了解产品",
                "direction": "in",
                "ts": time.time(),
            },
        }
        n = ingest_collected_chats(tmp_store, [chat], publish_events=False)
        assert n >= 1

        # 草稿应已自动生成
        drafts = tmp_store.list_drafts(
            source_kind="inbox",
            conversation_id="wa:default:e2e",
            status="pending",
        )
        assert len(drafts) == 1
        d = drafts[0]
        assert d["peer_text"] == "你好，我想了解产品"
        assert d["draft_text"]


# ──────────────────────────────────────────────────────
# E3 API 健康端点
# ──────────────────────────────────────────────────────

class TestE3HealthAPIs:
    """E3：验证 /api/drafts/autosend-status 和 /api/drafts/risk-summary 均可用。"""

    def _client(self, tmp_store, role="admin"):
        return _make_app(tmp_store, role=role)

    def test_autosend_status_200(self, tmp_store):
        """GET /api/drafts/autosend-status 返回 200 + ok 字段。"""
        c = self._client(tmp_store)
        r = c.get("/api/drafts/autosend-status")
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True

    def test_autosend_status_has_worker_field(self, tmp_store):
        """autosend-status 包含 worker 字段（即使 worker 未启动时为 None）。"""
        c = self._client(tmp_store)
        d = c.get("/api/drafts/autosend-status").json()
        assert "worker" in d  # None when not started, dict when started

    def test_risk_summary_has_sla_overdue(self, tmp_store):
        """risk-summary（主管）包含 sla_overdue 字段。"""
        c = self._client(tmp_store, role="admin")
        r = c.get("/api/drafts/risk-summary")
        assert r.status_code == 200
        d = r.json()
        assert "sla_overdue" in d

    def test_risk_summary_sla_overdue_nonzero_when_overdue(self, tmp_store):
        """创建超时 L3 草稿，risk-summary 的 sla_overdue 应 > 0。"""
        old_ts = time.time() - 5 * 3600  # 5h 前
        # 手动直接写入旧时间戳草稿（绕过 upsert_draft 的 now）
        import sqlite3
        conn = tmp_store._conn
        with tmp_store._lock:
            conn.execute(
                """INSERT INTO reply_drafts
                    (draft_id, conversation_id, platform, account_id, chat_key,
                     source_kind, source_id, peer_text, draft_text, final_text,
                     draft_lang, translated_preview, risk_level, risk_reasons_json,
                     autopilot_level, status, decided_by, decided_at, sent_at, error,
                     created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("old:L3:001", "tg:default:x1", "telegram", "default", "x1",
                 "inbox", "old001", "客户问题", "回复", "",
                 "zh", "", "medium", "[]",
                 "L3", "pending", "", 0, 0, "",
                 old_ts, old_ts),
            )
            conn.commit()
        c = self._client(tmp_store, role="admin")
        d = c.get("/api/drafts/risk-summary").json()
        assert d.get("sla_overdue", 0) >= 1

    def test_health_endpoints_in_openapi(self, tmp_store):
        """autosend-status 和 risk-summary 均在 OpenAPI schema 中。"""
        c = self._client(tmp_store)
        paths = c.get("/openapi.json").json().get("paths", {})
        assert "/api/drafts/autosend-status" in paths
        assert "/api/drafts/risk-summary" in paths


class TestExpireStaleEndpoint:
    """POST /api/drafts/expire-stale — 一键清理陈旧群积压（主管专属）。"""

    def _insert_group_draft(self, store, did, peer, ago_days):
        ts = time.time() - ago_days * 86400
        with store._lock:
            store._conn.execute(
                "INSERT OR REPLACE INTO reply_drafts "
                "(draft_id,conversation_id,platform,account_id,chat_key,source_kind,"
                "source_id,autopilot_level,risk_level,draft_text,peer_text,status,"
                "risk_reasons_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, "telegram:acc:" + peer, "telegram", "acc", peer, "inbox",
                 "telegram:acc:" + peer, "L4", "high", "x", "y", "pending", "[]", ts, ts),
            )
            store._conn.commit()

    def test_non_supervisor_forbidden(self, tmp_store):
        c = _make_app(tmp_store, role="agent")
        r = c.post("/api/drafts/expire-stale", json={"dry_run": True})
        assert r.status_code == 403

    def test_dry_run_previews_without_mutation(self, tmp_store):
        self._insert_group_draft(tmp_store, "d-g1", "-1001560025690", 10)
        c = _make_app(tmp_store, role="admin")
        r = c.post("/api/drafts/expire-stale", json={"dry_run": True})
        assert r.status_code == 200
        d = r.json()
        assert d["dry_run"] is True and d["count"] == 1
        # 未变更
        assert any(x["draft_id"] == "d-g1" for x in tmp_store.list_drafts(status="pending"))

    def test_real_run_groups_only_spares_private(self, tmp_store):
        self._insert_group_draft(tmp_store, "d-g2", "-1002088370555", 10)
        self._insert_group_draft(tmp_store, "d-dm2", "8142915241", 10)  # 正 peer = 私聊
        c = _make_app(tmp_store, role="admin")
        r = c.post("/api/drafts/expire-stale",
                   json={"groups_only": True, "dry_run": False})
        assert r.status_code == 200
        d = r.json()
        ids = [x["draft_id"] for x in d["drafts"]]
        assert "d-g2" in ids and "d-dm2" not in ids
        pending_ids = [x["draft_id"] for x in tmp_store.list_drafts(status="pending")]
        assert "d-g2" not in pending_ids and "d-dm2" in pending_ids
