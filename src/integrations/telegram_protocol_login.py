"""Telegram protocol（pyrogram）扫码登录 provider（M2）。

实现 Telegram 官方「关联设备」二维码登录（``auth.ExportLoginToken`` 流），用于
在统一收件箱「账号管理 → ＋ 扫码新增」里**新增任意多个 Telegram 账号**（协议多开）。

安全 / 落地约束（重要）：
- pyrogram 的 QR 登录涉及 DC 迁移（``LoginTokenMigrateTo``）等底层细节，**无法在无真账号
  的环境联调**。因此本 provider 默认**不启用**，需操作者用测试号验证后，在
  ``config.platform_login.telegram.protocol_enabled: true`` 显式开启。
- 全程隔离：每次登录用独立 session 文件（``sessions/tg_login_*.session``），失败仅影响该
  次登录，**不触碰正在运行的主客户端**。
- 纯函数（``tg_login_url`` / ``resolve_credentials`` / ``is_pyrogram_available``）与状态机
  可单测；真实 pyrogram 调用集中在 ``TelegramQrLogin`` 内并全程 try/except 降级。

成功后：把账号写入 ``account_registry``（mode=protocol，meta 记 session_name/phone），
供账号池编排器后续以该 session 拉起 runner。
"""

from __future__ import annotations

import base64
import logging
import secrets
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider

logger = logging.getLogger(__name__)

_DEFAULT_SESSIONS_DIR = "sessions"
_registered = False


# ── 纯函数（可单测） ─────────────────────────────────────────────────────────

def is_pyrogram_available() -> bool:
    import importlib.util as u
    return u.find_spec("pyrogram") is not None


def tg_login_url(token: bytes) -> str:
    """构造 Telegram 关联设备登录 URL（二维码内容）。"""
    b64 = base64.urlsafe_b64encode(bytes(token)).decode().rstrip("=")
    return f"tg://login?token={b64}"


def resolve_credentials(config: Dict[str, Any]) -> Optional[Tuple[int, str]]:
    """从 config 解析 api_id/api_hash（先扁平 telegram.*，再取首个 account）。"""
    tg = (config or {}).get("telegram", {}) or {}
    api_id = tg.get("api_id")
    api_hash = tg.get("api_hash")
    if not (api_id and api_hash):
        for a in (tg.get("accounts") or []):
            if isinstance(a, dict) and a.get("api_id") and a.get("api_hash"):
                api_id, api_hash = a.get("api_id"), a.get("api_hash")
                break
    try:
        if api_id and api_hash:
            return int(api_id), str(api_hash)
    except (TypeError, ValueError):
        pass
    return None


def protocol_enabled(config: Dict[str, Any]) -> bool:
    pl = (config or {}).get("platform_login", {}) or {}
    tg = pl.get("telegram", {}) or {}
    return bool(tg.get("protocol_enabled", False))


# ── 登录状态机（真实 pyrogram 调用，全程降级保护） ───────────────────────────

class TelegramQrLogin:
    """管理一次 Telegram 二维码登录的生命周期（异步）。"""

    def __init__(self, api_id: int, api_hash: str, sessions_dir: str,
                 proxy: Optional[Dict[str, Any]] = None) -> None:
        self.api_id = int(api_id)
        self.api_hash = str(api_hash)
        self.sessions_dir = Path(sessions_dir)
        self.session_name = f"tg_login_{secrets.token_hex(6)}"
        self.proxy = proxy or None
        self.client: Any = None
        self.status = "pending"      # pending|authorized|expired|failed
        self.account_id = ""
        self.phone = ""
        self.qr_url = ""
        self.detail = ""
        # N2：扫码成功时导出的 session_string（in-memory 启动用，比文件 session 抗 DC 迁移）
        self.session_string = ""

    def result(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "account_id": self.account_id,
            "qr_url": self.qr_url,
            "detail": self.detail,
        }

    async def start(self) -> Dict[str, Any]:
        try:
            from pyrogram import Client  # noqa: WPS433 (lazy)
            from pyrogram.raw.functions.auth import ExportLoginToken

            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            client_kwargs: Dict[str, Any] = dict(
                api_id=self.api_id, api_hash=self.api_hash,
                workdir=str(self.sessions_dir),
            )
            if self.proxy:
                client_kwargs["proxy"] = self.proxy
            self.client = Client(self.session_name, **client_kwargs)
            await self.client.connect()
            r = await self.client.invoke(ExportLoginToken(
                api_id=self.api_id, api_hash=self.api_hash, except_ids=[]))
            await self._advance(r)
        except Exception as ex:  # noqa: BLE001
            self.status = "failed"
            self.detail = f"发起登录失败：{ex}"
            logger.debug("[tg_protocol_login] start 失败", exc_info=True)
            await self._safe_disconnect()
        return self.result()

    async def poll(self) -> Dict[str, Any]:
        if self.status in ("authorized", "failed", "expired"):
            return self.result()
        try:
            from pyrogram.raw.functions.auth import ExportLoginToken
            r = await self.client.invoke(ExportLoginToken(
                api_id=self.api_id, api_hash=self.api_hash, except_ids=[]))
            await self._advance(r)
        except Exception as ex:  # noqa: BLE001
            # 多为 token 过期 / 网络抖动 → 标记过期让前端刷新
            self.status = "expired"
            self.detail = str(ex)
            logger.debug("[tg_protocol_login] poll 失败", exc_info=True)
        return self.result()

    async def cancel(self) -> None:
        await self._safe_disconnect(remove_session=(self.status != "authorized"))

    # ── 内部 ──────────────────────────────────────────────────────────────

    async def _advance(self, r: Any) -> None:
        from pyrogram.raw.types.auth import (
            LoginToken, LoginTokenMigrateTo, LoginTokenSuccess,
        )
        if isinstance(r, LoginToken):
            self.qr_url = tg_login_url(r.token)
            self.status = "pending"
        elif isinstance(r, LoginTokenMigrateTo):
            from pyrogram.raw.functions.auth import ImportLoginToken
            await self._migrate(r.dc_id)
            r2 = await self.client.invoke(ImportLoginToken(token=r.token))
            await self._advance(r2)
        elif isinstance(r, LoginTokenSuccess):
            await self._finish(r)
        else:
            self.status = "failed"
            self.detail = f"未知的登录响应：{type(r).__name__}"

    async def _migrate(self, dc_id: int) -> None:
        """切换到目标 DC（扫码后账号归属 DC 通常与默认 DC 不同）。"""
        from pyrogram.session import Auth, Session
        await self.client.session.stop()
        await self.client.storage.dc_id(dc_id)
        test_mode = await self.client.storage.test_mode()
        auth_key = await Auth(self.client, dc_id, test_mode).create()
        await self.client.storage.auth_key(auth_key)
        self.client.session = Session(
            self.client, dc_id, auth_key, test_mode)
        await self.client.session.start()

    async def _finish(self, success: Any) -> None:
        try:
            user = success.authorization.user
            self.account_id = str(getattr(user, "id", "") or "")
            self.phone = str(getattr(user, "phone_number", "") or "")
            await self.client.storage.user_id(user.id)
            await self.client.storage.is_bot(False)
            self.status = "authorized"
            self.detail = ""
            # N2：趁连接未断导出 session_string（A 线可 in-memory 启动，抗文件 session DC 迁移不稳）
            try:
                self.session_string = str(await self.client.export_session_string() or "")
            except Exception:  # noqa: BLE001
                self.session_string = ""
                logger.debug("[tg_protocol_login] 导出 session_string 失败（忽略）", exc_info=True)
        except Exception as ex:  # noqa: BLE001
            self.status = "failed"
            self.detail = f"完成登录失败：{ex}"
            logger.debug("[tg_protocol_login] finish 失败", exc_info=True)
        finally:
            # disconnect 以把 session 落盘（供编排器后续拉起）
            await self._safe_disconnect(remove_session=False)

    async def _safe_disconnect(self, *, remove_session: bool = False) -> None:
        try:
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            if remove_session:
                f = self.sessions_dir / f"{self.session_name}.session"
                if f.exists():
                    f.unlink()
        except Exception:  # noqa: BLE001
            logger.debug("[tg_protocol_login] disconnect 清理失败", exc_info=True)


# ── provider 工厂 + 注册 ─────────────────────────────────────────────────────

def _to_pyrogram_proxy(proxy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把代理池条目转成 pyrogram 的 proxy 配置。"""
    if not proxy or not proxy.get("host"):
        return None
    out: Dict[str, Any] = {
        "scheme": str(proxy.get("scheme") or "socks5"),
        "hostname": str(proxy.get("host")),
        "port": int(proxy.get("port") or 0),
    }
    if proxy.get("username"):
        out["username"] = str(proxy.get("username"))
    if proxy.get("password"):
        out["password"] = str(proxy.get("password"))
    return out


def make_provider(config: Dict[str, Any], sessions_dir: str = _DEFAULT_SESSIONS_DIR):
    creds = resolve_credentials(config)

    async def _provider(request: Any, platform: str, mode: str, account_id: str,
                        ctx: Optional[Dict[str, Any]] = None):
        if creds is None:
            return {"instruction": "未配置 Telegram api_id/api_hash，无法发起协议登录。"}
        api_id, api_hash = creds
        proxy = _to_pyrogram_proxy((ctx or {}).get("proxy"))
        login = TelegramQrLogin(api_id, api_hash, sessions_dir, proxy=proxy)
        await login.start()

        async def _poll(session: Any) -> Dict[str, Any]:
            res = await login.poll()
            if res.get("status") == "authorized" and res.get("account_id"):
                try:
                    _meta = {"session_name": login.session_name, "phone": login.phone}
                    # N2：有 session_string 则一并存（A 线优先 in-memory 启动；见 telegram_client）
                    if getattr(login, "session_string", ""):
                        _meta["session_string"] = login.session_string
                    get_account_registry().upsert(
                        "telegram", res["account_id"], mode="protocol",
                        status="online", meta=_meta,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("[tg_protocol_login] 注册表写入失败", exc_info=True)
            return res

        async def _cancel(session: Any) -> None:
            await login.cancel()

        return {
            "qr_url": login.qr_url,
            "instruction": "用手机 Telegram：设置 → 设备 → 关联桌面设备，扫描二维码。",
            "poll": _poll,
            "cancel": _cancel,
            "state": login,
        }

    return _provider


def maybe_register(config: Dict[str, Any], *, sessions_dir: str = _DEFAULT_SESSIONS_DIR) -> bool:
    """按需注册 Telegram protocol provider。

    仅当：pyrogram 可用 + 配置了 api 凭据 + ``protocol_enabled: true`` 时注册（幂等）。
    返回是否已注册（已注册过也返回 True）。
    """
    global _registered
    if _registered:
        return True
    if not is_pyrogram_available():
        return False
    if resolve_credentials(config) is None:
        return False
    if not protocol_enabled(config):
        return False
    register_login_provider("telegram", "protocol", make_provider(config, sessions_dir))
    _registered = True
    logger.info("[tg_protocol_login] Telegram protocol 登录 provider 已注册")
    return True
