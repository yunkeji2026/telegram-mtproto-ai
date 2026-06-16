"""C0-1 授权状态只读 API。

``GET /api/admin/license`` —— 返回当前授权状态快照（state / plan / 到期 / 席位 /
渠道 / 功能位 / 提示）。本阶段仅展示，不做强制；前端「授权状态卡」消费此端点。
"""

from __future__ import annotations

from fastapi import Request


def register_license_routes(app, *, api_auth) -> None:
    @app.get("/api/admin/license")
    async def api_admin_license(request: Request):
        api_auth(request)
        try:
            from src.licensing import get_license_manager

            st = get_license_manager().status()
            data = st.to_dict()
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
            data["ok"] = True
            return data
        except Exception as e:  # pragma: no cover
            return {"ok": False, "msg": str(e)}
