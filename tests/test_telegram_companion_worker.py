"""N 线 核心4：统一运行时（协议号跑 A 线丰富 client）骨架单测。

全程用假 TelegramClient / 假凭据，无需真 Telegram 号、不连网络：
- A 线 initialize() 的 session-based 放宽（有 session_string/session 文件 → 免 phone）；
- A 线 start(block=False) 编排器托管模式（不进 idle、即返回）；
- TelegramCompanionWorker 的上下文注入、account_cfg 组装、start/send/stop/healthy/status；
- ensure_builtin_workers 按 companion_runtime flag 选 A 线 worker / B 线薄 worker。
"""
import asyncio
import types

import pytest


def _ensure_event_loop() -> None:
    """pyrogram sync wrap 在 import 时会调 asyncio.get_event_loop()，裸 MainThread 需先备好 loop。"""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


# ── 假 A 线 TelegramClient ────────────────────────────────────────────────────

class _FakeTelegramClient:
    last = None

    def __init__(self, config, skill_manager, ai_client=None, account_cfg=None):
        self.config = config
        self.skill_manager = skill_manager
        self.ai_client = ai_client
        self.account_cfg = account_cfg or {}
        self.running = False
        self.client = None
        self.initialized = False
        self.start_block = None
        self.stopped = False
        self.sent = []
        self._init_ret = True
        _FakeTelegramClient.last = self

    async def initialize(self):
        self.initialized = True
        return self._init_ret

    async def start(self, block=True):
        self.start_block = block
        self.running = True
        self.client = types.SimpleNamespace(is_connected=True)

    async def stop(self):
        self.stopped = True
        self.running = False

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


def _patch_fake_client(monkeypatch):
    _ensure_event_loop()
    import src.client.telegram_client as tcmod
    _FakeTelegramClient.last = None
    monkeypatch.setattr(tcmod, "TelegramClient", _FakeTelegramClient)


# ── flag / 上下文 ────────────────────────────────────────────────────────────

def test_companion_runtime_flag():
    from src.integrations.telegram_companion_worker import companion_runtime_enabled
    assert companion_runtime_enabled(None) is False
    assert companion_runtime_enabled({}) is False
    assert companion_runtime_enabled(
        {"platform_login": {"telegram": {"companion_runtime": True}}}) is True
    assert companion_runtime_enabled(
        {"platform_login": {"telegram": {"companion_runtime": False}}}) is False


def test_context_set_get_reset():
    from src.integrations.telegram_companion_worker import (
        companion_context_ready, get_companion_context, reset_companion_context,
        set_companion_context,
    )
    reset_companion_context()
    try:
        assert companion_context_ready() is False
        set_companion_context(config_manager="CM", skill_manager="SM", ai_client="AI")
        assert companion_context_ready() is True
        ctx = get_companion_context()
        assert ctx["config_manager"] == "CM"
        assert ctx["skill_manager"] == "SM"
        assert ctx["ai_client"] == "AI"
    finally:
        reset_companion_context()
    assert companion_context_ready() is False


# ── worker account_cfg 组装 ──────────────────────────────────────────────────

def test_account_cfg_assembles_session_and_proxy_and_personas():
    from src.integrations.telegram_companion_worker import TelegramCompanionWorker
    w = TelegramCompanionWorker(
        {"account_id": "u123", "label": "甜心", "proxy_id": "px-1",
         "meta": {"session_name": "tg_login_abc", "persona_ids": ["p1", "p2"]}},
        {},
    )
    cfg = w._account_cfg()
    assert cfg["account_id"] == "u123"
    assert cfg["account_label"] == "甜心"
    assert cfg["proxy_id"] == "px-1"
    assert cfg["session_name"] == "tg_login_abc"
    assert cfg["persona_ids"] == ["p1", "p2"]
    assert "session_string" not in cfg  # meta 无则不带
    assert cfg["mirror_inbox"] is True  # N4b：协议号默认镜像进收件箱


# ── worker 生命周期（用假 client） ───────────────────────────────────────────

async def test_worker_start_requires_context(monkeypatch):
    from src.integrations.telegram_companion_worker import (
        TelegramCompanionWorker, reset_companion_context,
    )
    reset_companion_context()
    w = TelegramCompanionWorker(
        {"account_id": "u1", "meta": {"session_name": "s"}}, {})
    with pytest.raises(RuntimeError):
        await w.start()


async def test_worker_start_requires_session(monkeypatch):
    from src.integrations.telegram_companion_worker import (
        TelegramCompanionWorker, reset_companion_context, set_companion_context,
    )
    reset_companion_context()
    set_companion_context(config_manager="CM", skill_manager="SM")
    try:
        w = TelegramCompanionWorker({"account_id": "u1", "meta": {}}, {})
        with pytest.raises(RuntimeError):
            await w.start()
    finally:
        reset_companion_context()


async def test_worker_start_builds_aline_client(monkeypatch):
    _patch_fake_client(monkeypatch)
    from src.integrations.telegram_companion_worker import (
        TelegramCompanionWorker, reset_companion_context, set_companion_context,
    )
    set_companion_context(config_manager="CM", skill_manager="SM", ai_client="AI")
    try:
        w = TelegramCompanionWorker(
            {"account_id": "u1", "proxy_id": "px",
             "meta": {"session_name": "tg_login_x", "persona_ids": ["p"]}}, {})
        await w.start()
        assert w.state == "running"
        fc = _FakeTelegramClient.last
        assert fc is not None
        assert fc.config == "CM" and fc.skill_manager == "SM" and fc.ai_client == "AI"
        assert fc.account_cfg["session_name"] == "tg_login_x"
        assert fc.account_cfg["proxy_id"] == "px"
        assert fc.account_cfg["mirror_inbox"] is True  # N4b
        assert fc.initialized is True
        assert fc.start_block is False  # 编排器托管：非阻塞启动
        assert await w.healthy() is True
    finally:
        reset_companion_context()


async def test_worker_start_raises_when_init_fails(monkeypatch):
    _patch_fake_client(monkeypatch)
    import src.client.telegram_client as tcmod

    class _FailInit(_FakeTelegramClient):
        async def initialize(self):
            return False

    monkeypatch.setattr(tcmod, "TelegramClient", _FailInit)
    from src.integrations.telegram_companion_worker import (
        TelegramCompanionWorker, reset_companion_context, set_companion_context,
    )
    set_companion_context(config_manager="CM", skill_manager="SM")
    try:
        w = TelegramCompanionWorker(
            {"account_id": "u1", "meta": {"session_name": "s"}}, {})
        with pytest.raises(RuntimeError):
            await w.start()
        assert w.client is None
    finally:
        reset_companion_context()


async def test_worker_send_stop_status(monkeypatch):
    _patch_fake_client(monkeypatch)
    from src.integrations.telegram_companion_worker import (
        TelegramCompanionWorker, reset_companion_context, set_companion_context,
    )
    set_companion_context(config_manager="CM", skill_manager="SM")
    try:
        w = TelegramCompanionWorker(
            {"account_id": "u1", "meta": {"session_name": "s"}}, {})
        await w.start()
        res = await w.send("12345", "你好呀")
        assert res["delivered"] is True
        assert _FakeTelegramClient.last.sent == [(12345, "你好呀")]  # 数字 chat_key 转 int

        res2 = await w.send("@channel", "hi")
        assert _FakeTelegramClient.last.sent[-1] == ("@channel", "hi")  # 非数字保持原样

        st = w.status()
        assert st["type"] == "telegram_companion"
        assert st["session"] == "s"
        assert st["account_id"] == "u1"

        await w.stop()
        assert w.state == "stopped"
        assert w.client is None
        assert await w.healthy() is False
    finally:
        reset_companion_context()


async def test_worker_send_returns_real_msg_id(monkeypatch):
    """P4-4：client 暴露 send_message_return_id 时，worker.send 回带真实 message.id
    （供已读回执双勾精确绑定）；缺该方法则优雅回落只回 bool、id 为空。"""
    from src.integrations.telegram_companion_worker import TelegramCompanionWorker

    class _CliWithId:
        async def send_message_return_id(self, chat_id, text):
            return True, "9911"

    class _CliLegacy:
        async def send_message(self, chat_id, text):
            return True

    w = TelegramCompanionWorker({"account_id": "u1", "meta": {}}, {})
    w.client = _CliWithId()
    res = await w.send("12345", "hi")
    assert res == {"delivered": True, "message_id": "9911"}

    w.client = _CliLegacy()
    res2 = await w.send("12345", "hi")
    assert res2 == {"delivered": True, "message_id": ""}


# ── A 线 initialize/start 改造 ───────────────────────────────────────────────

async def test_initialize_session_string_skips_phone(monkeypatch):
    _ensure_event_loop()
    import src.client.telegram_client as tcmod
    created = {}

    class _FakeClient:
        def __init__(self, **kw):
            created.update(kw)

    monkeypatch.setattr(tcmod, "PYROGRAM_AVAILABLE", True)
    monkeypatch.setattr(tcmod, "Client", _FakeClient)
    obj = tcmod.TelegramClient.__new__(tcmod.TelegramClient)
    obj.api_id = 1
    obj.api_hash = "h"
    obj.phone_number = None
    obj.session_string = "SESSIONSTR"
    obj.session_name = "n"
    obj.proxy_id = ""
    obj.client = None
    ok = await obj.initialize()
    assert ok is True
    assert created.get("session_string") == "SESSIONSTR"
    assert "phone_number" not in created


async def test_initialize_requires_phone_or_session(monkeypatch):
    _ensure_event_loop()
    import src.client.telegram_client as tcmod
    monkeypatch.setattr(tcmod, "PYROGRAM_AVAILABLE", True)
    obj = tcmod.TelegramClient.__new__(tcmod.TelegramClient)
    obj.api_id = 1
    obj.api_hash = "h"
    obj.phone_number = None
    obj.session_string = ""
    obj.session_name = "definitely_no_such_session_xyz_987"
    obj.proxy_id = ""
    obj.client = None
    ok = await obj.initialize()
    assert ok is False


async def test_start_block_false_returns_without_idle(monkeypatch):
    _ensure_event_loop()
    import src.client.telegram_client as tcmod
    obj = tcmod.TelegramClient.__new__(tcmod.TelegramClient)

    async def _auth():
        return True

    class _Inner:
        async def start(self):
            return None

        async def get_me(self):
            return None

    async def _mp():
        return None

    obj.client = _Inner()
    obj.running = False
    obj.user_info = object()  # 跳过 get_me
    obj._handle_authorization = _auth
    obj._setup_handlers = lambda: None
    obj._message_processor = _mp
    obj._register_reload_notifier = lambda: None
    obj._start_scheduler = lambda: None
    obj.config = types.SimpleNamespace(get=lambda k, d=None: {})

    # block=False 必须立即返回（不进入 idle 永久阻塞）
    await asyncio.wait_for(obj.start(block=False), timeout=2.0)
    assert obj.running is True


# ── ensure_builtin_workers 选择 ──────────────────────────────────────────────

def _run_ensure(monkeypatch, flag_on):
    import src.integrations.account_orchestrator as orch
    import src.integrations.telegram_protocol_login as tgl
    monkeypatch.setattr(tgl, "is_pyrogram_available", lambda: True)
    monkeypatch.setattr(tgl, "protocol_enabled", lambda c: True)
    monkeypatch.setattr(tgl, "resolve_credentials", lambda c: (1, "h"))
    saved = dict(orch._WORKER_FACTORIES)
    orch._WORKER_FACTORIES.clear()
    try:
        cfg = {"platform_login": {"telegram": {"companion_runtime": flag_on}}}
        orch.ensure_builtin_workers(cfg)
        f = orch.get_worker_factory("telegram", "protocol")
        assert f is not None
        return f({"account_id": "x", "meta": {"session_name": "s"}}, cfg)
    finally:
        orch._WORKER_FACTORIES.clear()
        orch._WORKER_FACTORIES.update(saved)


def test_ensure_builtin_workers_picks_companion_when_flag_on(monkeypatch):
    from src.integrations.telegram_companion_worker import TelegramCompanionWorker
    w = _run_ensure(monkeypatch, flag_on=True)
    assert isinstance(w, TelegramCompanionWorker)


def test_ensure_builtin_workers_picks_thin_when_flag_off(monkeypatch):
    from src.integrations.account_orchestrator import TelegramProtocolWorker
    w = _run_ensure(monkeypatch, flag_on=False)
    assert isinstance(w, TelegramProtocolWorker)


# ── N4b：A 线入站/出站收件箱镜像 ─────────────────────────────────────────────

def _bare_tc():
    _ensure_event_loop()
    import src.client.telegram_client as tcmod
    return tcmod.TelegramClient.__new__(tcmod.TelegramClient)


def test_emit_inbox_disabled_by_default(monkeypatch):
    import src.integrations.protocol_bridge as pb
    calls = []
    monkeypatch.setattr(pb, "emit_incoming", lambda m: calls.append(m))
    obj = _bare_tc()
    obj.account_id = "u1"
    # _mirror_inbox 未置 → getattr 缺省 False → 不镜像（standalone main.py 行为）
    obj._emit_inbox(chat_id=1, text="hi", direction="in")
    assert calls == []


def test_emit_inbox_enabled_emits_in(monkeypatch):
    import src.integrations.protocol_bridge as pb
    calls = []
    monkeypatch.setattr(pb, "emit_incoming", lambda m: calls.append(m))
    obj = _bare_tc()
    obj.account_id = "u1"
    obj._mirror_inbox = True
    obj._emit_inbox(chat_id=12345, text="在吗", direction="in",
                    name="甜心", msg_id="7")
    assert len(calls) == 1
    m = calls[0]
    assert m["platform"] == "telegram"
    assert m["account_id"] == "u1"
    assert m["chat_key"] == "12345"
    assert m["text"] == "在吗"
    assert m["direction"] == "in"
    assert m["name"] == "甜心"
    assert m["msg_id"] == "7"


def test_emit_inbox_out_direction(monkeypatch):
    import src.integrations.protocol_bridge as pb
    calls = []
    monkeypatch.setattr(pb, "emit_incoming", lambda m: calls.append(m))
    obj = _bare_tc()
    obj.account_id = "u1"
    obj._mirror_inbox = True
    obj._emit_inbox(chat_id=999, text="想你了", direction="out")
    assert len(calls) == 1
    assert calls[0]["direction"] == "out"
    assert calls[0]["text"] == "想你了"


def test_emit_inbox_swallows_errors(monkeypatch):
    import src.integrations.protocol_bridge as pb

    def _boom(_m):
        raise RuntimeError("sink down")

    monkeypatch.setattr(pb, "emit_incoming", _boom)
    obj = _bare_tc()
    obj.account_id = "u1"
    obj._mirror_inbox = True
    # 镜像失败绝不冒泡（不影响主消息流）
    obj._emit_inbox(chat_id=1, text="x", direction="in")
