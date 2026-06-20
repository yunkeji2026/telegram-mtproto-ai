"""Phase N2：扫码登录导出 session_string + preflight 接入 golive 总表。

N2-A：`TelegramQrLogin._finish` 趁连接未断导出 session_string（供 A 线 in-memory 启动）。
N2-B：`golive.build_checklist` 在 N 线启用时纳入「扫码陪聊就绪」子项。
"""
import pytest

from src.integrations.telegram_protocol_login import TelegramQrLogin
from src.utils.golive import build_checklist


# ── N2-A：session_string 导出 ────────────────────────────────────────────────

class _FakeStorage:
    async def user_id(self, *a):
        return None

    async def is_bot(self, *a):
        return None


class _FakeClient:
    def __init__(self, ss="SESSION_STRING_ABC", raises=False):
        self.storage = _FakeStorage()
        self._ss = ss
        self._raises = raises
        self.disconnected = False

    async def export_session_string(self):
        if self._raises:
            raise RuntimeError("boom")
        return self._ss

    async def disconnect(self):
        self.disconnected = True


class _Success:
    class authorization:
        class user:
            id = 123456
            phone_number = "8613800000000"


def test_init_session_string_empty():
    login = TelegramQrLogin(1, "hash", "/tmp")
    assert login.session_string == ""


async def test_finish_exports_session_string():
    login = TelegramQrLogin(1, "hash", "/tmp")
    login.client = _FakeClient("SS_OK")
    await login._finish(_Success)
    assert login.status == "authorized"
    assert login.account_id == "123456"
    assert login.session_string == "SS_OK"
    assert login.client.disconnected is True  # 已断开（落盘）


async def test_finish_export_failure_keeps_empty():
    login = TelegramQrLogin(1, "hash", "/tmp")
    login.client = _FakeClient(raises=True)
    await login._finish(_Success)
    # 导出失败不影响授权成功，session_string 保持空（回落文件 session）
    assert login.status == "authorized"
    assert login.session_string == ""


# ── N2-B：golive 纳入扫码陪聊就绪 ─────────────────────────────────────────────

def _golive(config):
    return build_checklist(
        config=config,
        channel_statuses=[{"id": "telegram", "name": "Telegram",
                           "ready": True, "configured": True}],
        config_errors=0, config_warnings=0,
        kb_ready={"available": True, "is_cold": False, "enabled_entries": 5},
        online_agents=1)


_AI = {"provider": "openai_compatible", "api_key": "sk-real"}


def test_golive_includes_companion_when_enabled_and_ready():
    out = _golive({
        "ai": _AI,
        "telegram": {"api_id": 1, "api_hash": "abcdef"},
        "platform_login": {"orchestrator_enabled": True,
                           "telegram": {"protocol_enabled": True,
                                        "companion_runtime": True}},
        "companion_send_gate": {"enabled": True},
        "proxy_pool": ["socks5://1.2.3.4:1080"],
    })
    c = next(c for c in out["checks"] if c["id"] == "companion")
    assert c["status"] == "ok"


def test_golive_companion_fail_blocks():
    # companion_runtime 开但 orchestrator 关 → preflight red → golive companion fail
    out = _golive({
        "ai": _AI,
        "telegram": {"api_id": 1, "api_hash": "abcdef"},
        "platform_login": {"orchestrator_enabled": False,
                           "telegram": {"protocol_enabled": True,
                                        "companion_runtime": True}},
    })
    c = next(c for c in out["checks"] if c["id"] == "companion")
    assert c["status"] == "fail"
    assert out["light"] == "red"


def test_golive_no_companion_when_disabled():
    out = _golive({"ai": _AI})
    assert not any(c["id"] == "companion" for c in out["checks"])
    assert out["light"] == "green"
