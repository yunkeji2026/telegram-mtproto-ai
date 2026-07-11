"""P0-1 桌面首启向导契约（A1 最小种子 / A2 AI Key overlay 保存 / A3 引导条 / A5 就绪灯）。

覆盖：
- config.desktop.min.yaml 不变量：无 YOUR_* 占位、translation.engines.order=["ai"]、
  危险子系统不显式开启；
- ConfigManager：桌面模式播种优先最小种子（非桌面回落 example）、save_ai_credentials
  写 overlay（主 config 字节不动）+ 即时合并 + 白名单/校验；
- 路由：GET /api/setup/ai + POST /api/setup/ai-key 注册与行为（占位 key 拒绝、
  成功后 ai_ready 透传、golive AI 项一次修绿）;
- TranslationService.rebind_ai_client 热换绑;
- 模板 wiring：workspace 引导条（window 可达的 dismiss）+ setup_wizard AI 卡。
"""

import pathlib

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
MIN_SEED = REPO / "config" / "config.desktop.min.yaml"


# ── A1：最小种子不变量 ─────────────────────────────────────────────


class TestDesktopMinSeed:
    def test_exists_and_parses(self):
        assert MIN_SEED.exists(), "config/config.desktop.min.yaml 必须随仓库分发"
        data = yaml.safe_load(MIN_SEED.read_text(encoding="utf-8"))
        assert isinstance(data, dict) and data

    def test_no_placeholders(self):
        text = MIN_SEED.read_text(encoding="utf-8")
        assert "YOUR_" not in text, "最小种子不得含 YOUR_* 占位"

    def test_translation_shortest_path(self):
        data = yaml.safe_load(MIN_SEED.read_text(encoding="utf-8"))
        assert (data.get("translation") or {}).get("engines", {}).get("order") == ["ai"]
        ai = data.get("ai") or {}
        assert ai.get("provider") == "openai_compatible"
        assert str(ai.get("api_key") or "") == ""  # key 留空由向导写 overlay

    def test_dangerous_subsystems_not_enabled(self):
        data = yaml.safe_load(MIN_SEED.read_text(encoding="utf-8"))
        for key in ("line_rpa", "messenger_rpa", "whatsapp_rpa", "contacts",
                    "platform_login", "monetization", "companion", "protocol"):
            sub = data.get(key)
            if sub is None:
                continue  # 未列出 = 走代码默认（关）
            assert not (isinstance(sub, dict) and sub.get("enabled")), (
                f"最小种子不得显式开启 {key}")

    def test_required_sections_for_validation(self):
        data = yaml.safe_load(MIN_SEED.read_text(encoding="utf-8"))
        for section in ("telegram", "ai", "skills"):
            assert section in data, f"_validate_config 必需段缺失: {section}"

    def test_packaged_as_seed_in_build_script(self):
        build = (REPO / "desktop" / "build" / "build_backend.py").read_text(encoding="utf-8")
        assert "config.desktop.min.yaml" in build, "build_backend.py 须把最小种子打进包"
        assert "config.example.yaml" in build, "完整 example 仍应随包（回落/参考）"


# ── A1：播种优先级（桌面模式→min；否则→example） ───────────────────


class TestSeedPreference:
    def _mgr(self, tmp_path, monkeypatch, desktop: bool):
        from src.utils.config_manager import ConfigManager
        target = tmp_path / "cfg" / "config.yaml"
        monkeypatch.setenv("AITR_CONFIG_PATH", str(target))
        if desktop:
            monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
        else:
            monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
        ConfigManager()  # 构造即播种
        return target

    def test_desktop_mode_seeds_min(self, tmp_path, monkeypatch):
        target = self._mgr(tmp_path, monkeypatch, desktop=True)
        assert target.exists()
        text = target.read_text(encoding="utf-8")
        assert "YOUR_" not in text
        data = yaml.safe_load(text)
        assert (data.get("translation") or {}).get("engines", {}).get("order") == ["ai"]

    def test_server_mode_seeds_example(self, tmp_path, monkeypatch):
        target = self._mgr(tmp_path, monkeypatch, desktop=False)
        assert target.exists()
        assert "YOUR_" in target.read_text(encoding="utf-8")  # example 的占位仍在


# ── A2：save_ai_credentials 写 overlay ────────────────────────────


class TestSaveAiCredentials:
    def _mgr(self, tmp_path):
        from src.utils.config_manager import ConfigManager
        cfg = tmp_path / "config.yaml"
        cfg.write_text("# 主配置注释必须保留\nai:\n  api_key: \"\"\n", encoding="utf-8")
        m = ConfigManager(str(cfg))
        m.config = {"ai": {"api_key": ""}}
        return m, cfg

    def test_writes_overlay_not_main_config(self, tmp_path):
        m, cfg = self._mgr(tmp_path)
        before = cfg.read_bytes()
        ok, msg = m.save_ai_credentials({"api_key": "sk-test-123", "base_url": "https://api.deepseek.com"})
        assert ok, msg
        assert cfg.read_bytes() == before, "主 config.yaml 不得被改写（注释永续）"
        overlay = tmp_path / "config.local.yaml"
        assert overlay.exists()
        data = yaml.safe_load(overlay.read_text(encoding="utf-8"))
        assert data["ai"]["api_key"] == "sk-test-123"
        assert data["ai"]["base_url"] == "https://api.deepseek.com"

    def test_merges_into_live_config(self, tmp_path):
        m, _ = self._mgr(tmp_path)
        m.save_ai_credentials({"api_key": "sk-live"})
        assert m.config["ai"]["api_key"] == "sk-live"

    def test_partial_update_skips_empty(self, tmp_path):
        m, _ = self._mgr(tmp_path)
        m.save_ai_credentials({"api_key": "sk-1", "model": "deepseek-chat"})
        ok, _ = m.save_ai_credentials({"api_key": "sk-2", "model": ""})
        assert ok
        overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
        assert overlay["ai"]["api_key"] == "sk-2"
        assert overlay["ai"]["model"] == "deepseek-chat"  # 空值不清掉已存字段

    def test_rejects_empty_and_unknown_provider(self, tmp_path):
        m, _ = self._mgr(tmp_path)
        ok, _ = m.save_ai_credentials({})
        assert not ok
        ok, msg = m.save_ai_credentials({"api_key": "sk", "provider": "bogus"})
        assert not ok and "provider" in msg

    def test_whitelist_blocks_arbitrary_keys(self, tmp_path):
        m, _ = self._mgr(tmp_path)
        m.save_ai_credentials({"api_key": "sk", "evil": "1", "system_prompt": "x"})
        overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
        assert "evil" not in overlay["ai"] and "system_prompt" not in overlay["ai"]


# ── A2：路由注册 + 行为 ───────────────────────────────────────────


def _build_app(config_manager):
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_setup_routes import register_setup_routes
    app = FastAPI()
    register_setup_routes(app, api_auth=lambda request: None,
                          config_manager=config_manager)
    return app


def test_setup_ai_routes_registered():
    app = _build_app(None)
    live = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), m))
    assert ("/api/setup/ai", "GET") in live
    assert ("/api/setup/ai-key", "POST") in live


class TestAiKeyEndpoint:
    def _client_mgr(self, tmp_path):
        from fastapi.testclient import TestClient
        from src.utils.config_manager import ConfigManager
        cfg = tmp_path / "config.yaml"
        cfg.write_text("ai:\n  api_key: \"\"\n", encoding="utf-8")
        m = ConfigManager(str(cfg))
        m.config = {"ai": {"api_key": "", "provider": "openai_compatible",
                           "base_url": "https://api.deepseek.com", "model": "deepseek-chat"}}
        return TestClient(_build_app(m)), m

    def test_status_masks_key(self, tmp_path):
        client, m = self._client_mgr(tmp_path)
        r = client.get("/api/setup/ai").json()
        assert r["ok"] is True and r["configured"] is False
        m.config["ai"]["api_key"] = "sk-abcdefghijklmnop"
        r = client.get("/api/setup/ai").json()
        assert r["configured"] is True
        assert "sk-abcdefghijklmnop" not in str(r), "完整 key 不得回显"
        assert r["api_key_masked"].startswith("sk-a") and "…" in r["api_key_masked"]

    def test_placeholder_key_rejected(self, tmp_path):
        client, _ = self._client_mgr(tmp_path)
        for bad in ("", "  ", "YOUR_API_KEY"):
            r = client.post("/api/setup/ai-key", json={"api_key": bad}).json()
            assert r["ok"] is False and r.get("detail")

    def test_save_persists_and_reports_ready(self, tmp_path, monkeypatch):
        import src.web.routes.unified_inbox_setup_routes as mod
        client, m = self._client_mgr(tmp_path)

        async def _fake_reload(app, cm):
            return True

        monkeypatch.setattr(mod, "reload_ai_runtime", _fake_reload)
        r = client.post("/api/setup/ai-key", json={
            "api_key": "sk-good", "base_url": "https://api.deepseek.com"}).json()
        assert r["ok"] is True and r["ai_ready"] is True
        overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
        assert overlay["ai"]["api_key"] == "sk-good"
        # golive AI 项一次修绿：保存后 checklist 的 ai check 变 ok
        from src.utils.golive import build_checklist
        out = build_checklist(config=m.config, channel_statuses=[],
                              config_errors=0, config_warnings=0,
                              kb_ready={"available": True, "is_cold": False,
                                        "enabled_entries": 5},
                              online_agents=1)
        ai = next(c for c in out["checks"] if c["id"] == "ai")
        assert ai["status"] == "ok"

    def test_reload_failure_reports_not_ready_but_saved(self, tmp_path, monkeypatch):
        import src.web.routes.unified_inbox_setup_routes as mod
        client, _ = self._client_mgr(tmp_path)

        async def _fake_reload(app, cm):
            return False

        monkeypatch.setattr(mod, "reload_ai_runtime", _fake_reload)
        r = client.post("/api/setup/ai-key", json={"api_key": "sk-unverified"}).json()
        assert r["ok"] is True and r["ai_ready"] is False
        assert (tmp_path / "config.local.yaml").exists()


# ── 守卫：session 用户须主管；纯 Bearer（桌面壳）放行 ────────────────


class TestSupervisorOrShellGuard:
    def _req(self, session):
        # 类体不参与闭包作用域 → 属性在实例上挂（session=None 模拟纯 Bearer 无 session）
        class _Req:
            pass
        r = _Req()
        r.scope = {"session": session} if session is not None else {}
        r.session = session or {}
        return r

    def test_bearer_only_passes(self):
        from src.web.routes.unified_inbox_setup_routes import _require_supervisor_or_shell
        _require_supervisor_or_shell(self._req(None))  # 不抛 = 放行

    def test_agent_session_blocked(self):
        from fastapi import HTTPException
        from src.web.routes.unified_inbox_setup_routes import _require_supervisor_or_shell
        with pytest.raises(HTTPException):
            _require_supervisor_or_shell(self._req({"user_id": 7, "role": "agent"}))

    def test_master_session_passes(self):
        from src.web.routes.unified_inbox_setup_routes import _require_supervisor_or_shell
        _require_supervisor_or_shell(self._req({"user_id": 1, "role": "master"}))


# ── 热换绑：TranslationService.rebind_ai_client ────────────────────


def test_translation_service_rebind_ai_client():
    from src.ai.translation_service import TranslationService

    class _Client:
        async def chat(self, *a, **k):
            return ""

    old, new = _Client(), _Client()
    svc = TranslationService(ai_client=old)
    svc.rebind_ai_client(new)
    assert svc.ai_client is new
    ai_engines = [e for e in svc._router._engines if getattr(e, "name", "") == "ai"]
    assert ai_engines and all(e._ai is new for e in ai_engines)


# ── A3/A5：模板与桌面壳 wiring（静态字符串契约） ────────────────────


class TestFrontendWiring:
    def test_workspace_base_guide_bar(self):
        src = (REPO / "src" / "web" / "templates" / "workspace_base.html").read_text(encoding="utf-8")
        assert 'id="ws-aiguide"' in src
        assert "ai_key_missing" in src
        assert "function _wsAiGuideDismiss()" in src  # 顶层声明 = window 可达
        assert "/workspace/setup#ai" in src

    def test_page_ctx_exposes_ai_key_missing(self):
        src = (REPO / "src" / "web" / "routes" / "unified_inbox_workspace_pages_routes.py").read_text(encoding="utf-8")
        assert "ai_key_missing" in src and "_is_placeholder" in src

    def test_setup_wizard_ai_card(self):
        src = (REPO / "src" / "web" / "templates" / "setup_wizard.html").read_text(encoding="utf-8")
        assert 'id="ai"' in src  # 引导条深链锚点
        assert "/api/setup/ai-key" in src and "/api/setup/test-ai" in src
        assert "function swAiSave(" in src and "function swAiTest(" in src

    def test_first_run_wizard_wiring(self):
        fr = (REPO / "desktop" / "renderer" / "first-run.js").read_text(encoding="utf-8")
        assert "setupTestAi" in fr and "setupSaveAiKey" in fr and "setupAiStatus" in fr
        idx = (REPO / "desktop" / "renderer" / "index.html").read_text(encoding="utf-8")
        assert "first-run-model.js" in idx
        pre = (REPO / "desktop" / "shell-preload.js").read_text(encoding="utf-8")
        for ch in ("desktop:setup-ai-status", "desktop:setup-test-ai", "desktop:setup-save-ai-key"):
            assert ch in pre
        mainjs = (REPO / "desktop" / "main.js").read_text(encoding="utf-8")
        assert "/api/setup/ai-key" in mainjs and "/api/setup/test-ai" in mainjs
