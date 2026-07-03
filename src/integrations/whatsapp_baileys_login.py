"""WhatsApp 协议多开（Baileys）扫码登录 provider（M3）。

WhatsApp 没有官方多账号协议库，社区主流方案是 **Baileys（Node.js）**。本模块是 Python 侧
桥接：把统一收件箱「账号管理 → ＋ 扫码新增（协议）」的登录请求转发给一个独立运行的
**Baileys Node 微服务**（见 ``services/whatsapp-baileys/``），由它生成网页二维码、维护
WhatsApp 连接、回报账号上线。

落地约束（与 M2 一致的谨慎姿态）：
- 需先 ``npm install`` 并启动 Node 微服务，且需真号扫码联调；故默认**不启用**，需在
  ``config.platform_login.whatsapp.protocol_enabled: true`` 显式开启。
- 桥接通过 HTTP 调用微服务；服务不可达时**优雅降级**为错误提示，不影响主进程。
- 网络调用集中在 ``_post_json`` / ``_get_json`` 两个可被测试替换的薄封装里。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8790"
_registered = False


def service_base_url(config: Dict[str, Any]) -> str:
    pl = (config or {}).get("platform_login", {}) or {}
    wa = pl.get("whatsapp", {}) or {}
    return str(wa.get("baileys_url") or _DEFAULT_BASE_URL).rstrip("/")


def protocol_enabled(config: Dict[str, Any]) -> bool:
    pl = (config or {}).get("platform_login", {}) or {}
    wa = pl.get("whatsapp", {}) or {}
    return bool(wa.get("protocol_enabled", False))


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
            # 透传给 Node 微服务（由 Baileys 侧通过 agent 应用，一号一代理）
            payload["proxy_url"] = proxy.get("url") or ""
        try:
            data = await _post_json(f"{base}/login/start", payload)
        except Exception as ex:  # noqa: BLE001
            logger.debug("[wa_baileys] start 调用失败", exc_info=True)
            return {"instruction": f"无法连接 WhatsApp 协议服务（{ex}）。请确认 Baileys 微服务已启动。"}

        login_id = str(data.get("login_id") or "")
        qr_image = str(data.get("qr_image") or "")
        qr_url = str(data.get("qr_url") or "")

        async def _poll(session: Any) -> Dict[str, Any]:
            try:
                res = await _get_json(f"{base}/login/{login_id}/status")
            except Exception as ex:  # noqa: BLE001
                logger.debug("[wa_baileys] status 调用失败", exc_info=True)
                return {"status": "pending", "detail": str(ex)}
            st = _normalize_status(res.get("status"))
            aid = str(res.get("account_id") or "")
            if st == "authorized" and aid:
                try:
                    get_account_registry().upsert(
                        "whatsapp", aid, mode="protocol", status="online",
                        meta={"baileys_login_id": login_id})
                except Exception:  # noqa: BLE001
                    logger.debug("[wa_baileys] 注册表写入失败", exc_info=True)
                # P4 身份化：Baileys 微服务若在 status 里回传 pushname/name → 富集自身昵称
                # （前向兼容：字段缺失则 enrich 内部无有效字段直接 no-op；flag 默认关）
                try:
                    from src.integrations.account_self_profile import enrich_from_fields
                    await enrich_from_fields(
                        "whatsapp", aid,
                        name=str(res.get("pushname") or res.get("name") or ""),
                        avatar_url=str(res.get("avatar_url") or res.get("profile_pic_url") or ""),
                        config=config)
                except Exception:  # noqa: BLE001
                    logger.debug("[wa_baileys] self_profile 富集失败（忽略）", exc_info=True)
            return {"status": st, "account_id": aid,
                    "detail": str(res.get("detail") or ""),
                    "qr_image": str(res.get("qr_image") or "")}

        async def _cancel(session: Any) -> None:
            try:
                await _post_json(f"{base}/login/{login_id}/cancel", {})
            except Exception:  # noqa: BLE001
                logger.debug("[wa_baileys] cancel 调用失败", exc_info=True)

        return {
            "qr_image": qr_image,
            "qr_url": qr_url,
            "instruction": "用手机 WhatsApp：设置 → 已关联的设备 → 关联新设备，扫描二维码。",
            "poll": _poll,
            "cancel": _cancel,
            "state": {"login_id": login_id, "base": base},
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 WhatsApp protocol provider（幂等）。

    仅当 ``protocol_enabled: true`` 时注册（服务可达性在 start 时检测并降级）。
    """
    global _registered
    if _registered:
        return True
    if not protocol_enabled(config):
        return False
    register_login_provider("whatsapp", "protocol", make_provider(config))
    _registered = True
    logger.info("[wa_baileys] WhatsApp protocol 登录 provider 已注册 (base=%s)",
                service_base_url(config))
    return True
