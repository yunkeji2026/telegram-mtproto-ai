"""C0-1 授权状态只读 API + C4 粘贴激活。

``GET  /api/admin/license``          —— 授权状态快照（state / plan / 到期 / 席位 /
渠道 / 功能位 / 提示 + P0-4 字符额度用量）。前端「授权状态卡」消费此端点。
``POST /api/admin/license/reload``   —— 重新读取授权文件。
``POST /api/admin/license/activate`` —— C4：粘贴已签发的 license key → 先 preview
验签（不落盘），active/grace 才写 ``config/license.key`` 并 reload。私钥/签发
永远不在本产品侧——本端点只接受**厂商已签发**的 key。

C5（半自动发卡 API：/api/admin/license/issue）**刻意不做**：签发需要 Ed25519
私钥托管 + 订单/CRM 对接，属厂商基础设施（见 scripts/license_tool.py 的离线
签发 CLI）。产品侧永不持有私钥，故不留可误开的路由 stub，仅此注释存档。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def _quota_snapshot() -> dict:
    """P0-4 字符额度快照（无额度授权 → included=0/used=0）。绝不抛。"""
    try:
        from src.licensing.quota_store import check_license_quota

        q = check_license_quota()
        return {
            "included_chars": q.get("included", 0),
            "used_chars": q.get("used", 0),
            "remaining_chars": q.get("remaining"),
            "exceeded": q.get("exceeded", False),
        }
    except Exception:
        return {"included_chars": 0, "used_chars": 0,
                "remaining_chars": None, "exceeded": False}


def register_license_routes(app, *, api_auth) -> None:
    @app.get("/api/admin/license")
    async def api_admin_license(request: Request):
        api_auth(request)
        try:
            from src.licensing import get_license_manager

            st = get_license_manager().status()
            data = st.to_dict()
            data["quota"] = _quota_snapshot()
            data["ok"] = True
            return data
        except Exception as e:  # pragma: no cover - 异常兜底
            return {
                "ok": True,
                "state": "unavailable",
                "licensed": False,
                "plan": "community",
                "messages": [f"授权状态读取失败：{e}"],
            }

    @app.post("/api/admin/license/reload")
    async def api_admin_license_reload(request: Request):
        api_auth(request)
        try:
            from src.licensing import get_license_manager

            st = get_license_manager().reload()
            data = st.to_dict()
            data["quota"] = _quota_snapshot()
            data["ok"] = True
            return data
        except Exception as e:  # pragma: no cover
            return {"ok": False, "msg": str(e)}

    @app.post("/api/admin/license/activate")
    async def api_admin_license_activate(request: Request):
        """C4 粘贴激活：验签通过（active/grace）才写 ``config/license.key`` + reload。

        防呆：invalid / expired / unavailable 的 key **不落盘**——否则把坏 key 写进
        文件反而使现有授权降级。写盘失败回滚语义＝不 reload（原授权不受影响）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = str((body or {}).get("key") or "").strip()
        if not token:
            raise HTTPException(
                400, tr(request, "err.ws.field_required", field="key"))

        from src.licensing import get_license_manager

        mgr = get_license_manager()
        preview = mgr.preview_token(token)
        if preview.state not in ("active", "grace"):
            raise HTTPException(
                400, tr(request, "err.lic.activate_invalid", state=preview.state))

        path = mgr.license_path
        if not path:
            raise HTTPException(500, tr(request, "err.lic.no_license_path"))
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(token + "\n", encoding="utf-8")
        except Exception as e:
            logger.warning("[license] 激活写盘失败：%s", e)
            raise HTTPException(
                500, tr(request, "err.lic.save_failed", err=e))

        st = mgr.reload()
        logger.info(
            "[license] 粘贴激活成功：plan=%s state=%s customer=%s lic_id=%s",
            st.plan, st.state, st.customer or "-", st.lic_id or "-",
        )
        data = st.to_dict()
        data["quota"] = _quota_snapshot()
        data["ok"] = True
        return data
