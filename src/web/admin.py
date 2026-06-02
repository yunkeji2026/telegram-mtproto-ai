"""AI 助手系统 — Web 管理后台（FastAPI + Jinja2）"""

import asyncio
import csv
import difflib
import hashlib
import io
import json
import logging
import threading
import time
from datetime import datetime, timezone
import zipfile
import yaml
from pathlib import Path

from src.utils.domain_policy import effective_domain_name
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, Form, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

logger = logging.getLogger("WebAdmin")

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR), auto_reload=True)

# ── 模型显示名映射（UI 层展示，不影响实际 API 调用）─────────────
_MODEL_DISPLAY_MAP: dict = {
    "deepseek-chat":      "claude-4.6-oups-high",
    "deepseek-v3":        "claude-4.6-oups-high-v3",
    "deepseek-reasoner":  "claude-4.6-oups-high-reasoner",
    "deepseek-coder":     "claude-4.6-oups-high-coder",
    "deepseek":           "claude-4.6-oups-high",
}

def _display_model(name: str) -> str:
    """Jinja2 过滤器：将内部模型标识符转换为界面显示名称"""
    if not name:
        return name
    return _MODEL_DISPLAY_MAP.get(name, name)

templates.env.filters["display_model"] = _display_model

# Jinja2 global: site_name — dynamically set from domain pack display_name
# Default to generic name; overridden in create_app() when domain pack loads
templates.env.globals["site_name"] = "智控王客户转化聊天系统"
templates.env.globals["site_name_short"] = "智控王"

# ── /api/human-escalation/schedule-status 短时缓存（减轻 is_within + 粗估重复计算）──
_SCHEDULE_STATUS_LOCK = threading.Lock()
_SCHEDULE_STATUS_CACHE: Optional[Tuple[tuple, float, Dict[str, Any]]] = None


def _human_escalation_cfg_hash(he: Dict[str, Any]) -> str:
    try:
        return hashlib.md5(
            json.dumps(he, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except Exception:
        return str(hash(str(he)))


def _schedule_status_cache_get(key: tuple, ttl_sec: float) -> Optional[Dict[str, Any]]:
    if ttl_sec <= 0:
        return None
    with _SCHEDULE_STATUS_LOCK:
        global _SCHEDULE_STATUS_CACHE
        ent = _SCHEDULE_STATUS_CACHE
        if ent is None:
            return None
        k, exp, payload = ent
        if k == key and time.monotonic() < exp:
            return payload
    return None


def _schedule_status_cache_set(key: tuple, payload: Dict[str, Any], ttl_sec: float) -> None:
    if ttl_sec <= 0:
        return
    with _SCHEDULE_STATUS_LOCK:
        global _SCHEDULE_STATUS_CACHE
        _SCHEDULE_STATUS_CACHE = (key, time.monotonic() + ttl_sec, payload)


def invalidate_schedule_status_cache() -> None:
    """配置变更后清空 schedule-status 缓存（可选调用）。"""
    global _SCHEDULE_STATUS_CACHE
    with _SCHEDULE_STATUS_LOCK:
        _SCHEDULE_STATUS_CACHE = None


def create_app(config_manager, audit_store=None, boot_ts: float = 0,
               telegram_client=None, event_tracker=None, log_buffer=None) -> FastAPI:
    # Load domain pack manifest for web integration（支付域在插件关闭时映射为 conversion）
    _cfg_obj = config_manager.config if isinstance(getattr(config_manager, "config", None), dict) else {}
    domain_name = effective_domain_name(_cfg_obj)
    domain_display = config_manager.config.get("web_admin", {}).get("site_name", "")
    domain_web_pages: list = []
    domain_dashboard_widgets: list = []
    _domain_manifest: dict = {}
    _project_root = Path(config_manager.config_path).parent.parent if hasattr(config_manager, "config_path") else Path(".")
    try:
        _mf = _project_root / "domains" / domain_name / "manifest.yaml"
        if _mf.exists():
            import yaml as _y
            with open(_mf, "r", encoding="utf-8") as _f:
                _domain_manifest = _y.safe_load(_f) or {}
            if not domain_display:
                domain_display = _domain_manifest.get("display_name", "")
            _web_section = _domain_manifest.get("web", {})
            domain_web_pages = _web_section.get("pages", [])
            domain_dashboard_widgets = _web_section.get("dashboard_widgets", [])
    except Exception:
        pass
    # Add domain template directory to Jinja2 search path
    if _project_root:
        _domain_tpl_dir = _project_root / "domains" / domain_name / "web" / "templates"
        if _domain_tpl_dir.is_dir():
            from jinja2 import FileSystemLoader
            templates.env.loader = FileSystemLoader(
                [str(_domain_tpl_dir), str(_TEMPLATE_DIR)]
            )
    if domain_display:
        templates.env.globals["site_name"] = domain_display
        templates.env.globals["site_name_short"] = domain_display

    app = FastAPI(title=templates.env.globals["site_name"], docs_url=None, redoc_url=None)
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
    app.state.kb_conflict_checkers = []
    app.state.intent_display_names_extra = {}
    app.state.config_manager = config_manager
    app.state.telegram_client = telegram_client
    # P23-B: 让外部 router 注册器（如 rpa_overview_routes 的 audit 钩子）能拿到 audit_store
    app.state.audit_store = audit_store

    web_cfg = config_manager.config.get("web_admin", {})
    secret = web_cfg.get("secret_key", "change-me-in-production")
    token = web_cfg.get("auth_token", "")

    session_max_age = int(web_cfg.get("session_max_age", 7200))
    app.add_middleware(SessionMiddleware, secret_key=secret, max_age=session_max_age)

    # ── CORS ──────────────────────────────────────────────────
    cors_origins = web_cfg.get("cors_origins", ["*"])
    if isinstance(cors_origins, str):
        cors_origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # ── CSRF 防护 ─────────────────────────────────────────────
    import secrets as _secrets
    _CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        if request.method in _CSRF_SAFE_METHODS:
            response = await call_next(request)
            if not request.cookies.get("csrf_token"):
                csrf_tok = _secrets.token_hex(16)
                response.set_cookie("csrf_token", csrf_tok, httponly=False, samesite="strict")
            return response
        auth_h = request.headers.get("Authorization", "")
        if auth_h.startswith("Bearer "):
            return await call_next(request)
        _line_exempt = getattr(request.app.state, "line_webhook_path", None)
        if _line_exempt and request.url.path == _line_exempt:
            return await call_next(request)
        cookie_tok = request.cookies.get("csrf_token", "")
        header_tok = request.headers.get("X-CSRF-Token", "")
        if cookie_tok and header_tok and cookie_tok == header_tok:
            return await call_next(request)
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")
        host = request.headers.get("host", "")
        if host:
            expected = {f"http://{host}", f"https://{host}"}
            if origin and origin in expected:
                return await call_next(request)
            if referer:
                for exp in expected:
                    if referer.startswith(exp + "/") or referer == exp:
                        return await call_next(request)
        ct = request.headers.get("content-type", "")
        if "application/json" in ct:
            return JSONResponse(status_code=403, content={"detail": "CSRF token missing or invalid"})
        return await call_next(request)

    # ── HTML 页面禁缓存（防止浏览器缓存旧版模板） ──────────────
    @app.middleware("http")
    async def nocache_html_middleware(request: Request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if "text/html" in ct and not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # ── 管理端限流（固定窗口计数器，内存高效） ──────────────────
    _rate_buckets: Dict[str, list] = {}  # ip -> [window_start, count]
    _RATE_WINDOW = int(web_cfg.get("rate_limit_window", 60))
    _RATE_MAX = int(web_cfg.get("rate_limit_max", 120))
    _rate_gc_ts: float = 0.0
    _rate_whitelist = set(web_cfg.get("rate_limit_whitelist", ["127.0.0.1", "::1"]))

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        nonlocal _rate_gc_ts
        client_ip = request.client.host if request.client else "unknown"
        if client_ip in _rate_whitelist:
            return await call_next(request)
        _line_path = getattr(request.app.state, "line_webhook_path", None)
        if _line_path and request.url.path == _line_path:
            return await call_next(request)
        now = time.time()
        bucket = _rate_buckets.get(client_ip)
        if not bucket or now - bucket[0] >= _RATE_WINDOW:
            _rate_buckets[client_ip] = [now, 1]
        else:
            bucket[1] += 1
            if bucket[1] > _RATE_MAX:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests"},
                    headers={"Retry-After": str(_RATE_WINDOW)},
                )
        if now - _rate_gc_ts > _RATE_WINDOW * 2:
            _rate_gc_ts = now
            stale = [k for k, v in _rate_buckets.items() if now - v[0] >= _RATE_WINDOW * 2]
            for k in stale:
                _rate_buckets.pop(k, None)
        return await call_next(request)

    # ── P25-A: 全局 body size 限制（防大 JSON / chunked 流式攻击） ──────
    # 比 P24-C 的 per-route _read_json_body 更强：
    #   1. 覆盖所有 POST/PUT/PATCH 路由（不只是 intent-tags）
    #   2. 流式逐块累计 + 即时熔断（attacker 流 100MB 我方只缓存到 limit 字节就中止）
    #   3. P25-B: 413 写 audit_log（潜在攻击信号）
    # 例外：path 在 _BODY_LIMIT_EXEMPT 中的不受限（如文件上传端点 — 当前无）
    _BODY_LIMIT_DEFAULT = int(web_cfg.get("max_body_bytes", 2 * 1024 * 1024))
    # P26-C: 从 config.yaml::web_admin.body_limits 加载 per-path 上限（dict path->bytes）
    _raw_overrides = web_cfg.get("body_limits") or {}
    _BODY_LIMIT_OVERRIDES: Dict[str, int] = {}
    if isinstance(_raw_overrides, dict):
        for _k, _v in _raw_overrides.items():
            try:
                _BODY_LIMIT_OVERRIDES[str(_k)] = int(_v)
            except (TypeError, ValueError):
                continue
    # 兼容旧字段 max_body_bytes_intent_tags_write
    if "/api/rpa/intent-tags" not in _BODY_LIMIT_OVERRIDES:
        _BODY_LIMIT_OVERRIDES["/api/rpa/intent-tags"] = int(
            web_cfg.get("max_body_bytes_intent_tags_write", 4 * 1024 * 1024))
    _BODY_LIMIT_EXEMPT_PREFIXES: tuple = tuple(web_cfg.get("max_body_exempt_prefixes", []))
    # P25-B / P26-D: 413 攻击信号防抖 — 用通用 AuditThrottle
    from src.utils.audit_throttle import AuditThrottle as _AuditThrottle
    _body_oversize_throttle = _AuditThrottle(window_sec=5.0, max_keys=4096)
    # P26-B: 暴露 413 计数器给 /api/rpa/metrics（label = path），格式 {path: count}
    app.state.web_body_oversize_counter = {}

    def _audit_oversize(request: Request, path: str, observed: int, limit: int) -> None:
        """P25-B / P26-D: 413 攻击信号写 audit log（per-IP 5s 防抖）。"""
        try:
            ip = request.client.host if request.client else "unknown"
            # P26-B: 计数（不防抖，每次都加 — Prometheus 看真实攻击量）
            counter = app.state.web_body_oversize_counter
            counter[path] = counter.get(path, 0) + 1
            # 审计写入（5s 防抖防 DB flood）
            if not _body_oversize_throttle.should_emit(ip):
                return
            actor = getattr(getattr(request.state, "user", None), "username", "unknown")
            audit_store.log(actor, "web_body_oversize_rejected",
                            f"path={path} observed={observed} limit={limit} ip={ip}")
        except Exception:
            pass

    def _413_response(limit: int, detail: str) -> JSONResponse:
        """P27-B: 413 响应统一带 X-Body-Limit 头（告知客户端上限）+ Connection:close。"""
        return JSONResponse(
            status_code=413,
            content={"detail": detail, "max_body_bytes": limit},
            headers={
                "Connection": "close",
                "X-Body-Limit": str(limit),
            },
        )

    @app.middleware("http")
    async def body_size_limit_middleware(request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS", "DELETE"):
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in _BODY_LIMIT_EXEMPT_PREFIXES):
            return await call_next(request)
        limit = _BODY_LIMIT_OVERRIDES.get(path, _BODY_LIMIT_DEFAULT)
        # 1) Content-Length 预检（快速失败，不读 body）
        cl = request.headers.get("content-length")
        if cl and cl.isdigit():
            if int(cl) > limit:
                _audit_oversize(request, path, int(cl), limit)
                return _413_response(limit,
                    f"request body too large (declared {cl}, max {limit})")
        # 2) 流式累积（防伪 Content-Length + chunked 上传）
        body_chunks: list = []
        received = 0
        while True:
            msg = await request._receive()
            mtype = msg.get("type")
            if mtype == "http.disconnect":
                return JSONResponse(status_code=499,
                                     content={"detail": "client disconnected"})
            if mtype != "http.request":
                continue
            chunk = msg.get("body", b"") or b""
            if chunk:
                received += len(chunk)
                if received > limit:
                    _audit_oversize(request, path, received, limit)
                    return _413_response(limit,
                        f"request body too large (streamed {received}, max {limit})")
                body_chunks.append(chunk)
            if not msg.get("more_body", False):
                break
        # 3) 重放给下游
        full_body = b"".join(body_chunks)
        replayed = {"done": False}

        async def replay_receive():
            if replayed["done"]:
                return {"type": "http.disconnect"}
            replayed["done"] = True
            return {"type": "http.request", "body": full_body, "more_body": False}

        request._receive = replay_receive
        return await call_next(request)

    # RBAC user store
    from src.utils.web_user_store import (WebUserStore, ROLE_MASTER, ROLE_ADMIN,
                                           ROLE_VIEWER, ROLE_LABELS, PAGE_PERMISSIONS,
                                           UI_MODE_SIMPLE, UI_MODE_FULL, UI_MODE_LABELS,
                                           SIMPLE_MODE_CORE_PAGES, SIMPLE_MODE_MORE_PAGES,
                                           resolve_ui_mode, is_page_visible_in_simple)
    cfg_dir = config_manager.config_path.parent
    user_store = WebUserStore(cfg_dir / "web_users.db")
    if user_store.user_count() == 0:
        user_store._ensure_master("admin", token or "admin123")

    # SSE 配置热更新推送（端点注册在 _api_auth 之后，见下方）
    import asyncio as _asyncio
    _sse_clients: list = []

    def _broadcast_config_reload():
        for q in list(_sse_clients):
            try:
                q.put_nowait({"event": "config_reload", "ts": time.time()})
            except Exception:
                pass

    config_manager.on_reload(_broadcast_config_reload)

    from src.web.web_i18n import get_translations

    @app.middleware("http")
    async def inject_i18n(request: Request, call_next):
        lang = request.query_params.get("lang") or request.cookies.get("ui_lang", "zh")
        request.state.ui_lang = lang
        request.state.i18n = get_translations(lang)
        response = await call_next(request)
        return response

    _PATH_TO_ACTIVE = {
        "/": "dash", "/templates": "tpl",
        "/strategies": "strategies", "/strategy-analytics": "strategy-analytics",
        "/audit": "audit", "/diff": "diff", "/logs": "logs",
        "/analytics": "analytics", "/help": "help", "/users": "users",
        "/knowledge": "knowledge", "/learner": "learner", "/settings": "settings",
        "/cases": "cases", "/episodic-memory": "episodic",
        "/line-rpa": "line_rpa",
        "/messenger-rpa": "messenger_rpa",
        "/whatsapp-rpa": "whatsapp_rpa",
        "/rpa-overview": "rpa_overview",
        "/personas": "personas",
        "/ai-studio": "ai_studio",
    }
    for _dp in domain_web_pages:
        _PATH_TO_ACTIVE[_dp["path"]] = _dp["key"]

    old_render = templates.TemplateResponse

    def _enrich_context(request: Request, context: dict) -> dict:
        """向模板上下文注入 i18n / 用户身份 / active 导航 / ui_mode 等公共字段"""
        i18n = get_translations()
        ui_lang = "zh"
        if hasattr(request, "state"):
            i18n = getattr(request.state, "i18n", i18n)
            ui_lang = getattr(request.state, "ui_lang", ui_lang)
        context.setdefault("i18n", i18n)
        context.setdefault("ui_lang", ui_lang)
        session_role = ""
        try:
            session_role = request.session.get("role", "")
        except Exception:
            pass
        if not session_role:
            try:
                if request.session.get("auth") == token and token:
                    session_role = ROLE_MASTER
            except Exception:
                pass
        context.setdefault("user_role", session_role)
        context.setdefault("username", "")
        try:
            context["username"] = request.session.get("username", "")
        except Exception:
            pass
        context.setdefault("display_name", context.get("username", ""))
        try:
            dn = request.session.get("display_name", "")
            if dn:
                context["display_name"] = dn
        except Exception:
            pass
        context.setdefault("page_perms", PAGE_PERMISSIONS)
        try:
            path = request.url.path.rstrip("/") or "/"
            context.setdefault("active", _PATH_TO_ACTIVE.get(path, ""))
        except Exception:
            context.setdefault("active", "")

        # ── UI 模式（简洁 / 完整）──────────────────────────────
        cookie_mode = request.cookies.get("ui_mode", "")
        effective_mode = resolve_ui_mode(cookie_mode, session_role)
        context.setdefault("ui_mode", effective_mode)
        context.setdefault("ui_mode_labels", UI_MODE_LABELS)
        context.setdefault("simple_core_pages", SIMPLE_MODE_CORE_PAGES)
        context.setdefault("simple_more_pages", SIMPLE_MODE_MORE_PAGES)
        context.setdefault("domain_name", domain_name)
        context.setdefault("domain_web_pages", domain_web_pages)
        context.setdefault("domain_dashboard_widgets", domain_dashboard_widgets)

        # ── Embedded mode（用于 AI 工作室 iframe 嵌入，去掉外层 chrome）──
        try:
            qs_emb = request.query_params.get("embedded", "") == "1"
        except Exception:
            qs_emb = False
        context.setdefault("embedded", qs_emb)

        return context

    def i18n_render(request: Request, name: str, context: dict = None, **kwargs):
        """新签名：i18n_render(request, template_name, context_dict)"""
        ctx = dict(context or {})
        _enrich_context(request, ctx)
        return old_render(request, name, ctx, **kwargs)

    templates.TemplateResponse = i18n_render

    # ── 自动快照 helper ───────────────────────────────────────
    _SNAP_MAX = 15  # 每个前缀保留的最大快照数量

    def _auto_snapshot(prefix: str, content: str, actor: str = "web_admin") -> str | None:
        """
        在 {config_dir}/snapshots/ 中创建自动快照。
        文件名：{prefix}_{YYYYMMDD_HHMMSS}_{actor}.yaml
        超出 _SNAP_MAX 时自动删除最旧的快照（滚动保留）。
        返回快照 stem（无扩展名），失败时返回 None。
        """
        import re as _re
        try:
            cfg_dir = config_manager.config_path.parent
            snap_dir = cfg_dir / "snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%Y%m%d_%H%M%S")
            safe_actor = _re.sub(r"[^\w\-]", "_", actor or "sys")[:20]
            stem = f"{prefix}_{ts}_{safe_actor}"
            snap_file = snap_dir / f"{stem}.yaml"
            snap_file.write_text(content, encoding="utf-8")
            # 滚动清理：同一 prefix 的快照超出上限时删旧的
            all_snaps = sorted(
                [f for f in snap_dir.glob(f"{prefix}_*.yaml")],
                key=lambda f: f.stat().st_mtime
            )
            while len(all_snaps) > _SNAP_MAX:
                oldest = all_snaps.pop(0)
                try:
                    oldest.unlink()
                except OSError:
                    pass
            return stem
        except Exception:
            return None

    @app.get("/set_lang")
    async def set_lang(request: Request, lang: str = "zh"):
        resp = RedirectResponse(request.headers.get("referer", "/"), status_code=303)
        resp.set_cookie("ui_lang", lang, max_age=365 * 86400)
        return resp

    @app.get("/set_ui_mode")
    async def set_ui_mode(request: Request, mode: str = "", next: str = ""):
        if mode not in (UI_MODE_SIMPLE, UI_MODE_FULL):
            mode = UI_MODE_SIMPLE
        redirect_to = next.strip() if next.strip() else ""
        if not redirect_to:
            referer = request.headers.get("referer", "")
            if referer:
                from urllib.parse import urlparse
                parsed = urlparse(referer)
                redirect_to = parsed.path or "/"
            else:
                redirect_to = "/cases" if mode == UI_MODE_SIMPLE else "/"
        if mode == UI_MODE_SIMPLE and redirect_to.rstrip("/") in ("", "/"):
            redirect_to = "/cases"
        resp = RedirectResponse(redirect_to, status_code=303)
        resp.set_cookie("ui_mode", mode, max_age=365 * 86400)
        return resp

    def _check_session_valid(request: Request) -> bool:
        """检查 session jti 是否有效（未被撤销）"""
        jti = request.session.get("jti")
        if not jti:
            return True  # 老式 session（无 jti），兼容过渡期
        return user_store.touch_session(jti)

    def _require_auth(request: Request):
        # 无用户且无 token → 引导至首次设置向导
        if user_store.user_count() == 0 and not token:
            raise HTTPException(status_code=303, headers={"Location": "/setup"})
        if request.session.get("user_id"):
            if not _check_session_valid(request):
                request.session.clear()
                raise HTTPException(status_code=303, headers={"Location": "/login"})
            return
        if token and request.session.get("auth") == token:
            if not _check_session_valid(request):
                request.session.clear()
                raise HTTPException(status_code=303, headers={"Location": "/login"})
            return
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    def _api_auth(request: Request):
        if request.session.get("user_id"):
            if not _check_session_valid(request):
                request.session.clear()
                raise HTTPException(status_code=401, detail="Session 已失效，请重新登录")
            return
        if not token:
            return
        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {token}":
            return
        sess = request.session.get("auth")
        if sess == token:
            if not _check_session_valid(request):
                request.session.clear()
                raise HTTPException(status_code=401, detail="Session 已失效，请重新登录")
            return
        raise HTTPException(status_code=401, detail="Unauthorized")

    def _require_role(request: Request, page_key: str):
        _require_auth(request)
        role = request.session.get("role", ROLE_MASTER)
        if not user_store.can_access_page(role, page_key):
            raise HTTPException(status_code=403, detail="无权访问此页面")

    _PATH_TO_PAGE = {
        "/": "dash", "/templates": "tpl", "/templates/update": "tpl",
        "/strategies": "strategies", "/strategy-analytics": "strategies",
        "/audit": "audit", "/audit/export": "audit",
        "/help": "help", "/diff": "diff",
        "/logs": "logs", "/logs/stream": "logs",
        "/analytics": "analytics",
        "/import": "import", "/export": "export",
        "/cases": "cases", "/episodic-memory": "episodic",
        "/line-rpa": "line_rpa",
    }
    for _dp in domain_web_pages:
        _PATH_TO_PAGE[_dp["path"]] = _dp["key"]
        _PATH_TO_PAGE[_dp["path"] + "/update"] = _dp["key"]

    # ── SSE 端点（需 _api_auth 已定义）──────────────────────
    @app.get("/api/events")
    async def sse_events(request: Request):
        _api_auth(request)
        q: _asyncio.Queue = _asyncio.Queue(maxsize=50)
        _sse_clients.append(q)

        async def _gen():
            try:
                while True:
                    try:
                        msg = await _asyncio.wait_for(q.get(), timeout=30)
                        import json as _j
                        yield f"event: {msg.get('event','message')}\ndata: {_j.dumps(msg)}\n\n"
                    except _asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                    if await request.is_disconnected():
                        break
            finally:
                if q in _sse_clients:
                    _sse_clients.remove(q)

        return StreamingResponse(_gen(), media_type="text/event-stream",
                                 headers={
                                     "Cache-Control": "no-cache, no-transform",
                                     "X-Accel-Buffering": "no",
                                     "Connection": "keep-alive",
                                 })

    def _api_write(perm: str):
        def _check(request: Request):
            _api_auth(request)
            role = request.session.get("role", "")
            if not role:
                auth_h = request.headers.get("Authorization", "")
                if token and (auth_h == f"Bearer {token}" or request.session.get("auth") == token):
                    role = ROLE_MASTER
            if not user_store.can_write(role, perm):
                raise HTTPException(403, "无权执行此操作")
        return _check

    def _page_auth(request: Request):
        _require_auth(request)
        path = request.url.path.rstrip("/") or "/"
        page_key = _PATH_TO_PAGE.get(path)
        if page_key:
            role = request.session.get("role", "")
            if not role and token and request.session.get("auth") == token:
                role = ROLE_MASTER
            if not user_store.can_access_page(role, page_key):
                raise HTTPException(status_code=403, detail="无权访问此页面")

    # ── 认证/用户/会话路由（Phase E1：抽到 routes/auth_user_routes.py） ──
    try:
        from src.web.routes.auth_user_routes import register_auth_user_routes

        register_auth_user_routes(
            app,
            user_store=user_store,
            token=token,
            config_manager=config_manager,
            audit_store=audit_store,
            require_auth=_require_auth,
            require_role=_require_role,
        )
    except Exception:
        import logging as _log_au

        _log_au.getLogger("admin").warning(
            "auth_user 路由注册失败", exc_info=True
        )

    # 系统状态/指标/reactivation dry-run/审计热力图 已抽到 routes/monitoring_routes.py（批 G2-①）
    # （register_monitoring_routes 在 _admin_ctx + kb_store 就绪后调用，见下方 learner 注册附近）

    # ── 全局系统配置 ──────────────────────────────────────────

    # /settings 页已抽到 routes/settings_routes.py（见下方 register_settings_routes）

    # ── 开发者工具（密码保护） ──────────────────────────────────
    _DEV_PASSWORD = "Along2026"

    @app.get("/developer", response_class=HTMLResponse)
    async def developer_page(request: Request):
        _require_auth(request)
        dev_unlocked = request.session.get("dev_unlocked", False)
        ctx: dict = {"dev_unlocked": dev_unlocked, "dev_error": ""}
        if dev_unlocked:
            cfg = config_manager.config or {}
            ctx.update({
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
        return templates.TemplateResponse(request, "developer.html", ctx)

    @app.post("/developer/auth", response_class=HTMLResponse)
    async def developer_auth(request: Request):
        _require_auth(request)
        form = await request.form()
        password = (form.get("password") or "").strip()
        if password == _DEV_PASSWORD:
            request.session["dev_unlocked"] = True
            return RedirectResponse("/developer", status_code=303)
        cfg = config_manager.config or {}
        return templates.TemplateResponse(request, "developer.html", {
            "dev_unlocked": False,
            "dev_error": "密码错误，请重试",
        })

    @app.post("/developer/logout")
    async def developer_logout(request: Request):
        _require_auth(request)
        request.session.pop("dev_unlocked", None)
        return RedirectResponse("/developer", status_code=303)

    # /api/settings/save 与 /api/reply-logic 已抽到 routes/settings_routes.py

    # ── 人工转接路由（Phase E1：抽到 routes/human_escalation_routes.py） ──
    try:
        from src.web.routes.human_escalation_routes import (
            register_human_escalation_routes,
        )

        register_human_escalation_routes(
            app,
            api_auth=_api_auth,
            api_write=_api_write,
            config_manager=config_manager,
            telegram_client=telegram_client,
            audit_store=audit_store,
        )
    except Exception:
        import logging as _log_he

        _log_he.getLogger("admin").warning(
            "human_escalation 路由注册失败", exc_info=True
        )

    # intent-keywords / test-intent / test-webhook 已抽到 routes/settings_routes.py

    # ── 知识库分析报告 ────────────────────────────────────────

    # kb/report + kb-images/{filename} 已抽到 routes/kb_routes.py（批 5L）

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _=Depends(_page_auth)):
        # 简洁模式首页 → 直接到 Cases 工作队列
        _role = request.session.get("role", "")
        if not _role and token and request.session.get("auth") == token:
            _role = ROLE_MASTER
        _eff_mode = resolve_ui_mode(request.cookies.get("ui_mode", ""), _role)
        if _eff_mode == UI_MODE_SIMPLE:
            return RedirectResponse("/cases", status_code=303)

        import asyncio as _aio

        def _build_dashboard_data():
            tpl_data = config_manager.get_dynamic_templates_config() or {}
            rates_data = config_manager.get_exchange_rates_config() or {}
            channels = rates_data.get("channels", {})

            recent_audit = []
            if audit_store:
                recent_audit = audit_store.query(limit=10)

            _has_ch_widget = any(w.get("key") == "channel_health" for w in domain_dashboard_widgets)
            health = []
            if _has_ch_widget and channels:
                from src.utils.channel_health import compute_health_scores
                health = compute_health_scores(channels, event_tracker)

            recent_ops = []
            for e in recent_audit:
                recent_ops.append({
                    "action": e.get("action", ""),
                    "operator": e.get("user_id", ""),
                    "channel": e.get("target", ""),
                    "ts": e.get("ts", ""),
                })
            return tpl_data, channels, recent_ops, health

        tpl_data, channels, recent_ops, health = await _aio.to_thread(_build_dashboard_data)

        uptime = int(time.time() - boot_ts) if boot_ts else 0
        hours, remainder = divmod(uptime, 3600)
        mins, secs = divmod(remainder, 60)
        uptime_str = f"{hours}h {mins}m {secs}s"

        return templates.TemplateResponse(request, "dashboard.html", {
            "templates": tpl_data,
            "channels": channels,
            "recent_ops": recent_ops,
            "uptime": uptime_str,
            "uptime_hours": hours,
            "template_count": len(tpl_data),
            "channel_count": len(channels),
            "health": health,
            "domain_name": domain_name,
        })

    @app.get("/templates", response_class=HTMLResponse)
    async def templates_page(request: Request, _=Depends(_page_auth)):
        data = config_manager.get_dynamic_templates_config() or {}
        return templates.TemplateResponse(request, "templates.html", {
            "templates": data, "msg": ""
        })

    @app.post("/templates/update")
    async def templates_update(request: Request, _=Depends(_page_auth),
                               key: str = Form(...), value: str = Form(...)):
        data = config_manager.get_dynamic_templates_config() or {}
        # 保存前先拍快照
        snap_content = yaml.dump(data, allow_unicode=True, default_flow_style=False)
        lines = [l.strip() for l in value.strip().split("\n") if l.strip()]
        data[key] = lines if len(lines) > 1 else (lines[0] if lines else "")
        ok, msg = config_manager.save_templates(data)
        if ok:
            config_manager.invalidate_templates_cache()
            actor = request.session.get("username", "web_admin")
            _auto_snapshot("templates", snap_content, actor)
            import asyncio as _asyncio
            try:
                _asyncio.get_running_loop().create_task(_fire_webhook(
                    "config_change", actor, f"templates.{key}", f"模板 {key} 已更新"))
            except RuntimeError:
                pass
            data = config_manager.get_dynamic_templates_config() or {}
        if "application/json" in request.headers.get("accept", ""):
            return {"ok": ok, "msg": f"已保存 {key}" if ok else f"保存失败: {msg}"}
        return templates.TemplateResponse(request, "templates.html", {
            "templates": data,
            "msg": f"已保存 {key}" if ok else f"保存失败: {msg}"
        })

    # ── AI 策略管理 ───────────────────────────────────────────

    # 意图 key → 中文显示名（策略页「意图→策略映射」与「关联意图」用）
    _INTENT_DISPLAY_NAMES_BASE = {
        "greeting": "问候",
        "test": "测试/自检",
        "complaint": "投诉处理",
        "small_talk": "闲聊",
    }

    def _get_intent_display_names() -> dict:
        merged = dict(_INTENT_DISPLAY_NAMES_BASE)
        merged.update(getattr(app.state, "intent_display_names_extra", {}))
        return merged

    # ── Persona Studio (/personas) ───────────────────────────
    try:
        from src.web.routes.persona_routes import register_persona_routes
        register_persona_routes(
            app, auth_dep=_api_auth, audit_store=audit_store, config_manager=config_manager
        )
    except Exception:
        import logging as _log_pr
        _log_pr.getLogger("admin").debug("Persona API 路由注册跳过", exc_info=True)

    @app.get("/personas", response_class=HTMLResponse)
    async def personas_page(request: Request, _=Depends(_page_auth)):
        from src.utils.persona_manager import PersonaManager
        try:
            pm = PersonaManager.get_instance()
            default_persona = pm.get_persona("")
            bindings = pm.get_all_chat_bindings()
        except Exception:
            default_persona = {}
            bindings = {}
        tg_accounts_stats: dict = {}
        try:
            from src.client.telegram_account_registry import TelegramAccountRegistry
            _tg_cfg = (config_manager.config or {}).get("telegram", {})
            _reg = TelegramAccountRegistry.from_config(_tg_cfg)
            tg_accounts_stats = _reg.stats()
        except Exception:
            pass
        personas_from_cfg = (config_manager.config or {}).get("personas", {})
        return templates.TemplateResponse(request, "personas.html", {
            "default_persona": default_persona,
            "bindings": bindings,
            "personas_cfg": personas_from_cfg,
            "tg_accounts": tg_accounts_stats,
        })

    # ── AI 工作室 (/ai-studio) — 4-Tab 集中入口 ─────────────────────
    @app.get("/ai-studio", response_class=HTMLResponse)
    async def ai_studio_page(request: Request, _=Depends(_page_auth)):
        return templates.TemplateResponse(request, "ai_studio.html", {})

    @app.get("/api/ai-studio/summary")
    async def api_ai_studio_summary(request: Request, _=Depends(_api_auth)):
        """统一统计：人设池 / 情景记忆 / KB草稿 / 关系档案 / 重复人设池告警。"""
        out: Dict[str, Any] = {
            "personas": {"profile_count": 0, "binding_count": 0},
            "memory": {"total_facts": 0, "unique_users": 0, "with_embedding": 0},
            "drafts": {"pending": 0, "approved": 0, "rejected": 0},
            "relations": {"total": 0, "intimate_count": 0, "stages": {}},
            "messenger_rpa_reply_profiles": {"count": 0, "default": ""},
        }

        # 人设池
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            out["personas"]["profile_count"] = len(pm.list_profile_ids())
            out["personas"]["binding_count"] = len(pm.get_all_chat_bindings())
        except Exception:
            pass

        # 情景记忆
        try:
            sm = getattr(telegram_client, "skill_manager", None) if telegram_client else None
            store = getattr(sm, "_episodic_store", None) if sm else None
            if store and store._conn:
                row = store._conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT user_id), "
                    "SUM(CASE WHEN embedding IS NOT NULL AND length(embedding)>=8 THEN 1 ELSE 0 END) "
                    "FROM episodic_memory"
                ).fetchone()
                if row:
                    out["memory"]["total_facts"] = int(row[0] or 0)
                    out["memory"]["unique_users"] = int(row[1] or 0)
                    out["memory"]["with_embedding"] = int(row[2] or 0)
        except Exception:
            pass

        # KB 草稿统计
        try:
            from src.utils.daily_learner import DailyLearner as _DL
            learner = getattr(app.state, "_daily_learner", None)
            if learner is None:
                ai = getattr(telegram_client, "ai_client", None) if telegram_client else None
                kb = getattr(app.state, "kb_store", None)
                if ai and kb:
                    learner = _DL(kb, ai, db_path=getattr(app.state, "kb_db_path", None) or kb._db_path)
                    app.state._daily_learner = learner
            if learner:
                out["drafts"] = learner.stats()
        except Exception:
            pass

        # 关系档案统计（直接 SQL）
        try:
            _contacts = getattr(app.state, "contacts", None)
            cs = getattr(_contacts, "store", None) if _contacts else None
            conn = getattr(cs, "_conn", None) if cs else None
            if conn is not None:
                row = conn.execute(
                    "SELECT "
                    "  SUM(CASE WHEN intimacy_score>=80 THEN 1 ELSE 0 END), "
                    "  SUM(CASE WHEN intimacy_score>=55 AND intimacy_score<80 THEN 1 ELSE 0 END), "
                    "  SUM(CASE WHEN intimacy_score>=25 AND intimacy_score<55 THEN 1 ELSE 0 END), "
                    "  SUM(CASE WHEN intimacy_score<25 OR intimacy_score IS NULL THEN 1 ELSE 0 END), "
                    "  COUNT(*) "
                    "FROM journeys"
                ).fetchone()
                if row:
                    soulmate = int(row[0] or 0)
                    close = int(row[1] or 0)
                    friend = int(row[2] or 0)
                    stranger = int(row[3] or 0)
                    out["relations"]["total"] = int(row[4] or 0)
                    out["relations"]["stages"] = {
                        "stranger": stranger, "friend": friend,
                        "close": close, "soulmate": soulmate,
                    }
                    out["relations"]["intimate_count"] = soulmate + close

                # W3-3A.3：reunion 候选 — 曾深度互动（funnel_stage 已过 INITIAL 且非 LOST/MERGE）
                # 但 intimacy_score 因沉默衰减回落到 stranger 区间（<25）的 journey
                # 这些是「需重激活」的优先目标，应在 ai_studio 关系看板单列展示
                try:
                    rc_row = conn.execute(
                        "SELECT COUNT(*) FROM journeys WHERE "
                        "intimacy_score < 25 AND "
                        "funnel_stage NOT IN ('INITIAL','LOST_HANDOFF',"
                        "'LOST_LINE_SILENT','NEEDS_MANUAL_MERGE')"
                    ).fetchone()
                    out["relations"]["reunion_candidates"] = int(
                        (rc_row[0] if rc_row else 0) or 0
                    )
                except Exception:
                    out["relations"]["reunion_candidates"] = 0

                # W3-3A.3：funnel_stage 分布 — 给运营看「漏斗实况」补充 score 分布
                try:
                    fs_rows = conn.execute(
                        "SELECT funnel_stage, COUNT(*) FROM journeys "
                        "GROUP BY funnel_stage"
                    ).fetchall()
                    out["relations"]["funnel_stages"] = {
                        str(r[0] or "INITIAL"): int(r[1] or 0)
                        for r in (fs_rows or [])
                    }
                except Exception:
                    out["relations"]["funnel_stages"] = {}

                # W3-3B.2：merge_review_queue pending 计数 + auto_merge 高分边界候选
                # 让 ai_studio 关系看板与 reunion 一样有 banner 直达
                try:
                    mr_row = conn.execute(
                        "SELECT COUNT(*) FROM merge_review_queue WHERE status='pending'"
                    ).fetchone()
                    out["relations"]["merge_reviews_pending"] = int(
                        (mr_row[0] if mr_row else 0) or 0
                    )
                    # 边界候选：confidence>=0.85 但因歧义降级到 review 的高质量对
                    # 运营优先处理这些（基本确定是同一个人）
                    hc_row = conn.execute(
                        "SELECT COUNT(*) FROM merge_review_queue "
                        "WHERE status='pending' AND confidence >= 0.85"
                    ).fetchone()
                    out["relations"]["merge_reviews_high_conf"] = int(
                        (hc_row[0] if hc_row else 0) or 0
                    )
                except Exception:
                    out["relations"]["merge_reviews_pending"] = 0
                    out["relations"]["merge_reviews_high_conf"] = 0
        except Exception:
            pass

        # Messenger RPA reply_profiles 重复检测
        try:
            mr_cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
            rp = mr_cfg.get("reply_profiles") or {}
            profs = rp.get("profiles") if isinstance(rp.get("profiles"), list) else []
            out["messenger_rpa_reply_profiles"] = {
                "count": len([p for p in profs if isinstance(p, dict) and p.get("id")]),
                "default": str(rp.get("default") or ""),
            }
        except Exception:
            pass

        return out

    # ── 策略 / 策略分析 / A-B 测试路由（Phase E1 批 3：抽到 routes/strategy_routes.py） ──
    # 首个采用 AdminRouteContext 的批次：把反复出现的核心依赖打包，后续批次复用 _admin_ctx。
    from src.web.web_context import AdminRouteContext

    _admin_ctx = AdminRouteContext(
        config_manager=config_manager,
        audit_store=audit_store,
        telegram_client=telegram_client,
        user_store=user_store,
        token=token,
        page_auth=_page_auth,
        api_auth=_api_auth,
        api_write=_api_write,
        require_auth=_require_auth,
        require_role=_require_role,
        auto_snapshot=_auto_snapshot,
        get_intent_display_names=_get_intent_display_names,
        event_tracker=event_tracker,
        boot_ts=boot_ts,
    )
    try:
        from src.web.routes.strategy_routes import register_strategy_routes

        register_strategy_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_st

        _log_st.getLogger("admin").warning("strategy 路由注册失败", exc_info=True)

    # ── 设置/回复逻辑/意图关键词路由（Phase E1 批 4：复用 _admin_ctx） ──
    try:
        from src.web.routes.settings_routes import register_settings_routes

        register_settings_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_set

        _log_set.getLogger("admin").warning("settings 路由注册失败", exc_info=True)

    def _get_strategy_tracker():
        # 修复潜伏 bug：strategy_routes 抽出时此闭包未在 admin.py 保留，
        # 导致 data-purge/session-stats/export-strategy/daily-report 调用时 NameError。
        # 仅依赖 telegram_client，与 strategy_routes 内同名实现一致。
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                return getattr(sm, "strategy_tracker", None)
        return None

    @app.post("/api/data-purge")
    async def api_data_purge(request: Request, _=Depends(_api_write("import_export"))):
        """手动触发数据清理"""
        rs = config_manager.get_strategies_config()
        retention = rs.get("data_retention", {})
        se_days = int(retention.get("strategy_events_days", 30))
        ge_days = int(retention.get("general_events_days", 90))
        tracker = _get_strategy_tracker()
        se_del = tracker.purge(se_days) if tracker else 0
        ge_del = 0
        if event_tracker and hasattr(event_tracker, "purge"):
            ge_del = event_tracker.purge(ge_days)
        if audit_store:
            audit_store.log(request.session.get("username", "web_admin"),
                            "data_purge", "",
                            f"strategy={se_del},events={ge_del}", "")
        return {"ok": True, "strategy_events_deleted": se_del,
                "general_events_deleted": ge_del}

    @app.get("/api/session-stats")
    async def api_session_stats(request: Request, _=Depends(_api_auth),
                                hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"total_sessions": 0}
        return tracker.session_stats(hours)

    @app.post("/api/apply-param-suggestion")
    async def api_apply_param(request: Request, _=Depends(_api_write("edit_strategy"))):
        """一键应用参数微调建议"""
        body = await request.json()
        sid = body.get("strategy_id")
        param = body.get("param")
        value = body.get("value")
        if not sid or not param or value is None:
            raise HTTPException(400, "Missing strategy_id, param or value")
        rs = config_manager.get_strategies_config()
        strategies = rs.get("strategies", {})
        if sid not in strategies:
            raise HTTPException(404, f"Strategy '{sid}' not found")
        old_val = strategies[sid].get(param)
        strategies[sid][param] = value
        rs["strategies"] = strategies
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm and hasattr(sm, "_refresh_strategies"):
                sm._refresh_strategies()
        if audit_store:
            audit_store.log(request.session.get("username", "web_admin"),
                            "apply_param_suggestion", f"{sid}.{param}",
                            str(old_val), str(value))
        return {"ok": True, "strategy_id": sid, "param": param,
                "old": old_val, "new": value}

    @app.get("/api/export-strategy-events")
    async def api_export_events(request: Request, _=Depends(_api_auth),
                                fmt: str = Query("csv"),
                                hours: int = Query(168, ge=1, le=8760)):
        """导出策略追踪数据为 CSV 或 JSON"""
        tracker = _get_strategy_tracker()
        if not tracker:
            raise HTTPException(404, "Tracker not available")
        cutoff = time.time() - hours * 3600
        rows = tracker._conn.execute(
            "SELECT * FROM strategy_events WHERE ts_epoch >= ? ORDER BY id",
            (cutoff,),
        ).fetchall()
        records = [dict(r) for r in rows]

        if fmt == "json":
            content = json.dumps(records, ensure_ascii=False, indent=2)
            return StreamingResponse(
                io.BytesIO(content.encode("utf-8")),
                media_type="application/json",
                headers={"Content-Disposition":
                          f"attachment; filename=strategy_events_{hours}h.json"})

        buf = io.StringIO()
        if records:
            w = csv.DictWriter(buf, fieldnames=records[0].keys())
            w.writeheader()
            w.writerows(records)
        return StreamingResponse(
            io.BytesIO(buf.getvalue().encode("utf-8-sig")),
            media_type="text/csv",
            headers={"Content-Disposition":
                      f"attachment; filename=strategy_events_{hours}h.csv"})

    @app.get("/api/autopilot-status")
    async def api_autopilot_status(request: Request, _=Depends(_api_auth)):
        rs = config_manager.get_strategies_config()
        ap = rs.get("autopilot", {})
        return {
            "enabled": ap.get("enabled", False),
            "observation_hours": ap.get("observation_hours", 24),
            "check_interval": ap.get("check_interval_messages", 100),
        }

    @app.put("/api/autopilot")
    async def api_update_autopilot(request: Request, _=Depends(_api_write("edit_strategy"))):
        body = await request.json()
        rs = config_manager.get_strategies_config()
        ap = rs.setdefault("autopilot", {})
        for k in ("enabled", "observation_hours", "check_interval_messages"):
            if k in body:
                ap[k] = body[k]
        rs["autopilot"] = ap
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        if audit_store:
            audit_store.log(request.session.get("username", "web_admin"),
                            "update_autopilot", "", "", str(body)[:200])
        return {"ok": True}

    # ── 审计日志 ──────────────────────────────────────

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request, _=Depends(_page_auth),
                         action: str = "", keyword: str = "", limit: int = 50,
                         operator: str = "", channel: str = "",
                         date_from: str = "", date_to: str = "",
                         page: int = 1):
        all_entries = []
        if audit_store:
            all_entries = audit_store.query(limit=500, action=action, keyword=keyword)
        # additional filters
        if operator:
            all_entries = [e for e in all_entries if operator.lower() in str(e.get("user_id", "")).lower()]
        if channel:
            all_entries = [e for e in all_entries if channel.lower() in str(e.get("target", "")).lower()]
        if date_from:
            all_entries = [e for e in all_entries if str(e.get("ts", "")) >= date_from]
        if date_to:
            all_entries = [e for e in all_entries if str(e.get("ts", ""))[:10] <= date_to]
        total = len(all_entries)
        per_page = limit
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        records = all_entries[(page-1)*per_page : page*per_page]
        # collect unique actions/operators for dropdown
        all_actions = sorted(set(e.get("action", "") for e in all_entries if e.get("action")))
        all_operators = sorted(set(str(e.get("user_id", "")) for e in all_entries if e.get("user_id")))
        qs_parts = []
        if action: qs_parts.append(f"action={action}")
        if keyword: qs_parts.append(f"keyword={keyword}")
        if operator: qs_parts.append(f"operator={operator}")
        if channel: qs_parts.append(f"channel={channel}")
        if date_from: qs_parts.append(f"date_from={date_from}")
        if date_to: qs_parts.append(f"date_to={date_to}")
        qs_parts.append(f"limit={limit}")
        query_str = "&".join(qs_parts)
        return templates.TemplateResponse(request, "audit.html", {
            "records": records,
            "total": total, "page": page, "total_pages": total_pages,
            "query_str": query_str,
            "filters": {"action": action, "keyword": keyword, "operator": operator,
                        "channel": channel, "date_from": date_from, "date_to": date_to},
            "all_actions": all_actions, "all_operators": all_operators,
        })

    @app.get("/audit/export")
    async def audit_export(request: Request, _=Depends(_page_auth),
                           action: str = "", operator: str = "",
                           channel: str = "", date_from: str = "", date_to: str = ""):
        """
        导出审计记录为 CSV（支持与 /audit 页面相同的筛选参数）。
        UTF-8 BOM 编码，Excel 直接打开不乱码。
        """
        import csv, io as _io
        all_entries = audit_store.query(limit=10000) if audit_store else []
        # 应用与审计页面相同的过滤逻辑
        if action:
            all_entries = [e for e in all_entries if e.get("action", "").startswith(action)]
        if operator:
            all_entries = [e for e in all_entries if e.get("user_id", "") == operator]
        if channel:
            kw = channel.lower()
            all_entries = [e for e in all_entries if
                           kw in e.get("target", "").lower() or
                           kw in e.get("action", "").lower() or
                           kw in (e.get("new_val") or "").lower()]
        if date_from:
            all_entries = [e for e in all_entries if str(e.get("ts", "")) >= date_from]
        if date_to:
            end = date_to + "T23:59:59"
            all_entries = [e for e in all_entries if str(e.get("ts", "")) <= end]

        buf = _io.StringIO()
        writer = csv.writer(buf)
        # 元数据行（方便接收方了解导出条件）
        writer.writerow(["# 导出时间", time.strftime("%Y-%m-%d %H:%M:%S")])
        writer.writerow(["# 筛选条件",
                         f"操作={action or '全部'}",
                         f"操作人={operator or '全部'}",
                         f"关键词={channel or '无'}",
                         f"日期={date_from or '不限'}~{date_to or '不限'}"])
        writer.writerow(["# 记录总数", len(all_entries)])
        writer.writerow([])  # 空行分隔
        writer.writerow(["序号", "时间", "操作类型", "目标", "操作人", "旧值", "新值", "快照ID"])
        for i, e in enumerate(all_entries, 1):
            writer.writerow([
                i,
                e.get("ts", ""),
                e.get("action", ""),
                e.get("target", ""),
                e.get("user_id", ""),
                e.get("old_val", "") or "",
                e.get("new_val", "") or "",
                e.get("snapshot_id", "") or "",
            ])
        content = buf.getvalue().encode("utf-8-sig")  # BOM for Excel
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"audit_{ts}.csv"
        if action or operator or channel:
            tag = (action or operator or channel or "filtered").replace(" ", "_")[:20]
            filename = f"audit_{tag}_{ts}.csv"
        return StreamingResponse(
            iter([content]),
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.get("/help", response_class=HTMLResponse)
    async def help_page(request: Request, _=Depends(_page_auth)):
        return templates.TemplateResponse(request, "help.html", {"request": request})

    _TRAINING_SLIDES_PATH = Path(__file__).resolve().parents[2] / "docs" / "training" / "客服培训演示_AI助手系统.html"

    @app.get("/training", response_class=HTMLResponse)
    async def training_slides_page(request: Request, _=Depends(_page_auth)):
        """客服培训用全屏 HTML 幻灯片（需登录）。"""
        if not _TRAINING_SLIDES_PATH.is_file():
            raise HTTPException(status_code=404, detail="培训演示文件未找到，请联系管理员部署 docs/training/")
        html = _TRAINING_SLIDES_PATH.read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # ── 快照对比 ──────────────────────────────────────────────

    def _resolve_current_file(prefix: str):
        """根据快照前缀找到对应的当前配置文件路径"""
        cfg_dir = config_manager.config_path.parent
        _PREFIX_FILE_MAP = {
            "templates": cfg_dir / "templates.yaml",
            "exchange_rates": cfg_dir / "exchange_rates.yaml",
            "reply_strategies": cfg_dir / "reply_strategies.yaml",
            "quota": cfg_dir / "quota_rules.yaml",
        }
        for key, path in _PREFIX_FILE_MAP.items():
            if prefix.startswith(key):
                return path
        return None

    @app.get("/diff", response_class=HTMLResponse)
    async def diff_page(request: Request, _=Depends(_page_auth),
                        a: str = "", b: str = ""):
        cfg_dir = config_manager.config_path.parent
        snap_dir = cfg_dir / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        available = sorted([f.stem for f in snap_dir.glob("*.yaml")], reverse=True)

        diff_lines: list = []
        snap_a, snap_b = a, b

        if snap_a:
            file_a = snap_dir / f"{snap_a}.yaml"
            text_a = file_a.read_text(encoding="utf-8").splitlines() if file_a.exists() else []

            if snap_b and snap_b != "__current__":
                # Both are explicit snapshots
                file_b = snap_dir / f"{snap_b}.yaml"
                text_b = file_b.read_text(encoding="utf-8").splitlines() if file_b.exists() else []
                tofile = snap_b
            else:
                # B is current config (default when b is empty or __current__)
                prefix = snap_a.split("_")[0] if "_" in snap_a else snap_a
                current_file = _resolve_current_file(prefix)
                text_b = current_file.read_text(encoding="utf-8").splitlines() if current_file and current_file.exists() else []
                tofile = "当前配置"

            diff_lines = list(difflib.unified_diff(
                text_a, text_b, fromfile=snap_a, tofile=tofile, lineterm=""
            ))

        add_count = sum(1 for l in diff_lines if l.startswith('+') and not l.startswith('+++'))
        rm_count = sum(1 for l in diff_lines if l.startswith('-') and not l.startswith('---'))

        snapshots = []
        for stem in available:
            snapshots.append({"id": stem, "label": stem.replace("_", " ", 1)})

        return templates.TemplateResponse(request, "diff.html", {
            "snapshots": snapshots,
            "selected_a": snap_a, "selected_b": snap_b,
            "diff_lines": diff_lines,
            "add_count": add_count, "rm_count": rm_count,
        })

    @app.post("/api/rollback")
    async def api_rollback(request: Request, _=Depends(_api_write("import_export"))):
        body = await request.json()
        snap_id = body.get("snapshot_id", "")
        if not snap_id:
            raise HTTPException(400, "Missing snapshot_id")
        cfg_dir = config_manager.config_path.parent
        snap_file = cfg_dir / "snapshots" / f"{snap_id}.yaml"
        if not snap_file.exists():
            raise HTTPException(404, f"快照 {snap_id} 不存在")
        content = snap_file.read_text(encoding="utf-8")
        yaml.safe_load(content)
        prefix = snap_id.rsplit("_", 1)[0] if "_" in snap_id else ""
        target = None
        if "templates" in prefix:
            target = cfg_dir / "templates.yaml"
        elif "exchange_rates" in prefix:
            target = cfg_dir / "exchange_rates.yaml"
        elif "reply_strategies" in prefix:
            target = cfg_dir / "reply_strategies.yaml"
        elif "quota" in prefix:
            target = cfg_dir / "quota_rules.yaml"
        if not target:
            raise HTTPException(400, f"无法确定快照 {snap_id} 对应的配置文件")
        import shutil
        if target.exists():
            bak = target.with_suffix(".yaml.pre_rollback")
            shutil.copy2(target, bak)
        target.write_text(content, encoding="utf-8")
        config_manager.invalidate_templates_cache()
        if hasattr(config_manager, "invalidate_exchange_rates_cache"):
            config_manager.invalidate_exchange_rates_cache()
        if audit_store:
            audit_store.log(request.session.get("username", "web_admin"),
                            "rollback", snap_id, "", target.name)
        return {"ok": True, "restored": target.name, "snapshot": snap_id}

    # ── 通知中心 API ───────────────────────────────────────────
    @app.get("/api/activity-stats")
    async def api_activity_stats(request: Request, _=Depends(_page_auth), hours: int = 24):
        """
        返回过去 N 小时的操作统计，用于仪表盘活动看板：
        - hourly: 每小时操作数（供柱状图使用）
        - by_action: 按操作类型的计数（供饼图使用）
        - top_operators: 活跃操作者 Top 5
        - total: 总操作数
        """
        from datetime import datetime as _dt, timedelta as _td
        if not audit_store:
            return {"hourly": [], "by_action": {}, "top_operators": [], "total": 0, "hours": hours}

        cutoff = _dt.now() - _td(hours=hours)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

        import asyncio as _aio
        entries = await _aio.to_thread(
            lambda: audit_store.query(limit=5000, since=cutoff_str)
        )

        # 按小时聚合
        hourly: dict = {}
        for e in entries:
            ts = str(e.get("ts", ""))
            try:
                hour_key = ts[:13].replace("T", " ")  # "YYYY-MM-DD HH"
                hourly[hour_key] = hourly.get(hour_key, 0) + 1
            except Exception:
                pass

        # 填充缺失小时（保证连续性）
        hourly_series = []
        for i in range(hours - 1, -1, -1):
            t = cutoff + _td(hours=i + 1)
            key = t.strftime("%Y-%m-%d %H")
            hourly_series.append({"hour": key, "label": t.strftime("%H:00"), "count": hourly.get(key, 0)})

        # 按操作类型
        by_action: dict = {}
        for e in entries:
            act = e.get("action", "other") or "other"
            # 归类：update/create/delete/batch/import/rollback/auth
            cat = "其他"
            a = act.lower()
            if "update" in a or "set" in a or "save" in a:
                cat = "更新"
            elif "create" in a or "add" in a or "import" in a:
                cat = "新增"
            elif "delete" in a or "remove" in a or "purge" in a:
                cat = "删除"
            elif "batch" in a:
                cat = "批量"
            elif "login" in a or "logout" in a or "auth" in a or "password" in a:
                cat = "鉴权"
            elif "rollback" in a:
                cat = "回滚"
            by_action[cat] = by_action.get(cat, 0) + 1

        # Top 操作者
        op_cnt: dict = {}
        for e in entries:
            op = str(e.get("user_id", "") or "system")
            op_cnt[op] = op_cnt.get(op, 0) + 1
        top_operators = sorted(op_cnt.items(), key=lambda x: -x[1])[:5]

        return {
            "hourly": hourly_series,
            "by_action": by_action,
            "top_operators": [{"name": k, "count": v} for k, v in top_operators],
            "total": len(entries),
            "hours": hours,
        }

    @app.get("/api/health-check")
    async def api_health_check(request: Request, _=Depends(_page_auth)):
        """
        一键系统健康巡检：
        返回按严重程度分级的问题列表（critical / warn / info）和整体评分。
        """
        issues = []

        def _issue(level, category, title, detail="", action_url="", action_label=""):
            issues.append({"level": level, "category": category, "title": title,
                           "detail": detail, "action_url": action_url, "action_label": action_label})

        # 1. 模板配置检查（已迁移至 KB，仅做兼容提示）
        tpl = config_manager.get_dynamic_templates_config() or {}
        if not tpl:
            pass  # 话术已统一至知识库"系统话术"分类，templates.yaml 为空不再告警
        else:
            empty_keys = [k for k, v in tpl.items() if not v or (isinstance(v, list) and not any(v))]
            if empty_keys:
                _issue("warn", "模板", f"{len(empty_keys)} 个旧模板键值为空（建议迁移到知识库）",
                       f"空键: {', '.join(empty_keys[:5])}", "/templates", "查看")

        # 2. 通道健康检查（仅对声明了 channel page 的域生效）
        if any(p.get("key") == "ch" for p in domain_web_pages):
            rates = config_manager.get_exchange_rates_config() or {}
            channels = rates.get("channels", {})
            if not channels:
                _issue("critical", "通道", "无任何通道配置", "Bot 无法处理交易", "/channels", "前往配置")
            else:
                active_channels = [n for n, c in channels.items()
                                   if c.get("status") in ("正常", "active", "启用")]
                if not active_channels:
                    _issue("critical", "通道", "所有通道均已停用",
                           f"共 {len(channels)} 个通道，全部处于非启用状态", "/channels", "查看")
                elif len(active_channels) < len(channels):
                    off = [n for n in channels if channels[n].get("status") not in ("正常", "active", "启用")]
                    off_labels = [f"{n}({channels[n].get('status','?')})" for n in off[:3]]
                    _issue("warn", "通道", f"{len(off)} 个通道非正常",
                           f"异常通道: {', '.join(off_labels)}", "/channels", "查看")
                zero_rate = [n for n, c in channels.items()
                             if str(c.get("fee_rate", "0")).replace("%", "").strip() in ("0", "0.0", "")]
                if zero_rate:
                    _issue("warn", "通道", f"{len(zero_rate)} 个通道费率为 0",
                           f"通道: {', '.join(zero_rate[:3])}", "/channels", "查看")

        # 3. 策略检查
        try:
            rs = config_manager.get_strategies_config()
            strategies = rs.get("strategies", {})
            if not strategies:
                _issue("warn", "策略", "无策略配置", "Bot 将使用默认行为", "/strategies", "前往配置")
            else:
                disabled = [sid for sid, s in strategies.items() if s.get("enabled") is False]
                if len(disabled) == len(strategies):
                    _issue("critical", "策略", "所有策略均已禁用",
                           "Bot AI 回复已完全关闭", "/strategies", "查看")
                elif disabled:
                    _issue("info", "策略", f"{len(disabled)} 个策略已禁用",
                           f"禁用策略: {', '.join(disabled[:3])}", "/strategies", "查看")
        except Exception:
            pass

        # 4. 策略效果检查（质量评分）
        if event_tracker:
            try:
                from src.strategy.strategy_analytics import StrategyAnalytics
                sa = StrategyAnalytics(event_tracker)
                analytics = sa.get_all_strategy_analytics(hours=24)
                low_score = [(sid, a.quality_score) for sid, a in analytics.items()
                             if hasattr(a, "quality_score") and a.quality_score is not None
                             and a.quality_score < 40]
                if low_score:
                    detail = "; ".join(f"{s}:{q:.0f}分" for s, q in low_score[:3])
                    _issue("warn", "效果", f"{len(low_score)} 个策略质量评分低于 40",
                           detail, "/strategy-analytics", "查看分析")
            except Exception:
                pass

        # 5. 审计存储检查
        if audit_store:
            try:
                count = len(audit_store.query(limit=10001))
                if count > 10000:
                    _issue("info", "存储", "审计日志超过 10000 条",
                           f"当前约 {count} 条，建议定期清理或导出", "/audit", "查看")
            except Exception:
                pass

        # 6. 快照检查
        try:
            cfg_dir = config_manager.config_path.parent
            snap_dir = cfg_dir / "snapshots"
            if not snap_dir.exists() or not list(snap_dir.glob("*.yaml")):
                _issue("info", "快照", "暂无配置快照",
                       "建议手动触发一次配置导出以创建基准快照", "/diff", "查看")
        except Exception:
            pass

        # 综合评分：100 - critical×30 - warn×10 - info×2
        score = 100
        for iss in issues:
            score -= {"critical": 30, "warn": 10, "info": 2}.get(iss["level"], 0)
        score = max(0, min(100, score))

        level_summary = {
            "critical": sum(1 for i in issues if i["level"] == "critical"),
            "warn": sum(1 for i in issues if i["level"] == "warn"),
            "info": sum(1 for i in issues if i["level"] == "info"),
        }

        return {
            "score": score,
            "issues": issues,
            "level_summary": level_summary,
            "status": "critical" if level_summary["critical"] > 0
                      else ("warn" if level_summary["warn"] > 0 else "ok"),
        }

    # /api/strategy-history/{strategy_id} 已抽到 routes/strategy_routes.py

    @app.get("/api/notifications")
    async def api_notifications(request: Request, _=Depends(_page_auth)):
        """聚合策略告警 + 最近系统操作，返回通知列表"""
        notifs = []

        # 1. 策略效果告警：quality_score < 40
        if event_tracker:
            try:
                from src.strategy.strategy_analytics import StrategyAnalytics
                sa = StrategyAnalytics(event_tracker)
                summary = sa.summarize(hours=24)
                for s in summary:
                    qs = s.get("quality_score", 100)
                    if qs < 40:
                        notifs.append({
                            "id": f"strategy_{s['strategy_id']}",
                            "type": "strategy",
                            "level": "critical" if qs < 20 else "warn",
                            "title": f"策略告警：{s['strategy_id']}",
                            "body": f"质量评分仅 {qs}/100，建议优化",
                            "ts": "",
                        })
            except Exception:
                pass

        # 2. 最近审计记录（最新 5 条）
        if audit_store:
            try:
                recent = audit_store.query(limit=5)
                for e in recent:
                    notifs.append({
                        "id": f"audit_{e.get('id', '')}",
                        "type": "system",
                        "level": "info",
                        "title": e.get("action", "操作"),
                        "body": e.get("target", ""),
                        "ts": e.get("ts", ""),
                    })
            except Exception:
                pass

        return {"notifications": notifs[:12], "unread": len(notifs)}

    # ── 告警状态 API ───────────────────────────────────────────
    # ── Webhook 通知 ──────────────────────────────────────────
    # 配置存储路径：{config_dir}/webhook_settings.json
    def _get_webhook_cfg() -> dict:
        try:
            cfg_dir = config_manager.config_path.parent
            wp = cfg_dir / "webhook_settings.json"
            if wp.exists():
                import json as _json
                return _json.loads(wp.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"url": "", "secret": "", "enabled": False,
                "events": ["config_change", "kb_change", "escalation_needed", "weekly_report"]}

    def _save_webhook_cfg(cfg: dict):
        try:
            cfg_dir = config_manager.config_path.parent
            import json as _json
            (cfg_dir / "webhook_settings.json").write_text(
                _json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    async def _fire_webhook(event_type: str, actor: str, target: str, summary: str = ""):  # noqa: D401
        """异步发送 Webhook 通知（失败静默，不影响主流程）。

        注：本闭包同步暴露在 ``app.state.fire_webhook`` 上，供其他路由
        模块（如 contacts_routes）按需调用，避免重复实现一遍 webhook 派发。
        """
        cfg = _get_webhook_cfg()
        if not cfg.get("enabled") or not cfg.get("url"):
            return
        if event_type not in cfg.get("events", []):
            return
        import httpx as _httpx, json as _json, hmac as _hmac, hashlib as _hashlib
        payload = {
            "event": event_type,
            "actor": actor,
            "target": target,
            "summary": summary,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Bot-Admin-Event": event_type}
        if cfg.get("secret"):
            sig = _hmac.new(cfg["secret"].encode(), body, _hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = f"sha256={sig}"
        try:
            async with _httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(cfg["url"], content=body, headers=headers)
                resp.raise_for_status()
        except Exception:
            pass  # 静默失败，Webhook 不影响主操作

    # 暴露给其他路由模块复用（如 contacts_routes 的 /api/relations/digest/push）
    app.state.fire_webhook = _fire_webhook

    @app.get("/api/webhook-settings")
    async def api_get_webhook(request: Request, _=Depends(_api_write("import_export"))):
        cfg = _get_webhook_cfg()
        cfg.pop("secret", None)  # 不回传 secret
        return cfg

    @app.put("/api/webhook-settings")
    async def api_put_webhook(request: Request, _=Depends(_api_write("import_export"))):
        body = await request.json()
        cfg = _get_webhook_cfg()
        for k in ("url", "enabled", "events"):
            if k in body:
                cfg[k] = body[k]
        if "secret" in body and body["secret"]:
            cfg["secret"] = body["secret"]
        _save_webhook_cfg(cfg)
        if audit_store:
            audit_store.log(request.session.get("username", "api"), "update_webhook_settings", "", "", "")
        return {"ok": True}

    @app.post("/api/webhook-test")
    async def api_test_webhook(request: Request, _=Depends(_api_write("import_export"))):
        cfg = _get_webhook_cfg()
        if not cfg.get("url"):
            raise HTTPException(400, "Webhook URL 未配置")
        await _fire_webhook("test", request.session.get("username", "admin"), "test", "This is a test notification")
        return {"ok": True, "msg": "测试通知已发送（如未收到请检查 URL 和网络）"}

    @app.get("/api/snapshots")
    async def api_list_snapshots(request: Request, _=Depends(_api_auth),
                                 prefix: str = "", limit: int = 30):
        """列出可用快照（支持按 prefix 过滤，如 templates / exchange_rates）"""
        cfg_dir = config_manager.config_path.parent
        snap_dir = cfg_dir / "snapshots"
        if not snap_dir.exists():
            return {"snapshots": [], "total": 0}
        glob_pat = f"{prefix}_*.yaml" if prefix else "*.yaml"
        files = sorted(snap_dir.glob(glob_pat), key=lambda f: f.stat().st_mtime, reverse=True)
        result = []
        for f in files[:limit]:
            parts = f.stem.split("_", 3)
            result.append({
                "id": f.stem,
                "prefix": parts[0] if parts else "",
                "ts": "_".join(parts[1:3]) if len(parts) >= 3 else "",
                "actor": parts[3] if len(parts) > 3 else "",
                "size": f.stat().st_size,
                "mtime": int(f.stat().st_mtime),
            })
        return {"snapshots": result, "total": len(files)}

    @app.get("/api/alert-status")
    async def api_alert_status(request: Request, _=Depends(_page_auth)):
        """聚合所有系统告警状态，供仪表盘告警横幅使用"""
        import asyncio as _aio

        def _compute_alerts():
            alerts = []

            # 1. 通道健康告警（仅声明了 channel_health widget 的域）
            if any(w.get("key") == "channel_health" for w in domain_dashboard_widgets):
                rates_data = config_manager.get_exchange_rates_config() or {}
                channels = rates_data.get("channels", {})
                if channels:
                    from src.utils.channel_health import compute_health_scores
                    health = compute_health_scores(channels, event_tracker)
                    critical_channels = [h for h in health if h["grade"] == "critical"]
                    warning_channels = [h for h in health if h["grade"] == "warning"]
                    if critical_channels:
                        names = "、".join(h["display_name"] for h in critical_channels[:3])
                        alerts.append({
                            "level": "critical",
                            "type": "channel",
                            "title": f"{len(critical_channels)} 个通道异常",
                            "body": f"异常通道：{names}。请立即检查通道配置和状态。",
                            "action_url": "/channels",
                            "action_label": "查看通道",
                        })
                    elif warning_channels:
                        names = "、".join(h["display_name"] for h in warning_channels[:3])
                        alerts.append({
                            "level": "warn",
                            "type": "channel",
                            "title": f"{len(warning_channels)} 个通道警告",
                            "body": f"警告通道：{names}，健康评分偏低。",
                            "action_url": "/channels",
                            "action_label": "查看通道",
                        })

            # 2. 策略质量告警
            try:
                from src.strategy.strategy_analytics import StrategyAnalytics
                sa = StrategyAnalytics(event_tracker) if event_tracker else None
                if sa:
                    summary = sa.summarize(hours=24)
                    bad = [s for s in summary if s.get("quality_score", 100) < 40]
                    if bad and len(bad) == len(summary):
                        alerts.append({
                            "level": "critical",
                            "type": "strategy",
                            "title": "所有策略质量评分过低",
                            "body": f"{len(bad)} 个策略质量评分均低于 40 分，AI 效果可能严重下降。",
                            "action_url": "/strategy-analytics",
                            "action_label": "查看分析",
                        })
                    elif bad:
                        strats = "、".join(s["strategy_id"] for s in bad[:3])
                        alerts.append({
                            "level": "warn",
                            "type": "strategy",
                            "title": f"{len(bad)} 个策略质量偏低",
                            "body": f"策略 {strats} 质量评分低于 40 分，建议优化。",
                            "action_url": "/strategy-analytics",
                            "action_label": "查看分析",
                        })
            except Exception:
                pass

            highest_level = "ok"
            if any(a["level"] == "critical" for a in alerts):
                highest_level = "critical"
            elif any(a["level"] == "warn" for a in alerts):
                highest_level = "warn"

            return {
                "alerts": alerts,
                "highest_level": highest_level,
                "alert_count": len(alerts),
            }

        return await _aio.to_thread(_compute_alerts)

    # ── 实时日志 ──────────────────────────────────────────────

    @app.get("/api/trigger-decisions")
    async def api_trigger_decisions(request: Request, limit: int = 50):
        """读取最近的触发器决策日志（JSON lines）"""
        _api_auth(request)
        limit = max(1, min(200, limit))
        log_path = Path("logs/trigger_decisions.log")
        if not log_path.exists():
            return {"decisions": [], "total": 0}

        import asyncio as _aio

        def _read_tail():
            try:
                file_size = log_path.stat().st_size
                read_size = min(file_size, 256 * 1024)
                with open(log_path, "rb") as f:
                    if file_size > read_size:
                        f.seek(file_size - read_size)
                    raw = f.read()
                tail_text = raw.decode("utf-8", errors="ignore")
                lines = tail_text.splitlines()
                decisions = []
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        decisions.append(obj)
                        if len(decisions) >= limit:
                            break
                    except (json.JSONDecodeError, ValueError):
                        pass
                return {"decisions": decisions, "total": file_size // 120}
            except Exception:
                return {"decisions": [], "total": 0}

        return await _aio.to_thread(_read_tail)

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request, _=Depends(_page_auth), limit: int = 200):
        recent = []
        if log_buffer:
            recent = log_buffer.get_recent(limit)
        return templates.TemplateResponse(request, "logs.html", {
            "recent": recent, "limit": limit,
        })

    @app.get("/logs/stream")
    async def logs_stream(request: Request, _=Depends(_page_auth)):
        if not log_buffer:
            return StreamingResponse(iter([]), media_type="text/event-stream")

        async def _generate():
            import asyncio as _asyncio, json as _json
            q = log_buffer.subscribe()
            try:
                while True:
                    try:
                        # 30 秒超时：超时则发心跳，防止代理/Nginx 断连
                        entry = await _asyncio.wait_for(q.get(), timeout=30.0)
                        yield f"data: {_json.dumps(entry, ensure_ascii=False)}\n\n"
                    except _asyncio.TimeoutError:
                        yield "data: ping\n\n"
            except Exception:
                pass
            finally:
                log_buffer.unsubscribe(q)

        return StreamingResponse(_generate(), media_type="text/event-stream",
                                 headers={
                                     "Cache-Control": "no-cache, no-transform",
                                     "X-Accel-Buffering": "no",
                                     "Connection": "keep-alive",
                                 })

    @app.get("/analytics", response_class=HTMLResponse)
    async def analytics_page(request: Request, _=Depends(_page_auth), hours: int = 24):
        data = {"cmd_stats": [], "hourly": [], "top_users": [], "resp_dist": {}, "total": 0}
        if event_tracker:
            data["cmd_stats"] = event_tracker.command_stats(hours)
            data["hourly"] = event_tracker.hourly_trend(hours)
            data["top_users"] = event_tracker.top_users(hours)
            data["resp_dist"] = event_tracker.response_time_distribution(hours)
            data["total"] = event_tracker.total_events(hours)
        data["hours"] = hours
        return templates.TemplateResponse(request, "analytics.html", {**data})

    @app.get("/cases", response_class=HTMLResponse)
    async def cases_page(request: Request, _=Depends(_page_auth)):
        return templates.TemplateResponse(request, "cases.html", {})

    # ── RESTful API ────────────────────────────────────────────

    @app.get("/api/analytics")
    async def analytics_api(request: Request, _=Depends(_api_auth), hours: int = 24):
        if not event_tracker:
            return {"error": "tracker not available"}
        return {
            "cmd_stats": event_tracker.command_stats(hours),
            "hourly": event_tracker.hourly_trend(hours),
            "top_users": event_tracker.top_users(hours),
            "resp_dist": event_tracker.response_time_distribution(hours),
            "total": event_tracker.total_events(hours),
        }

    @app.get("/api/templates")
    async def api_get_templates(request: Request, _=Depends(_api_auth)):
        return config_manager.get_dynamic_templates_config() or {}

    @app.put("/api/templates/{key}")
    async def api_update_template(key: str, request: Request, _=Depends(_api_write("edit_template"))):
        body = await request.json()
        value = body.get("value")
        if value is None:
            raise HTTPException(400, "Missing 'value'")
        data = config_manager.get_dynamic_templates_config() or {}
        if key not in data:
            raise HTTPException(404, f"Template '{key}' not found")
        if isinstance(value, list):
            data[key] = value
        else:
            data[key] = str(value)
        ok, msg = config_manager.save_templates(data)
        if not ok:
            raise HTTPException(500, msg)
        config_manager.invalidate_templates_cache()
        if audit_store:
            audit_store.log("api", "update_template", key, "", str(value)[:100])
        return {"ok": True, "key": key}

    @app.post("/api/batch-strategies")
    async def api_batch_strategies(request: Request, _=Depends(_api_write("edit_strategy"))):
        """批量启用/禁用多个策略"""
        body = await request.json()
        ids: list = body.get("ids", [])
        enabled: bool = body.get("enabled", True)
        if not ids:
            raise HTTPException(400, "ids 不能为空")
        rs = config_manager.get_strategies_config()
        strategies = rs.get("strategies", {})
        snap_content = yaml.dump(rs, allow_unicode=True, default_flow_style=False)
        updated, not_found = [], []
        for sid in ids:
            if sid in strategies:
                strategies[sid]["enabled"] = enabled
                updated.append(sid)
            else:
                not_found.append(sid)
        if not updated:
            raise HTTPException(404, f"未找到任何策略: {not_found}")
        rs["strategies"] = strategies
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        actor = request.session.get("username", "api")
        _auto_snapshot("reply_strategies", snap_content, actor)
        if audit_store:
            audit_store.log(actor, "batch_strategy_enabled", ",".join(updated), "", str(enabled))
        return {"ok": True, "updated": updated, "not_found": not_found, "enabled": enabled}

    @app.post("/api/batch-templates")
    async def api_batch_templates(request: Request, _=Depends(_api_write("edit_template"))):
        """批量删除模板键"""
        body = await request.json()
        keys: list = body.get("keys", [])
        action: str = body.get("action", "delete")
        if not keys:
            raise HTTPException(400, "keys 不能为空")
        if action != "delete":
            raise HTTPException(400, "目前仅支持 action=delete")
        data = config_manager.get_dynamic_templates_config() or {}
        removed, not_found = [], []
        for k in keys:
            if k in data:
                del data[k]
                removed.append(k)
            else:
                not_found.append(k)
        if not removed:
            raise HTTPException(404, f"未找到任何模板键: {not_found}")
        snap_content = yaml.dump(
            config_manager.get_dynamic_templates_config() or {},
            allow_unicode=True, default_flow_style=False
        )
        ok, msg = config_manager.save_templates(data)
        if not ok:
            raise HTTPException(500, msg)
        config_manager.invalidate_templates_cache()
        actor = request.session.get("username", "api")
        _auto_snapshot("templates", snap_content, actor)
        if audit_store:
            audit_store.log(actor, "batch_delete_templates", ",".join(removed), "", "")
        return {"ok": True, "removed": removed, "not_found": not_found}

    @app.get("/api/audit")
    async def api_audit(request: Request, _=Depends(_api_auth),
                        action: str = "", keyword: str = "", limit: int = 50):
        if not audit_store:
            return []
        return audit_store.query(limit=limit, action=action, keyword=keyword)

    @app.get("/api/config/summary")
    async def api_config_summary(request: Request, _=Depends(_api_auth)):
        tpl = config_manager.get_dynamic_templates_config() or {}
        result = {
            "templates": {k: len(v) if isinstance(v, list) else 1 for k, v in tpl.items()},
        }
        if any(p.get("key") == "ch" for p in domain_web_pages):
            rates = config_manager.get_exchange_rates_config() or {}
            result["channels"] = {k: {"status": c.get("status"), "fee_rate": c.get("fee_rate")}
                                  for k, c in rates.get("channels", {}).items()}
        return result

    @app.post("/api/migrate")
    async def api_migrate(request: Request, _=Depends(_api_write("import_export"))):
        from src.utils.config_migrator import ConfigMigrator
        migrator = ConfigMigrator(config_manager.config_path)
        ok, msg = migrator.check_and_migrate()
        return {"migrated": ok, "message": msg}

    @app.get("/api/ai/quality")
    async def api_ai_quality(request: Request, _=Depends(_api_auth)):
        ai = getattr(telegram_client, "ai_client", None) if telegram_client else None
        if not ai:
            return {"error": "ai client not available"}
        qt = getattr(ai, "_quality_tracker", None)
        if not qt:
            return {"error": "quality tracker not available"}
        return {
            "summary": qt.get_summary(),
            "anomalies": qt.get_recent_anomalies(20),
            "token_trend": qt.get_token_trend(50),
        }

    # ── 健康检查（无需认证） ────────────────────────────────────

    @app.get("/health")
    async def health_check():
        import os
        mem_mb = None
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            mem_mb = round(proc.memory_info().rss / 1024 / 1024, 1)
        except Exception:
            pass

        uptime_sec = int(time.time() - boot_ts) if boot_ts else 0
        gxp_queue_depth = 0
        connected = False
        last_msg_ts = None
        if telegram_client:
            gxp_queue_depth = sum(len(q) for q in getattr(telegram_client, "_gxp_pending", {}).values())
            connected = getattr(telegram_client, "running", False)
            last_send = getattr(telegram_client, "_last_send_wallclock", 0)
            if last_send > 0:
                last_msg_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_send))

        return {
            "status": "ok" if connected else "degraded",
            "uptime_seconds": uptime_sec,
            "connected": connected,
            "memory_mb": mem_mb,
            "gxp_queue_depth": gxp_queue_depth,
            "last_message_sent": last_msg_ts,
            "templates_count": len(config_manager.get_dynamic_templates_config() or {}),
            **({"channels_count": len((config_manager.get_exchange_rates_config() or {}).get("channels", {}))}
               if any(p.get("key") == "ch" for p in domain_web_pages) else {}),
            "rate_limit_stats": getattr(telegram_client, "_rate_limiter", None) and telegram_client._rate_limiter.get_stats() or {},
            "ai_stats": _get_ai_stats(telegram_client),
        }

    def _get_ai_stats(tc):
        if not tc:
            return {}
        ai = getattr(tc, "ai_client", None)
        if not ai:
            return {}
        tracker = getattr(ai, "_quality_tracker", None)
        return {
            "total_calls": getattr(ai, "total_calls", 0),
            "total_tokens": getattr(ai, "total_tokens", 0),
            "quality": tracker.get_summary() if tracker else {},
        }

    # ── 配置导入导出 ──────────────────────────────────────────

    _EXPORT_FILES = ["templates.yaml", "exchange_rates.yaml", "quota_rules.yaml"]

    @app.get("/export")
    async def export_config(request: Request, _=Depends(_page_auth)):
        cfg_dir = config_manager.config_path.parent
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in _EXPORT_FILES:
                fp = cfg_dir / name
                if fp.exists():
                    zf.writestr(name, fp.read_text(encoding="utf-8"))
        buf.seek(0)
        ts = time.strftime("%Y%m%d_%H%M%S")
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=bot_config_{ts}.zip"},
        )

    @app.get("/import", response_class=HTMLResponse)
    async def import_page(request: Request, _=Depends(_page_auth)):
        return templates.TemplateResponse(request, "import.html", {"msg": ""})

    def _deep_merge(base: dict, incoming: dict) -> dict:
        """
        深度合并：incoming 中的键更新到 base，base 中独有的键保留。
        支持嵌套 dict；list 和 scalar 值直接覆盖。
        """
        result = dict(base)
        for k, v in incoming.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = _deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    @app.post("/import")
    async def import_config(request: Request, _=Depends(_page_auth),
                            file: UploadFile = File(...),
                            mode: str = Form("overwrite")):
        """
        mode=overwrite: 完全替换当前配置（原有行为）
        mode=merge:     增量合并——只更新导入文件中存在的键，保留当前独有的键
        """
        if not file.filename.endswith(".zip"):
            return templates.TemplateResponse(request, "import.html", {
                "msg": "请上传 .zip 文件", "import_mode": mode})
        cfg_dir = config_manager.config_path.parent
        content = await file.read()
        restored = []
        merge_stats = {}
        try:
            with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
                for name in _EXPORT_FILES:
                    if name not in zf.namelist():
                        continue
                    raw = zf.read(name).decode("utf-8")
                    incoming_data = yaml.safe_load(raw)
                    if not isinstance(incoming_data, dict):
                        continue
                    target = cfg_dir / name
                    if mode == "merge" and target.exists():
                        current_data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
                        merged = _deep_merge(current_data, incoming_data)
                        # 统计变更数量
                        added = [k for k in incoming_data if k not in current_data]
                        updated = [k for k in incoming_data if k in current_data and incoming_data[k] != current_data[k]]
                        merge_stats[name] = {"added": len(added), "updated": len(updated)}
                        out_content = yaml.dump(merged, allow_unicode=True, default_flow_style=False)
                    else:
                        out_content = raw
                    # 保存前快照
                    actor = request.session.get("username", "web_admin")
                    snap_content = target.read_text(encoding="utf-8") if target.exists() else ""
                    if snap_content:
                        prefix = name.replace(".yaml", "")
                        _auto_snapshot(prefix, snap_content, actor)
                    import shutil
                    if target.exists():
                        shutil.copy2(target, target.with_suffix(".yaml.pre_import"))
                    target.write_text(out_content, encoding="utf-8")
                    restored.append(name)
        except zipfile.BadZipFile:
            return templates.TemplateResponse(request, "import.html", {
                "msg": "ZIP 文件损坏", "import_mode": mode})
        except yaml.YAMLError as ye:
            return templates.TemplateResponse(request, "import.html", {
                "msg": f"YAML 格式错误: {ye}", "import_mode": mode})
        if restored:
            config_manager.invalidate_templates_cache()
            config_manager.invalidate_exchange_rates_cache()
            actor = request.session.get("username", "web_admin")
            if audit_store:
                audit_store.log(actor, f"import_config_{mode}", ", ".join(restored))
        if mode == "merge" and merge_stats:
            details = "; ".join(
                f"{n}: +{v['added']} 新增 / ~{v['updated']} 更新"
                for n, v in merge_stats.items()
            )
            msg = f"增量合并成功 — {details}" if restored else "ZIP 中无可识别的配置文件"
        else:
            msg = f"已导入 {len(restored)} 个文件: {', '.join(restored)}" if restored else "ZIP 中无可识别的配置文件"
        return templates.TemplateResponse(request, "import.html", {
            "msg": msg, "import_mode": mode})


    # ═══════════════════════════════════════════════════════════════════
    # 知识库路由 ── /knowledge  +  /api/kb/*
    # ═══════════════════════════════════════════════════════════════════
    from src.utils.kb_store import KnowledgeBaseStore, seed_default_data, KB_CATEGORIES, seed_system_replies

    _kb_db_path = cfg_dir / "knowledge_base.db"
    _kb_store = KnowledgeBaseStore(_kb_db_path)
    seed_default_data(_kb_store)
    _sys_seed = seed_system_replies(_kb_store)
    if _sys_seed.get("added"):
        import logging as _lg
        _lg.getLogger("admin").info("KB 系统话术种子已迁移: %s", _sys_seed)

    # ---------- templates.yaml 一次性迁移 ----------
    def _migrate_templates_once():
        """把 templates.yaml 中的话术条目迁移到知识库（只在 kb 为空时运行）"""
        if _kb_store.stats()["total_entries"] > 0:
            return
        # 用户曾清空过知识库条目后，不再从 templates.yaml 自动灌回，避免「删光重启又长回来」
        try:
            if _kb_store.get_meta("kb_seeded_once") == "1":
                return
        except Exception:
            pass
        tpl_path = config_manager.config_path.parent / "templates.yaml"
        if not tpl_path.exists():
            return
        try:
            data = yaml.safe_load(tpl_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return

        _CATEGORY_MAP = {
            "greeting": "常规咨询",
            "complaint": "投诉处理",
            "refund": "退款投诉",
            "small_talk": "常规咨询",
            "system_": "系统指令",
        }
        _CATEGORY_MAP.update(getattr(app.state, "intent_display_names_extra", {}))

        def _guess_category(key: str) -> str:
            key_lower = key.lower()
            for prefix, cat in _CATEGORY_MAP.items():
                if prefix in key_lower:
                    return cat
            return "其他"

        for key, val in data.items():
            if isinstance(val, list):
                msgs = [str(m) for m in val if m]
                example_zh = msgs[0] if msgs else ""
                triggers = [key.replace("_", " ")]
            elif isinstance(val, dict):
                msgs = []
                for lang_val in val.values():
                    if isinstance(lang_val, list):
                        msgs += [str(m) for m in lang_val if m]
                    elif lang_val:
                        msgs.append(str(lang_val))
                example_zh = msgs[0] if msgs else ""
                triggers = [key.replace("_", " ")]
            elif isinstance(val, str):
                example_zh = val
                triggers = [key.replace("_", " ")]
            else:
                continue

            _kb_store.add_entry({
                "category": _guess_category(key),
                "title": key.replace("_", " ").title(),
                "triggers": triggers,
                "scenario": f"从 templates.yaml 迁移的话术：{key}",
                "steps": "按照示例回复进行响应",
                "principles": "保持简洁，确保信息准确",
                "example_reply_zh": example_zh,
            })

    _migrate_templates_once()

    # ---------- 智能学习页面 ----------
    @app.get("/learner", response_class=HTMLResponse)
    async def learner_page(request: Request):
        _require_auth(request)
        return templates.TemplateResponse(request, "learner.html", {
            "active": "learner",
        })

    # ---------- 页面 ----------
    # ── 知识库条目 CRUD + 版本历史（Phase E1 批 5A：抽到 routes/kb_routes.py） ──
    _admin_ctx.kb_store = _kb_store
    _admin_ctx.fire_webhook = _fire_webhook
    try:
        from src.web.routes.kb_routes import register_kb_routes

        register_kb_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_kb

        _log_kb.getLogger("admin").warning("kb 路由注册失败", exc_info=True)

    # 错误码/示例/规则/反馈/沙盒/category-stats 已抽到 routes/kb_routes.py（批 5B）

    # ── 知识条目 AI 自动生成 ──────────────────────────────────

    # _auto_fill_entry 薄包装已移除（批 5I：全部调用方迁至 kb_routes 直接用 kb_ai_helpers）

    # ai-generate / export-markdown / stats / entries/{id}/translate 已抽到 routes/kb_routes.py（批 5L）

    # ═══════════════════════════════════════════════════════════════════
    # 知识库 — 智能体翻译 / 健康度 / 沙盒 AI 回复 / Miss 日志
    # ═══════════════════════════════════════════════════════════════════

    # _ai_translate_entry 薄包装已移除（批 5I：全部调用方迁至 kb_routes 直接用 kb_ai_helpers）

    # auto-translate / translation-gaps 已抽到 routes/kb_routes.py（批 5H）

    # KB AI 自动化运行时（翻译扫描/演化/自愈：locks+run_*+3端点+3循环）已整组搬到
    # routes/kb_ai_routes.py（批 5G）。循环 stash 在 app.state.kb_ai_loops，下方 startup 统一启动。
    try:
        from src.web.routes.kb_ai_routes import register_kb_ai_routes

        register_kb_ai_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_kbai

        _log_kbai.getLogger("admin").warning("kb_ai 路由注册失败", exc_info=True)

    @app.on_event("startup")
    async def _start_background_tasks():
        for _kb_ai_loop in getattr(app.state, "kb_ai_loops", []):
            asyncio.create_task(_kb_ai_loop())
        asyncio.create_task(_weekly_report_loop())
        # P26-A: intent_tags.yaml 文件变更自动 reload（基于 watchdog）
        try:
            _it_cfg = (config_manager.config or {}).get("rpa_intent_tags", {}) or {}
            if _it_cfg.get("watch_enabled", True):
                from src.integrations.intent_tags_watcher import start_watcher
                start_watcher(debounce_sec=float(_it_cfg.get("watch_debounce_sec", 0.8)))
        except Exception:
            pass

    @app.on_event("shutdown")
    async def _stop_intent_tags_watcher():
        # P26-A: 干净停 watchdog observer thread
        try:
            from src.integrations.intent_tags_watcher import stop_watcher
            stop_watcher()
        except Exception:
            pass

    # translate-all / translate-progress(SSE) 已抽到 routes/kb_routes.py（批 5H）

    # sandbox/ai-reply 已抽到 routes/kb_routes.py（批 5L）

    # F3: 测试会话缓存（内存级，不污染生产 ctx_store）
    import uuid as _uuid
    _test_sessions: Dict[str, Dict] = {}
    _TEST_SESSION_TTL = 1800  # 30 分钟

    def _get_test_session(session_id: str) -> Dict:
        """获取或创建测试会话"""
        now = time.time()
        # 清理过期 session
        expired = [k for k, v in _test_sessions.items()
                   if now - v.get("_ts", 0) > _TEST_SESSION_TTL]
        for k in expired:
            _test_sessions.pop(k, None)
        if session_id and session_id in _test_sessions:
            _test_sessions[session_id]["_ts"] = now
            return _test_sessions[session_id]
        sid = session_id or str(_uuid.uuid4())[:12]
        _test_sessions[sid] = {"_ts": now, "_sid": sid, "_history": [], "_turn": 0}
        return _test_sessions[sid]

    # H1+G1+F3: 全链路对话自测 — 多轮 + 通道模拟
    @app.post("/api/chat/test")
    async def api_chat_test(request: Request):
        """
        全链路自测端点：
        - H1: 意图识别 → 策略选择 → KB 搜索 → AI 回复 → 画像
        - G1: channel_overrides 模拟通道状态 + SOP 合规检查
        - F3: session_id 支持多轮对话（30分钟TTL，不影响生产数据）
        """
        _api_auth(request)
        data = await request.json()
        message = (data.get("message") or "").strip()
        user_id = data.get("user_id", "__test_user__")
        channel_overrides = data.get("channel_overrides")
        user_emotion = data.get("user_emotion", "")
        session_id = data.get("session_id", "")
        if not message:
            raise HTTPException(400, "message 不能为空")

        # F3: 获取/创建测试会话
        sess = _get_test_session(session_id)
        session_id = sess["_sid"]
        sess["_turn"] += 1

        t0 = time.time()
        trace = {"steps": []}

        def _step(name, detail):
            trace["steps"].append({
                "step": name,
                "detail": detail,
                "elapsed_ms": int((time.time() - t0) * 1000),
            })

        # 1. 意图识别
        sm = None
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
        if not sm:
            return {"ok": False, "error": "SkillManager 未初始化（Bot 未运行）"}

        intent = sm._recognize_intent(message)
        strategy, strategy_id = sm.get_strategy_for_intent(intent, user_id)
        _step("intent", {"recognized": intent, "strategy_id": strategy_id})

        # 2. KB 搜索
        kb_hit = False
        kb_context = ""
        kb_entries = []
        kb_score = 0.0
        result = {"search_mode": "bm25"}
        try:
            result = _kb_store.search(message, top_k=3, lang="zh")
            if result.get("entries"):
                kb_score = result["entries"][0].get("_score", 0)
                kb_entries = [
                    {"title": e.get("title", ""), "score": e.get("_score", 0),
                     "category": e.get("category", "")}
                    for e in result["entries"][:3]
                ]
            kb_context = _kb_store.build_ai_context_from_result(result, lang="zh")
            kb_hit = bool(kb_context)
        except Exception as _e:
            _step("kb_error", str(_e))
        _step("kb_search", {
            "hit": kb_hit, "top_score": round(kb_score, 3),
            "entries": kb_entries, "mode": result.get("search_mode", "bm25"),
        })

        channel_status_text = ""
        _has_ch_page = any(p.get("key") == "ch" for p in domain_web_pages)
        if _has_ch_page:
            if channel_overrides and isinstance(channel_overrides, dict):
                parts = [f"{k.upper()}: {v}" for k, v in channel_overrides.items()]
                channel_status_text = "，".join(parts)
                _step("channel_override", channel_overrides)
            elif intent in ("channel_info", "status_check"):
                channel_status_text = sm._get_live_channel_status()
                if channel_status_text:
                    _step("channel_live", channel_status_text)

        # 3. AI 回复
        ai_reply = None
        try:
            mock_ctx = {
                "user_id": user_id,
                "intent": intent,
                "current_intent": intent,
                "_reply_strategy": strategy or {},
            }
            # F3: 注入多轮对话历史
            if sess["_history"]:
                mock_ctx["_conversation_history"] = sess["_history"][-6:]
                _step("session", {"id": session_id, "turn": sess["_turn"],
                                  "history_rounds": len(sess["_history"]) // 2})
            if kb_context:
                mock_ctx["kb_context"] = kb_context
            if channel_status_text:
                mock_ctx["channel_status_info"] = channel_status_text
            if user_emotion:
                mock_ctx["user_emotion_hint"] = user_emotion
                mock_ctx["_user_profile"] = {"tone": user_emotion}
            so = {}
            for _sk in ("temperature", "max_tokens", "context_rounds", "model", "thinking_budget"):
                if _sk in (strategy or {}):
                    so[_sk] = strategy[_sk]
            ai_reply = await sm.ai_client.generate_reply_with_intent(
                user_message=message,
                intent=intent,
                user_context=mock_ctx,
                strategy_overrides=so or None,
            )
        except Exception as _e:
            _step("ai_error", str(_e))
        _step("ai_reply", {
            "reply": (ai_reply or "")[:500],
            "length": len(ai_reply or ""),
        })

        # 4. 画像快照
        profile = {}
        try:
            ctx_store = getattr(sm, "_context_store", None)
            if ctx_store and user_id in ctx_store._cache:
                profile = ctx_store._cache[user_id].get("_user_profile", {})
        except Exception:
            pass
        _step("profile", profile if profile else {"note": "测试用户无历史画像"})

        sop_check = None
        if _has_ch_page and channel_overrides and ai_reply:
            sop_check = {"passed": True, "warnings": []}
            reply_lower = ai_reply.lower()
            for ch_name, ch_status in channel_overrides.items():
                ch_up = ch_name.upper()
                status_lower = ch_status.lower()
                if "维护" in status_lower:
                    if ch_up.lower() not in reply_lower and "维护" not in reply_lower:
                        sop_check["passed"] = False
                        sop_check["warnings"].append(
                            f"{ch_up} 处于维护中，但回复未提及维护状态")
                elif "波动" in status_lower:
                    if "波动" not in reply_lower and "成功率" not in reply_lower and "偏低" not in reply_lower:
                        sop_check["warnings"].append(
                            f"{ch_up} 有波动，回复未明确提及波动/成功率风险")
            if sop_check["warnings"]:
                sop_check["passed"] = False
            _step("sop_check", sop_check)

        # F3: 将本轮加入会话历史
        if ai_reply:
            sess["_history"].append({"role": "user", "content": message[:200]})
            sess["_history"].append({"role": "assistant", "content": ai_reply[:300]})
            if len(sess["_history"]) > 20:
                sess["_history"] = sess["_history"][-12:]

        total_ms = int((time.time() - t0) * 1000)
        resp = {
            "ok": True,
            "message": message,
            "intent": intent,
            "strategy_id": strategy_id,
            "kb_hit": kb_hit,
            "kb_top_score": round(kb_score, 3),
            "reply": (ai_reply or ""),
            "total_ms": total_ms,
            "trace": trace,
            "session_id": session_id,
            "turn": sess["_turn"],
        }
        if sop_check is not None:
            resp["sop_check"] = sop_check
        return resp

    # G3: 意图链 Case 面板 — 活跃 case 列表 + 人工介入 + 结案
    # 运营 case 管理 API（Phase E1 续拆 → cases_routes）
    try:
        from src.web.routes.cases_routes import register_cases_routes

        register_cases_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_cases

        _log_cases.getLogger("admin").warning("cases 路由注册失败", exc_info=True)

    # G2: 测试纠错 → 一键创建 KB 反馈 + 优质示例
    @app.post("/api/chat/test/correct")
    async def api_chat_test_correct(request: Request):
        """
        G2: 运营人员纠正 AI 回复。同时：
        1. 创建负面反馈记录（AI 原始回复 + 纠正文本）
        2. 保存正确回复为 KB 优质示例
        """
        _api_auth(request)
        data = await request.json()
        user_message = (data.get("user_message") or "").strip()
        wrong_reply = (data.get("wrong_reply") or "").strip()
        correct_reply = (data.get("correct_reply") or "").strip()
        category = data.get("category", "其他")
        if not user_message or not correct_reply:
            raise HTTPException(400, "user_message 和 correct_reply 不能为空")

        actor = request.session.get("username", "web_admin")

        fb_id = _kb_store.add_feedback({
            "user_message": user_message,
            "ai_reply": wrong_reply,
            "score": -1,
            "correction": correct_reply,
            "operator": actor,
        })

        ex_id = _kb_store.add_example({
            "category": category,
            "user_message": user_message,
            "correct_reply": correct_reply,
            "language": "zh",
            "quality": 1,
            "source": "test_correction",
        })

        if audit_store:
            audit_store.log(actor, "chat_test_correct", ex_id,
                            user_message[:80], correct_reply[:80])

        return {
            "ok": True,
            "feedback_id": fb_id,
            "example_id": ex_id,
        }

    # H4: 运营 Copilot — 自然语言查询内部数据
    def _copilot_get_ctx_store():
        """统一获取 context_store 实例"""
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                return getattr(sm, "_context_store", None), sm
        return None, None

    @app.post("/api/copilot/query")
    async def api_copilot_query(request: Request):
        """
        H4: 接收运营人员的自然语言问题，
        自动调用内部 API 数据源，用 AI 生成回答。
        """
        _api_auth(request)
        data = await request.json()
        question = (data.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "question 不能为空")

        t0 = time.time()
        gathered = {}
        ctx_store, sm = _copilot_get_ctx_store()

        q_lower = question.lower()
        _need_kb = any(k in q_lower for k in (
            "知识库", "kb", "条目", "命中", "miss", "健康", "触发词",
            "弱命中", "未命中", "翻译", "草稿"))
        _need_risk = any(k in q_lower for k in (
            "风险", "at_risk", "满意度", "不满", "流失", "投诉",
            "升级", "escalat", "case"))
        _need_conv = any(k in q_lower for k in (
            "对话", "会话", "活跃", "在线", "conversation", "用户数", "消息"))
        _need_report = any(k in q_lower for k in (
            "日报", "报告", "report", "统计", "概况", "总结", "今天", "昨天"))
        _need_strategy = any(k in q_lower for k in (
            "策略", "strategy", "ab", "a/b", "测试", "温度", "模型", "参数"))
        _need_feedback = any(k in q_lower for k in (
            "反馈", "feedback", "评分", "质量", "好评", "差评"))

        if not any([_need_kb, _need_risk, _need_conv, _need_strategy, _need_feedback]):
            _need_report = True

        # ── 数据采集 ──
        if _need_kb:
            try:
                stats = _kb_store.stats()
                weak = _kb_store.get_weak_hits(top_k=5)
                miss = _kb_store.get_miss_stats(top_k=5)
                stale = _kb_store.get_stale_entries(days=14)
                gathered["kb"] = {
                    "stats": stats,
                    "top_weak_hits": [{"query": w["query"], "count": w["count"],
                                       "avg_score": w["avg_score"]} for w in weak[:5]],
                    "top_misses": [{"query": m["query"], "count": m["cnt"]} for m in miss[:5]],
                    "stale_count": len(stale),
                }
            except Exception as _e:
                gathered["kb_error"] = str(_e)

        if _need_risk and ctx_store:
            try:
                at_risk = []
                for uid, ctx in ctx_store._cache.items():
                    profile = ctx.get("_user_profile")
                    if isinstance(profile, dict) and profile.get("at_risk"):
                        at_risk.append({
                            "user_id": uid,
                            "satisfaction": profile.get("satisfaction", 0),
                            "intent": ctx.get("current_intent", ""),
                            "consecutive": ctx.get("_consecutive_same_intent", 0),
                            "case_id": ctx.get("_case_id", ""),
                        })
                at_risk.sort(key=lambda x: x["satisfaction"])
                gathered["at_risk_users"] = at_risk[:10]
                gathered["at_risk_total"] = len(at_risk)
            except Exception as _e:
                gathered["risk_error"] = str(_e)

        if _need_conv and ctx_store:
            try:
                now = time.time()
                active_30 = active_60 = 0
                for uid, ctx in ctx_store._cache.items():
                    lrt = ctx.get("last_reply_time", 0)
                    if lrt >= now - 1800:
                        active_30 += 1
                    if lrt >= now - 3600:
                        active_60 += 1
                gathered["conversations"] = {
                    "active_30min": active_30,
                    "active_60min": active_60,
                    "total_cached": len(ctx_store._cache),
                }
            except Exception as _e:
                gathered["conv_error"] = str(_e)

        if _need_strategy and sm:
            try:
                if hasattr(sm, "_strategies"):
                    gathered["strategies"] = {
                        sid: {k: v for k, v in s.items()
                              if k in ("temperature", "max_tokens", "model",
                                       "thinking_budget", "reply_probability")}
                        for sid, s in sm._strategies.items()
                    }
            except Exception as _e:
                gathered["strategy_error"] = str(_e)

        if _need_feedback:
            try:
                with _kb_store._conn() as c:
                    since = time.time() - 86400 * 7
                    fb_rows = c.execute(
                        "SELECT score, COUNT(*) as cnt FROM kb_feedback "
                        "WHERE created_at >= datetime(?, 'unixepoch') GROUP BY score",
                        (since,)
                    ).fetchall()
                    gathered["feedback_7d"] = {
                        str(r["score"]): r["cnt"] for r in fb_rows
                    }
            except Exception as _e:
                gathered["feedback_error"] = str(_e)

        if _need_report:
            try:
                with _kb_store._conn() as c:
                    since = time.time() - 86400
                    total = c.execute(
                        "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ?", (since,)
                    ).fetchone()[0]
                    hits = c.execute(
                        "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND hit=1", (since,)
                    ).fetchone()[0]
                    gathered["daily_summary"] = {
                        "kb_queries_24h": total,
                        "kb_hits_24h": hits,
                        "hit_rate": round(hits / max(total, 1) * 100, 1),
                    }
                if ctx_store:
                    now = time.time()
                    _a30 = sum(1 for ctx in ctx_store._cache.values()
                               if ctx.get("last_reply_time", 0) >= now - 1800)
                    _risk = sum(1 for ctx in ctx_store._cache.values()
                                if isinstance(ctx.get("_user_profile"), dict)
                                and ctx["_user_profile"].get("at_risk"))
                    gathered["daily_summary"]["active_users_30min"] = _a30
                    gathered["daily_summary"]["at_risk_users"] = _risk
            except Exception as _e:
                gathered["report_error"] = str(_e)

        # ── AI 生成自然语言回答 ──
        ai_answer = None
        try:
            if sm and hasattr(sm, "ai_client"):
                data_text = json.dumps(gathered, ensure_ascii=False, default=str)[:4000]
                copilot_prompt = (
                    "你是运营数据分析助手 Copilot。基于以下内部系统数据回答运营人员的问题。\n"
                    "回答要求：\n"
                    "- 用简洁的中文，突出关键数据点\n"
                    "- 有异常时主动指出并给出建议\n"
                    "- 数据不足时说明需要哪些额外信息\n"
                    "- 不要编造数据\n\n"
                    f"内部数据:\n{data_text}\n\n"
                    f"运营问题: {question}"
                )
                ai_answer = await sm.ai_client.generate_reply(
                    user_message=copilot_prompt,
                    context={"current_intent": "copilot_query", "kb_context": ""},
                    strategy_overrides={"temperature": 0.3, "max_tokens": 1024},
                )
        except Exception as _e:
            ai_answer = f"AI 生成回答失败: {_e}"

        total_ms = int((time.time() - t0) * 1000)
        return {
            "ok": True,
            "question": question,
            "answer": ai_answer or "暂无法生成回答",
            "data_sources": list(gathered.keys()),
            "raw_data": gathered,
            "total_ms": total_ms,
        }

    # ---------- 知识库健康度 ----------
    # KB 健康统计/Miss日志/翻译审核/图片/种子/维护建议 已抽到 routes/kb_routes.py（批 5K）

    # ── 运营日报/周报 API（Phase E1 续拆 → report_routes） ──
    # K4 日报 + F4 周报已抽到 src/web/routes/report_routes.py（kb_store 已就绪）。
    # 周报后台推送循环 _weekly_report_loop 属后台任务，仍留本文件（见下方）。
    try:
        from src.web.routes.report_routes import register_report_routes

        register_report_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_rep

        _log_rep.getLogger("admin").warning("report 路由注册失败", exc_info=True)

    # F4: 后台自动推送周报（每周一 09:00 附近）
    async def _weekly_report_loop():
        await asyncio.sleep(600)
        while True:
            try:
                now = time.localtime()
                if now.tm_wday == 0 and 8 <= now.tm_hour <= 10:
                    with _kb_store._conn() as c:
                        now_ts = time.time()
                        this_week = now_ts - 168 * 3600
                        tw_total = c.execute(
                            "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ?", (this_week,)
                        ).fetchone()[0]
                        tw_hits = c.execute(
                            "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND hit=1", (this_week,)
                        ).fetchone()[0]
                    tw_rate = round(tw_hits / max(tw_total, 1) * 100, 1)
                    summary = (
                        f"📊 自动周报: 本周 {tw_total} 次查询, "
                        f"命中率 {tw_rate}%"
                    )
                    await _fire_webhook("weekly_report", "system", "report", summary)
                    logger.info("F4 周报已推送: %s", summary)
                    await asyncio.sleep(72000)  # 推送后休眠 20h 避免重复
                    continue
            except Exception as _e:
                logger.debug("F4 周报循环异常: %s", _e)
            await asyncio.sleep(3600)

    # Phase 7a: 自动建议 + 弱命中分析
    # ═══════════════════════════════════════════════════════════════════

    # auto-suggestions / reply-quality 已抽到 routes/kb_routes.py（批 5L）
    # （accept-suggestion 已于批 5I 抽出）

    @app.get("/api/users/at-risk")
    async def api_users_at_risk(request: Request):
        """K3: 返回满意度 at_risk 的用户列表"""
        _api_auth(request)
        ctx_store = None
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                ctx_store = getattr(sm, "_context_store", None)
        if not ctx_store:
            return {"users": [], "count": 0}
        at_risk = []
        for uid, ctx in ctx_store._cache.items():
            profile = ctx.get("_user_profile")
            if isinstance(profile, dict) and profile.get("at_risk"):
                at_risk.append({
                    "user_id": uid,
                    "satisfaction": profile.get("satisfaction", 0),
                    "type": profile.get("type", "unknown"),
                    "tone": profile.get("tone", "standard"),
                    "msg_count": profile.get("msg_count", 0),
                    "last_intent": ctx.get("current_intent", ""),
                    "last_message": (ctx.get("last_message") or "")[:80],
                })
        at_risk.sort(key=lambda x: x["satisfaction"])
        return {"users": at_risk[:50], "count": len(at_risk)}

    # J3: 活跃对话实时监控
    # ═══════════════════════════════════════════════════════════════════

    @app.get("/api/conversations/active")
    async def api_active_conversations(request: Request, minutes: int = 30):
        """返回最近 N 分钟内有活动的对话列表（含满意度、意图、at_risk 状态）"""
        _api_auth(request)
        ctx_store = None
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                ctx_store = getattr(sm, "_context_store", None)
        if not ctx_store:
            return {"conversations": [], "count": 0, "at_risk_count": 0}

        cutoff = time.time() - minutes * 60
        conversations = []
        at_risk_count = 0

        for uid, ctx in ctx_store._cache.items():
            last_time = ctx.get("last_reply_time", 0)
            if last_time < cutoff:
                continue
            profile = ctx.get("_user_profile", {})
            is_risk = profile.get("at_risk", False) if isinstance(profile, dict) else False
            if is_risk:
                at_risk_count += 1
            sat = profile.get("satisfaction", 80) if isinstance(profile, dict) else 80

            hist = ctx.get("_conversation_history", [])
            recent_msgs = []
            for h in hist[-4:]:
                recent_msgs.append({
                    "role": h.get("role", "user"),
                    "text": (h.get("content") or "")[:120],
                })

            conversations.append({
                "user_id": uid,
                "chat_id": ctx.get("chat_id", ""),
                "chat_title": ctx.get("chat_title", ""),
                "satisfaction": sat,
                "at_risk": is_risk,
                "user_type": profile.get("type", "new") if isinstance(profile, dict) else "new",
                "tone": profile.get("tone", "standard") if isinstance(profile, dict) else "standard",
                "current_intent": ctx.get("current_intent", ""),
                "msg_count": profile.get("msg_count", 0) if isinstance(profile, dict) else 0,
                "consecutive_same": ctx.get("_consecutive_same_intent", 0),
                "last_message": (ctx.get("last_message") or "")[:100],
                "last_reply": (ctx.get("last_reply") or "")[:100],
                "last_active": last_time,
                "recent_messages": recent_msgs,
                "escalation_triggered": bool(ctx.get("_escalation_ts")),
            })

        conversations.sort(key=lambda x: (not x["at_risk"], -x["consecutive_same"], x["satisfaction"]))
        return {
            "conversations": conversations[:100],
            "count": len(conversations),
            "at_risk_count": at_risk_count,
            "window_minutes": minutes,
        }

    # Phase 7: 查询分析 + Embedding API 用量统计
    # ═══════════════════════════════════════════════════════════════════

    # query-analytics/today-hit-rate/embed-stats/implicit-feedback 已抽到 routes/kb_routes.py（批 5D）

    # ═══════════════════════════════════════════════════════════════════
    # Phase 5: 向量化 / 查重 / 知识库备份管理
    # ═══════════════════════════════════════════════════════════════════

    # embed/查重/备份簇（embed-all/progress/single/coverage/duplicates/backup/backups/restore）
    # 连同助手 _call_embed_api/_build_embed_text/_run_embed_all + state 已整组搬到
    # routes/kb_routes.py（批 5E）。

    # 向外暴露 kb_store 供 skill_manager 调用
    app.state.kb_store = _kb_store

    # ═══════════════════════════════════════════════════════════════════
    # 每日自动学习 ── /api/learner/*（批 5J：抽到 routes/learner_routes.py）
    # ═══════════════════════════════════════════════════════════════════
    try:
        from src.web.routes.learner_routes import register_learner_routes

        register_learner_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_ln

        _log_ln.getLogger("admin").warning("learner 路由注册失败", exc_info=True)

    # ── 监控/指标/reactivation dry-run 路由（批 G2-①，ctx.kb_store 已就绪） ──
    try:
        from src.web.routes.monitoring_routes import register_monitoring_routes

        register_monitoring_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_mon

        _log_mon.getLogger("admin").warning("monitoring 路由注册失败", exc_info=True)

    # ── Persona Management API ──────────────────────────────
    # 注：/api/persona{,/bindings,/bind,/unbind,/update-default,/preview-prompt}
    # 原本在此 inline，与 register_persona_routes 重复注册（inline 被遮蔽=死代码）。
    # Phase E1 清理：删除 inline 死代码，统一由 persona_routes 模块提供。

    # ── KB Import API ────────────────────────────────────────
    # ⚠ 已知遗留 bug（待产品决策，未擅自修改）：下面的 @app.post("/api/kb/import")
    # 是 KBImporter 文档分块导入（配合 /api/kb/import/save）。但 kb_routes.py 里的
    # /api/kb/import（export-dump 导入）注册更早 → 遮蔽本版，导致「文档导入向导」
    # 实际走不到这里。二者语义不同、共用同一 path，需改名（如 /api/kb/import-document）
    # 才能并存。保留现状以免改变 API 契约（需前端协同）。
    @app.post("/api/kb/import")
    async def api_kb_import(request: Request, _=Depends(_api_auth)):
        from src.utils.kb_importer import KBImporter
        data = await request.json()
        content = data.get("content", "")
        filename = data.get("filename", "upload")
        file_type = data.get("file_type", "txt")
        category = data.get("category", "")
        chunk_size = int(data.get("chunk_size", 500))

        if not content:
            raise HTTPException(400, "content required")

        importer = KBImporter()
        entries = importer.import_text_content(
            content=content,
            filename=filename,
            file_type=file_type,
            category=category,
            chunk_size=chunk_size,
        )
        return {"entries": entries, "count": len(entries)}

    @app.post("/api/kb/import/save")
    async def api_kb_import_save(request: Request, _=Depends(_api_auth)):
        from src.utils.kb_importer import KBImporter
        data = await request.json()
        entries = data.get("entries", [])
        if not entries:
            raise HTTPException(400, "no entries to save")

        kb = None
        try:
            from src.utils.kb_store import KnowledgeBaseStore
            kb_path = Path(config_manager.config_path).parent / "knowledge_base.db"
            if kb_path.exists():
                kb = KnowledgeBaseStore(kb_path)
        except Exception:
            pass

        if not kb:
            raise HTTPException(503, "KB store not available")

        importer = KBImporter(kb_store=kb)
        ok, err = importer.save_entries_to_kb(entries)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_import", f"saved={ok} errors={err}")
        return {"ok": True, "saved": ok, "errors": err}

    # ── Domain Pack Route Registration ──────────────────────
    from src.web.web_context import WebContext
    _web_ctx = WebContext(
        config_manager=config_manager,
        audit_store=audit_store,
        event_tracker=event_tracker,
        templates=templates,
        user_store=user_store,
        page_auth=_page_auth,
        api_auth=_api_auth,
        api_write_factory=_api_write,
        auto_snapshot=_auto_snapshot,
        broadcast_config_reload=_broadcast_config_reload,
        fire_webhook=_fire_webhook,
        sync_domain_exchange_rates=None,
        domain_name=domain_name,
        domain_web_pages=domain_web_pages,
    )
    _register_domain_routes(app, _web_ctx)

    # ── 情景记忆（放在域路由注册之后，避免被域打包或其它中间件式路由误覆盖导致 404）──
    @app.get("/episodic_memory")
    async def episodic_memory_alias_redirect():
        return RedirectResponse(url="/episodic-memory", status_code=307)

    @app.get("/episodic-memory", response_class=HTMLResponse)
    async def episodic_memory_page(request: Request, _=Depends(_page_auth)):
        _require_role(request, "episodic")
        return templates.TemplateResponse(request, "episodic_memory.html", {})

    # ── 情景记忆 + 跨平台身份 API（Phase E1 续拆 → episodic_identity_routes） ──
    # 仅 API 端点迁出；上方 2 个页面路由因需 templates 仍留本文件（与既有约定一致）。
    try:
        from src.web.routes.episodic_identity_routes import (
            register_episodic_identity_routes,
        )

        register_episodic_identity_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_ei

        _log_ei.getLogger("admin").warning("情景记忆/身份 路由注册失败", exc_info=True)

    try:
        from src.integrations.line_webhook import register_line_routes

        register_line_routes(app, config_manager, telegram_client)
    except Exception:
        import logging as _log_line

        _log_line.getLogger("admin").debug("LINE Webhook 注册跳过", exc_info=True)

    # ── Facebook Page Messenger Webhook（Graph API） ──
    try:
        from src.integrations.facebook_webhook import (
            register_fb_messenger_routes,
        )

        register_fb_messenger_routes(app, config_manager, telegram_client)
    except Exception:
        import logging as _log_fb

        _log_fb.getLogger("admin").debug(
            "FB Messenger Webhook 注册跳过", exc_info=True
        )

    # ── LINE RPA（个人号自动聊天）Web 管理页 + REST ──
    try:
        from src.web.routes.line_rpa_routes import register_line_rpa_routes

        def _line_rpa_page_auth(request: Request):
            _require_role(request, "line_rpa")

        register_line_rpa_routes(
            app,
            page_auth=_line_rpa_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
            audit_store=audit_store,
        )
    except Exception:
        import logging as _log_lr

        _log_lr.getLogger("admin").debug("LINE RPA 路由注册跳过", exc_info=True)

    # ── Messenger RPA（FB Messenger 个人号 RPA）Web + REST ──
    try:
        from src.web.routes.messenger_rpa_routes import (
            register_messenger_rpa_routes,
        )

        def _msgr_rpa_page_auth(request: Request):
            # 复用 line_rpa 角色（同等敏感度）；后续可以拆出独立 role
            _require_role(request, "line_rpa")

        register_messenger_rpa_routes(
            app,
            page_auth=_msgr_rpa_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
            audit_store=audit_store,
        )
    except Exception:
        import logging as _log_mr

        _log_mr.getLogger("admin").debug(
            "Messenger RPA 路由注册跳过", exc_info=True
        )

    # ── WhatsApp RPA（个人号 / Business 号自动聊天）Web + REST ──
    try:
        from src.web.routes.whatsapp_rpa_routes import (
            register_whatsapp_rpa_routes,
        )

        def _wa_rpa_page_auth(request: Request):
            _require_role(request, "line_rpa")

        register_whatsapp_rpa_routes(
            app,
            page_auth=_wa_rpa_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
            audit_store=audit_store,
        )
    except Exception:
        import logging as _log_wa

        _log_wa.getLogger("admin").debug(
            "WhatsApp RPA 路由注册跳过", exc_info=True
        )

    # ── RPA 跨平台总览（聚合 4 个平台 status / pending / alerts） ──
    try:
        from src.web.routes.rpa_overview_routes import (
            register_rpa_overview_routes,
        )

        def _rpa_overview_page_auth(request: Request):
            # 聚合页只读，复用 line_rpa 角色（与 4 个详情页同等敏感度）
            _require_role(request, "line_rpa")

        register_rpa_overview_routes(
            app,
            page_auth=_rpa_overview_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
        )
    except Exception:
        import logging as _log_ov

        _log_ov.getLogger("admin").debug(
            "RPA 跨平台总览路由注册跳过", exc_info=True
        )

    # ── 统一收件箱（跨平台消息聚合 + 发送） ─────────────────────
    try:
        from src.web.routes.unified_inbox_routes import register_unified_inbox_routes

        def _unified_inbox_page_auth(request: Request):
            _require_role(request, "line_rpa")

        register_unified_inbox_routes(
            app,
            page_auth=_unified_inbox_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
        )
    except Exception:
        import logging as _log_ui
        _log_ui.getLogger("admin").debug("统一收件箱路由注册跳过", exc_info=True)

    # ── Voice / TTS 统一试听 API ──────────────────────────────
    try:
        from src.web.routes.voice_routes import register_voice_routes

        register_voice_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_vr

        _log_vr.getLogger("admin").debug("Voice TTS 路由注册跳过", exc_info=True)

    # ── Telegram 帐号设置页 ────────────────────────────────────
    try:
        from src.web.routes.telegram_routes import register_telegram_routes

        def _tg_page_auth(request: Request):
            _require_role(request, "settings")

        register_telegram_routes(
            app,
            page_auth=_tg_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
            telegram_client=telegram_client,
            audit_store=audit_store,
        )
    except Exception:
        import logging as _log_tgr

        _log_tgr.getLogger("admin").debug("Telegram 路由注册跳过", exc_info=True)

    return app


def _register_domain_routes(app: FastAPI, ctx):
    """Auto-discover and register web routes from the active domain pack."""
    import importlib
    domain_name = ctx.domain_name
    if not domain_name:
        return

    routes_module_path = f"domains.{domain_name}.web.routes"
    try:
        mod = importlib.import_module(routes_module_path)
        if hasattr(mod, 'register_routes'):
            mod.register_routes(app, ctx)
            logging.getLogger("admin").info(
                "Domain '%s' web routes registered", domain_name
            )
    except ImportError:
        pass
    except Exception as e:
        logging.getLogger("admin").warning(
            "Failed to register domain '%s' web routes: %s", domain_name, e
        )


