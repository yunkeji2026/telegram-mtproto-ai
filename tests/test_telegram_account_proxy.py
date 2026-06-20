"""N 线 核心2：A 线每号独立代理（proxy_id）单测。

验证 proxy_id 经 TelegramAccountRegistry 贯通到 account_cfg/stats，
以及 TelegramClient._resolve_proxy 复用 proxy_pool + _to_pyrogram_proxy。
"""
import asyncio

from src.client.telegram_account_registry import TelegramAccountRegistry


def _ensure_event_loop() -> None:
    """pyrogram 的 sync wrap 在 import 时调用 ``asyncio.get_event_loop()``；
    裸 MainThread（pytest-asyncio 跑完异步用例后已拆 loop）无 loop 会抛错。
    导入 telegram_client 前先确保有 loop（仅测试需要，生产中在事件循环内导入）。"""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


# ── 注册表 proxy_id 贯通 ─────────────────────────────────────────────────────

def test_multi_account_parses_proxy_id():
    reg = TelegramAccountRegistry.from_config({
        "accounts": [
            {"id": "a", "api_id": 1, "api_hash": "h", "phone_number": "+1",
             "proxy_id": "px-a", "enabled": True},
            {"id": "b", "api_id": 2, "api_hash": "h2", "phone_number": "+2",
             "enabled": True},
        ]
    })
    a = reg.get("a")
    b = reg.get("b")
    assert a.proxy_id == "px-a"
    assert b.proxy_id == ""
    assert a.account_cfg()["proxy_id"] == "px-a"
    # 无 proxy_id 的账号 cfg 不带该键（保持精简）
    assert "proxy_id" not in b.account_cfg()


def test_stats_exposes_proxy_id():
    reg = TelegramAccountRegistry.from_config({
        "accounts": [
            {"id": "a", "api_id": 1, "api_hash": "h", "phone_number": "+1",
             "proxy_id": "px-a", "enabled": True},
        ]
    })
    st = reg.stats()
    assert st["accounts"][0]["proxy_id"] == "px-a"


def test_single_account_fallback_parses_flat_proxy_id():
    reg = TelegramAccountRegistry.from_config({
        "api_id": 1, "api_hash": "h", "phone_number": "+1", "proxy_id": "px-flat",
    })
    ctx = reg.primary()
    assert ctx.account_id == "default"
    assert ctx.proxy_id == "px-flat"
    assert ctx.account_cfg()["proxy_id"] == "px-flat"


# ── TelegramClient._resolve_proxy ────────────────────────────────────────────

def _bare_client(proxy_id: str):
    """绕过重 __init__ 造一个仅含 _resolve_proxy 所需属性的实例。"""
    _ensure_event_loop()
    from src.client.telegram_client import TelegramClient
    obj = TelegramClient.__new__(TelegramClient)
    obj.proxy_id = proxy_id
    return obj


def test_resolve_proxy_empty_returns_none():
    assert _bare_client("")._resolve_proxy() is None


def test_resolve_proxy_reads_pool_and_converts(monkeypatch):
    import src.integrations.proxy_pool as pool_mod

    class _FakePool:
        def get(self, pid, *, mask=True):
            assert pid == "px-a"
            return {"scheme": "socks5", "host": "1.2.3.4", "port": 1080,
                    "username": "u", "password": "p"}

    monkeypatch.setattr(pool_mod, "get_proxy_pool", lambda: _FakePool())
    proxy = _bare_client("px-a")._resolve_proxy()
    assert proxy == {
        "scheme": "socks5", "hostname": "1.2.3.4", "port": 1080,
        "username": "u", "password": "p",
    }


def test_resolve_proxy_swallows_errors(monkeypatch):
    import src.integrations.proxy_pool as pool_mod

    def _boom():
        raise RuntimeError("pool down")

    monkeypatch.setattr(pool_mod, "get_proxy_pool", _boom)
    assert _bare_client("px-a")._resolve_proxy() is None
