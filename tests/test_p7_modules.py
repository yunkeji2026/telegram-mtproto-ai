"""P7 新模块单元测试 — ContextStore / PermissionManager / TemplateEngine / LogBuffer / PluginLoader"""

import asyncio
import tempfile
import time
import pytest
from pathlib import Path

# ── ContextStore ──────────────────────────────────────────────

from src.utils.context_store import ContextStore


class TestContextStore:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self.store = ContextStore(db_path=Path(self._tmpdir) / "ctx.db", ttl_days=30)

    def test_get_creates_default(self):
        ctx = self.store.get("user_1")
        assert ctx["user_id"] == "user_1"
        assert ctx["reply_count"] == 0
        assert ctx["stage"] == "start"

    def test_persist_and_restore(self):
        ctx = self.store.get("user_2")
        ctx["last_message"] = "hello world"
        ctx["reply_count"] = 5
        self.store.mark_dirty("user_2")
        self.store.flush("user_2")
        store2 = ContextStore(db_path=Path(self._tmpdir) / "ctx.db")
        ctx2 = store2.get("user_2")
        assert ctx2["last_message"] == "hello world"
        assert ctx2["reply_count"] == 5

    def test_non_persist_keys_excluded(self):
        ctx = self.store.get("user_3")
        ctx["_send_to_chat"] = lambda: None
        ctx["last_message"] = "test"
        self.store.mark_dirty("user_3")
        self.store.flush("user_3")
        store2 = ContextStore(db_path=Path(self._tmpdir) / "ctx.db")
        ctx2 = store2.get("user_3")
        assert "_send_to_chat" not in ctx2
        assert ctx2["last_message"] == "test"

    def test_eviction(self):
        store = ContextStore(db_path=Path(self._tmpdir) / "evict.db", max_memory=5)
        for i in range(10):
            ctx = store.get(f"u{i}")
            ctx["reply_count"] = i
            store.mark_dirty(f"u{i}")
        store.flush_all()
        assert len(store._cache) <= 5


# ── PermissionManager ────────────────────────────────────────

from src.utils.permissions import PermissionManager, ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_OPERATOR


class TestPermissionManager:
    def test_legacy_compat(self):
        cfg = {"telegram": {"quota_config_commands": {
            "enabled": True, "allowed_user_ids": [111, 222]
        }}}
        pm = PermissionManager(cfg)
        assert pm.get_role("111") == ROLE_SUPER_ADMIN
        assert pm.get_role("222") == ROLE_SUPER_ADMIN
        assert pm.get_role("999") is None

    def test_rbac_roles(self):
        cfg = {"telegram": {"quota_config_commands": {
            "enabled": True, "allowed_user_ids": [],
            "roles": {
                "super_admins": [100],
                "admins": [200],
                "operators": [300],
            }
        }}}
        pm = PermissionManager(cfg)
        assert pm.has_permission(100, "batch_modify") is True
        assert pm.has_permission(200, "batch_modify") is False
        assert pm.has_permission(200, "edit_template") is True
        assert pm.has_permission(300, "edit_template") is False
        assert pm.has_permission(300, "view_config") is True

    def test_check_method(self):
        cfg = {"telegram": {"quota_config_commands": {
            "enabled": True, "roles": {"operators": [400]}
        }}}
        pm = PermissionManager(cfg)
        assert pm.check(400, "view_config") is None
        assert pm.check(400, "batch_modify") == "insufficient_role"
        assert pm.check(999, "view_config") == "no_permission"

    def test_disabled(self):
        cfg = {"telegram": {"quota_config_commands": {"enabled": False}}}
        pm = PermissionManager(cfg)
        assert pm.enabled is False

    def test_role_display(self):
        cfg = {"telegram": {"quota_config_commands": {
            "enabled": True, "roles": {"admins": [500]}
        }}}
        pm = PermissionManager(cfg)
        assert "管理员" in pm.get_role_display(500)
        assert "无权限" in pm.get_role_display(999)


# ── TemplateEngine ────────────────────────────────────────────

from src.utils.template_engine import render_template, extract_variables, preview_template


class TestTemplateEngine:
    def test_basic_render(self):
        result = render_template("通道 {channel_name} 费率 {fee_rate}", {
            "channel_name": "EP", "fee_rate": "3.5%"
        })
        assert result == "通道 EP 费率 3.5%"

    def test_builtin_vars(self):
        result = render_template("当前时间: {date}")
        assert time.strftime("%Y-%m-%d") in result

    def test_missing_var_preserved(self):
        result = render_template("Hi {unknown_var}!", {})
        assert "{unknown_var}" in result

    def test_no_vars(self):
        result = render_template("普通文本没有变量")
        assert result == "普通文本没有变量"

    def test_extract_variables(self):
        vars = extract_variables("{a} is {b} and {c}")
        assert set(vars) == {"a", "b", "c"}

    def test_preview(self):
        result = preview_template("通道: {channel_name}, 费率: {fee_rate}")
        assert "EasyPaisa" in result
        assert "3.5%" in result


# ── LogBuffer ─────────────────────────────────────────────────

import logging
from src.utils.log_buffer import LogBuffer


class TestLogBuffer:
    def test_emit_and_get_recent(self):
        buf = LogBuffer(maxlen=10)
        logger = logging.getLogger("test_log_buffer")
        logger.addHandler(buf)
        logger.setLevel(logging.DEBUG)
        logger.info("test message 1")
        logger.warning("test message 2")
        recent = buf.get_recent(10)
        assert len(recent) >= 2
        assert any("test message 1" in e["message"] for e in recent)

    def test_buffer_overflow(self):
        buf = LogBuffer(maxlen=3)
        logger = logging.getLogger("test_overflow")
        logger.addHandler(buf)
        logger.setLevel(logging.DEBUG)
        for i in range(10):
            logger.info(f"msg {i}")
        recent = buf.get_recent(100)
        assert len(recent) == 3
        assert "msg 9" in recent[-1]["message"]


# ── PluginLoader ──────────────────────────────────────────────

from src.utils.plugin_loader import PluginLoader


class TestPluginLoader:
    def test_discover_empty(self):
        tmpdir = Path(tempfile.mkdtemp())
        loader = PluginLoader(tmpdir)
        assert loader.discover() == []

    def test_discover_skips_underscore(self):
        tmpdir = Path(tempfile.mkdtemp())
        (tmpdir / "_hidden.py").write_text("pass")
        (tmpdir / "visible.py").write_text("pass")
        loader = PluginLoader(tmpdir)
        found = loader.discover()
        assert "visible" in found
        assert "_hidden" not in found

    def test_list_plugins(self):
        tmpdir = Path(tempfile.mkdtemp())
        (tmpdir / "myplugin.py").write_text("pass")
        loader = PluginLoader(tmpdir, {"plugins": {"enabled": True, "disabled": ["myplugin"]}})
        info = loader.list_plugins()
        assert len(info) == 1
        assert info[0]["disabled"] is True
