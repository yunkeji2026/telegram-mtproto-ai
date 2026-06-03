"""开发者工具页面路由（Phase E1 续拆，从 admin.py 抽出）。

端点：
  GET  /developer
  POST /developer/auth
  POST /developer/logout

依赖：templates / require_auth / config_manager（经 AdminRouteContext）。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

_DEV_PASSWORD = "Along2026"


def register_developer_page_routes(app, ctx) -> None:
    templates = ctx.templates
    config_manager = ctx.config_manager
    _require_auth = ctx.require_auth

    @app.get("/developer", response_class=HTMLResponse)
    async def developer_page(request: Request):
        _require_auth(request)
        dev_unlocked = request.session.get("dev_unlocked", False)
        page_ctx: dict = {"dev_unlocked": dev_unlocked, "dev_error": ""}
        if dev_unlocked:
            cfg = config_manager.config or {}
            page_ctx.update({
                "ai": cfg.get("ai", {}),
                "voice_ai": (
                    ((cfg.get("messenger_rpa") or {}).get("voice_output") or {})
                    if isinstance(cfg.get("messenger_rpa"), dict)
                    else {}
                ),
                "wb": cfg.get("web_admin", {}),
                "tg": cfg.get("telegram", {}),
                "notif": cfg.get("notifications", cfg.get("webhook", {})),
            })
        return templates.TemplateResponse(request, "developer.html", page_ctx)

    @app.post("/developer/auth", response_class=HTMLResponse)
    async def developer_auth(request: Request):
        _require_auth(request)
        form = await request.form()
        password = (form.get("password") or "").strip()
        if password == _DEV_PASSWORD:
            request.session["dev_unlocked"] = True
            return RedirectResponse("/developer", status_code=303)
        return templates.TemplateResponse(request, "developer.html", {
            "dev_unlocked": False,
            "dev_error": "密码错误，请重试",
        })

    @app.post("/developer/logout")
    async def developer_logout(request: Request):
        _require_auth(request)
        request.session.pop("dev_unlocked", None)
        return RedirectResponse("/developer", status_code=303)
