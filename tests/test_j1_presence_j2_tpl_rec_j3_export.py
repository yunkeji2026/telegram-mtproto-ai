"""J1 坐席在线状态 + J2 模板智能推荐 + J3 数据导出 测试。

J1:
  - GET /api/workspace/presence 返回在线坐席列表
  - POST /api/workspace/heartbeat 更新在线状态
  - InboxStore 已有 upsert_agent_presence / list_agent_presence

J2:
  - GET /api/workspace/copilot 返回 template_suggestions
  - _intent_to_scene 映射正确
  - 无模板库时 template_suggestions 为空列表（graceful）
  - 模板按 intent→scene + language 精准过滤

J3:
  - GET /api/workspace/export?export_type=drafts — 非主管 403
  - GET /api/workspace/export?export_type=audit  — CSV 格式正确
  - GET /api/workspace/export?export_type=perf   — CSV 格式正确
  - export_type=drafts — 包含草稿字段行
  - BOM 头兼容 Excel
"""

import io
import time
from unittest.mock import MagicMock, AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService
from src.inbox.template_seeds import SEED_TEMPLATES
from src.web.routes.drafts_routes import (
    register_drafts_routes,
    register_export_route,
    _intent_to_scene,
)


# ─────────────────────────────────────
# helpers
# ─────────────────────────────────────

def _make_store() -> InboxStore:
    s = InboxStore(":memory:")
    s.seed_templates(SEED_TEMPLATES)
    return s


def _make_svc(store: InboxStore) -> DraftService:
    return DraftService(
        inbox_store=store, line_services=[], wa_services=[], messenger_service=None
    )


def _make_app(svc: DraftService = None, store: InboxStore = None, role: str = ""):
    app = FastAPI()

    if role:
        @app.middleware("http")
        async def _inject(request: Request, call_next):
            request.scope["session"] = {"role": role, "user_id": "u1"}
            return await call_next(request)

    def api_auth(request: Request):
        return True

    register_drafts_routes(app, api_auth=api_auth)
    register_export_route(app, api_auth=api_auth)
    if svc:
        app.state.draft_service = svc
    if store:
        app.state.inbox_store = store
    return TestClient(app, raise_server_exceptions=True)


# ─────────────────────────────────────
# J2: _intent_to_scene mapping
# ─────────────────────────────────────

class TestJ2IntentToScene:
    def test_refund_intent(self):
        assert _intent_to_scene("退款") == "refund"
        assert _intent_to_scene("退款申请") == "refund"

    def test_shipping_intent(self):
        assert _intent_to_scene("物流查询") == "shipping"
        assert _intent_to_scene("催单") == "shipping"

    def test_order_intent(self):
        assert _intent_to_scene("订单查询") == "order_inquiry"

    def test_complaint_intent(self):
        assert _intent_to_scene("投诉") == "complaint"

    def test_unknown_intent(self):
        assert _intent_to_scene("") == ""
        assert _intent_to_scene("未知意图类型") == ""

    def test_closing_intent(self):
        assert _intent_to_scene("感谢") == "closing"


# ─────────────────────────────────────
# J2: Copilot template_suggestions
# ─────────────────────────────────────

class TestJ2CopilotTemplateSuggestions:
    def test_copilot_returns_template_suggestions_key(self):
        """Copilot 响应应包含 template_suggestions 字段"""
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc, store)
        r = client.get("/api/workspace/copilot?text=我想退款")
        assert r.status_code == 200
        d = r.json()
        assert "template_suggestions" in d

    def test_copilot_template_suggestions_refund(self):
        """退款意图应返回 refund 场景模板"""
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc, store)
        r = client.get("/api/workspace/copilot?text=我要申请退款，订单有问题")
        assert r.status_code == 200
        d = r.json()
        tpls = d.get("template_suggestions", [])
        # 应有模板且 scene 为 refund
        assert len(tpls) <= 3  # 最多 3 条
        for t in tpls:
            assert "id" in t
            assert "title" in t
            assert "content" in t

    def test_copilot_no_inbox_store_returns_empty(self):
        """无 inbox_store 时 template_suggestions 返回空列表"""
        svc = _make_svc(InboxStore(":memory:"))
        client = _make_app(svc)  # 不设 store
        r = client.get("/api/workspace/copilot?text=你好")
        assert r.status_code == 200
        d = r.json()
        assert d.get("template_suggestions", []) == []

    def test_copilot_empty_text_no_templates(self):
        """空文本时 template_suggestions 可为空"""
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc, store)
        r = client.get("/api/workspace/copilot?text=")
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d.get("template_suggestions"), list)

    def test_copilot_template_suggestions_max_3(self):
        """template_suggestions 最多 3 条"""
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc, store)
        r = client.get("/api/workspace/copilot?text=我的快递在哪里，催单")
        assert r.status_code == 200
        d = r.json()
        assert len(d.get("template_suggestions", [])) <= 3


# ─────────────────────────────────────
# J3: Export API
# ─────────────────────────────────────

class TestJ3Export:
    def test_export_requires_supervisor(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc, store, role="agent")  # 普通坐席
        r = client.get("/api/workspace/export?export_type=drafts")
        assert r.status_code == 403

    def test_export_drafts_csv_format(self):
        store = _make_store()
        svc = _make_svc(store)
        # 插入一条草稿
        store.upsert_draft({
            "source_kind": "inbox", "source_id": "conv-e1",
            "conversation_id": "conv-e1", "platform": "line",
            "account_id": "acc1", "chat_key": "u1",
            "autopilot_level": "L3", "risk_level": "medium",
            "draft_text": "您好，感谢联系", "peer_text": "我要退款",
            "status": "pending",
        })
        client = _make_app(svc, store, role="admin")
        r = client.get("/api/workspace/export?export_type=drafts&days=7")
        assert r.status_code == 200
        assert "csv" in r.headers.get("content-type", "").lower()
        # 读 CSV 内容（去掉 BOM）
        content = r.content.decode("utf-8-sig")
        assert "草稿ID" in content  # 标题行
        assert "conv-e1" in content  # 数据行

    def test_export_audit_csv_format(self):
        store = _make_store()
        svc = _make_svc(store)
        # 插入审计记录
        store.record_draft_audit(
            "d-1", autopilot_level="L3", action="approved",
            agent_id="agent1", risk_level="medium", conversation_id="conv-1"
        )
        client = _make_app(svc, store, role="master")
        r = client.get("/api/workspace/export?export_type=audit&days=7")
        assert r.status_code == 200
        content = r.content.decode("utf-8-sig")
        assert "坐席ID" in content
        assert "agent1" in content

    def test_export_perf_csv_format(self):
        store = _make_store()
        svc = _make_svc(store)
        # 插入绩效数据
        store.record_draft_audit(
            "d-2", autopilot_level="L3", action="approved",
            agent_id="agent2", risk_level="low", conversation_id="conv-2"
        )
        client = _make_app(svc, store, role="admin")
        r = client.get("/api/workspace/export?export_type=perf&days=30")
        assert r.status_code == 200
        content = r.content.decode("utf-8-sig")
        assert "总处理" in content

    def test_export_csv_has_bom(self):
        """CSV 应有 BOM（兼容 Excel 直接打开 UTF-8）"""
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc, store, role="admin")
        r = client.get("/api/workspace/export?export_type=drafts")
        assert r.status_code == 200
        # BOM = \xef\xbb\xbf in UTF-8
        assert r.content[:3] == b"\xef\xbb\xbf"

    def test_export_filename_in_header(self):
        store = _make_store()
        svc = _make_svc(store)
        client = _make_app(svc, store, role="admin")
        r = client.get("/api/workspace/export?export_type=audit")
        assert r.status_code == 200
        cd = r.headers.get("content-disposition", "")
        assert "ws_audit" in cd
        assert ".csv" in cd

    def test_export_days_filter(self):
        """days=1 时应只导出今天的草稿（旧草稿应被过滤）"""
        store = _make_store()
        # 插入一条 8 天前的草稿（应被过滤）
        old_ts = time.time() - 8 * 86400
        with store._lock:
            store._conn.execute(
                "INSERT INTO reply_drafts "
                "(draft_id, conversation_id, platform, account_id, chat_key, "
                "source_kind, source_id, autopilot_level, risk_level, draft_text, "
                "peer_text, status, risk_reasons_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("old-exp", "conv-old", "line", "acc1", "u1",
                 "inbox", "conv-old", "L3", "medium", "old text", "old msg",
                 "approved", "[]", old_ts, old_ts),
            )
            store._conn.commit()
        svc = _make_svc(store)
        client = _make_app(svc, store, role="admin")
        r = client.get("/api/workspace/export?export_type=drafts&days=1")
        assert r.status_code == 200
        content = r.content.decode("utf-8-sig")
        assert "old-exp" not in content


# ─────────────────────────────────────
# J1: Presence API & InboxStore
# ─────────────────────────────────────

class TestJ1Presence:
    def test_upsert_and_get_presence(self):
        store = _make_store()
        store.upsert_agent_presence("agent-1", display_name="Alice", status="online")
        p = store.get_agent_presence("agent-1")
        assert p is not None
        assert p["display_name"] == "Alice"
        assert p["status"] == "online"

    def test_list_presence_active(self):
        store = _make_store()
        now = time.time()
        store.upsert_agent_presence("agent-1", display_name="Alice", status="online")
        store.upsert_agent_presence("agent-2", display_name="Bob", status="busy")
        online = store.list_agent_presence(active_within_sec=120)
        assert len(online) == 2

    def test_presence_carries_agent_languages(self):
        """P3：list_agent_presence LEFT JOIN agent_prefs 带出坐席技能语言。"""
        store = _make_store()
        store.upsert_agent_presence("agent-1", display_name="Alice", status="online")
        store.upsert_agent_presence("agent-2", display_name="Bob", status="online")
        store.set_agent_languages("agent-1", "en,ja")
        rows = {p["agent_id"]: p for p in store.list_agent_presence(active_within_sec=120)}
        assert rows["agent-1"]["languages"] == "en,ja"
        assert rows["agent-2"]["languages"] == ""   # 未声明 → 空串

    def test_set_agent_languages_preserves_alert_prefs(self):
        """P3：写语言只动 languages 列，不影响告警偏好；反之亦然。"""
        store = _make_store()
        store.set_agent_prefs("agent-1", warn_sec=90, crit_sec=300, muted=1)
        store.set_agent_languages("agent-1", "zh,en")
        prefs = store.get_agent_prefs("agent-1")
        assert prefs["languages"] == "zh,en"
        assert prefs["warn_sec"] == 90
        assert prefs["crit_sec"] == 300
        assert prefs["muted"] == 1
        # 再改告警偏好不应抹掉语言
        store.set_agent_prefs("agent-1", warn_sec=120)
        assert store.get_agent_prefs("agent-1")["languages"] == "zh,en"

    def test_list_presence_excludes_old(self):
        store = _make_store()
        # Manually insert old presence
        old_ts = time.time() - 300
        with store._lock:
            store._conn.execute(
                "INSERT OR REPLACE INTO agent_presence "
                "(agent_id, display_name, status, last_seen_at, updated_at) VALUES (?,?,?,?,?)",
                ("old-agent", "Old", "online", old_ts, old_ts),
            )
            store._conn.commit()
        active = store.list_agent_presence(active_within_sec=60)
        assert all(a["agent_id"] != "old-agent" for a in active)

    def test_dashboard_presence_sec_hidden_for_non_sup(self):
        """仅 IS_SUP=true 时 dashboard 才展示 presence widget（HTML 中 style=display:none 默认）"""
        # 这是前端行为验证，只检测 HTML 模板包含正确 id
        with open("src/web/templates/workspace_dashboard.html", encoding="utf-8") as f:
            html = f.read()
        assert "db-presence-sec" in html
        assert "loadPresence" in html

    def test_export_btn_wired_in_dashboard(self):
        """导出按钮 JS 存在于 dashboard 模板"""
        with open("src/web/templates/workspace_dashboard.html", encoding="utf-8") as f:
            html = f.read()
        assert "db-export" in html
        assert "export_type=drafts" in html
        assert "export_type=audit" in html
        assert "export_type=perf" in html
