"""M2：Telegram protocol（pyrogram）扫码登录 provider 单测。

仅覆盖纯函数 / 门控 / 状态机的 pending 分支（不联网、不需真账号）。
真实扫码成功 + DC 迁移路径需用测试号联调（protocol_enabled 默认 false）。
"""

from __future__ import annotations

import asyncio
import base64
import time

import pytest

from src.integrations import platform_login as pl
from src.integrations import telegram_protocol_login as tpl


def test_tg_login_url_format():
    url = tpl.tg_login_url(b"abc")
    assert url == "tg://login?token=" + base64.urlsafe_b64encode(b"abc").decode().rstrip("=")
    assert url.startswith("tg://login?token=")


def test_resolve_credentials_flat_and_accounts():
    assert tpl.resolve_credentials({}) is None
    assert tpl.resolve_credentials({"telegram": {"api_id": 0, "api_hash": ""}}) is None
    flat = tpl.resolve_credentials({"telegram": {"api_id": 123, "api_hash": "h"}})
    assert flat == (123, "h")
    nested = tpl.resolve_credentials(
        {"telegram": {"accounts": [{"api_id": 9, "api_hash": "z"}]}})
    assert nested == (9, "z")


def test_protocol_enabled_flag():
    assert tpl.protocol_enabled({}) is False
    assert tpl.protocol_enabled(
        {"platform_login": {"telegram": {"protocol_enabled": True}}}) is True


def test_maybe_register_gated_off_by_default():
    tpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)
    # 有凭据但未开 protocol_enabled → 不注册
    cfg = {"telegram": {"api_id": 1, "api_hash": "h"}}
    assert tpl.maybe_register(cfg) is False
    assert pl.mode_available("telegram", "protocol") is False


def test_maybe_register_when_enabled():
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    tpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "h"},
        "platform_login": {"telegram": {"protocol_enabled": True}},
    }
    try:
        assert tpl.maybe_register(cfg) is True
        assert pl.mode_available("telegram", "protocol") is True
        # 幂等
        assert tpl.maybe_register(cfg) is True
    finally:
        tpl._registered = False
        pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)


def test_state_machine_pending_branch(tmp_path):
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    # pyrogram 顶层 import 会触发 sync 模块调用 get_event_loop()，
    # 在 xdist worker 线程里无 loop 会抛 RuntimeError —— 先确保有 loop。
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from pyrogram.raw.types.auth import LoginToken
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    tok = LoginToken(expires=int(time.time()) + 30, token=b"abc")
    asyncio.run(login._advance(tok))
    assert login.status == "pending"
    assert login.qr_url.startswith("tg://login?token=")
    assert login.result()["status"] == "pending"
