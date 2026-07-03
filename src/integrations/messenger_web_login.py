"""Messenger 网页模式（web mode）登录 provider（M5）。

Messenger 没有像 WhatsApp Baileys 那样的干净协议库，但它**有官方网页版 messenger.com**。
本模块是 Python 侧桥接：把统一收件箱「账号管理 → ＋ 扫码新增（网页）」的登录请求转发给
一个独立运行的 **Playwright Node 微服务**（见 ``services/messenger-web/``），由它用隔离
浏览器加载 messenger.com、完成官方登录、维护连接、DOM 收发，功能对齐官方网页版。

落地约束（与 M2/M3 一致的谨慎姿态）：
- 需先 ``npm install`` 并启动 Node 微服务，且需真号登录联调；故默认**不启用**，需在
  ``config.platform_login.messenger.web_enabled: true`` 显式开启。
- 桥接通过 HTTP 调用微服务；服务不可达时**优雅降级**为错误提示，不影响主进程。
- 网络调用集中在 ``_post_json`` / ``_get_json`` 两个可被测试替换的薄封装里。

契约与 ``whatsapp_baileys_login`` 对齐：login/start → poll(status) → 成功落库 + self_profile 富集。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8791"
_registered = False


def service_base_url(config: Dict[str, Any]) -> str:
    pl = (config or {}).get("platform_login", {}) or {}
    mg = pl.get("messenger", {}) or {}
    return str(mg.get("web_url") or _DEFAULT_BASE_URL).rstrip("/")


def web_enabled(config: Dict[str, Any]) -> bool:
    pl = (config or {}).get("platform_login", {}) or {}
    mg = pl.get("messenger", {}) or {}
    return bool(mg.get("web_enabled", False))


# ── HTTP 薄封装（测试可 monkeypatch） ────────────────────────────────────────

async def _post_json(url: str, payload: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


async def _get_json(url: str, timeout: float = 20.0) -> Dict[str, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


def _normalize_status(raw: str) -> str:
    s = str(raw or "").lower()
    if s in ("open", "authorized", "connected", "online"):
        return "authorized"
    if s in ("scanned", "pairing"):
        return "scanned"
    if s in ("expired", "timeout"):
        return "expired"
    if s in ("failed", "error", "logged_out"):
        return "failed"
    return "pending"


# ── provider 工厂 + 注册 ─────────────────────────────────────────────────────

def make_provider(config: Dict[str, Any]):
    base = service_base_url(config)

    async def _provider(request: Any, platform: str, mode: str, account_id: str,
                        ctx: Optional[Dict[str, Any]] = None):
        proxy = (ctx or {}).get("proxy") or {}
        payload: Dict[str, Any] = {"account_id": account_id or ""}
        if proxy.get("host"):
            payload["proxy_url"] = proxy.get("url") or ""
        try:
            data = await _post_json(f"{base}/login/start", payload)
        except Exception as ex:  # noqa: BLE001
            logger.debug("[messenger_web] start 调用失败", exc_info=True)
            return {"instruction": f"无法连接 Messenger 网页服务（{ex}）。请确认 messenger-web 微服务已启动。"}

        login_id = str(data.get("login_id") or "")
        qr_image = str(data.get("qr_image") or "")

        async def _poll(session: Any) -> Dict[str, Any]:
            try:
                res = await _get_json(f"{base}/login/{login_id}/status")
            except Exception as ex:  # noqa: BLE001
                logger.debug("[messenger_web] status 调用失败", exc_info=True)
                return {"status": "pending", "detail": str(ex)}
            st = _normalize_status(res.get("status"))
            aid = str(res.get("account_id") or "")
            if st == "authorized" and aid:
                try:
                    get_account_registry().upsert(
                        "messenger", aid, mode="web", status="online",
                        meta={"messenger_login_id": login_id})
                except Exception:  # noqa: BLE001
                    logger.debug("[messenger_web] 注册表写入失败", exc_info=True)
                # self_profile 富集：微服务若回传昵称/头像 URL → 富集账号自身身份
                try:
                    from src.integrations.account_self_profile import enrich_from_fields
                    await enrich_from_fields(
                        "messenger", aid,
                        name=str(res.get("name") or ""),
                        avatar_url=str(res.get("avatar_url") or res.get("profile_pic_url") or ""),
                        config=config)
                except Exception:  # noqa: BLE001
                    logger.debug("[messenger_web] self_profile 富集失败（忽略）", exc_info=True)
            return {"status": st, "account_id": aid,
                    "detail": str(res.get("detail") or ""),
                    "qr_image": str(res.get("qr_image") or "")}

        async def _cancel(session: Any) -> None:
            try:
                await _post_json(f"{base}/login/{login_id}/cancel", {})
            except Exception:  # noqa: BLE001
                logger.debug("[messenger_web] cancel 调用失败", exc_info=True)

        return {
            "qr_image": qr_image,
            "instruction": "在弹出的浏览器窗口内用官方方式登录 Messenger（扫码 / 账密 / 2FA 均可）。"
                           "登录成功后本窗口会自动确认。",
            "poll": _poll,
            "cancel": _cancel,
            "state": {"login_id": login_id, "base": base},
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 Messenger web provider（幂等）。

    仅当 ``web_enabled: true`` 时注册（服务可达性在 start 时检测并降级）。
    """
    global _registered
    if _registered:
        return True
    if not web_enabled(config):
        return False
    register_login_provider("messenger", "web", make_provider(config))
    _registered = True
    logger.info("[messenger_web] Messenger web 登录 provider 已注册 (base=%s)",
                service_base_url(config))
    return True
