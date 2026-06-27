from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pathlib import Path

from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


class LineSvc:
    account_id = "line-a"
    _merged_cfg = {"label": "LINE-A"}

    def list_chats(self, limit):
        return [{
            "chat_key": "line-room",
            "name": "Line User",
            "last_peer_text": "こんにちは",
            "last_ts": 100,
            "unread_count": 2,
        }]

    def status(self):
        return {"running": True, "serial": "line-serial"}

    async def send_to_chat(self, chat_key, text):
        return {"chat_key": chat_key, "text": text}


class WhatsAppSvc:
    account_id = "wa-a"
    _merged_cfg = {"label": "WA-A"}

    def list_pending(self, status="pending", limit=20):
        return [{
            "chat_key": "wa-room",
            "peer_name": "WA User",
            "peer_text": "hello friend",
            "ts": 110,
        }]

    def status(self):
        return {"running": True, "serial": "wa-serial"}

    async def send_to_chat(self, chat_key, text):
        return {"chat_key": chat_key, "text": text}


class MessengerSvc:
    is_running = True

    def list_approvals(self, status="pending", limit=20):
        return [{
            "account_id": "ms-a",
            "chat_key": "ms-room",
            "name": "Messenger User",
            "peer_text": "hola, gracias",
            "ts": 120,
        }]

    async def send_to_chat_name(self, chat_name, text):
        return {"chat_name": chat_name, "text": text}


class TelegramClient:
    running = True
    _recent_messages = [
        {"chat_id": "tg-room", "user_name": "TG User", "text": "你好", "ts": 130},
        {"chat_id": "tg-room", "user_name": "TG User", "text": "hello again", "ts": 131},
    ]

    async def send_message(self, chat_key, text):
        return {"chat_key": chat_key, "text": text}


class FakeAI:
    async def chat(self, prompt, context=None):
        return "你好朋友"


def _client():
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app,
        page_auth=page_auth,
        api_auth=api_auth,
        templates=_Templates(),
    )
    app.state.line_rpa_services = [LineSvc()]
    app.state.whatsapp_rpa_services = [WhatsAppSvc()]
    app.state.messenger_rpa_service = MessengerSvc()
    app.state.telegram_client = TelegramClient()
    app.state.ai_client = FakeAI()
    return TestClient(app)


def test_unified_inbox_chats_returns_four_platforms_and_message_shape():
    c = _client()
    resp = c.get("/api/unified-inbox/chats?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    platforms = {row["platform"] for row in data["chats"]}
    assert {"line", "whatsapp", "messenger", "telegram"} <= platforms
    row = data["chats"][0]
    assert "conversation_id" in row
    assert "last_message" in row
    assert "language" in row["last_message"]
    assert row["can_send"] is True
    assert "multi_choice" in row["send_modes"]


def test_unified_inbox_thread_returns_telegram_history():
    c = _client()
    resp = c.get("/api/unified-inbox/thread?platform=telegram&account_id=default&chat_key=tg-room")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert [m["text"] for m in data["messages"]] == ["你好", "hello again"]


def test_unified_inbox_translate_endpoint_uses_service():
    c = _client()
    resp = c.post("/api/unified-inbox/translate", json={"text": "hello friend", "target_lang": "zh"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["translation"]["translated_text"] == "你好朋友"


def test_translate_auto_resolves_via_conversation_language():
    """P1-2：/translate 的 target_lang=auto 由服务端按会话语言解析（与 /send 同源）。"""
    c = _client()
    c.app.state.inbox_store = _LangStore(language="ja")
    resp = c.post(
        "/api/unified-inbox/translate",
        json={"text": "hello", "target_lang": "auto",
              "platform": "line", "account_id": "line-a", "chat_key": "line-room"},
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["resolved_target"] == "ja"
    assert d["translation"]["target_lang"] == "ja"


def test_translate_auto_unknown_language_skips_translation():
    """P1-2：auto 无法解析客户语言 → resolved_target='' 且不翻译，回原文。"""
    c = _client()
    c.app.state.inbox_store = _LangStore(language="unknown")
    resp = c.post(
        "/api/unified-inbox/translate",
        json={"text": "hello", "target_lang": "auto",
              "platform": "line", "account_id": "line-a", "chat_key": "line-room"},
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["resolved_target"] == ""
    assert d["ok"] is False
    assert d["translation"]["translated_text"] == "hello"
    assert d["translation"]["provider"] == "none"


def test_translate_normalizes_noncanonical_target():
    """P1-2：/translate 显式非规范码（zh-cn）归一到 zh。"""
    c = _client()
    resp = c.post(
        "/api/unified-inbox/translate",
        json={"text": "hello", "target_lang": "zh-cn"},
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["resolved_target"] == "zh"
    assert d["translation"]["target_lang"] == "zh"


def test_unified_inbox_analyze_endpoint_returns_suggestions():
    c = _client()
    resp = c.post(
        "/api/unified-inbox/analyze",
        json={"text": "hi", "messages": [{"text": "hi"}], "chat": {"language": "en"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["analysis"]["intent"] == "打招呼"
    assert len(data["analysis"]["suggestions"]) == 3


def test_unified_inbox_profile_endpoint_returns_contact_shape():
    c = _client()
    resp = c.get("/api/unified-inbox/profile?platform=telegram&account_id=default&chat_key=tg-room")
    assert resp.status_code == 200
    data = resp.json()
    profile = data["profile"]
    assert profile["display_name"] == "TG User"
    assert profile["relationship"]["stage"] in {"初识", "升温", "稳定陪伴"}
    assert profile["activity"]["message_count"] == 2
    assert "tags" in profile


def test_unified_inbox_automation_mode_roundtrip():
    c = _client()
    get_resp = c.get("/api/unified-inbox/automation?platform=telegram&account_id=default&chat_key=tg-room")
    assert get_resp.status_code == 200
    assert get_resp.json()["mode"] == "review"

    set_resp = c.post(
        "/api/unified-inbox/automation",
        json={"platform": "telegram", "account_id": "default", "chat_key": "tg-room", "mode": "multi_choice"},
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["mode"] == "multi_choice"

    chats = c.get("/api/unified-inbox/chats?limit=10").json()["chats"]
    tg = next(row for row in chats if row["platform"] == "telegram")
    assert tg["automation_mode"] == "multi_choice"


def test_unified_inbox_automation_stats_returns_conversation_audit(tmp_path):
    """B-1：全自动安全条 API —— 按会话聚合今日 autosend/blocked + 近期审计记录。"""
    from src.inbox.store import InboxStore

    c = _client()
    store = InboxStore(tmp_path / "auto_stats.db")
    c.app.state.inbox_store = store
    cid = "telegram:default:tg-room"
    store.record_draft_audit("d1", action="autosend", conversation_id=cid, autopilot_level="L2")
    store.record_draft_audit("d2", action="blocked", conversation_id=cid, autopilot_level="L4", reason="高风险")
    store.record_draft_audit("d3", action="autosend_failed", conversation_id=cid, autopilot_level="L2", reason="平台投递失败")

    resp = c.get("/api/unified-inbox/automation-stats?platform=telegram&account_id=default&chat_key=tg-room")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["stats"]["autosend"] >= 1
    assert data["stats"]["blocked"] >= 1
    # 投递失败可视化：autosend_failed 计入 stats.failed 且出现在 recent
    assert data["stats"]["failed"] >= 1
    assert any(r.get("action") == "autosend" for r in data["recent"])
    assert any(r.get("action") == "autosend_failed" for r in data["recent"])


def test_inbox_store_conversations_blocked_counts_batch(tmp_path):
    """B-2 风控可视：批量查会话今日 blocked 次数（单次 IN 查询，供列表高亮）。"""
    from src.inbox.store import InboxStore

    store = InboxStore(tmp_path / "inbox.db")
    cid_a = "telegram:default:a"
    cid_b = "telegram:default:b"
    store.record_draft_audit("d1", action="blocked", conversation_id=cid_a, autopilot_level="L4")
    store.record_draft_audit("d2", action="blocked", conversation_id=cid_a, autopilot_level="L3")
    store.record_draft_audit("d3", action="autosend", conversation_id=cid_b, autopilot_level="L2")
    counts = store.conversations_blocked_counts([cid_a, cid_b, "telegram:default:none"], since_ts=0.0)
    assert counts.get(cid_a) == 2
    # 仅 autosend 的会话不计入；无记录会话不出现
    assert cid_b not in counts
    assert "telegram:default:none" not in counts
    # 空入参安全
    assert store.conversations_blocked_counts([], since_ts=0.0) == {}


def test_unified_inbox_send_supports_four_platforms():
    c = _client()
    cases = [
        ("line", "line-a", "line-room"),
        ("whatsapp", "wa-a", "wa-room"),
        ("messenger", "ms-a", "ms-room"),
        ("telegram", "default", "tg-room"),
    ]
    for platform, account_id, chat_key in cases:
        resp = c.post(
            "/api/unified-inbox/send",
            json={"platform": platform, "account_id": account_id, "chat_key": chat_key, "text": "hi"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True


class _LangStore:
    """最小 inbox_store stub：仅实现 outbound 自动翻译需要的 get_conversation。"""

    def __init__(self, language="zh"):
        self._language = language

    def get_conversation(self, cid):
        return {"id": cid, "language": self._language}

    def record_agent_send(self, *a, **k):
        return None


def test_send_without_target_lang_sends_original():
    c = _client()
    resp = c.post(
        "/api/unified-inbox/send",
        json={"platform": "line", "account_id": "line-a", "chat_key": "line-room", "text": "hello"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["sent_text"] == "hello"
    assert data["original_text"] == "hello"
    assert data["translation"] is None


def test_send_with_target_lang_translates_before_send():
    c = _client()
    resp = c.post(
        "/api/unified-inbox/send",
        json={
            "platform": "line", "account_id": "line-a", "chat_key": "line-room",
            "text": "hello friend", "target_lang": "zh",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["original_text"] == "hello friend"
    assert data["sent_text"] == "你好朋友"  # FakeAI 译文
    assert data["translation"]["ok"] is True


def test_send_skip_translate_sends_original():
    c = _client()
    resp = c.post(
        "/api/unified-inbox/send",
        json={
            "platform": "line", "account_id": "line-a", "chat_key": "line-room",
            "text": "hello", "target_lang": "zh", "skip_translate": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sent_text"] == "hello"
    assert data["translation"] is None


def test_send_target_auto_infers_conversation_language():
    c = _client()
    c.app.state.inbox_store = _LangStore(language="zh")
    resp = c.post(
        "/api/unified-inbox/send",
        json={
            "platform": "line", "account_id": "line-a", "chat_key": "line-room",
            "text": "hello friend", "target_lang": "auto",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sent_text"] == "你好朋友"
    assert data["translation"]["target_lang"] == "zh"


def test_send_target_auto_normalizes_noncanonical_conv_language():
    """P0：会话语言为非规范码（zh-cn）时，auto 推断应归一到 zh 并正常翻译。"""
    c = _client()
    c.app.state.inbox_store = _LangStore(language="zh-cn")
    resp = c.post(
        "/api/unified-inbox/send",
        json={
            "platform": "line", "account_id": "line-a", "chat_key": "line-room",
            "text": "hello friend", "target_lang": "auto",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sent_text"] == "你好朋友"
    assert data["translation"]["target_lang"] == "zh"


def test_send_explicit_noncanonical_target_lang_normalized():
    """P0：显式传入非规范 target_lang（zh-cn）也应归一后翻译。"""
    c = _client()
    resp = c.post(
        "/api/unified-inbox/send",
        json={
            "platform": "line", "account_id": "line-a", "chat_key": "line-room",
            "text": "hello friend", "target_lang": "zh-cn",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sent_text"] == "你好朋友"
    assert data["translation"]["target_lang"] == "zh"


def test_outbound_translation_store_roundtrip(tmp_path):
    """P1：旁路表 record/get 往返；译文==原文（未真正翻译）不记录。"""
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:line-a:line-room"
    assert store.record_outbound_translation(
        cid, sent_text="你好朋友", original_text="hello friend",
        target_lang="zh", provider="ai") is True
    assert store.record_outbound_translation(
        cid, sent_text="same", original_text="same") is False
    xmap = store.get_outbound_translations(cid)
    assert len(xmap) == 1
    row = next(iter(xmap.values()))
    assert row["original_text"] == "hello friend"
    assert row["target_lang"] == "zh"
    assert row["provider"] == "ai"


def test_send_records_outbound_translation(tmp_path):
    """P1：一击直发（target_lang 翻译）后，出向译文→原文写入旁路表。"""
    from src.inbox.store import InboxStore
    c = _client()
    store = InboxStore(tmp_path / "inbox.db")
    c.app.state.inbox_store = store
    resp = c.post(
        "/api/unified-inbox/send",
        json={"platform": "line", "account_id": "line-a", "chat_key": "line-room",
              "text": "hello friend", "target_lang": "zh"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sent_text"] == "你好朋友"
    xmap = store.get_outbound_translations("line:line-a:line-room")
    assert any(v["original_text"] == "hello friend" for v in xmap.values())


def test_send_increments_outbound_translation_funnel():
    """P1-4：一击直发翻译后，出向翻译漏斗 translated/coverage/by_lang 计数增加。"""
    from src.ai.outbound_translation_stats import get_outbound_translation_stats
    st = get_outbound_translation_stats()
    st.reset()
    c = _client()
    # 翻译发送（target_lang=zh，FakeAI 译出「你好朋友」）
    r1 = c.post("/api/unified-inbox/send", json={
        "platform": "line", "account_id": "line-a", "chat_key": "line-room",
        "text": "hello friend", "target_lang": "zh"})
    assert r1.status_code == 200, r1.text
    # 原文直发（不请求翻译）
    r2 = c.post("/api/unified-inbox/send", json={
        "platform": "line", "account_id": "line-a", "chat_key": "line-room",
        "text": "hi"})
    assert r2.status_code == 200, r2.text
    d = st.dump()
    assert d["sends_total"] == 2
    assert d["translated"] == 1
    assert d["requested"] == 1
    assert d["by_target_lang"].get("zh") == 1
    st.reset()


def test_cleanup_outbound_translations_removes_aged(tmp_path):
    """P3：旁路表按龄清理——超 30 天删除，近期保留。"""
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:line-a:line-room"
    store.record_outbound_translation(
        cid, sent_text="你好", original_text="hi", target_lang="zh", provider="ai")
    # 手动把 created_at 调到 40 天前，触发默认 30 天清理
    with store._lock:
        store._conn.execute(
            "UPDATE outbound_translations SET created_at = ?",
            (store._now() - 40 * 86400,))
        store._conn.commit()
    assert store.cleanup_outbound_translations() == 1
    assert store.get_outbound_translations(cid) == {}
    # 近期记录保留
    store.record_outbound_translation(
        cid, sent_text="你好2", original_text="hi2", target_lang="zh")
    assert store.cleanup_outbound_translations() == 0
    assert len(store.get_outbound_translations(cid)) == 1


def test_dashboard_exposes_translation_funnel():
    """P3：经理看板端点 /api/workspace/dashboard 透出 translation 漏斗。"""
    from src.ai.outbound_translation_stats import get_outbound_translation_stats
    st = get_outbound_translation_stats()
    st.reset()
    c = _client()
    c.post("/api/unified-inbox/send", json={
        "platform": "line", "account_id": "line-a", "chat_key": "line-room",
        "text": "hello friend", "target_lang": "zh"})
    r = c.get("/api/workspace/dashboard")
    assert r.status_code == 200, r.text
    tr = r.json().get("translation") or {}
    assert tr.get("sends_total", 0) >= 1
    assert tr.get("translated", 0) >= 1
    assert tr.get("by_target_lang", {}).get("zh", 0) >= 1
    st.reset()


def test_prefs_roundtrip_agent_languages(tmp_path):
    """P3：坐席经 /api/workspace/prefs 声明技能语言 → 规范化去重落库 + 回显。"""
    from src.inbox.store import InboxStore
    c = _client()
    store = InboxStore(tmp_path / "inbox.db")
    c.app.state.inbox_store = store
    r = c.post("/api/workspace/prefs", json={"languages": ["EN", "ja", "zh-CN", "ja"]})
    assert r.status_code == 200, r.text
    assert r.json()["prefs"]["languages"] == "en,ja,zh"   # 规范化 + 去重 + 保序
    g = c.get("/api/workspace/prefs")
    assert g.status_code == 200, g.text
    assert g.json()["prefs"]["languages"] == "en,ja,zh"


def test_outbound_xlate_daily_roundtrip(tmp_path):
    """P3：按日漏斗持久化 record/get 往返——totals/coverage/by_lang/trend。"""
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    # 译出（zh）
    store.record_outbound_xlate(requested=True, translated=True, target_lang="zh")
    # 译出（en）+ 降级
    store.record_outbound_xlate(requested=True, translated=True, target_lang="en",
                                degraded=True)
    # 原文直发（未请求翻译）
    store.record_outbound_xlate(requested=False)
    # auto 解析失败
    store.record_outbound_xlate(requested=True, is_auto=True, auto_resolved=False)
    s = store.get_outbound_xlate_stats(0)
    assert s["sends_total"] == 4
    assert s["translated"] == 2
    assert s["skipped"] == 1            # auto 失败那条：请求了但未译
    assert s["degraded"] == 1
    assert s["auto_requested"] == 1
    assert s["auto_unresolved"] == 1
    assert s["coverage_rate"] == round(2 / 4, 4)
    assert s["by_target_lang"] == {"en": 1, "zh": 1}
    assert len(s["trend"]) == 1
    assert s["trend"][0]["sends"] == 4
    assert s["trend"][0]["cov_pct"] == 50.0


def test_dashboard_translation_uses_persistent_window(tmp_path):
    """P3：挂上 inbox store 后，看板 translation 走按日持久化（含 trend）。"""
    from src.inbox.store import InboxStore
    c = _client()
    store = InboxStore(tmp_path / "inbox.db")
    c.app.state.inbox_store = store
    r1 = c.post("/api/unified-inbox/send", json={
        "platform": "line", "account_id": "line-a", "chat_key": "line-room",
        "text": "hello friend", "target_lang": "zh"})
    assert r1.status_code == 200, r1.text
    r = c.get("/api/workspace/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    tr = body.get("translation") or {}
    assert tr.get("sends_total", 0) >= 1
    assert tr.get("translated", 0) >= 1
    assert "trend" in tr
    # 入站漏斗字段随看板一并透出（跨语言总览）
    assert "translation_inbound" in body
    # 直接核对落库
    assert store.get_outbound_xlate_stats(0)["by_target_lang"].get("zh", 0) >= 1


def test_workspace_dashboard_template_contains_translation_panel():
    """P3：看板模板含跨语言翻译面板容器 + 渲染逻辑。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "workspace_dashboard.html"
    html = path.read_text(encoding="utf-8")
    assert "db-xlate" in html
    assert "跨语言翻译" in html
    assert "d.translation" in html
    assert "coverage_rate" in html
    assert "by_target_lang" in html
    # 按日窗 + 覆盖率趋势折线（持久化增强）
    assert "按所选日窗" in html
    assert "cov_pct" in html
    # 跨语言总览：出向 + 入站两段（入站客户来源语言分布）
    assert "跨语言总览" in html
    assert "translation_inbound" in html
    assert "by_source_lang" in html


def test_dashboard_exposes_auto_claim_window(tmp_path):
    """P3：看板端点透出 auto_claim 按日聚合（派单量/命中/语言分布/趋势）。"""
    from src.inbox.store import InboxStore
    c = _client()
    store = InboxStore(tmp_path / "inbox.db")
    store.record_auto_claim(matched=True, lang="ja")
    store.record_auto_claim(matched=False, lang="")
    c.app.state.inbox_store = store
    r = c.get("/api/workspace/dashboard")
    assert r.status_code == 200, r.text
    ac = r.json().get("auto_claim") or {}
    assert ac.get("claimed", 0) == 2
    assert ac.get("lang_matched", 0) == 1
    assert ac.get("by_lang", {}).get("ja", 0) == 1
    assert "trend" in ac


def test_workspace_dashboard_template_contains_auto_claim_panel():
    """P3：看板模板含自动派单面板段（命中率 + 语言分布 + 趋势）。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "workspace_dashboard.html"
    html = path.read_text(encoding="utf-8")
    assert "d.auto_claim" in html
    assert "自动派单（按语言路由）" in html
    assert "按语言命中率" in html
    assert "acs.trend" in html


def test_helpers_slice1_reexport_identity():
    """巨石拆分 slice 1 / slice 39：纯 helper 在 unified_inbox_helpers，orchestrator 不再重导出。"""
    import src.web.routes.unified_inbox_helpers as helpers
    import src.web.routes.unified_inbox_routes as routes
    for name in ("_fmt_ts", "_detect_language", "_detect_risk_signals",
                 "_derive_tiered_replies", "_build_context_summary", "_dnd_active",
                 "_RISK_PATTERNS", "_LANG_TEMPLATES", "_ID_KEYWORDS", "_EN_KEYWORDS"):
        assert hasattr(helpers, name), f"helpers 缺少 {name}"
        assert not hasattr(routes, name), f"orchestrator 仍重导出 {name}"


def test_services_slice2_reexport_identity():
    """巨石拆分 slice 2 / slice 39：服务累加器在 unified_inbox_services，orchestrator 不再重导出。"""
    import src.web.routes.unified_inbox_services as services
    import src.web.routes.unified_inbox_routes as routes
    for name in ("_get_line_services", "_get_whatsapp_services", "_get_messenger_service",
                 "_get_telegram_client", "_get_translation_service",
                 "_get_chat_assistant_service", "_automation_store", "_inbox_store",
                 "_ecommerce_tools", "_contacts_store", "_contacts_gateway"):
        assert hasattr(services, name), f"services 缺少 {name}"
        assert not hasattr(routes, name), f"orchestrator 仍重导出 {name}"


def test_auth_slice3_reexport_identity():
    """巨石拆分 slice 3 / slice 39：身份/权限基座在 unified_inbox_auth，orchestrator 不再重导出。"""
    import src.web.routes.unified_inbox_auth as auth
    import src.web.routes.unified_inbox_routes as routes
    for name in ("_SUPERVISOR_ROLES", "_session_agent", "_is_supervisor",
                 "_require_supervisor", "_publish_follow_up", "_agent_from_request",
                 "_user_store_from_config"):
        assert hasattr(auth, name), f"auth 缺少 {name}"
        assert not hasattr(routes, name), f"orchestrator 仍重导出 {name}"


def test_context_slice4_reexport_identity():
    """巨石拆分 slice 4 / slice 39：Copilot 上下文族在 unified_inbox_context，orchestrator 不再重导出。"""
    import src.web.routes.unified_inbox_context as context
    import src.web.routes.unified_inbox_routes as routes
    for name in ("_conv_relationship_context", "_build_copilot_context",
                 "_maybe_polish_copilot", "_record_copilot_impression_if_prefill",
                 "_record_copilot_adopt_from_send", "_mention_context_for_conv",
                 "_build_contact_relationship_payload", "_build_relationship_stage_payload"):
        assert hasattr(context, name), f"context 缺少 {name}"
        assert not hasattr(routes, name), f"orchestrator 仍重导出 {name}"


def test_context_slice5_reexport_identity():
    """巨石拆分 slice 5 / slice 39：档案/时间线族在 context+helpers，orchestrator 不再重导出。"""
    import src.web.routes.unified_inbox_context as context
    import src.web.routes.unified_inbox_helpers as helpers
    import src.web.routes.unified_inbox_routes as routes
    for name in ("_memory_bullets", "_lookup_contacts_enrichment",
                 "_build_contact_timeline", "_collect_quick_templates",
                 "_context_relationship", "_build_profile", "_profile_tags"):
        assert hasattr(context, name), f"context 缺少 {name}"
        assert not hasattr(routes, name), f"orchestrator 仍重导出 {name}"
    for name in ("FUNNEL_STAGE_LABELS", "_PLATFORM_LABELS", "_EVENT_LABELS"):
        assert hasattr(helpers, name), f"helpers 缺少 {name}"
        assert not hasattr(routes, name), f"orchestrator 仍重导出 {name}"


def test_sla_slice6_reexport_identity():
    """巨石拆分 slice 6 / slice 39：SLA 族在 unified_inbox_sla，orchestrator 不再重导出。"""
    import src.web.routes.unified_inbox_routes as routes
    import src.web.routes.unified_inbox_sla as sla
    for name in ("_SLA_WARN_SEC", "_SLA_CRIT_SEC", "_sla_cfg", "_agent_sla_cfg",
                 "_sla_alert_snapshot", "_presence_stale_sec", "_escalation_snapshot",
                 "_sla_detail", "_agent_frt_detail"):
        assert hasattr(sla, name), f"sla 缺少 {name}"
        assert not hasattr(routes, name), f"orchestrator 仍重导出 {name}"


def test_aggregate_slice7_reexport_identity():
    """巨石拆分 slice 7 / slice 39：聚合族在 aggregate+helpers，orchestrator 不再重导出。"""
    import src.web.routes.unified_inbox_aggregate as agg
    import src.web.routes.unified_inbox_helpers as helpers
    import src.web.routes.unified_inbox_routes as routes
    for name in ("_INBOX_ADAPTERS", "_collect_all_chats", "_collect_chats_from_store",
                 "_chats_for_listing", "_ingest_best_effort", "_ingest_thread_best_effort",
                 "_is_protocol_account", "_read_automation_mode", "_write_automation_mode",
                 "_read_from_store_enabled", "_store_conv_as_chat", "_thread_messages_from_store",
                 "_enrich_outbound_originals"):
        assert hasattr(agg, name), f"aggregate 缺少 {name}"
        assert not hasattr(routes, name), f"orchestrator 仍重导出 {name}"
    assert hasattr(helpers, "AUTOMATION_MODES")
    assert not hasattr(routes, "AUTOMATION_MODES")


def test_proxy_fingerprint_routes_slice8_registers_contract():
    """巨石拆分 slice 8（路由域试切）：register_proxy_fingerprint_routes 子注册函数
    挂载的端点路径/方法与基线一致（自包含、仅依赖 api_auth）。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_proxy_routes import register_proxy_fingerprint_routes
    app = FastAPI()
    register_proxy_fingerprint_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/proxies", "GET"), ("/api/proxies", "POST"),
        ("/api/proxies/{proxy_id}", "DELETE"), ("/api/proxies/{proxy_id}/test", "POST"),
        ("/api/fingerprints", "GET"), ("/api/fingerprints/generate", "POST"),
    }
    assert expected <= live, f"代理/指纹路由域端点缺失：{expected - live}"


def test_platform_login_routes_slice9_registers_contract():
    """巨石拆分 slice 9：register_platform_login_routes 子注册函数挂载的登录端点
    路径/方法与基线一致（域内 helper 随搬，仅依赖 api_auth + config_manager）。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_login_routes import register_platform_login_routes
    app = FastAPI()
    register_platform_login_routes(app, api_auth=lambda request: None, config_manager=None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/platforms/{platform}/modes", "GET"),
        ("/api/platforms/{platform}/login/start", "POST"),
        ("/api/platforms/{platform}/login/{login_id}/status", "GET"),
        ("/api/platforms/{platform}/login/{login_id}/cancel", "POST"),
    }
    assert expected <= live, f"平台登录路由域端点缺失：{expected - live}"


def test_account_routes_slice10_registers_contract():
    """巨石拆分 slice 10：register_account_routes 子注册函数挂载账号/编排器/自动回复
    端点；register 时副作用（sink/autoreply 注册）+ startup 钩子时序随域同搬。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_account_routes import register_account_routes
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None, config_manager=None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/internal/protocol/ingest", "POST"),
        ("/api/accounts", "GET"),
        ("/api/accounts/orchestrator", "GET"),
        ("/api/accounts/orchestrator/sync", "POST"),
        ("/api/accounts/protocol/readiness", "GET"),
        ("/api/accounts/{platform}/{account_id}/start", "POST"),
        ("/api/accounts/{platform}/{account_id}/stop", "POST"),
        ("/api/accounts/{platform}/{account_id}/restart", "POST"),
        ("/api/accounts/{platform}/{account_id}/auto-reply", "POST"),
        ("/api/accounts/{platform}/{account_id}/auto-reply/override", "POST"),
        ("/api/accounts/auto-reply/audit", "GET"),
        ("/api/accounts/auto-reply/config", "GET"),
        ("/api/accounts/auto-reply/config", "POST"),
        ("/api/accounts/auto-reply/health", "GET"),
        ("/api/accounts/auto-reply/webhooks", "GET"),
        ("/api/accounts/auto-reply/webhooks", "POST"),
        ("/api/accounts/auto-reply/webhooks/test", "POST"),
        ("/api/accounts/auto-reply/stream", "GET"),
    }
    assert expected <= live, f"账号路由域端点缺失：{expected - live}"


def test_workspace_presence_routes_slice11_registers_contract():
    """巨石拆分 slice 11：register_workspace_presence_routes 子注册函数挂载坐席协作
    presence/会话租约/web漏斗端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_workspace_presence_routes import (
        register_workspace_presence_routes,
    )
    app = FastAPI()
    register_workspace_presence_routes(app, api_auth=lambda request: None, config_manager=None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/presence", "GET"), ("/api/workspace/presence", "POST"),
        ("/api/workspace/heartbeat", "POST"), ("/api/workspace/claims", "GET"),
        ("/api/workspace/claim", "POST"), ("/api/workspace/claim/renew", "POST"),
        ("/api/workspace/claim/release", "POST"),
        ("/api/workspace/metrics/web-funnel", "GET"),
    }
    assert expected <= live, f"坐席协作路由域端点缺失：{expected - live}"


def test_workspace_contacts_routes_slice12_registers_contract():
    """巨石拆分 slice 12：register_workspace_contacts_routes 子注册函数挂载工作台联系人/
    CRM/跟进任务端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from src.web.routes.unified_inbox_workspace_contacts_routes import (
        register_workspace_contacts_routes,
    )

    class _Tpl:
        def TemplateResponse(self, request, name, ctx):  # pragma: no cover - 仅供挂载
            return HTMLResponse("")

    app = FastAPI()
    register_workspace_contacts_routes(
        app, api_auth=lambda request: None, page_auth=lambda request: None,
        templates=_Tpl(), config_manager=None,
    )
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/contacts/overview", "GET"),
        ("/api/workspace/contacts/merge", "POST"),
        ("/api/workspace/contacts/merge-contact", "POST"),
        ("/api/workspace/contacts/split", "POST"),
        ("/api/workspace/merge-reviews", "GET"),
        ("/api/workspace/merge-reviews/{review_id}", "POST"),
        ("/api/workspace/contacts/search", "GET"),
        ("/api/workspace/contact/{contact_id}", "GET"),
        ("/workspace/contact/{contact_id}", "GET"),
        ("/api/workspace/contacts/list", "GET"),
        ("/api/workspace/contact/{contact_id}/crm", "POST"),
        ("/api/workspace/follow-ups", "GET"),
        ("/api/workspace/contact/{contact_id}/follow-up", "POST"),
        ("/api/workspace/follow-up/{task_id}/done", "POST"),
        ("/api/workspace/follow-up/{task_id}/assign", "POST"),
        ("/api/workspace/follow-up/{task_id}/snooze", "POST"),
        ("/api/workspace/my-tasks", "GET"),
        ("/api/workspace/contact/{contact_id}/tasks", "GET"),
    }
    assert expected <= live, f"工作台联系人路由域端点缺失：{expected - live}"


def test_workspace_escalation_routes_slice13_registers_contract():
    """巨石拆分 slice 13：register_workspace_escalation_routes 子注册函数挂载 SLA告警/
    坐席身份/升级队列端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_workspace_escalation_routes import (
        register_workspace_escalation_routes,
    )
    app = FastAPI()
    register_workspace_escalation_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/sla-alerts", "GET"),
        ("/api/workspace/me", "GET"),
        ("/api/workspace/escalations", "GET"),
        ("/api/workspace/escalations/mine", "GET"),
        ("/api/workspace/escalation/{esc_id}/assign", "POST"),
        ("/api/workspace/escalation-log", "GET"),
    }
    assert expected <= live, f"升级队列路由域端点缺失：{expected - live}"


def test_workspace_prefs_routes_slice14_registers_contract():
    """巨石拆分 slice 14：register_workspace_prefs_routes 子注册函数挂载坐席偏好/
    SLA明细/SLA一键建任务端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_workspace_prefs_routes import (
        register_workspace_prefs_routes,
    )
    app = FastAPI()
    register_workspace_prefs_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/prefs", "GET"), ("/api/workspace/prefs", "POST"),
        ("/api/workspace/sla-detail", "GET"),
        ("/api/workspace/agent-frt-detail", "GET"),
        ("/api/workspace/sla/create-task", "POST"),
    }
    assert expected <= live, f"坐席偏好/SLA明细路由域端点缺失：{expected - live}"


def test_workspace_prefs_set_languages_persists_via_store_slice14():
    """slice 14 专项：POST /api/workspace/prefs 带 languages 时仍走 inbox.set_agent_languages
    （归一化去重），保证 match_language 坐席语言栈写链路在搬家后不变。"""
    import asyncio

    from src.web.routes.unified_inbox_workspace_prefs_routes import (
        register_workspace_prefs_routes,
    )

    captured: Dict[str, Any] = {}

    class _Inbox:
        def set_agent_prefs(self, agent_id, **kw):
            return {"agent_id": agent_id, **kw}

        def set_agent_languages(self, agent_id, langs):
            captured["agent_id"] = agent_id
            captured["langs"] = langs
            return {"agent_id": agent_id, "languages": langs}

    class _Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    handlers: Dict[str, Any] = {}

    class _App:
        def get(self, path, **kw):
            def deco(fn):
                handlers[("GET", path)] = fn
                return fn
            return deco

        def post(self, path, **kw):
            def deco(fn):
                handlers[("POST", path)] = fn
                return fn
            return deco

    import src.web.routes.unified_inbox_workspace_prefs_routes as mod
    orig_inbox = mod._inbox_store
    orig_agent = mod._session_agent
    mod._inbox_store = lambda request: _Inbox()
    mod._session_agent = lambda request: {"agent_id": "a1", "display_name": "A"}
    try:
        register_workspace_prefs_routes(_App(), api_auth=lambda request: None)
        fn = handlers[("POST", "/api/workspace/prefs")]
        out = asyncio.run(fn(_Req({"languages": ["EN", "zh", "en", " ja "]})))
    finally:
        mod._inbox_store = orig_inbox
        mod._session_agent = orig_agent
    assert out["ok"] is True
    assert captured["agent_id"] == "a1"
    # 归一化 + 去重：en/zh/ja（顺序保留，重复 en 去掉）
    parts = captured["langs"].split(",")
    assert parts[0] == "en" and "zh" in parts and "ja" in parts
    assert parts.count("en") == 1


def test_workspace_tags_routes_slice15_registers_contract():
    """巨石拆分 slice 15：register_workspace_tags_routes 子注册函数挂载标签体系/
    会话级标签·摘要·归档端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_workspace_tags_routes import (
        register_workspace_tags_routes,
    )
    app = FastAPI()
    register_workspace_tags_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/tags", "GET"),
        ("/api/workspace/tag-stats", "GET"),
        ("/api/workspace/tag-library", "GET"),
        ("/api/workspace/tag-library", "POST"),
        ("/api/workspace/tag-library/{tag}", "DELETE"),
        ("/api/workspace/conv/{conversation_id}/summarize", "POST"),
        ("/api/workspace/conv/{conversation_id}/tags", "GET"),
        ("/api/workspace/conv/{conversation_id}/tags", "PUT"),
        ("/api/workspace/conv/{conversation_id}/archive", "PATCH"),
    }
    assert expected <= live, f"标签体系路由域端点缺失：{expected - live}"


def test_workspace_dashboard_routes_slice16_registers_contract():
    """巨石拆分 slice 16b：register_workspace_dashboard_routes 子注册函数挂载经营日报CSV/
    经理仪表盘端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_dashboard_routes import (
        register_workspace_dashboard_routes,
    )
    app = FastAPI()
    register_workspace_dashboard_routes(
        app, api_auth=lambda request: None, config_manager=None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/daily-report.csv", "GET"),
        ("/api/workspace/dashboard", "GET"),
    }
    assert expected <= live, f"日报/仪表盘路由域端点缺失：{expected - live}"


def test_reports_slice16a_helpers_are_module_level():
    """slice 16a：逐日聚合计算层已下沉为 unified_inbox_reports 模块级纯函数（可独立 import）。"""
    from src.web.routes.unified_inbox_reports import (
        _agent_daily_report_rows,
        _daily_report_rows,
    )
    assert callable(_daily_report_rows)
    assert callable(_agent_daily_report_rows)


def test_workspace_pages_routes_slice17_registers_contract():
    """巨石拆分 slice 17 + 38a：register_workspace_pages_routes 挂载工作台 HTML 页面壳，
    含主入口 /workspace、旧 redirect /unified-inbox 及 4 个子页面，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from src.web.routes.unified_inbox_workspace_pages_routes import (
        register_workspace_pages_routes,
    )

    class _Tpl:
        def TemplateResponse(self, request, name, ctx):  # pragma: no cover
            return HTMLResponse("")

    app = FastAPI()
    register_workspace_pages_routes(
        app, page_auth=lambda request: None, templates=_Tpl(), config_manager=None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/workspace", "GET"),
        ("/unified-inbox", "GET"),
        ("/workspace/contacts", "GET"), ("/workspace/tasks", "GET"),
        ("/workspace/dash", "GET"), ("/workspace/escalations", "GET"),
        ("/workspace/roi", "GET"), ("/workspace/setup", "GET"),
        ("/workspace/kb-start", "GET"), ("/workspace/golive", "GET"),
        ("/workspace/ai-quality", "GET"), ("/workspace/usage", "GET"),
    }
    assert expected <= live, f"工作台页面壳端点缺失：{expected - live}"


def test_unified_inbox_routes_slice38a_is_pure_orchestrator():
    """slice 38a：register_unified_inbox_routes 不再内联 @app 装饰 handler（纯 orchestrator）。"""
    import inspect
    from src.web.routes import unified_inbox_routes as mod
    src = inspect.getsource(mod.register_unified_inbox_routes)
    assert "@app.get" not in src and "@app.post" not in src


def test_unified_inbox_routes_slice39_orchestrator_surface():
    """slice 39：orchestrator 不再重导出 helpers/services/auth/context/sla/aggregate 符号。"""
    import src.web.routes.unified_inbox_routes as mod
    assert hasattr(mod, "register_unified_inbox_routes")
    leaked = [
        n for n in dir(mod)
        if n.startswith("_") and not n.startswith("__")
    ]
    leaked += [
        n for n in ("AUTOMATION_MODES", "FUNNEL_STAGE_LABELS")
        if hasattr(mod, n)
    ]
    assert not leaked, f"orchestrator 仍暴露内部符号: {leaked}"


def test_unified_inbox_routes_slice38b_register_order_unchanged():
    """slice 38b：分组注释后 register_* 调用顺序与拆分前一致（startup/路由时序守卫）。"""
    import inspect
    import re
    from src.web.routes import unified_inbox_routes as mod
    src = inspect.getsource(mod.register_unified_inbox_routes)
    names = re.findall(r"^\s+register_(\w+)_routes\(", src, re.MULTILINE)
    assert len(names) == 34, f"orchestrator 应挂载 34 个子域，实际 {len(names)}"
    assert names == [
        "workspace_pages",
        "realtime", "read",
        "platform_login", "setup", "proxy_fingerprint", "account",
        "workspace_presence", "workspace_contacts",
        "workspace_escalation", "workspace_prefs",
        "workspace_dashboard", "roi", "quality", "usage", "workspace_tags",
        "aux_read", "translate", "desktop",
        "conversion_outreach", "analyze",
        "stored_read", "send",
        "intel_profile", "template", "batch_notif",
        "queue_webhook", "collab_mention", "collab_context",
        "relationship_stage", "copilot", "workflow",
        "routing_search", "qa_churn",
    ]


def test_contacts_export_csv_slice17_归并入_contacts_module():
    """巨石拆分 slice 17：contacts/export.csv straggler 已归并入 contacts 域模块，
    随 register_workspace_contacts_routes 一并挂载（彻底关闭 contacts 域）。"""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from src.web.routes.unified_inbox_workspace_contacts_routes import (
        register_workspace_contacts_routes,
    )

    class _Tpl:
        def TemplateResponse(self, request, name, ctx):  # pragma: no cover
            return HTMLResponse("")

    app = FastAPI()
    register_workspace_contacts_routes(
        app, api_auth=lambda request: None, page_auth=lambda request: None,
        templates=_Tpl(), config_manager=None)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/workspace/contacts/export.csv" in paths


def test_relationship_stage_routes_slice18_registers_contract():
    """巨石拆分 slice 18：register_relationship_stage_routes 子注册函数挂载关系阶段
    可视化/进阶·降级·回暖/客户级对齐·时间轴端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_relationship_routes import (
        register_relationship_stage_routes,
    )
    app = FastAPI()
    register_relationship_stage_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/conv/{conversation_id}/relationship-stage", "GET"),
        ("/api/workspace/conv/{conversation_id}/relationship-stage/confirm", "POST"),
        ("/api/workspace/conv/{conversation_id}/relationship-stage/downgrade", "POST"),
        ("/api/workspace/conv/{conversation_id}/relationship-stage/reunion", "POST"),
        ("/api/workspace/contact/{contact_id}/relationship-stage", "GET"),
        ("/api/workspace/contact/{contact_id}/relationship-stage/sync", "POST"),
        ("/api/workspace/contact/{contact_id}/stage-timeline", "GET"),
    }
    assert expected <= live, f"关系阶段路由域端点缺失：{expected - live}"


def test_copilot_routes_slice19_registers_contract():
    """巨石拆分 slice 19：register_copilot_routes 子注册函数挂载剧本引擎/互动积分/AI 副驾
    端点（Phase40/41/42），路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_copilot_routes import register_copilot_routes
    app = FastAPI()
    register_copilot_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/conv/{conversation_id}/script-suggestions", "GET"),
        ("/api/workspace/script-topics", "GET"),
        ("/api/workspace/script-topics", "POST"),
        ("/api/workspace/script-topics/{topic_id}", "PUT"),
        ("/api/workspace/script-topics/{topic_id}", "DELETE"),
        ("/api/workspace/contact/{contact_id}/engagement", "GET"),
        ("/api/workspace/contact/{contact_id}/engagement", "POST"),
        ("/api/workspace/conv/{conversation_id}/copilot-prefill", "GET"),
        ("/api/workspace/conv/{conversation_id}/reply-suggest", "POST"),
    }
    assert expected <= live, f"Copilot 副驾路由域端点缺失：{expected - live}"


def test_workflow_routes_slice20_registers_contract():
    """巨石拆分 slice 20：register_workflow_routes 挂载动作推荐/自定义动作·工作链/
    工作链执行可视化端点（Phase37/47），路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_workflow_routes import register_workflow_routes
    app = FastAPI()
    register_workflow_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/conv/{conversation_id}/next-actions", "GET"),
        ("/api/workspace/conv/{conversation_id}/execute-action", "POST"),
        ("/api/workspace/workflow-actions", "GET"),
        ("/api/workspace/workflow-actions", "POST"),
        ("/api/workspace/workflow-actions/{action_id}", "PUT"),
        ("/api/workspace/workflow-actions/{action_id}", "DELETE"),
        ("/api/workspace/workflow-chains", "GET"),
        ("/api/workspace/workflow-chains", "POST"),
        ("/api/workspace/workflow-chains/{chain_id}", "PUT"),
        ("/api/workspace/workflow-chains/{chain_id}", "DELETE"),
        ("/api/workspace/chain-executions", "GET"),
        ("/api/workspace/conv/{conversation_id}/chain-executions", "GET"),
        ("/api/workspace/chain-executions/{exec_id}/cancel", "POST"),
        ("/api/workspace/conv/{conversation_id}/start-chain", "POST"),
    }
    assert expected <= live, f"动作/工作链路由域端点缺失：{expected - live}"


def test_routing_search_routes_slice21_registers_contract():
    """巨石拆分 slice 21：register_routing_search_routes 挂载分流路由规则引擎(Phase38)
    + 全局跨资源搜索(Phase39)端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_routing_search_routes import (
        register_routing_search_routes,
    )
    app = FastAPI()
    register_routing_search_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/routing-rules", "GET"),
        ("/api/workspace/routing-rules", "POST"),
        ("/api/workspace/routing-rules/{rule_id}", "PUT"),
        ("/api/workspace/routing-rules/{rule_id}", "DELETE"),
        ("/api/workspace/routing-rules/evaluate", "POST"),
        ("/api/workspace/search", "GET"),
    }
    assert expected <= live, f"分流路由/全局搜索路由域端点缺失：{expected - live}"


def test_qa_churn_routes_slice22_registers_contract():
    """巨石拆分 slice 22：register_qa_churn_routes 挂载 QA 质检评分(Phase34)
    + 流失预警·活跃热力图(Phase35)端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_qa_churn_routes import register_qa_churn_routes
    app = FastAPI()
    register_qa_churn_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/conv/{conversation_id}/qa-score", "GET"),
        ("/api/workspace/conv/{conversation_id}/qa-score", "POST"),
        ("/api/workspace/agent-qa-stats", "GET"),
        ("/api/workspace/churn-risks", "GET"),
        ("/api/workspace/activity-heatmap", "GET"),
    }
    assert expected <= live, f"QA 质检/流失预警路由域端点缺失：{expected - live}"


def test_queue_webhook_routes_slice23_registers_contract():
    """巨石拆分 slice 23：register_queue_webhook_routes 挂载 Queue Monitor 看板(Phase29)
    + Webhook 外发配置(Phase28)端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_queue_webhook_routes import (
        register_queue_webhook_routes,
    )
    app = FastAPI()
    register_queue_webhook_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/queue-monitor", "GET"),
        ("/api/workspace/queue-monitor/reassign", "POST"),
        ("/api/workspace/webhook-outbound", "GET"),
        ("/api/workspace/webhook-outbound/test", "POST"),
    }
    assert expected <= live, f"Queue Monitor/Webhook 路由域端点缺失：{expected - live}"


def test_collab_mention_routes_slice24_registers_contract():
    """巨石拆分 slice 24：register_collab_mention_routes 挂载坐席协作注解(Phase25)
    + @mention 智能路由(Phase48)端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_collab_mention_routes import (
        register_collab_mention_routes,
    )
    app = FastAPI()
    register_collab_mention_routes(app, api_auth=lambda request: None, config_manager=None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/conv/{conversation_id}/mention-suggestions", "GET"),
        ("/api/workspace/conv/{conversation_id}/notes", "GET"),
        ("/api/workspace/conv/{conversation_id}/notes", "POST"),
        ("/api/workspace/conv/{conversation_id}/notes/{note_id}", "PATCH"),
        ("/api/workspace/conv/{conversation_id}/notes/{note_id}", "DELETE"),
    }
    assert expected <= live, f"协作注解/@mention 路由域端点缺失：{expected - live}"


def test_collab_context_routes_slice25_registers_contract():
    """巨石拆分 slice 25：register_collab_context_routes 挂载客户 360°时间轴(Phase31)
    + 多坐席协作剧本上下文(Phase45)端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_collab_context_routes import (
        register_collab_context_routes,
    )
    app = FastAPI()
    register_collab_context_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/contact/{contact_id}/timeline", "GET"),
        ("/api/workspace/contact/{contact_id}/collab-context", "GET"),
        ("/api/workspace/conv/{conversation_id}/collab-context", "GET"),
    }
    assert expected <= live, f"360°时间轴/协作上下文路由域端点缺失：{expected - live}"


def test_batch_notif_routes_slice26_registers_contract():
    """巨石拆分 slice 26：register_batch_notif_routes 挂载批量操作(Phase23)
    + 通知中心(Phase24)端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_batch_notif_routes import (
        register_batch_notif_routes,
    )
    app = FastAPI()
    register_batch_notif_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/batch/archive", "POST"),
        ("/api/workspace/batch/tags", "POST"),
        ("/api/workspace/batch/assign", "POST"),
        ("/api/workspace/notifications", "GET"),
        ("/api/workspace/notifications/read", "POST"),
    }
    assert expected <= live, f"批量操作/通知中心路由域端点缺失：{expected - live}"


def test_template_routes_slice27_registers_contract():
    """巨石拆分 slice 27：register_template_routes 挂载 I3 回复模板库 API 端点，
    路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_template_routes import register_template_routes
    app = FastAPI()
    register_template_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/reply-templates", "GET"),
        ("/api/reply-templates", "POST"),
        ("/api/reply-templates/{template_id}", "PUT"),
        ("/api/reply-templates/{template_id}", "DELETE"),
        ("/api/reply-templates/{template_id}/use", "POST"),
    }
    assert expected <= live, f"模板库路由域端点缺失：{expected - live}"


def test_intel_profile_routes_slice28_registers_contract():
    """巨石拆分 slice 28：register_intel_profile_routes 挂载 I1 对话智能元数据
    + K3 客户画像聚合 API 端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_intel_profile_routes import (
        register_intel_profile_routes,
    )
    app = FastAPI()
    register_intel_profile_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/unified-inbox/conv-meta", "GET"),
        ("/api/unified-inbox/contact-profile", "GET"),
    }
    assert expected <= live, f"智能元数据/客户画像路由域端点缺失：{expected - live}"


def test_stored_read_routes_slice29_registers_contract():
    """巨石拆分 slice 29：register_stored_read_routes 挂载 A1 store-backed 读端点
    (stored-chats/history) + 自动化模式(GET/POST)，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_stored_read_routes import (
        register_stored_read_routes,
    )
    app = FastAPI()
    register_stored_read_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/unified-inbox/stored-chats", "GET"),
        ("/api/unified-inbox/history", "GET"),
        ("/api/unified-inbox/automation", "GET"),
        ("/api/unified-inbox/automation", "POST"),
        ("/api/unified-inbox/automation-stats", "GET"),
    }
    assert expected <= live, f"A1 持久化读/自动化模式路由域端点缺失：{expected - live}"


def test_send_routes_slice30_registers_contract():
    """巨石拆分 slice 30：register_send_routes 挂载消息/媒体/语音发送写路径端点，
    路径/方法与基线一致（send/send-media/send-voice 用 page_auth，send-caps 用 api_auth）。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_send_routes import register_send_routes
    app = FastAPI()
    register_send_routes(app, api_auth=lambda request: None, page_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/unified-inbox/send", "POST"),
        ("/api/unified-inbox/send-media", "POST"),
        ("/api/unified-inbox/send-voice", "POST"),
        ("/api/unified-inbox/send-caps", "GET"),
    }
    assert expected <= live, f"发送写路径路由域端点缺失：{expected - live}"


def test_resolve_conv_language_downsunk_to_services():
    """slice 30 配套：_resolve_conv_language 在 services（translate 与 send 共用）。"""
    from src.web.routes.unified_inbox_services import _resolve_conv_language
    assert callable(_resolve_conv_language)


def test_roi_routes_p0_3_registers_contract():
    """P0-3：register_roi_routes 挂载老板视角 ROI 概览端点。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_roi import register_roi_routes
    app = FastAPI()
    register_roi_routes(app, api_auth=lambda request: None, config_manager=None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    assert ("/api/workspace/roi", "GET") in live


def test_roi_summary_aggregates_automation_and_health():
    """P0-3：build_roi_summary 蒸馏 AI/人工拆分 + 节省人力 + 配置健康度。"""
    from src.web.routes.unified_inbox_roi import build_roi_summary

    class _Inbox:
        def get_automation_roi_stats(self, since):
            return {"ai_sent": 80, "human_sent": 20, "suppressed": 5,
                    "total_sent": 100, "ai_share": 0.8, "trend": []}

    class _State:
        inbox_store = _Inbox()
        contacts_store = None

    class _App:
        state = _State()

    class _Req:
        app = _App()

    class _CM:
        config = {"ai": {"provider": "openai_compatible", "api_key": "k",
                          "base_url": "u", "model": "m"}}
        config_path = None

    out = build_roi_summary(_Req(), _CM(), span=7)
    assert out["automation"]["ai_sent"] == 80
    assert out["automation"]["ai_share_pct"] == 80.0
    assert out["automation"]["saved_hours"] == round(80 * 180 / 3600, 1)
    assert "compare" in out["automation"]
    assert out["config_health"]["available"] is True
    assert out["config_health"]["status"] == "ok"


def test_roi_summary_money_and_compare():
    """P0-3 再优化：cost_per_hour 出金额卡；环比基于上一等长窗口。"""
    from src.web.routes.unified_inbox_roi import build_roi_summary

    calls = {}

    class _Inbox:
        def get_automation_roi_stats(self, since, until_ts=None):
            # 当前窗口（无 until）：ai=100；上一窗口（有 until）：ai=50
            if until_ts is None:
                calls["cur"] = (since, until_ts)
                return {"ai_sent": 100, "human_sent": 0, "suppressed": 0,
                        "total_sent": 100, "ai_share": 1.0, "trend": []}
            calls["prev"] = (since, until_ts)
            return {"ai_sent": 50, "human_sent": 0, "suppressed": 0,
                    "total_sent": 50, "ai_share": 1.0, "trend": []}

    class _State:
        inbox_store = _Inbox()
        contacts_store = None

    class _App:
        state = _State()

    class _Req:
        app = _App()

    class _CM:
        config = {"ai": {"provider": "openai_compatible", "api_key": "k",
                         "base_url": "u", "model": "m"},
                  "workspace": {"roi": {"sec_per_reply": 180, "cost_per_hour": 30}}}
        config_path = None

    out = build_roi_summary(_Req(), _CM(), span=7)
    a = out["automation"]
    assert a["cost_per_hour"] == 30
    assert a["saved_money"] == round(round(100 * 180 / 3600, 1) * 30, 2)
    # 环比：当前 ai=100 vs 上一窗口 ai=50 → +100%
    assert a["compare"]["ai_sent"] == 50
    assert a["compare"]["ai_sent_delta_pct"] == 100.0
    # 上一窗口区间为 [cur_since - 7d, cur_since)
    assert calls["prev"][1] == calls["cur"][0]


def test_roi_summary_config_health_flags_errors():
    """P0-3：配置有 footgun 时健康度卡反映 error。"""
    from src.web.routes.unified_inbox_roi import build_roi_summary

    class _State:
        inbox_store = None
        contacts_store = None

    class _App:
        state = _State()

    class _Req:
        app = _App()

    class _CM:
        config = {"ai": {"provider": "deepseek"}}  # footgun → error
        config_path = None

    out = build_roi_summary(_Req(), _CM(), span=7)
    assert out["config_health"]["status"] == "error"
    assert out["config_health"]["errors"] >= 1


def test_automation_roi_stats_splits_ai_vs_human(tmp_path):
    """P0-3：InboxStore.get_automation_roi_stats 按 action 正确拆 AI/人工/拦截。"""
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    cid = "web:web:roi"
    for action in ["autosend", "autosend", "approved", "edit_send",
                   "force_override", "rejected", "blocked"]:
        store.record_draft_audit(
            f"d-{action}", autopilot_level="L2", action=action,
            agent_id="" if action == "autosend" else "a1",
            conversation_id=cid)
    stats = store.get_automation_roi_stats(0.0)
    assert stats["ai_sent"] == 2
    assert stats["human_sent"] == 3      # approved + edit_send + force_override
    assert stats["suppressed"] == 2      # rejected + blocked
    assert stats["total_sent"] == 5
    assert stats["ai_share"] == round(2 / 5, 3)
    store.close()


def test_automation_roi_stats_until_ts_windows(tmp_path):
    """P0-3 再优化：until_ts 半开区间正确切分（环比上一窗口用）。"""
    import time as _t
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    now = _t.time()
    # 旧窗口（10 天前）2 条 autosend；新窗口（今天）3 条 autosend
    for _ in range(2):
        store.record_draft_audit("d", action="autosend", ts=now - 10 * 86400)
    for _ in range(3):
        store.record_draft_audit("d", action="autosend", ts=now)
    boundary = now - 5 * 86400
    old = store.get_automation_roi_stats(0.0, until_ts=boundary)
    new = store.get_automation_roi_stats(boundary)
    assert old["ai_sent"] == 2
    assert new["ai_sent"] == 3
    store.close()


def test_quality_routes_p3_1_registers_contract():
    """P3-1：register_quality_routes 挂载 AI 质量看板端点。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_quality_routes import register_quality_routes
    app = FastAPI()
    register_quality_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    assert ("/api/workspace/ai-quality", "GET") in live


def test_usage_routes_c0_2_registers_contract():
    """C0-2：register_usage_routes 挂载用量计量端点。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_usage_routes import register_usage_routes
    app = FastAPI()
    register_usage_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    assert ("/api/workspace/usage", "GET") in live


def test_usage_stats_aggregates_messages_calls_agents(tmp_path):
    """C0-2：get_usage_stats 聚合消息量/AI 调用/活跃坐席，含按日 trend。"""
    import time as _t
    from src.inbox.models import InboxMessage
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    cid = "web:web:u"
    now = _t.time()
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="m1", direction="in",
        text="hi", ts=now - 100))
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="m2", direction="out",
        text="hello", ts=now - 90))
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="m3", direction="out",
        text="more", ts=now - 80))
    store.record_draft_audit("d1", action="autosend", autopilot_level="L2",
                             agent_id="", conversation_id=cid, ts=now - 70)
    store.record_draft_audit("d2", action="edit_send", autopilot_level="L3",
                             agent_id="alice", conversation_id=cid, ts=now - 60)
    store.record_draft_audit("d3", action="approved", autopilot_level="L3",
                             agent_id="bob", conversation_id=cid, ts=now - 50)
    u = store.get_usage_stats(0.0)
    assert u["messages_in"] == 1
    assert u["messages_out"] == 2
    assert u["messages_total"] == 3
    assert u["ai_calls"] == 3
    assert u["ai_sent"] == 1
    assert u["active_agents"] == 2  # alice + bob（空 agent 的 autosend 不计坐席）
    assert sorted(u["active_agent_ids"]) == ["alice", "bob"]
    assert u["trend"] and "messages" in u["trend"][0]
    store.close()


def test_usage_stats_until_ts_windows(tmp_path):
    """C0-2：until_ts 半开区间正确切分，供环比使用。"""
    import time as _t
    from src.inbox.models import InboxMessage
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    cid = "web:web:w"
    base = 1_000_000.0
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="a", direction="in",
        text="old", ts=base + 10))
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="b", direction="in",
        text="new", ts=base + 100))
    win = store.get_usage_stats(base, until_ts=base + 50)
    assert win["messages_total"] == 1  # 仅 base+10 落入 [base, base+50)
    store.close()


def test_quality_stats_splits_actions_and_levels(tmp_path):
    """P3-1：get_quality_stats 按动作+等级拆分，派生率正确。"""
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    rows = [
        ("autosend", "L2"), ("autosend", "L2"), ("autosend", "L1"),
        ("approved", "L3"), ("edit_send", "L3"), ("rejected", "L2"),
        ("blocked", "L4"),
    ]
    for action, lv in rows:
        store.record_draft_audit(
            f"d-{action}-{lv}", autopilot_level=lv, action=action,
            agent_id="" if action == "autosend" else "a1")
    st = store.get_quality_stats(0.0)
    assert st["counts"]["autosend"] == 3
    assert st["total"] == 7
    # auto_pass_rate = 3/7
    assert st["auto_pass_rate"] == round(3 / 7, 3)
    # human_sent = approved+edit_send+force_override = 2；edit_rate = 1/2
    assert st["human_sent"] == 2
    assert st["edit_rate"] == round(1 / 2, 3)
    # levels：L4=1 (blocked)，high_risk_rate = (L3+L4)/leveled
    assert st["levels"]["L4"] == 1
    leveled = sum(st["levels"].values())
    assert st["high_risk_rate"] == round((st["levels"].get("L3", 0) + 1) / leveled, 3)
    store.close()


def test_quality_stats_until_ts_windows(tmp_path):
    """P3-1：until_ts 半开区间切分（质量环比上一窗口用）。"""
    import time as _t
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    now = _t.time()
    store.record_draft_audit("old", action="autosend", autopilot_level="L2",
                             ts=now - 10 * 86400)
    store.record_draft_audit("new", action="autosend", autopilot_level="L2", ts=now)
    boundary = now - 5 * 86400
    old = store.get_quality_stats(0.0, until_ts=boundary)
    new = store.get_quality_stats(boundary)
    assert old["counts"]["autosend"] == 1
    assert new["counts"]["autosend"] == 1
    store.close()


def test_quality_summary_hints_flag_high_edit_rate():
    """P3-1：build_quality_summary 对高改写率给出运营建议。"""
    from src.web.routes.unified_inbox_quality_routes import build_quality_summary

    class _Inbox:
        def get_quality_stats(self, since, until_ts=None):
            if until_ts is not None:
                return {"counts": {}, "levels": {}, "total": 0,
                        "auto_pass_rate": 0.0, "edit_rate": 0.0,
                        "reject_rate": 0.0, "block_rate": 0.0,
                        "high_risk_rate": 0.0, "trend": []}
            return {"counts": {"autosend": 10, "approved": 10, "edit_send": 30,
                               "rejected": 5, "blocked": 5, "force_override": 0},
                    "levels": {"L2": 40, "L3": 20},
                    "total": 60, "human_sent": 40,
                    "auto_pass_rate": 10 / 60, "edit_rate": 30 / 40,
                    "reject_rate": 5 / 60, "block_rate": 5 / 60,
                    "high_risk_rate": 20 / 60, "trend": []}

    class _State:
        inbox_store = _Inbox()
        contacts_store = None

    class _App:
        state = _State()

    class _Req:
        app = _App()

    out = build_quality_summary(_Req(), span=7)
    assert out["available"] is True
    assert out["metrics"]["edit_rate"] == 75.0
    assert any("改写率" in h["text"] for h in out["hints"])


def test_setup_routes_p1_1_registers_contract():
    """P1-1：register_setup_routes 挂载渠道接入向导端点。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_setup_routes import register_setup_routes
    app = FastAPI()
    register_setup_routes(app, api_auth=lambda request: None, config_manager=None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    assert ("/api/setup/channels", "GET") in live
    assert ("/api/setup/channels/{channel}", "POST") in live
    assert ("/api/setup/checklist", "GET") in live


def test_dashboard_wires_onboarding_to_checklist():
    """P2-2：概览页接入首次使用引导（拉 checklist + 容器 + 函数）。"""
    import pathlib
    src = pathlib.Path(
        "src/web/templates/workspace_dashboard.html").read_text(encoding="utf-8")
    assert 'id="db-onboard"' in src
    assert "loadOnboard" in src
    assert "/api/setup/checklist" in src
    # 红灯不可忽略：dismiss 仅对 yellow 生效
    assert "d.light==='yellow'" in src


def test_golive_checklist_red_when_ai_missing():
    """P2-1：AI 未配置 → 硬性 fail → 红灯，不可上线。"""
    from src.utils.golive import build_checklist
    out = build_checklist(
        config={"ai": {}},
        channel_statuses=[],
        config_errors=0, config_warnings=0,
        kb_ready={"available": True, "is_cold": True, "enabled_entries": 0},
        online_agents=0)
    assert out["light"] == "red"
    assert out["ready"] is False
    ai = next(c for c in out["checks"] if c["id"] == "ai")
    assert ai["status"] == "fail"


def test_golive_checklist_green_when_all_ready():
    """P2-1：AI+渠道+配置齐、KB 非空、坐席在线 → 绿灯。"""
    from src.utils.golive import build_checklist
    out = build_checklist(
        config={"ai": {"provider": "openai_compatible", "api_key": "sk-real"}},
        channel_statuses=[{"id": "web", "name": "网页", "ready": True,
                           "configured": True}],
        config_errors=0, config_warnings=0,
        kb_ready={"available": True, "is_cold": False, "enabled_entries": 12},
        online_agents=2)
    assert out["light"] == "green"
    assert out["ready"] is True
    assert out["summary"]["fail"] == 0
    assert out["summary"]["warn"] == 0


def test_golive_checklist_yellow_when_only_soft_gaps():
    """P2-1：硬性满足但 KB 冷 / 无坐席 → 黄灯，仍可上线。"""
    from src.utils.golive import build_checklist
    out = build_checklist(
        config={"ai": {"provider": "ollama", "api_key": "ollama"}},
        channel_statuses=[{"id": "telegram", "name": "Telegram",
                           "ready": True, "configured": True}],
        config_errors=0, config_warnings=2,
        kb_ready={"available": True, "is_cold": True, "enabled_entries": 1},
        online_agents=0)
    assert out["light"] == "yellow"
    assert out["ready"] is True   # 无 fail → 可上线
    assert out["summary"]["fail"] == 0
    assert out["summary"]["warn"] >= 2


def test_channel_setup_status_detects_missing_and_filled():
    """P1-1：channel_status 正确判定缺项/已填，密钥打码回显。"""
    from src.utils.channel_setup import channel_status
    cfg = {
        "telegram": {"enabled": True, "api_id": 12345,
                     "api_hash": "abcdef0123456789", "phone_number": ""},
        "line": {"enabled": False, "channel_access_token": "YOUR_TOKEN"},
    }
    st = {c["id"]: c for c in channel_status(cfg)}
    tg = st["telegram"]
    assert tg["enabled"] is True
    api_hash = next(f for f in tg["fields"] if f["key"] == "telegram.api_hash")
    assert api_hash["filled"] is True
    assert "•" in api_hash["display"]  # 密钥打码
    assert tg["missing"] == []         # api_id/api_hash 齐，phone 可选
    # line：token 是占位符 → 缺项
    line = st["line"]
    assert line["configured"] is False
    assert any("Token" in m for m in line["missing"])
    # web：无字段渠道，启用即就绪
    web = st["web"]
    assert web["fields"] == []


def test_channel_setup_apply_only_known_fields():
    """P1-1：apply_channel_values 只认声明字段、类型强转、自动启用。"""
    from src.utils.channel_setup import apply_channel_values
    overlay = {}
    ok, msg = apply_channel_values(overlay, "telegram", {
        "api_id": "98765", "api_hash": "deadbeefcafe",
        "evil_key": "should_be_ignored",  # 未声明 → 忽略
    })
    assert ok, msg
    assert overlay["telegram"]["api_id"] == 98765      # str→int 强转
    assert overlay["telegram"]["api_hash"] == "deadbeefcafe"
    assert overlay["telegram"]["enabled"] is True      # 自动启用
    assert "evil_key" not in overlay["telegram"]       # 注入被拦
    # 类型错误：api_id 非数字 → 报错
    ok2, msg2 = apply_channel_values({}, "telegram", {"api_id": "not_a_number"})
    assert ok2 is False
    # 未知渠道
    ok3, _ = apply_channel_values({}, "nope", {"x": "y"})
    assert ok3 is False


async def test_config_manager_overlay_merge_and_save(tmp_path):
    """P1-1：save_channel_credentials 写 overlay + 深合并，主配置注释不被改写。"""
    from src.utils.config_manager import ConfigManager
    main = tmp_path / "config.yaml"
    main.write_text(
        "# 这是注释，必须保留\n"
        "telegram:\n  enabled: false\n  api_id: 111\n  api_hash: 'orig_hash'\n"
        "  phone_number: '+100'\n"
        "ai:\n  api_key: 'k'\n  provider: 'openai_compatible'\n  base_url: 'u'\n  model: 'm'\n"
        "skills: {}\n",
        encoding="utf-8")
    cm = ConfigManager(str(main))
    assert await cm.load()
    ok, msg, _ = cm.save_channel_credentials(
        "telegram", {"api_id": "555", "api_hash": "h0h0h0h0h0"})
    assert ok, msg
    # 主配置注释/原值未被改写
    assert "# 这是注释，必须保留" in main.read_text(encoding="utf-8")
    # overlay 文件已生成且含凭证
    overlay = tmp_path / "config.local.yaml"
    assert overlay.exists()
    assert "555" in overlay.read_text(encoding="utf-8")
    # 内存配置即时生效（深合并覆盖）
    assert cm.config["telegram"]["api_id"] == 555
    assert cm.config["telegram"]["enabled"] is True
    # 重新加载仍生效（overlay 在 load 时合并）
    cm2 = ConfigManager(str(main))
    assert await cm2.load()
    assert cm2.config["telegram"]["api_id"] == 555


def test_translate_routes_slice40_registers_contract():
    """巨石拆分 slice 40：register_translate_routes 合并文本+媒体翻译端点，路径/方法与基线一致。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_translate_routes import register_translate_routes
    app = FastAPI()
    register_translate_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/unified-inbox/translate", "POST"),
        ("/api/unified-inbox/translation-engines", "GET"),
        ("/api/unified-inbox/translate-image", "POST"),
        ("/api/unified-inbox/translate-voice", "POST"),
        ("/api/unified-inbox/translate-message-media", "POST"),
    }
    assert expected <= live, f"翻译路由域端点缺失：{expected - live}"


def test_conversion_outreach_routes_slice32_registers_contract():
    """巨石拆分 slice 32：register_conversion_outreach_routes 挂载转化标记 + 批量触达端点，
    路径/方法与基线一致（mark-conversion POST、outreach preview/execute POST、batch GET）。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_conversion_outreach_routes import (
        register_conversion_outreach_routes,
    )
    app = FastAPI()
    register_conversion_outreach_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/unified-inbox/mark-conversion", "POST"),
        ("/api/unified-inbox/outreach/preview", "POST"),
        ("/api/unified-inbox/outreach/execute", "POST"),
        ("/api/unified-inbox/outreach/batch", "GET"),
    }
    assert expected <= live, f"转化/触达路由域端点缺失：{expected - live}"


def test_desktop_routes_slice33_registers_contract():
    """巨石拆分 slice 33：register_desktop_routes 挂载桌面壳 smart-reply/guard-check/ingest，
    路径/方法与基线一致（三端点均 POST + api_auth）。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_desktop_routes import register_desktop_routes
    app = FastAPI()
    register_desktop_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/desktop/smart-reply", "POST"),
        ("/api/desktop/guard-check", "POST"),
        ("/api/desktop/ingest", "POST"),
    }
    assert expected <= live, f"桌面壳路由域端点缺失：{expected - live}"


def test_analyze_routes_slice34_registers_contract():
    """巨石拆分 slice 34：register_analyze_routes 挂载 AI 分析 + 会话画像端点，
    路径/方法与基线一致（analyze POST + api_auth，profile GET + 内联 api_auth）。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_analyze_routes import register_analyze_routes
    app = FastAPI()
    register_analyze_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/unified-inbox/analyze", "POST"),
        ("/api/unified-inbox/profile", "GET"),
    }
    assert expected <= live, f"AI 分析/画像路由域端点缺失：{expected - live}"


def test_realtime_routes_slice36_registers_contract():
    """巨石拆分 slice 36：register_realtime_routes 挂载 SSE + typing 端点，
    路径/方法与基线一致（stream GET + typing POST）。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_realtime_routes import register_realtime_routes
    app = FastAPI()
    register_realtime_routes(app, api_auth=lambda request: None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/workspace/stream", "GET"),
        ("/api/workspace/typing", "POST"),
    }
    assert expected <= live, f"协作实时路由域端点缺失：{expected - live}"


def test_aux_read_routes_slice37a_registers_contract():
    """巨石拆分 slice 37a：register_aux_read_routes 挂载辅助读路径端点，
    路径/方法与基线一致（templates/search-messages/kb-search 均 GET + 内联 api_auth）。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_aux_read_routes import register_aux_read_routes
    app = FastAPI()
    register_aux_read_routes(app, api_auth=lambda request: None, config_manager=None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/unified-inbox/templates", "GET"),
        ("/api/unified-inbox/search-messages", "GET"),
        ("/api/unified-inbox/kb-search", "GET"),
    }
    assert expected <= live, f"辅助读路径端点缺失：{expected - live}"


def test_read_routes_slice37b_registers_contract():
    """巨石拆分 slice 37b：register_read_routes 挂载主读路径端点，
    路径/方法与基线一致（chats/thread 均 GET + 内联 api_auth）。"""
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_read_routes import register_read_routes
    app = FastAPI()
    register_read_routes(app, api_auth=lambda request: None, config_manager=None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    expected = {
        ("/api/unified-inbox/chats", "GET"),
        ("/api/unified-inbox/thread", "GET"),
    }
    assert expected <= live, f"主读路径端点缺失：{expected - live}"


def test_enrich_outbound_originals_downsunk_to_aggregate():
    """slice 37b 配套：_enrich_outbound_originals 在 aggregate（thread 读路径富集）。"""
    from src.web.routes.unified_inbox_aggregate import _enrich_outbound_originals
    assert callable(_enrich_outbound_originals)


def test_enrich_outbound_originals_attaches_agent_original(tmp_path):
    """P1：thread 富集把中文原文挂到出向消息（agent_original/agent_xlate），入向不受影响。"""
    from src.inbox.store import InboxStore
    from src.web.routes.unified_inbox_aggregate import _enrich_outbound_originals
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:line-a:line-room"
    store.record_outbound_translation(
        cid, sent_text="你好朋友", original_text="hello friend",
        target_lang="zh", provider="ai")

    class _State:
        inbox_store = store

    class _App:
        state = _State()

    class _Req:
        app = _App()

    msgs = [{"direction": "out", "text": "你好朋友"}, {"direction": "in", "text": "hi"}]
    _enrich_outbound_originals(_Req, cid, msgs)
    assert msgs[0]["agent_original"] == "hello friend"
    assert msgs[0]["agent_xlate"]["target_lang"] == "zh"
    assert "agent_original" not in msgs[1]


def test_send_target_auto_unknown_language_sends_original():
    c = _client()
    c.app.state.inbox_store = _LangStore(language="unknown")
    resp = c.post(
        "/api/unified-inbox/send",
        json={
            "platform": "line", "account_id": "line-a", "chat_key": "line-room",
            "text": "hello", "target_lang": "auto",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sent_text"] == "hello"
    assert data["translation"] is None


def test_unified_inbox_template_contains_translation_controls():
    """重构后三栏布局：翻译控件、AI草稿、会话列表等核心功能校验。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    # 翻译控件（P55：双向翻译条 xlate-in/out + xlateMsg）
    assert "xlate-in" in html
    assert "xlate-out" in html
    assert "xlateMsg" in html
    assert "翻译" in html
    # A：发送翻译目标支持「自动（客户语言）」——前端解析为会话 language 后复用两击预览
    assert 'value="auto"' in html
    assert "自动（客户语言）" in html
    # 平台导航（三栏布局核心）
    assert "nav-rail" in html
    assert "conv-items" in html
    assert "chat-panel" in html
    # AI 草稿
    assert "toggleAiDraft" in html
    assert "draft-panel" in html
    # 回复功能
    assert "reply-textarea" in html
    assert "sendMsg" in html
    # 账号管理抽屉
    assert "account-drawer" in html
    assert "openDrawer" in html


def test_unified_inbox_template_contains_oneclick_outbound_translation():
    """P0：出向一击直发 —— 发送前预览开关 + sendMsg 把 target_lang 传给后端 + 出向原文副行。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    # 发送前预览开关（默认关＝一击直发）
    assert "xlate-out-preview" in html
    assert "_onXlatePreviewToggle" in html
    assert "_outPreview" in html
    # 一击直发：sendMsg 在非预览模式把 target_lang 交后端翻译
    assert "_serverXlate" in html
    assert "body.target_lang=_outLang" in html
    # 出向「原文 → 译文」映射 + 副行展示
    assert "_outOrigMap" in html
    assert "msg-orig-sub" in html
    # 翻译质量徽章（降级/失败时提示译文可能不准）
    assert "xl-warn-badge" in html
    assert "_xlWarnBadge" in html
    # P1：服务端持久出向原文副行（跨刷新/重启）优先于会话级内存映射
    assert "agent_original" in html
    assert "agent_xlate" in html
    # P1-2：'auto' 交服务端统一解析（预览与一击同源），前端读 resolved_target 回落
    assert "resolved_target" in html
    assert "lang==='auto'" in html


def test_unified_inbox_translation_tokens_no_theme_drift():
    """P0-P3 美化：翻译链路语义色全部 token 化（亮/暗双块定义），防止再退回写死色造成暗色失真。

    历史 bug：翻译模块的靛蓝/紫/红字面量散落、无暗色覆盖 → 暗色下深字压深底看不清。
    收口为 --xl-* token 后，本哨兵锁定『双块都定义 + 控件用 var() + 无写死报错红』，
    任何回退（重新引入 style="color:#xxxxxx" 或删掉暗色块）都会在 CI 被点名。
    """
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    # 亮 + 暗双块都必须定义这些语义 token（出现 >=2 次＝至少各一次）
    for tok in (
        "--xl-accent:",
        "--xl-trans-text:",
        "--xl-warn-text:",
        "--xl-err-text:",
        "--xl-badge-bg:",
        "--btn-border:",
    ):
        assert html.count(tok) >= 2, f"翻译 token {tok} 缺少亮或暗定义（防漂移）"
    # 暗色主题块本身存在
    assert '[data-cp-theme="dark"]' in html
    # 翻译报错文字不得再用写死深红内联（暗色下不可读）——必须走 .xl-err token 类
    assert 'style="color:#b91c1c;"' not in html
    assert ".xl-err{color:var(--xl-err-text)" in html
    # 关键控件确实引用 token 而非字面量
    assert ".xl-loading{color:var(--xl-accent)" in html
    assert "color:var(--xl-warn-text)" in html
    # P1：单次翻译工具分区 + 状态强语义胶囊
    assert "xl-tools-row" in html
    assert ".rt-xl-status.on{" in html
    # P2：多线路对照弹窗已卡片化 + 主题感知（不再写死白底）
    assert "xlcompare-card" in html
    assert "xlcompare-opt" in html
    # P3+：管理员认领从占满状态栏的横幅收敛为头部紧凑按钮（同款配色，亮暗双适配）
    assert 'id="claim-hdr-btn"' in html
    assert ".claim-hdr-btn.mine" in html
    assert ".claim-hdr-btn.other" in html
    # 方案A：AI 自动化档位收敛为单一入口（删除重复的「全自动·开」autopilot-btn），
    # mode-select 全自动档变绿 + 「待审」按档位智能显隐
    assert 'id="autopilot-btn"' not in html
    assert "function toggleAutopilot" not in html
    assert "_syncAutoUi" in html
    assert ".mode-select.auto-on" in html
    # 原生 confirm 弹窗已替换为同款主题确认框
    assert "function _appConfirm" in html
    # B-1/B-4：全自动安全条 + 暂停记忆原档
    assert 'id="auto-safety-bar"' in html
    assert "function pauseAutoMode" in html
    assert "function _loadAutoStats" in html
    assert "/api/unified-inbox/automation-stats" in html
    # B-2：风控可视——被拦截会话列表高亮 + 风险 chip + 进 diff 签名
    assert "conv-risk-chip" in html
    assert ".conv-item.risk-blocked" in html
    assert "c.risk_blocked" in html
    # 投递失败可视化：安全条/记录弹窗反映 autosend_failed
    assert "autosend_failed" in html
    assert "has-fail" in html
    assert ".autolog-row .act.fail" in html


def _client_with_auto_assign():
    """启用 auto_assign 的 chats API 客户端，预置一个在线坐席（内存 coordinator）。"""
    from src.workspace.agent_coordinator import AgentCoordinator

    class _CM:
        config = {"workspace": {"auto_assign": {"enabled": True}}}

    app = FastAPI()

    def page_auth(request: Request):
        return None

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app,
        page_auth=page_auth,
        api_auth=api_auth,
        templates=_Templates(),
        config_manager=_CM(),
    )
    app.state.line_rpa_services = [LineSvc()]
    app.state.whatsapp_rpa_services = []
    app.state.messenger_rpa_service = None
    app.state.telegram_client = None
    app.state.ai_client = FakeAI()
    coord = AgentCoordinator(store=None)
    coord.set_presence("alice", display_name="Alice", status="online")
    app.state.agent_coordinator = coord
    return TestClient(app)


def test_chats_attach_suggested_agent_when_auto_assign_enabled():
    c = _client_with_auto_assign()
    resp = c.get("/api/unified-inbox/chats")
    assert resp.status_code == 200, resp.text
    chats = resp.json()["chats"]
    assert chats, "应至少有一个 LINE 会话"
    sugg = [ch.get("suggested_agent") for ch in chats if ch.get("suggested_agent")]
    assert sugg, "启用 auto_assign 后未认领会话应附 suggested_agent"
    assert sugg[0]["agent_id"] == "alice"


def test_chats_no_suggested_agent_when_disabled():
    c = _client()  # config_manager=None → auto_assign 默认关
    resp = c.get("/api/unified-inbox/chats")
    assert resp.status_code == 200, resp.text
    chats = resp.json()["chats"]
    assert all("suggested_agent" not in ch for ch in chats)


def test_unified_inbox_template_contains_drafts_panel():
    """三栏重构：草稿面板使用内嵌 draft-panel 设计，包含审批 API 调用。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    # 核心：草稿面板存在
    assert "待审草稿" in html
    assert "draft-panel" in html
    # 草稿 API 调用
    assert "approveDraft" in html
    assert "rejectDraft" in html
    assert "/api/drafts/" in html and "/resolve" in html  # 草稿处置端点
    assert "risk-badge" in html or "risk-" in html  # 风险徽章（样式类）
    # 内嵌面板校验
    assert "loadDrafts" in html
    assert "draft-card-mini" in html or "draft-panel-items" in html


def test_unified_inbox_template_contains_assign_suggestion():
    """自动派单：会话列表「建议你接管」徽章 + 判定逻辑入模板。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    assert "_convSuggestMine" in html          # 判定函数
    assert "conv-suggest-chip" in html          # 徽章样式类
    assert "建议你接管" in html                 # 徽章文案
    assert "suggested_agent" in html            # 读取后端字段


def test_send_caps_endpoint_returns_flags():
    """send-caps：非 protocol 账号应返回不支持直发媒体/语音。"""
    c = _client()
    resp = c.get("/api/unified-inbox/send-caps?platform=line&account_id=line-a")
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["ok"] is True
    assert d["platform"] == "line"
    assert d["can_media"] is False
    assert d["can_voice"] is False


def test_unified_inbox_template_contains_send_caps_gating():
    """媒体/语音按钮按能力置灰的逻辑入模板。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    assert "_loadSendCaps" in html
    assert "_applySendCaps" in html
    assert "send-caps" in html
    assert "暂不支持收件箱直发媒体" in html
