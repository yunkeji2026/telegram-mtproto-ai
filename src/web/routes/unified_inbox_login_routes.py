"""统一收件箱——平台扫码登录路由域（巨石拆分 slice 9）。

把"平台扫码登录（P3/M1：多方式并存 · 无限扫码 / 多账号接入 + 自助重连）"这一
自包含子域，从 ``register_unified_inbox_routes`` 巨型闭包中抽出，封装为
``register_platform_login_routes(app, *, api_auth, config_manager)``，由主 register
顺序调用。端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫保证）。

域内闭包级 helper（_platform_login_cfg / _platform_login_enabled / _login_qr_data_url /
_ensure_login_providers / _persist_login_account）随域同搬，保持闭包捕获 config_manager
的原行为。子注册函数只收自身真正需要的依赖（api_auth + config_manager）。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Dict

from fastapi import Request

from src.inbox.channel_adapters import status_via_adapters
from src.integrations.account_registry import get_account_registry
from src.integrations.fingerprint import get_fingerprint_store
from src.integrations.platform_login import (
    SUPPORTED_PLATFORMS,
    get_login_manager,
    get_login_provider,
    list_modes,
    mode_available,
    online_account_keys,
)
from src.integrations.proxy_pool import get_proxy_pool
from src.web.routes.unified_inbox_aggregate import _INBOX_ADAPTERS

logger = logging.getLogger(__name__)


def register_platform_login_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载平台扫码登录相关端点（/api/platforms/{platform}/...）。"""

    # ── 平台扫码登录（P3/M1：多方式并存 · 无限扫码 / 多账号接入 + 自助重连） ──
    def _platform_login_cfg() -> Dict[str, Any]:
        try:
            if config_manager is None:
                return {}
            return (config_manager.config or {}).get("platform_login", {}) or {}
        except Exception:
            return {}

    def _platform_login_enabled() -> bool:
        return bool(_platform_login_cfg().get("enabled", True))

    def _login_qr_data_url(qr_url: str) -> str:
        """把 tg://login?token=… 等登录 URL 服务端渲染为 base64 PNG data URL。

        令牌不出本机（避免泄露给第三方 QR 服务）。qrcode/PIL 缺失或失败时返回空串，
        前端回落为显示链接 / 设备端指引。
        """
        text = str(qr_url or "")
        if not text:
            return ""
        try:
            import base64
            import io
            import qrcode
            img = qrcode.make(text)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return "data:image/png;base64," + \
                base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            logger.debug("登录二维码服务端渲染失败", exc_info=True)
            return ""

    def _ensure_login_providers() -> None:
        """按需注册真实 per-(platform,mode) provider（幂等、全程降级）。"""
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        try:
            from src.integrations.telegram_protocol_login import maybe_register as _tg_reg
            _tg_reg(cfg)
        except Exception:
            logger.debug("注册 telegram protocol provider 失败", exc_info=True)
        try:
            from src.integrations.whatsapp_baileys_login import maybe_register as _wa_reg
            _wa_reg(cfg)
        except Exception:
            logger.debug("注册 whatsapp baileys provider 失败", exc_info=True)

    def _persist_login_account(platform: str, account_id: str, sess: Any) -> None:
        """登录成功后把账号 + mode + 代理 + 指纹 + 备注落库，并把代理标记为已分配。"""
        try:
            get_account_registry().upsert(
                platform, account_id, mode=getattr(sess, "mode", "device"),
                status="online",
                label=(getattr(sess, "label", "") or None),
                proxy_id=(getattr(sess, "proxy_id", "") or None),
                fingerprint_id=(getattr(sess, "fingerprint_id", "") or None),
            )
            if getattr(sess, "proxy_id", ""):
                get_proxy_pool().assign(sess.proxy_id, f"{platform}:{account_id}")
        except Exception:
            logger.debug("账号注册表上线 upsert 失败", exc_info=True)

    @app.get("/api/platforms/{platform}/modes")
    async def api_platform_login_modes(platform: str, request: Request):
        api_auth(request)
        platform = str(platform or "").lower()
        if platform not in SUPPORTED_PLATFORMS:
            return {"ok": False, "detail": f"不支持的平台: {platform}"}
        _ensure_login_providers()
        platform_cfg = _platform_login_cfg().get(platform, {}) or {}
        return {"ok": True, "platform": platform, "modes": list_modes(platform, platform_cfg)}

    @app.post("/api/platforms/{platform}/login/start")
    async def api_platform_login_start(platform: str, request: Request):
        api_auth(request)
        if not _platform_login_enabled():
            return {"ok": False, "detail": "扫码登录功能未启用（platform_login.enabled）"}
        platform = str(platform or "").lower()
        if platform not in SUPPORTED_PLATFORMS:
            return {"ok": False, "detail": f"不支持的平台: {platform}"}
        if platform == "web":
            return {"ok": False, "detail": "网页客服为服务端原生渠道，无需扫码登录。"}
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        account_id = str((body or {}).get("account_id") or "")
        # M4：账号配置（防关联）
        cfg_label = str((body or {}).get("label") or "")
        cfg_group = str((body or {}).get("group") or "")
        cfg_proxy_id = str((body or {}).get("proxy_id") or "")
        cfg_use_fp = bool((body or {}).get("use_fingerprint") or False)
        _ensure_login_providers()
        # 解析登录方式：缺省取该平台默认 mode
        platform_cfg = _platform_login_cfg().get(platform, {}) or {}
        modes = list_modes(platform, platform_cfg)
        mode = str((body or {}).get("mode") or "").lower()
        if not mode:
            mode = next((m["mode"] for m in modes if m["recommended"]),
                        modes[0]["mode"] if modes else "device")
        if not mode_available(platform, mode):
            return {"ok": False, "detail": f"{platform} 的「{mode}」登录方式暂未启用"}

        status_map = status_via_adapters(request, _INBOX_ADAPTERS)
        baseline = online_account_keys(status_map, platform)
        # 重连场景：目标账号当前离线，从基线移除，使其上线时被判定为「新上线」
        if account_id:
            baseline.discard(account_id)

        # M4：解析代理 + 生成/绑定指纹，组装 provider 上下文
        fingerprint_id = ""
        login_ctx: Dict[str, Any] = {}
        if cfg_proxy_id:
            try:
                px = get_proxy_pool().get(cfg_proxy_id, mask=False)
                if px:
                    login_ctx["proxy"] = px
            except Exception:
                logger.debug("读取代理失败", exc_info=True)
        if cfg_use_fp:
            try:
                fp = get_fingerprint_store().create(seed=cfg_label or None,
                                                    label=cfg_label)
                fingerprint_id = fp["fingerprint_id"]
                login_ctx["fingerprint"] = fp["profile"]
            except Exception:
                logger.debug("生成指纹失败", exc_info=True)

        qr_url = qr_image = instruction = ""
        poll_fn = cancel_fn = provider_state = None
        provider = get_login_provider(platform, mode)
        if provider is not None:
            try:
                try:
                    info = provider(request, platform, mode, account_id, ctx=login_ctx)
                except TypeError:
                    info = provider(request, platform, mode, account_id)
                if inspect.isawaitable(info):
                    info = await info
                info = info or {}
                qr_url = str(info.get("qr_url") or "")
                qr_image = str(info.get("qr_image") or "")
                instruction = str(info.get("instruction") or "")
                account_id = str(info.get("account_id") or account_id)
                poll_fn = info.get("poll")
                cancel_fn = info.get("cancel")
                provider_state = info.get("state")
            except Exception:
                logger.debug("登录 provider[%s:%s] 失败（回落设备端指引）",
                             platform, mode, exc_info=True)

        sess = get_login_manager().create(
            platform, account_id, baseline, mode=mode,
            qr_url=qr_url, qr_image=qr_image, instruction=instruction,
            label=cfg_label, group=cfg_group,
            proxy_id=cfg_proxy_id, fingerprint_id=fingerprint_id,
            provider_state=provider_state, poll_fn=poll_fn, cancel_fn=cancel_fn,
        )
        # 落库：重连/已知账号即记录（mode + 代理 + 指纹持久化，供编排器重启后正确拉起）
        if account_id:
            try:
                get_account_registry().upsert(
                    platform, account_id, mode=mode, status="pending",
                    label=cfg_label or None, proxy_id=cfg_proxy_id or None,
                    fingerprint_id=fingerprint_id or None)
                if cfg_proxy_id:
                    get_proxy_pool().assign(cfg_proxy_id, f"{platform}:{account_id}")
            except Exception:
                logger.debug("账号注册表 upsert 失败", exc_info=True)
        return {
            "ok": True,
            "login_id": sess.login_id,
            "mode": sess.mode,
            "status": sess.status,
            "qr_url": sess.qr_url,
            "qr_image": sess.qr_image or _login_qr_data_url(sess.qr_url),
            "instruction": sess.instruction,
        }

    @app.get("/api/platforms/{platform}/login/{login_id}/status")
    async def api_platform_login_status(platform: str, login_id: str, request: Request):
        api_auth(request)
        platform = str(platform or "").lower()
        sess = get_login_manager().get(login_id)
        if sess is None:
            return {"ok": True, "status": "expired", "detail": "登录会话不存在或已过期"}
        if sess.status in ("authorized", "failed"):
            return {"ok": True, "status": sess.status, "detail": sess.detail}
        if sess.is_expired():
            sess.status = "expired"
            return {"ok": True, "status": "expired"}
        # provider 事件驱动（protocol/web）：直接问 provider 拿登录结果
        if sess.poll_fn is not None:
            try:
                res = sess.poll_fn(sess)
                if inspect.isawaitable(res):
                    res = await res
                res = res or {}
                st = str(res.get("status") or sess.status)
                sess.status = st
                if st == "authorized" and res.get("account_id"):
                    _persist_login_account(platform, str(res["account_id"]), sess)
                poll_qr = str(res.get("qr_url") or sess.qr_url)
                if poll_qr and not sess.qr_url:
                    sess.qr_url = poll_qr
                return {"ok": True, "status": st,
                        "detail": str(res.get("detail") or ""),
                        "qr_url": poll_qr,
                        "qr_image": str(res.get("qr_image") or "")
                        or _login_qr_data_url(poll_qr)}
            except Exception:
                logger.debug("provider poll 失败", exc_info=True)
                return {"ok": True, "status": sess.status}
        # 实时对比基线：检测到该平台有新账号上线 → 判定登录成功
        try:
            status_map = status_via_adapters(request, _INBOX_ADAPTERS)
            online = online_account_keys(status_map, platform)
            new_accounts = online - sess.baseline
            if new_accounts:
                sess.status = "authorized"
                for aid in new_accounts:
                    _persist_login_account(platform, aid, sess)
                return {"ok": True, "status": "authorized"}
        except Exception:
            logger.debug("登录状态轮询失败", exc_info=True)
        return {"ok": True, "status": sess.status, "instruction": sess.instruction}

    @app.post("/api/platforms/{platform}/login/{login_id}/cancel")
    async def api_platform_login_cancel(platform: str, login_id: str, request: Request):
        api_auth(request)
        sess = get_login_manager().get(login_id)
        if sess is not None and sess.cancel_fn is not None:
            try:
                res = sess.cancel_fn(sess)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                logger.debug("provider cancel 失败", exc_info=True)
        get_login_manager().cancel(login_id)
        return {"ok": True}
