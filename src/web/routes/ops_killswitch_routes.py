"""G1 全局 Kill-Switch 管理 API（反封号护栏三件套）。

把 ``src/ops/kill_switch.py`` 的紧急停发开关暴露给运营：查看 / 置位 / 解除。
读用 api_auth，写（置位/解除）用 manage_ops 权限（与「确认运维事件」同级）。

端点：
- GET    /api/ops/kill-switch       当前生效的作用域列表
- POST   /api/ops/kill-switch       置位 {scope?, reason?, ttl_sec?}（scope 缺省=global）
- DELETE /api/ops/kill-switch       解除 {scope?}（scope 缺省=global）
"""

from __future__ import annotations

from fastapi import HTTPException, Request


def register_ops_killswitch_routes(app, ctx) -> None:
    """挂载 /api/ops/kill-switch* 到 app。"""
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write

    def _ks():
        from src.ops.kill_switch import get_kill_switch
        return get_kill_switch()

    def _actor(request: Request) -> str:
        try:
            return str(
                request.session.get("username")
                or request.session.get("role")
                or "admin"
            )
        except Exception:
            return "admin"

    @app.get("/api/ops/kill-switch")
    async def api_ops_killswitch_status(request: Request):
        """当前所有生效的紧急停发作用域（global 优先）。"""
        _api_auth(request)
        items = _ks().status()
        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "frozen": any(i["scope"] == "global" for i in items),
        }

    @app.post("/api/ops/kill-switch")
    async def api_ops_killswitch_set(request: Request):
        """🛑 置位紧急停发。需 manage_ops 权限。

        body：``{scope?, reason?, ttl_sec?}``。scope 缺省 ``global``（一键全停）；
        ttl_sec>0 时到点自动恢复（防「停了忘开」）。
        """
        _api_write("manage_ops")(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        scope = str((body or {}).get("scope") or "global").strip()
        reason = str((body or {}).get("reason") or "")[:500]
        try:
            ttl_sec = float((body or {}).get("ttl_sec") or 0)
        except (TypeError, ValueError):
            ttl_sec = 0.0
        try:
            rec = _ks().set(scope, reason=reason, actor=_actor(request), ttl_sec=ttl_sec)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "set": rec}

    @app.delete("/api/ops/kill-switch")
    async def api_ops_killswitch_clear(request: Request):
        """🔓 解除紧急停发。需 manage_ops 权限。body：``{scope?}``（缺省 global）。"""
        _api_write("manage_ops")(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        scope = str((body or {}).get("scope") or "global").strip()
        try:
            existed = _ks().clear(scope)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "scope": scope, "was_active": existed}
