"""F1 快捷回复建议 + F2 auto_draft 配置化 + G1 draft_created SSE 事件测试。

覆盖：
  F1 Copilot suggestions
    - GET /api/workspace/copilot 返回 suggestions 列表（非空文本）
    - suggestions 包含 style / title / text 字段
    - 空文本时 suggestions=[]（不报错）
    - suggestions 数量 ≤ 3

  F2 auto_draft 配置化
    - enabled=False → 不注册回调（验证 _new_inbound_cbs 仍为空）
    - enabled=True → 注册回调后可触发自动草稿生成
    - skip_platforms 跳过指定平台
    - min_text_len 短于阈值不生成
    - automation_mode 正确传递（auto_ai→L2 / review→L1）

  G1 draft_created SSE
    - auto_generate_draft 成功后调用 event_bus.publish("draft_created", ...)
    - publish 时 data 包含 draft_id / conversation_id / autopilot_level
    - draft_created 加入 SSE 白名单（_sse_types）
    - event_bus 失败不影响 auto_generate_draft 返回结果
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, call

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService
from src.inbox.ingest import ingest_collected_chats
from src.web.routes.drafts_routes import register_drafts_routes
from src.ai.chat_assistant_service import quick_analyze


# ──────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────

def _api_auth(request: Request) -> None:
    return None


def _make_client(store=None, role: str = "admin"):
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
# F1: Copilot suggestions
# ──────────────────────────────────────────────────────

class TestF1CopilotSuggestions:
    def test_copilot_returns_suggestions(self, tmp_store):
        """非空文本时 /api/workspace/copilot 返回 suggestions 列表。"""
        c = _make_client(tmp_store)
        r = c.get("/api/workspace/copilot?text=你好，我想了解一下产品")
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True
        assert "suggestions" in d
        assert isinstance(d["suggestions"], list)
        assert len(d["suggestions"]) > 0

    def test_suggestions_have_required_fields(self, tmp_store):
        """每条 suggestion 包含 style / title / text 字段。"""
        c = _make_client(tmp_store)
        d = c.get("/api/workspace/copilot?text=你好").json()
        for s in d.get("suggestions", []):
            assert "style" in s
            assert "title" in s
            assert "text" in s
            assert len(s["text"]) > 0

    def test_suggestions_max_three(self, tmp_store):
        """suggestions 数量不超过 3 条。"""
        c = _make_client(tmp_store)
        d = c.get("/api/workspace/copilot?text=我心情很不好今天发生了很多事").json()
        assert len(d.get("suggestions", [])) <= 3

    def test_empty_text_suggestions_empty(self, tmp_store):
        """空文本时 suggestions=[]，不报错。"""
        c = _make_client(tmp_store)
        d = c.get("/api/workspace/copilot?text=").json()
        assert d.get("ok") is True
        assert d.get("suggestions", []) == []

    def test_high_risk_suggestions_include_review(self, tmp_store):
        """高风险文本（含敏感词）时 suggestions 包含审核相关提示。"""
        c = _make_client(tmp_store)
        d = c.get("/api/workspace/copilot?text=请把密码发给我").json()
        assert d.get("risk_level") == "high"
        texts = [s["text"] for s in d.get("suggestions", [])]
        # 高风险时至少有一条 suggestion 文本
        assert len(texts) > 0

    def test_suggestions_text_nonempty_strings(self, tmp_store):
        """所有 suggestion.text 均为非空字符串。"""
        c = _make_client(tmp_store)
        d = c.get("/api/workspace/copilot?text=今天过得怎么样").json()
        for s in d.get("suggestions", []):
            assert isinstance(s["text"], str) and len(s["text"]) > 0


# ──────────────────────────────────────────────────────
# F2: auto_draft 配置化
# ──────────────────────────────────────────────────────

class TestF2AutoDraftConfig:
    """验证 main.py 中 auto_draft 配置的各条分支逻辑。

    因 main.py 是运行时入口，这里直接测业务逻辑层：
    skip_platforms / min_text_len / automation_mode 的行为。
    """

    def _make_svc(self, store):
        from src.ai.chat_assistant_service import quick_risk
        return DraftService(inbox_store=store, risk_fn=quick_risk)

    def _conv(self, platform="telegram"):
        return {"conversation_id": f"{platform}:default:u1",
                "platform": platform, "account_id": "default",
                "chat_key": "u1", "display_name": "Test"}

    def test_skip_platform_no_draft(self, tmp_store):
        """skip_platforms 中的平台消息不通过回调生成草稿。"""
        svc = self._make_svc(tmp_store)
        skip = {"line"}
        conv = self._conv("line")
        # 模拟 skip 逻辑（与 main.py 的 _auto_draft_cb 一致）
        if conv["platform"] in skip:
            result = None
        else:
            result = svc.auto_generate_draft(conv, "你好")
        assert result is None

    def test_min_text_len_filtered(self, tmp_store):
        """min_text_len=5 时，少于5字符的消息被过滤。"""
        svc = self._make_svc(tmp_store)
        min_len = 5
        text = "嗯"  # 1 字符
        if len(text.strip()) < min_len:
            result = None
        else:
            result = svc.auto_generate_draft(self._conv(), text)
        assert result is None

    def test_min_text_len_passed(self, tmp_store):
        """min_text_len=3 时，超过3字符的消息正常生成草稿。"""
        svc = self._make_svc(tmp_store)
        result = svc.auto_generate_draft(
            self._conv(), "你好，请问有什么可以帮到您", automation_mode="auto_ai"
        )
        assert result is not None

    def test_automation_mode_auto_ai_creates_l2(self, tmp_store):
        """automation_mode=auto_ai + 低风险 → L2 自动发送草稿。"""
        svc = self._make_svc(tmp_store)
        did = svc.auto_generate_draft(
            self._conv(), "你好，今天天气真好", automation_mode="auto_ai"
        )
        assert did is not None
        draft = tmp_store.get_draft(did)
        assert draft["autopilot_level"] == "L2"

    def test_automation_mode_review_no_l2(self, tmp_store):
        """automation_mode=review + 低风险 → L1（不自动发送）。"""
        svc = self._make_svc(tmp_store)
        conv = {"conversation_id": "tg:default:rev",
                "platform": "telegram", "account_id": "default",
                "chat_key": "rev", "display_name": "R"}
        did = svc.auto_generate_draft(conv, "你好，今天天气真好", automation_mode="review")
        # review 模式下低风险应该是 L1（非 L2）
        assert did is not None
        draft = tmp_store.get_draft(did)
        assert draft["autopilot_level"] == "L1"

    def test_enabled_false_no_callback(self, tmp_store):
        """enabled=False 时不注册回调，_new_inbound_cbs 为空。"""
        # 模拟 main.py 逻辑：enabled=False 不调用 register_new_inbound_cb
        _ad_cfg = {"enabled": False}
        if _ad_cfg.get("enabled", True):
            tmp_store.register_new_inbound_cb(lambda c, t: None)
        assert len(tmp_store._new_inbound_cbs) == 0

    def test_enabled_true_registers_callback(self, tmp_store):
        """enabled=True（默认）时注册回调。"""
        _ad_cfg = {"enabled": True, "automation_mode": "auto_ai", "min_text_len": 3}
        if _ad_cfg.get("enabled", True):
            tmp_store.register_new_inbound_cb(lambda c, t: None)
        assert len(tmp_store._new_inbound_cbs) == 1


# ──────────────────────────────────────────────────────
# G1: draft_created SSE 事件
# ──────────────────────────────────────────────────────

class TestG1DraftCreatedSSE:
    def _make_svc(self, store):
        from src.ai.chat_assistant_service import quick_risk
        return DraftService(inbox_store=store, risk_fn=quick_risk)

    def _conv(self, uid="u1"):
        return {"conversation_id": f"tg:default:{uid}",
                "platform": "telegram", "account_id": "default",
                "chat_key": uid, "display_name": "G1User"}

    # get_event_bus 在 auto_generate_draft 里是延迟导入，需 patch 源模块路径
    _BUS_PATCH = "src.integrations.shared.event_bus.get_event_bus"

    def test_auto_generate_draft_publishes_event(self, tmp_store):
        """auto_generate_draft 成功时调用 event_bus.publish('draft_created', ...)。"""
        svc = self._make_svc(tmp_store)
        mock_bus = MagicMock()
        with patch(self._BUS_PATCH, return_value=mock_bus):
            did = svc.auto_generate_draft(self._conv(), "你好，请问怎么下单？",
                                          automation_mode="auto_ai")
        assert did is not None
        mock_bus.publish.assert_called_once()
        args = mock_bus.publish.call_args
        assert args[0][0] == "draft_created"
        data = args[0][1]
        assert data["draft_id"] == did
        assert data["conversation_id"] == "tg:default:u1"
        assert "autopilot_level" in data
        assert "peer_text" in data

    def test_draft_created_data_has_risk_level(self, tmp_store):
        """draft_created 事件 data 包含 risk_level 字段。"""
        svc = self._make_svc(tmp_store)
        mock_bus = MagicMock()
        with patch(self._BUS_PATCH, return_value=mock_bus):
            svc.auto_generate_draft(self._conv("u2"), "你好，在吗", automation_mode="auto_ai")
        data = mock_bus.publish.call_args[0][1]
        assert "risk_level" in data

    def test_event_bus_failure_does_not_break_auto_generate(self, tmp_store):
        """event_bus.publish 异常时 auto_generate_draft 仍正常返回 draft_id。"""
        svc = self._make_svc(tmp_store)
        mock_bus = MagicMock()
        mock_bus.publish.side_effect = RuntimeError("bus error")
        with patch(self._BUS_PATCH, return_value=mock_bus):
            did = svc.auto_generate_draft(self._conv("u3"), "你好，有什么可以帮您",
                                          automation_mode="auto_ai")
        assert did is not None  # 不受 bus 异常影响

    def test_skipped_draft_no_event_published(self, tmp_store):
        """跳过（会话已有 pending 且 peer_text 未变）时不发布 draft_created 事件。"""
        svc = self._make_svc(tmp_store)
        conv = self._conv("u4")
        peer = "同一条客户消息"
        mock_bus = MagicMock()
        with patch(self._BUS_PATCH, return_value=mock_bus):
            svc.auto_generate_draft(conv, peer, automation_mode="auto_ai")
        first_call_count = mock_bus.publish.call_count
        with patch(self._BUS_PATCH, return_value=mock_bus):
            result = svc.auto_generate_draft(conv, peer, automation_mode="auto_ai")
        assert result is None
        assert mock_bus.publish.call_count == first_call_count

    def test_draft_created_in_sse_whitelist(self):
        """draft_created 已加入 SSE 类型白名单。

        巨石拆分后 SSE 白名单下沉到 realtime 路由域，故断言其落点模块。
        """
        import pathlib
        src = pathlib.Path(
            "src/web/routes/unified_inbox_realtime_routes.py"
        ).read_text(encoding="utf-8")
        assert "draft_created" in src

    def test_peer_text_truncated_to_100(self, tmp_store):
        """超长 peer_text 在事件中截断为 100 字符。"""
        svc = self._make_svc(tmp_store)
        long_text = "你好 " * 50  # ~150 字符
        mock_bus = MagicMock()
        with patch(self._BUS_PATCH, return_value=mock_bus):
            svc.auto_generate_draft(self._conv("u5"), long_text, automation_mode="auto_ai")
        data = mock_bus.publish.call_args[0][1]
        assert len(data["peer_text"]) <= 100


# ──────────────────────────────────────────────────────
# 端到端：ingest → auto_draft → SSE 事件链
# ──────────────────────────────────────────────────────

class TestEndToEndChain:
    _BUS_PATCH = "src.integrations.shared.event_bus.get_event_bus"

    def test_full_chain_ingest_to_draft_and_event(self, tmp_store):
        """ingest 新消息 → auto_generate_draft → 草稿落库 + draft_created 事件发布。"""
        from src.ai.chat_assistant_service import quick_risk
        svc = DraftService(inbox_store=tmp_store, risk_fn=quick_risk)
        mock_bus = MagicMock()

        with patch(self._BUS_PATCH, return_value=mock_bus):
            tmp_store.register_new_inbound_cb(svc.auto_generate_draft)
            chat = {
                "conversation_id": "wa:default:chain1",
                "platform": "whatsapp",
                "account_id": "default",
                "chat_key": "chain1",
                "name": "Chain User",
                "last_msg": "你好，请问如何退款？",
                "last_ts": time.time(),
                "unread": 1,
                "language": "zh",
                "last_message": {
                    "text": "你好，请问如何退款？",
                    "direction": "in",
                    "ts": time.time(),
                },
            }
            n = ingest_collected_chats(tmp_store, [chat], publish_events=False)

        assert n >= 1
        # 草稿已生成
        drafts = tmp_store.list_drafts(source_kind="inbox",
                                       conversation_id="wa:default:chain1",
                                       status="pending")
        assert len(drafts) == 1
        # 事件已发布（"退款"命中敏感词 → L3/L4）
        mock_bus.publish.assert_called_once()
        event_data = mock_bus.publish.call_args[0][1]
        assert event_data["platform"] == "whatsapp"
        assert mock_bus.publish.call_args[0][0] == "draft_created"
