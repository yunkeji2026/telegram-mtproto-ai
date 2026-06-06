"""I1 对话智能元数据 + I3 回复模板库 测试。

I1:
  - InboxStore.update_conv_meta / get_conv_meta
  - 情绪趋势计算 (rising / falling / stable)
  - rolling window 上限
  - ingest.ingest_collected_chats 触发 update_conv_meta
  - GET /api/unified-inbox/conv-meta API

I3:
  - InboxStore.seed_templates 幂等
  - list_templates 过滤（语言/场景/搜索）
  - create_template / update_template / delete_template (软删除)
  - increment_template_usage
  - GET/POST/PUT/DELETE /api/templates
  - POST /api/templates/{id}/use
  - GET /workspace/templates 页面
"""

import pytest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteClient

from src.inbox.store import InboxStore
from src.inbox.template_seeds import SEED_TEMPLATES
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


# ─────────────────────────────────────
# helpers
# ─────────────────────────────────────

def _make_store() -> InboxStore:
    return InboxStore(":memory:")


def _make_app(store: InboxStore):
    app = FastAPI()

    def auth(req: Request):
        return True

    register_unified_inbox_routes(
        app,
        api_auth=auth,
        page_auth=auth,
        templates=MagicMock(),
        config_manager=MagicMock(),
    )
    app.state.inbox_store = store
    return TestClient(app, raise_server_exceptions=True)


# ─────────────────────────────────────
# I1: InboxStore.conversation_meta
# ─────────────────────────────────────

class TestI1ConvMeta:
    def test_update_and_get(self):
        store = _make_store()
        store.update_conv_meta("conv-1", platform="line", intent="退款", emotion="不满", risk="medium")
        meta = store.get_conv_meta("conv-1")
        assert meta is not None
        assert meta["last_intent"] == "退款"
        assert meta["last_emotion"] == "不满"
        assert meta["last_risk"] == "medium"
        assert meta["msg_count"] == 1

    def test_get_nonexistent_returns_none(self):
        store = _make_store()
        assert store.get_conv_meta("no-such-conv") is None

    def test_update_empty_id_is_noop(self):
        store = _make_store()
        store.update_conv_meta("")  # should not raise
        assert store.get_conv_meta("") is None

    def test_rolling_intent_history(self):
        store = _make_store()
        for i in range(15):
            store.update_conv_meta("conv-roll", intent=f"intent-{i}", emotion="平稳")
        meta = store.get_conv_meta("conv-roll")
        ih = meta["intent_history"]
        # 默认 max_history=10，窗口不超过 10
        assert len(ih) <= 10
        # 最新的 intent 在末尾
        assert ih[-1] == "intent-14"

    def test_emotion_history_accumulates(self):
        store = _make_store()
        emotions = ["平稳", "平稳", "不满", "愤怒"]
        for e in emotions:
            store.update_conv_meta("conv-emo", emotion=e)
        meta = store.get_conv_meta("conv-emo")
        assert meta["emotion_history"] == emotions

    def test_msg_count_increments(self):
        store = _make_store()
        for _ in range(5):
            store.update_conv_meta("conv-cnt", emotion="平稳")
        meta = store.get_conv_meta("conv-cnt")
        assert meta["msg_count"] == 5

    def test_emotion_trend_rising(self):
        """愤怒 → 不满 → 催促：情绪恶化 → rising"""
        store = _make_store()
        for e in ["感谢", "感谢", "平稳", "不满", "愤怒"]:
            store.update_conv_meta("conv-r", emotion=e)
        meta = store.get_conv_meta("conv-r")
        assert meta["emotion_trend"] == "rising"

    def test_emotion_trend_falling(self):
        """愤怒 → 不满 → 平稳 → 感谢：情绪好转 → falling"""
        store = _make_store()
        for e in ["愤怒", "不满", "平稳", "满意", "感谢"]:
            store.update_conv_meta("conv-f", emotion=e)
        meta = store.get_conv_meta("conv-f")
        assert meta["emotion_trend"] == "falling"

    def test_emotion_trend_stable_single_message(self):
        store = _make_store()
        store.update_conv_meta("conv-s", emotion="平稳")
        meta = store.get_conv_meta("conv-s")
        assert meta["emotion_trend"] == "stable"


# ─────────────────────────────────────
# I1: ingest → auto conv_meta
# ─────────────────────────────────────

class TestI1IngestTrigger:
    def test_ingest_updates_conv_meta_on_inbound(self):
        """ingest_collected_chats 入站消息应自动写 conv_meta"""
        from src.inbox.ingest import ingest_collected_chats
        store = _make_store()
        chats = [{
            "conversation_id": "conv-ingest-1",
            "platform": "line",
            "account_id": "acc1",
            "chat_key": "user1",
            "display_name": "测试用户",
            "last_message": {"text": "我想退款，非常生气！", "direction": "in"},
        }]
        with patch("src.ai.chat_assistant_service.quick_analyze") as mock_qa:
            mock_qa.return_value = {"intent": "退款", "emotion": "愤怒", "risk_level": "high"}
            ingest_collected_chats(store, chats)
        # quick_analyze 应已被调用（ingest 内部 import 来自 src.ai.chat_assistant_service）
        mock_qa.assert_called_once()

    def test_ingest_skips_conv_meta_for_outbound(self):
        """出站消息不应触发 conv_meta 更新"""
        from src.inbox.ingest import ingest_collected_chats
        store = _make_store()
        chats = [{
            "conversation_id": "conv-out",
            "platform": "line",
            "account_id": "acc1",
            "chat_key": "user1",
            "last_message": {"text": "您好！", "direction": "out"},
        }]
        with patch("src.ai.chat_assistant_service.quick_analyze") as mock_qa:
            ingest_collected_chats(store, chats)
        mock_qa.assert_not_called()


# ─────────────────────────────────────
# I1: API
# ─────────────────────────────────────

class TestI1ConvMetaAPI:
    def test_get_conv_meta_not_found(self):
        store = _make_store()
        client = _make_app(store)
        r = client.get("/api/unified-inbox/conv-meta?conversation_id=nonexistent")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["found"] is False

    def test_get_conv_meta_found(self):
        store = _make_store()
        store.update_conv_meta("conv-api-1", platform="line", intent="订单查询", emotion="平稳", risk="low")
        client = _make_app(store)
        r = client.get("/api/unified-inbox/conv-meta?conversation_id=conv-api-1")
        assert r.status_code == 200
        d = r.json()
        assert d["found"] is True
        meta = d["meta"]
        assert meta["last_intent"] == "订单查询"
        assert meta["last_emotion"] == "平稳"
        assert "emotion_trend" in meta

    def test_get_conv_meta_missing_id_returns_400(self):
        store = _make_store()
        client = _make_app(store)
        r = client.get("/api/unified-inbox/conv-meta")
        assert r.status_code == 400


# ─────────────────────────────────────
# I3: InboxStore template methods
# ─────────────────────────────────────

class TestI3TemplateStore:
    def test_seed_templates_idempotent(self):
        store = _make_store()
        n1 = store.seed_templates(SEED_TEMPLATES)
        n2 = store.seed_templates(SEED_TEMPLATES)
        assert n1 == len(SEED_TEMPLATES)
        assert n2 == 0  # 幂等：第二次全跳过

    def test_list_all_templates(self):
        store = _make_store()
        store.seed_templates(SEED_TEMPLATES)
        templates = store.list_templates()
        assert len(templates) == len(SEED_TEMPLATES)

    def test_list_filter_by_language(self):
        store = _make_store()
        store.seed_templates(SEED_TEMPLATES)
        zh_templates = store.list_templates(language="zh")
        assert all(t["language"] == "zh" for t in zh_templates)
        assert len(zh_templates) > 0

    def test_list_filter_by_scene(self):
        store = _make_store()
        store.seed_templates(SEED_TEMPLATES)
        refund = store.list_templates(scene="refund")
        assert all(t["scene"] == "refund" for t in refund)
        assert len(refund) >= 2

    def test_list_search(self):
        store = _make_store()
        store.seed_templates(SEED_TEMPLATES)
        results = store.list_templates(search="退款")
        assert len(results) > 0
        # 所有结果标题或内容含"退款"
        for r in results:
            assert "退款" in r["title"] or "退款" in r["content"]

    def test_create_template(self):
        store = _make_store()
        tid = store.create_template(title="测试模板", content="这是测试内容", language="zh", scene="greeting")
        t = store.list_templates()
        assert any(x["id"] == tid for x in t)

    def test_update_template(self):
        store = _make_store()
        tid = store.create_template(title="旧标题", content="旧内容", language="zh")
        ok = store.update_template(tid, title="新标题", content="新内容")
        assert ok is True
        templates = store.list_templates()
        t = next((x for x in templates if x["id"] == tid), None)
        assert t is not None
        assert t["title"] == "新标题"

    def test_delete_template_soft(self):
        store = _make_store()
        tid = store.create_template(title="待删", content="内容", language="zh")
        ok = store.delete_template(tid)
        assert ok is True
        # 软删除：active_only=True 应看不到
        templates = store.list_templates(active_only=True)
        assert all(t["id"] != tid for t in templates)
        # active_only=False 可以看到
        templates_all = store.list_templates(active_only=False)
        t = next((x for x in templates_all if x["id"] == tid), None)
        assert t is not None
        assert t["is_active"] == 0

    def test_increment_usage(self):
        store = _make_store()
        tid = store.create_template(title="T", content="C", language="zh")
        store.increment_template_usage(tid)
        store.increment_template_usage(tid)
        templates = store.list_templates(active_only=False)
        t = next((x for x in templates if x["id"] == tid), None)
        assert t is not None
        assert t["used_count"] == 2

    def test_update_nonexistent_returns_false(self):
        store = _make_store()
        ok = store.update_template("nonexistent-id", title="X")
        assert ok is False


# ─────────────────────────────────────
# I3: Templates API
# ─────────────────────────────────────

class TestI3TemplateAPI:
    def _client_with_store(self, store=None):
        s = store or _make_store()
        s.seed_templates(SEED_TEMPLATES)
        return _make_app(s), s

    def test_list_templates_returns_all(self):
        client, _ = self._client_with_store()
        r = client.get("/api/reply-templates")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["count"] == len(SEED_TEMPLATES)

    def test_list_templates_filter_language(self):
        client, _ = self._client_with_store()
        r = client.get("/api/reply-templates?language=en")
        assert r.status_code == 200
        d = r.json()
        assert all(t["language"] == "en" for t in d["templates"])

    def test_list_templates_filter_scene(self):
        client, _ = self._client_with_store()
        r = client.get("/api/reply-templates?scene=greeting")
        assert r.status_code == 200
        d = r.json()
        assert all(t["scene"] == "greeting" for t in d["templates"])
        assert d["count"] >= 3  # zh/en/ja 三种开场白

    def test_create_template_api(self):
        client, store = self._client_with_store()
        r = client.post("/api/reply-templates", json={
            "title": "API创建模板",
            "content": "您好，这是通过API创建的模板",
            "language": "zh",
            "scene": "greeting",
        })
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "id" in d

    def test_create_template_missing_fields(self):
        client, _ = self._client_with_store()
        r = client.post("/api/reply-templates", json={"title": "只有标题"})
        assert r.status_code == 400

    def test_update_template_api(self):
        client, store = self._client_with_store()
        tid = store.create_template(title="Before", content="旧内容", language="zh")
        r = client.put(f"/api/reply-templates/{tid}", json={"title": "After", "content": "新内容"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True

    def test_update_template_not_found(self):
        client, _ = self._client_with_store()
        r = client.put("/api/reply-templates/nonexistent", json={"title": "X"})
        assert r.status_code == 404

    def test_delete_template_api_no_supervisor(self):
        """无主管权限时删除应返回 403"""
        client, store = self._client_with_store()
        tid = store.create_template(title="T", content="C", language="zh")
        r = client.delete(f"/api/reply-templates/{tid}")
        assert r.status_code == 403

    def test_use_template_increments_count(self):
        client, store = self._client_with_store()
        tid = store.create_template(title="T", content="C", language="zh")
        r = client.post(f"/api/reply-templates/{tid}/use")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # 验证 used_count 已增加
        templates = store.list_templates(active_only=False)
        t = next((x for x in templates if x["id"] == tid), None)
        assert t is not None
        assert t["used_count"] == 1

    def test_list_templates_search(self):
        client, _ = self._client_with_store()
        r = client.get("/api/reply-templates?search=退款")
        assert r.status_code == 200
        d = r.json()
        assert d["count"] > 0


# ─────────────────────────────────────
# I3: admin inventory check
# ─────────────────────────────────────

class TestI3AdminRoute:
    def test_templates_page_in_inventory(self):
        """确认 /workspace/templates 已在管理路由基线中。"""
        from tests.test_admin_route_inventory import _BASELINE
        assert "/workspace/templates\tGET" in _BASELINE
        assert "/api/reply-templates\tGET" in _BASELINE
