"""认证 / 用户 / 会话管理路由 — 从 admin.py 抽出（Phase E1）。

端点（与抽出前逐行一致）：
  GET  /login                       POST /login        GET /logout
  GET  /setup                       POST /api/setup    POST /api/setup/test-ai
  POST /api/change-password
  GET  /users   POST /users/create  POST /users/update/{user_id}  POST /users/delete/{user_id}
  GET  /api/sessions   POST /api/sessions/{jti}/revoke   POST /api/sessions/revoke-all

依赖通过 register 传入（闭包 + 单例）；模块级常量（templates / ROLE_*）直接 import，
减少参数穿线。
"""

from __future__ import annotations

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.utils.web_user_store import ROLE_ADMIN, ROLE_AGENT, ROLE_MASTER, ROLE_LABELS
from src.web.web_i18n import tr


def register_auth_user_routes(
    app,
    *,
    user_store,
    token,
    config_manager,
    audit_store=None,
    require_auth,
    require_role,
):
    # 复用 admin.py 模块级 templates（与其余页面同一 Jinja 环境）
    from src.web.admin import templates

    # ── 登录 / 登出 ───────────────────────────────────────────
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        has_users = user_store.user_count() > 0
        return templates.TemplateResponse(request, "login.html", {
            "error": "", "has_users": has_users
        })

    @app.post("/login")
    async def login_submit(request: Request, auth_token: str = Form(None),
                           username: str = Form(None), password: str = Form(None)):
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")[:200]
        # multi-user login
        if username and password:
            user = user_store.verify(username, password)
            if user:
                jti = user_store.create_session(user["username"], user["role"], ip, ua)
                request.session["user_id"] = user["id"]
                request.session["username"] = user["username"]
                request.session["role"] = user["role"]
                request.session["display_name"] = user.get("display_name", username)
                request.session["jti"] = jti
                # 角色化落地：坐席→收件箱工作台；管理员(主管)→数据看板；系统主/只读→管理后台
                _role = user["role"]
                if _role == ROLE_AGENT:
                    _dest = "/workspace"
                elif _role == ROLE_ADMIN:
                    _dest = "/workspace/dash"
                else:
                    _dest = "/"
                resp = RedirectResponse(_dest, status_code=303)
                # 语言跟人走：登录即套用该用户保存的 UI 语言（无偏好则不动，沿用 cookie/默认）
                _lang = (user.get("lang") or "").strip().lower()
                if _lang in ("zh", "en"):
                    resp.set_cookie("ui_lang", _lang, max_age=365 * 86400)
                return resp
            return templates.TemplateResponse(request, "login.html", {
                "error": tr(request, "err.auth.bad_credentials"), "has_users": True
            })
        # legacy token login
        if auth_token and auth_token == token:
            jti = user_store.create_session("admin", ROLE_MASTER, ip, ua)
            request.session["auth"] = token
            request.session["role"] = ROLE_MASTER
            request.session["username"] = "admin"
            request.session["jti"] = jti
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {
            "error": tr(request, "token_error"),
            "has_users": user_store.user_count() > 0
        })

    @app.get("/logout")
    async def logout(request: Request):
        jti = request.session.get("jti")
        if jti:
            user_store.revoke_session(jti)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ── 首次使用配置向导 ──────────────────────────────────────
    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        if user_store.user_count() > 0:
            return RedirectResponse("/", status_code=303)
        ai_cfg = config_manager.config.get("ai", {}) if hasattr(config_manager, "config") else {}
        return templates.TemplateResponse(request, "setup.html", {
            "ai_cfg": ai_cfg,
        })

    @app.post("/api/setup")
    async def setup_submit(request: Request):
        """初始化第一个管理员账户（仅在无用户时可调用）"""
        if user_store.user_count() > 0:
            raise HTTPException(400, tr(request, "err.auth.already_initialized"))
        body = await request.json()
        username = (body.get("username") or "").strip()
        password = body.get("password", "")
        confirm  = body.get("confirm", "")
        api_key  = (body.get("api_key") or "").strip()
        base_url = (body.get("base_url") or "").strip()
        model    = (body.get("model") or "").strip()

        if not username or not password:
            raise HTTPException(400, tr(request, "err.auth.user_pass_required"))
        if len(password) < 6:
            raise HTTPException(400, tr(request, "su_js_003"))
        if password != confirm:
            raise HTTPException(400, tr(request, "err.auth.pwd_mismatch_signup"))

        # 创建 master 账户
        result = user_store.create_user(username, password, ROLE_MASTER,
                                        display_name=username)
        if not result:
            raise HTTPException(500, tr(request, "err.auth.create_failed"))

        # 保存 AI 配置（如果提供了）
        if api_key:
            if not hasattr(config_manager, "config") or config_manager.config is None:
                config_manager.config = {}
            if "ai" not in config_manager.config:
                config_manager.config["ai"] = {}
            config_manager.config["ai"]["api_key"] = api_key
            if base_url:
                config_manager.config["ai"]["base_url"] = base_url
            if model:
                config_manager.config["ai"]["model"] = model
            try:
                config_manager.save()
            except Exception:
                pass  # AI 配置保存失败不影响主流程

        if audit_store:
            audit_store.log("setup", "create_master_user", username)
        return {"ok": True, "username": result["username"]}

    @app.post("/api/setup/test-ai")
    async def setup_test_ai(request: Request):
        """测试 AI API Key 有效性（无需登录，供向导页使用）"""
        body = await request.json()
        api_key  = (body.get("api_key") or "").strip()
        # 从当前配置读取默认值，不再硬编码 DeepSeek
        _ai_cfg = config_manager.get_ai_config() if config_manager else {}
        base_url = (body.get("base_url") or _ai_cfg.get("base_url", "")).rstrip("/")
        model    = (body.get("model") or _ai_cfg.get("model", "gemini-2.5-flash"))
        if api_key == "_keep_":
            api_key = _ai_cfg.get("api_key", "")
        if not api_key:
            raise HTTPException(400, tr(request, "err.auth.api_key_required"))
        if not base_url:
            raise HTTPException(400, tr(request, "err.auth.base_url_required"))
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 5},
                )
            if resp.status_code == 200:
                return {"ok": True, "msg": "连接成功"}
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")[:200]
            except Exception:
                detail = resp.text[:200]
            return {"ok": False, "msg": f"API 返回 HTTP {resp.status_code}: {detail}"}
        except Exception as e:
            return {"ok": False, "msg": f"连接失败: {e}"}

    # ── 修改密码 ──────────────────────────────────────────────
    @app.post("/api/change-password")
    async def api_change_password(request: Request):
        require_auth(request)
        body = await request.json()
        old_pw = body.get("old_password", "")
        new_pw = body.get("new_password", "")
        if not old_pw or not new_pw:
            raise HTTPException(400, tr(request, "err.auth.old_new_pwd_required"))
        if len(new_pw) < 6:
            raise HTTPException(400, tr(request, "base.shell.pwd_min_len"))
        uname = request.session.get("username", "")
        if not uname:
            raise HTTPException(400, tr(request, "err.auth.unknown_user"))
        user = user_store.verify(uname, old_pw)
        if not user:
            raise HTTPException(400, tr(request, "err.auth.wrong_current_pwd"))
        user_store.update_user(user["id"], password=new_pw)
        if audit_store:
            audit_store.log(uname, "change_password", uname)
        return {"ok": True}

    # ── 用户管理 ──────────────────────────────────────────────
    @app.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request):
        require_role(request, "users")
        users = user_store.list_users()
        return templates.TemplateResponse(request, "users.html", {
            "users": users, "role_labels": ROLE_LABELS, "msg": ""
        })

    @app.post("/users/create")
    async def users_create(request: Request, username: str = Form(...),
                           password: str = Form(...), role: str = Form("viewer"),
                           display_name: str = Form("")):
        require_role(request, "users")
        ajax = "application/json" in request.headers.get("accept", "")
        if len(password) < 6:
            if ajax:
                return {"ok": False, "detail": tr(request, "su_js_003")}
            users = user_store.list_users()
            return templates.TemplateResponse(request, "users.html", {
                "users": users, "role_labels": ROLE_LABELS,
                "msg": "密码至少 6 位", "msg_ok": False
            })
        result = user_store.create_user(username, password, role, display_name)
        if not result:
            if ajax:
                return {"ok": False, "detail": tr(request, "err.auth.user_exists_or_bad_role", username=username)}
            users = user_store.list_users()
            return templates.TemplateResponse(request, "users.html", {
                "users": users, "role_labels": ROLE_LABELS,
                "msg": f"创建失败：用户名 '{username}' 已存在或角色无效", "msg_ok": False
            })
        if audit_store:
            audit_store.log(request.session.get("username", ""), "create_user", username)
        if ajax:
            return {"ok": True, "id": result["id"], "username": result["username"], "role": result["role"]}
        return RedirectResponse("/users", status_code=303)

    @app.post("/users/update/{user_id}")
    async def users_update(user_id: int, request: Request, role: str = Form(None),
                           password: str = Form(None), enabled: str = Form(None)):
        require_role(request, "users")
        kw = {}
        if role:
            kw["role"] = role
        if password:
            kw["password"] = password
        if enabled is not None:
            kw["enabled"] = enabled == "1"
        user_store.update_user(user_id, **kw)
        if audit_store:
            audit_store.log(request.session.get("username", ""), "update_user", str(user_id))
        if "application/json" in request.headers.get("accept", ""):
            updated = user_store.get_user_by_id(user_id)
            return {
                "ok": True,
                "role": updated.get("role") if updated else None,
                "enabled": updated.get("enabled") if updated else None,
            }
        return RedirectResponse("/users", status_code=303)

    @app.post("/users/delete/{user_id}")
    async def users_delete(user_id: int, request: Request):
        require_role(request, "users")
        ok = user_store.delete_user(user_id)
        if audit_store and ok:
            audit_store.log(request.session.get("username", ""), "delete_user", str(user_id))
        if "application/json" in request.headers.get("accept", ""):
            return {"ok": ok, "detail": "" if ok else "无法删除（主帐号不可删除）"}
        return RedirectResponse("/users", status_code=303)

    # ── 会话管理 API ─────────────────────────────────────────
    @app.get("/api/sessions")
    async def api_sessions_list(request: Request):
        """列出所有活跃 session（仅 master）"""
        require_role(request, "users")
        sessions = user_store.list_sessions(include_revoked=False)
        current_jti = request.session.get("jti", "")
        for s in sessions:
            s["is_current"] = (s["jti"] == current_jti)
            # 脱敏 jti（仅传前8位用于展示，保留完整用于操作）
            s["jti_display"] = s["jti"][:8] + "…"
        return {"sessions": sessions, "total": len(sessions)}

    @app.post("/api/sessions/{jti}/revoke")
    async def api_session_revoke(jti: str, request: Request):
        """撤销指定 session（强制该设备下线）"""
        require_role(request, "users")
        current_jti = request.session.get("jti", "")
        if jti == current_jti:
            raise HTTPException(400, tr(request, "err.auth.cannot_kick_self"))
        user_store.revoke_session(jti)
        actor = request.session.get("username", "")
        if audit_store:
            audit_store.log(actor, "revoke_session", jti[:8])
        return {"ok": True}

    @app.post("/api/sessions/revoke-all")
    async def api_sessions_revoke_all(request: Request):
        """撤销除自己外的所有 session"""
        require_role(request, "users")
        current_jti = request.session.get("jti", "")
        sessions = user_store.list_sessions()
        revoked = 0
        for s in sessions:
            if s["jti"] != current_jti:
                user_store.revoke_session(s["jti"])
                revoked += 1
        actor = request.session.get("username", "")
        if audit_store and revoked:
            audit_store.log(actor, "revoke_all_sessions", f"revoked={revoked}")
        return {"ok": True, "revoked": revoked}
