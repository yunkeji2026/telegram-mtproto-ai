"""C1-1 白标/贴牌：品牌设置读写 API。

- ``GET  /api/admin/branding`` —— 当前生效品牌 + 白标可用性（驱动设置页）。
- ``POST /api/admin/branding`` —— 保存品牌到 config.local.yaml overlay 并即时刷新
  模板 globals；去除厂商署名仅旗舰版（white_label 功能位）生效。
"""

from __future__ import annotations

import logging

from fastapi import Request
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_branding_routes(app, *, api_auth, config_manager=None) -> None:
    @app.get("/api/admin/branding")
    async def api_admin_branding_get(request: Request):
        api_auth(request)
        from src.utils.branding import get_branding
        lic = None
        try:
            from src.licensing import get_license_manager
            lic = get_license_manager().status()
        except Exception:
            logger.debug("授权状态读取失败（已忽略）", exc_info=True)
        cfg = getattr(config_manager, "config", None) or {}
        brand = get_branding(cfg, lic)
        raw = dict((cfg.get("brand") or {}))
        brand["hide_powered_by"] = bool(raw.get("hide_powered_by", False))
        brand["ok"] = True
        return brand

    @app.post("/api/admin/branding")
    async def api_admin_branding_save(request: Request):
        api_auth(request)
        if config_manager is None:
            return {"ok": False, "detail": tr(request, "err.svc.config_manager_not_ready")}
        try:
            body = await request.json()
        except Exception:
            body = {}
        values = dict((body or {}).get("values") or body or {})
        ok, msg = config_manager.save_branding(values)
        if not ok:
            return {"ok": False, "detail": msg}
        # 即时刷新模板 globals（无需重启）
        refresh = getattr(app.state, "branding_refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                logger.debug("品牌 globals 刷新失败（已忽略）", exc_info=True)
        from src.utils.branding import get_branding
        lic = None
        try:
            from src.licensing import get_license_manager
            lic = get_license_manager().status()
        except Exception:
            pass
        brand = get_branding(config_manager.config or {}, lic)
        brand["ok"] = True
        brand["detail"] = msg
        return brand
