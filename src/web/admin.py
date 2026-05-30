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
                return RedirectResponse("/", status_code=303)
            return templates.TemplateResponse(request, "login.html", {
                "error": "用户名或密码错误", "has_users": True
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
            "error": "Token 错误",
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
            raise HTTPException(400, "系统已初始化，请通过用户管理页面操作")
        body = await request.json()
        username = (body.get("username") or "").strip()
        password = body.get("password", "")
        confirm  = body.get("confirm", "")
        api_key  = (body.get("api_key") or "").strip()
        base_url = (body.get("base_url") or "").strip()
        model    = (body.get("model") or "").strip()

        if not username or not password:
            raise HTTPException(400, "用户名和密码不能为空")
        if len(password) < 6:
            raise HTTPException(400, "密码至少 6 位")
        if password != confirm:
            raise HTTPException(400, "两次密码不一致")

        # 创建 master 账户
        result = user_store.create_user(username, password, ROLE_MASTER,
                                        display_name=username)
        if not result:
            raise HTTPException(500, "账户创建失败")

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
            raise HTTPException(400, "API Key 不能为空")
        if not base_url:
            raise HTTPException(400, "Base URL 不能为空")
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

    # ── 系统状态接口 ──────────────────────────────────────────

    @app.get("/api/system-info")
    async def api_system_info(request: Request):
        """返回运行状态摘要供 dashboard 状态栏使用"""
        _api_auth(request)

        import asyncio as _aio

        def _gather():
            bot_online = False
            if telegram_client:
                bot_online = bool(getattr(telegram_client, "running", False))

            last_activity = None
            if audit_store:
                last = audit_store.last_entry()
                if last:
                    last_activity = last.get("ts", "")

            mem_mb = None
            try:
                import psutil as _ps
                mem_mb = round(_ps.Process().memory_info().rss / 1024 / 1024, 1)
            except Exception:
                try:
                    import resource as _res
                    mem_mb = round(_res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024, 1)
                except Exception:
                    pass

            kb_entries = 0
            kb_db_mb = None
            try:
                s = _kb_store.stats()
                kb_entries = s.get("total_entries", 0)
                db_p = getattr(_kb_store, "db_path", None) or getattr(_kb_store, "_db_path", None)
                if db_p and Path(str(db_p)).exists():
                    kb_db_mb = round(Path(str(db_p)).stat().st_size / 1024 / 1024, 2)
            except Exception:
                pass

            ai_cfg = {}
            try:
                ai_cfg = config_manager.config.get("ai", {}) if config_manager.config else {}
            except Exception:
                pass
            embedding_ok = bool(ai_cfg.get("api_key", ""))

            uptime_s = int(time.time() - boot_ts) if boot_ts else 0

            return {
                "bot_online":    bot_online,
                "last_activity": last_activity,
                "memory_mb":     mem_mb,
                "uptime_s":      uptime_s,
                "kb_entries":    kb_entries,
                "kb_db_mb":      kb_db_mb,
                "admin_users":   user_store.user_count(),
                "embedding_ok":  embedding_ok,
            }

        return await _aio.to_thread(_gather)

    # ── Bot 实时性能指标（供 dashboard 展示）─────────────────
    @app.get("/api/vision-stats")
    async def api_vision_stats(request: Request):
        """Vision 调用统计——按 (task_name, model) 分桶，含 p50/p95/p99/avg
        + 失败原因 breakdown。

        Query params:
          since_hours: int = 24
          task: str = "" 仅按该 task 过滤
        """
        _api_auth(request)
        try:
            from src.integrations.messenger_rpa import vision_metrics as _vm
            since_hours = float(request.query_params.get("since_hours") or 24)
            task = (request.query_params.get("task") or "").strip() or None
            since_sec = max(60.0, min(since_hours * 3600.0, 30 * 24 * 3600.0))
            rows = _vm.summary(since_sec=since_sec, task_name=task)
            errors = _vm.error_breakdown(since_sec=since_sec, task_name=task)
            return {
                "since_hours": since_hours,
                "task": task,
                "tasks": [
                    {
                        "task_name": r.task_name,
                        "model": r.model,
                        "count": r.count,
                        "ok_count": r.ok_count,
                        "fail_count": r.fail_count,
                        "ok_rate": round(r.ok_rate, 4),
                        "p50_ms": r.p50_ms,
                        "p95_ms": r.p95_ms,
                        "p99_ms": r.p99_ms,
                        "avg_ms": r.avg_ms,
                        "max_ms": r.max_ms,
                    }
                    for r in rows
                ],
                "errors": errors,
            }
        except Exception as e:
            return {"error": f"vision_stats_unavailable:{type(e).__name__}"}

    @app.get("/api/bot-metrics")
    async def api_bot_metrics(request: Request):
        _api_auth(request)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
            snap = ms.snapshot()
            return {
                "messages_received": snap.get("messages_received", 0),
                "messages_replied":  snap.get("messages_replied", 0),
                "api_calls":         snap.get("api_calls", 0),
                "errors_count":      snap.get("errors_count", 0),
                "response_time_avg": snap.get("response_time_avg_ms", 0),
                "response_time_p99": snap.get("response_time_p99_ms", 0),
                "queue_size":        snap.get("queue_size", 0),
                "queue_drops":       snap.get("queue_drops", 0),
                "active_tasks":      snap.get("active_tasks", 0),
                "concurrency_limit": snap.get("concurrency_limit", 0),
                "trigger_layers":    snap.get("trigger_layers", {}),
                "circuit_breaker":   snap.get("circuit_breaker", {}),
                "reply_quality":     snap.get("reply_quality", {}),
                "memory":            snap.get("memory", {}),
                "companion_safe_skip": snap.get("companion_safe_skip", {}),
                "deferred_queue":    snap.get("deferred_queue", {}),
                "reactivation":      snap.get("reactivation", {}),
                "pacing":            snap.get("pacing", {}),
                "peer_typing_prefetch": snap.get("peer_typing_prefetch", {}),
                "startup_advisories": snap.get("startup_advisories", {}),
                "ai_healthy":        ms.ai_healthy(),
                "ai_errors":         ms._ai_consecutive_errors,
                "uptime_s":          round(ms.uptime_seconds()),
                "last_message_at":   snap.get("last_message_at"),
            }
        except Exception:
            return {"error": "metrics unavailable"}

    # ★ W2-D5.1：reactivation dry_run 样本审核端点
    @app.get("/api/reactivation/dry-run-samples")
    async def api_reactivation_dry_samples(
        request: Request, limit: int = 50, before_ts: float = 0,
    ):
        """返回 reactivation_loop dry_run 模式下最近生成的话术样本。

        ★ W3-D3.5：``before_ts`` 用于增量加载（>0 时只返早于此 ts 的）。

        运营审核流程：
          1. config 设 reactivation.enabled=true + dry_run=true
          2. 等几小时让 loop 跑出几条样本
          3. 调这个 API 看 LLM 生成的话术是否得体
          4. 验收通过后改 dry_run=false 真发
        """
        _api_auth(request)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            samples = get_metrics_store().reactivation_dry_samples(
                limit=limit,
                before_ts=before_ts if before_ts > 0 else None,
            )
            return {"count": len(samples), "samples": samples}
        except Exception as ex:
            return {"error": f"{type(ex).__name__}:{ex}"}

    # ★ W2-D6.2：dry_run sample feedback（运营点 like/dislike）
    @app.post("/api/reactivation/dry-run-feedback")
    async def api_reactivation_dry_feedback(request: Request):
        """提交对 dry_run 样本的人工反馈。

        body: {"sample_ts": float, "verdict": "like"|"dislike", "reason": "..."(opt)}
        - 写入 audit_store（永久审计）
        - 写入 metrics_store（dashboard 可见）
        后续 W3+ 用这些数据做 LLM prompt 调优 / 黑词学习
        """
        _api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        verdict = str(body.get("verdict", "")).strip().lower()
        if verdict not in ("like", "dislike"):
            return {"error": "verdict must be 'like' or 'dislike'"}
        sample_ts = float(body.get("sample_ts") or 0)
        reason = str(body.get("reason", "") or "")[:300]
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
            ms.record_reactivation_feedback(verdict, sample_ts)
            # ★ W2-D7.5：dislike → 把 reply_text 加入 in-memory 黑名单
            # 后续 reactivation_loop 生成时查相似度，命中重生成
            if verdict == "dislike" and sample_ts > 0:
                samples = ms.reactivation_dry_samples(limit=200)
                for s in samples:
                    if abs(float(s.get("ts") or 0) - sample_ts) < 1.0:
                        ms.add_disliked_reply(s.get("reply_text", ""))
                        break
        except Exception:
            pass
        if audit_store:
            try:
                audit_store.add_entry(
                    user="admin",
                    action="reactivation_dry_feedback",
                    detail=(
                        f"verdict={verdict} sample_ts={sample_ts} "
                        f"reason={reason[:120]}"
                    ),
                )
            except Exception:
                pass
        return {"ok": True, "verdict": verdict, "sample_ts": sample_ts}

    # ── 操作记录活动热力图 ────────────────────────────────────

    @app.get("/api/audit/activity")
    async def api_audit_activity(request: Request, days: int = 84):
        """返回过去 N 天每日活动数量，供热力图使用"""
        _api_auth(request)
        if not audit_store:
            return {"days": {}, "max": 0}
        try:
            rows = audit_store._conn.execute(
                "SELECT DATE(ts) as day, COUNT(*) as cnt "
                "FROM audit_log "
                "WHERE ts >= date('now', ? || ' days') "
                "GROUP BY day ORDER BY day",
                (f"-{days}",),
            ).fetchall()
            day_map = {r["day"]: r["cnt"] for r in rows}
            max_cnt = max(day_map.values(), default=1)
            return {"days": day_map, "max": max_cnt}
        except Exception:
            return {"days": {}, "max": 0}

    @app.post("/api/change-password")
    async def api_change_password(request: Request):
        _require_auth(request)
        body = await request.json()
        old_pw = body.get("old_password", "")
        new_pw = body.get("new_password", "")
        if not old_pw or not new_pw:
            raise HTTPException(400, "请输入旧密码和新密码")
        if len(new_pw) < 6:
            raise HTTPException(400, "新密码至少 6 位")
        uname = request.session.get("username", "")
        if not uname:
            raise HTTPException(400, "无法确定当前用户")
        user = user_store.verify(uname, old_pw)
        if not user:
            raise HTTPException(400, "当前密码错误")
        user_store.update_user(user["id"], password=new_pw)
        if audit_store:
            audit_store.log(uname, "change_password", uname)
        return {"ok": True}

    # ── 用户管理 ──────────────────────────────────────────────

    @app.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request):
        _require_role(request, "users")
        users = user_store.list_users()
        return templates.TemplateResponse(request, "users.html", {
            "users": users, "role_labels": ROLE_LABELS, "msg": ""
        })

    @app.post("/users/create")
    async def users_create(request: Request, username: str = Form(...),
                           password: str = Form(...), role: str = Form("viewer"),
                           display_name: str = Form("")):
        _require_role(request, "users")
        ajax = "application/json" in request.headers.get("accept", "")
        if len(password) < 6:
            if ajax:
                return {"ok": False, "detail": "密码至少 6 位"}
            users = user_store.list_users()
            return templates.TemplateResponse(request, "users.html", {
                "users": users, "role_labels": ROLE_LABELS,
                "msg": "密码至少 6 位"
            })
        result = user_store.create_user(username, password, role, display_name)
        if not result:
            if ajax:
                return {"ok": False, "detail": f"用户名 '{username}' 已存在或角色无效"}
            users = user_store.list_users()
            return templates.TemplateResponse(request, "users.html", {
                "users": users, "role_labels": ROLE_LABELS,
                "msg": f"创建失败：用户名 '{username}' 已存在或角色无效"
            })
        if audit_store:
            audit_store.log(request.session.get("username", ""), "create_user", username)
        if ajax:
            return {"ok": True, "id": result["id"], "username": result["username"], "role": result["role"]}
        return RedirectResponse("/users", status_code=303)

    @app.post("/users/update/{user_id}")
    async def users_update(user_id: int, request: Request, role: str = Form(None),
                           password: str = Form(None), enabled: str = Form(None)):
        _require_role(request, "users")
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
        _require_role(request, "users")
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
        _require_role(request, "users")
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
        _require_role(request, "users")
        current_jti = request.session.get("jti", "")
        if jti == current_jti:
            raise HTTPException(400, "不能踢出自己的当前会话")
        user_store.revoke_session(jti)
        actor = request.session.get("username", "")
        if audit_store:
            audit_store.log(actor, "revoke_session", jti[:8])
        return {"ok": True}

    @app.post("/api/sessions/revoke-all")
    async def api_sessions_revoke_all(request: Request):
        """撤销除自己外的所有 session"""
        _require_role(request, "users")
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

    # ── 全局系统配置 ──────────────────────────────────────────

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        _require_role(request, "settings")
        cfg = config_manager.config or {}
        ai  = cfg.get("ai", {})
        wb  = cfg.get("web_admin", {})
        tg  = cfg.get("telegram", {})
        notif = cfg.get("notifications", cfg.get("webhook", {}))
        he = cfg.get("human_escalation", {})
        try:
            he_agents_json = json.dumps(he.get("agents", []), ensure_ascii=False, indent=2)
        except Exception:
            he_agents_json = "[]"
        try:
            he_work_hours_json = json.dumps(he.get("work_hours", {}), ensure_ascii=False, indent=2)
        except Exception:
            he_work_hours_json = "{}"
        try:
            he_work_exceptions_json = json.dumps(he.get("work_exceptions", {}), ensure_ascii=False, indent=2)
        except Exception:
            he_work_exceptions_json = "{}"
        try:
            he_agent_teams_json = json.dumps(he.get("agent_teams", []), ensure_ascii=False, indent=2)
        except Exception:
            he_agent_teams_json = "[]"
        return templates.TemplateResponse(request, "settings.html", {
            "ai": ai, "wb": wb, "tg": tg, "notif": notif, "he": he,
            "he_agents_json": he_agents_json,
            "he_work_hours_json": he_work_hours_json,
            "he_work_exceptions_json": he_work_exceptions_json,
            "he_agent_teams_json": he_agent_teams_json,
        })

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

    @app.post("/api/settings/save")
    async def api_settings_save(request: Request, _=Depends(_api_write("manage_settings"))):
        body = await request.json()
        section = body.get("section", "")
        fields  = body.get("fields", {})
        if not section or not fields:
            raise HTTPException(400, "section 和 fields 不能为空")

        allowed_sections = {"ai", "voice_ai", "web_admin", "telegram", "notifications", "human_escalation"}
        if section not in allowed_sections:
            raise HTTPException(400, f"不允许修改 section: {section}")

        cfg = config_manager.config
        if cfg is None:
            cfg = {}
            config_manager.config = cfg
        updated = []
        if section == "voice_ai":
            mr = cfg.get("messenger_rpa")
            if not isinstance(mr, dict):
                mr = {}
                cfg["messenger_rpa"] = mr
            vo = mr.get("voice_output")
            if not isinstance(vo, dict):
                vo = {}
                mr["voice_output"] = vo
            vp = vo.get("voice_profile")
            if not isinstance(vp, dict):
                vp = {}
                vo["voice_profile"] = vp
            if "dashscope_api_key" in fields:
                key = str(fields.get("dashscope_api_key") or "").strip()
                if key:
                    vo["dashscope_api_key"] = key
                    updated.append("dashscope_api_key")
            simple_map = {
                "enabled": ("enabled", bool),
                "mode": ("mode", str),
                "trigger": ("trigger", str),
                "backend": ("backend", str),
                "voice": ("voice", str),
                "model": ("model", str),
                "format": ("format", str),
                "region": ("dashscope_region", str),
                "voice_profile_path": ("voice_profile_path", str),
                "send_audit_dir": ("send_audit_dir", str),
            }
            for src, (dst, caster) in simple_map.items():
                if src not in fields:
                    continue
                val = fields.get(src)
                if caster is bool:
                    val = bool(val)
                else:
                    val = str(val or "").strip()
                    if val == "" and dst not in ("voice_profile_path",):
                        continue
                vo[dst] = val
                updated.append(dst)
            for src, dst in (
                ("speaker_id", "speaker_id"),
                ("reference_audio_path", "reference_audio_path"),
            ):
                if src in fields and str(fields.get(src) or "").strip():
                    vp[dst] = str(fields.get(src) or "").strip()
                    updated.append(f"voice_profile.{dst}")
            vp["enabled"] = bool(fields.get("profile_enabled", vp.get("enabled", True)))
            vp["owner_consent"] = bool(fields.get("owner_consent", vp.get("owner_consent", True)))
            vp["backend"] = "voice_clone_command"
            region = str(vo.get("dashscope_region") or fields.get("region") or "cn").strip() or "cn"
            profile_path = str(
                vo.get("voice_profile_path")
                or fields.get("voice_profile_path")
                or "D:/workspace/telegram-mtproto-ai/voice_samples/qwen_my_voice.json"
            ).strip()
            vo["backend"] = "voice_clone_command"
            vo["voice"] = str(fields.get("voice") or vo.get("voice") or "my_voice").strip()
            vo["model"] = str(fields.get("model") or vo.get("model") or "qwen3-tts-vc-2026-01-22").strip()
            vo["format"] = str(fields.get("format") or vo.get("format") or "wav").strip()
            vp["command_args"] = [
                "python",
                "D:/workspace/telegram-mtproto-ai/tools/qwen_tts_wrapper.py",
                "--region", region,
                "--text", "{text}",
                "--out", "{out}",
                "--voice-profile", profile_path,
                "--language-type", "Japanese",
            ]
            config_manager.save()
            try:
                from src.ai.tts_pipeline import reset_tts_pipeline
                reset_tts_pipeline()
            except Exception:
                logger.debug("reset_tts_pipeline failed", exc_info=True)
            return {"ok": True, "updated": sorted(set(updated))}
        if section not in cfg:
            cfg[section] = {}

        # 逐字段更新（跳过空值以防止意外清空）
        updated = []
        if section == "human_escalation":
            if "agents_json" in fields:
                raw = fields.get("agents_json")
                try:
                    parsed = json.loads(raw) if (raw and str(raw).strip()) else []
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"agents_json 格式错误: {e}")
                if not isinstance(parsed, list):
                    raise HTTPException(400, "agents_json 必须是 JSON 数组")
                cfg[section]["agents"] = parsed
                updated.append("agents")
            if "work_hours_json" in fields:
                raw = fields.get("work_hours_json")
                try:
                    parsed = json.loads(raw) if (raw and str(raw).strip()) else {}
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"work_hours_json 格式错误: {e}")
                if not isinstance(parsed, dict):
                    raise HTTPException(400, "work_hours_json 必须是 JSON 对象")
                cfg[section]["work_hours"] = parsed
                updated.append("work_hours")
            if "work_exceptions_json" in fields:
                raw = fields.get("work_exceptions_json")
                try:
                    parsed = json.loads(raw) if (raw and str(raw).strip()) else {}
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"work_exceptions_json 格式错误: {e}")
                if not isinstance(parsed, dict):
                    raise HTTPException(400, "work_exceptions_json 必须是 JSON 对象")
                cfg[section]["work_exceptions"] = parsed
                updated.append("work_exceptions")
            if "agent_teams_json" in fields:
                raw = fields.get("agent_teams_json")
                try:
                    parsed = json.loads(raw) if (raw and str(raw).strip()) else []
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"agent_teams_json 格式错误: {e}")
                if not isinstance(parsed, list):
                    raise HTTPException(400, "agent_teams_json 必须是 JSON 数组")
                cfg[section]["agent_teams"] = parsed
                updated.append("agent_teams")

        _ftg_fields = {}
        for k, v in fields.items():
            if section == "human_escalation" and k in (
                "agents_json", "work_hours_json", "work_exceptions_json", "agent_teams_json",
            ):
                continue
            if section == "human_escalation" and k.startswith("forward_to_group_"):
                _ftg_fields[k.replace("forward_to_group_", "")] = v
                continue
            # API Key 等敏感字段若为空白则跳过（保留原值）
            if isinstance(v, str) and v.strip() == "" and k in ("api_key", "secret_key", "auth_token", "webhook_url"):
                continue
            # 数值转换
            if isinstance(v, str) and v.strip().lstrip("-").replace(".", "", 1).isdigit():
                try:
                    v = float(v) if "." in v else int(v)
                except ValueError:
                    pass
            # 布尔转换
            if isinstance(v, str) and v.lower() in ("true", "false"):
                v = v.lower() == "true"
            if section == "human_escalation" and k == "human_user_id":
                if isinstance(v, str) and v.strip() == "":
                    v = 0
                elif v is None:
                    v = 0
            cfg[section][k] = v
            updated.append(k)

        if section == "human_escalation" and _ftg_fields:
            ftg = cfg[section].get("forward_to_group")
            if not isinstance(ftg, dict):
                ftg = {}
            enabled_val = _ftg_fields.get("enabled")
            if isinstance(enabled_val, str):
                enabled_val = enabled_val.lower() == "true"
            ftg["enabled"] = bool(enabled_val) if enabled_val is not None else ftg.get("enabled", False)
            if "id" in _ftg_fields:
                ftg["group_id"] = str(_ftg_fields["id"]).strip()
            if "link" in _ftg_fields:
                ftg["group_link"] = str(_ftg_fields["link"]).strip()
            cfg[section]["forward_to_group"] = ftg
            updated.append("forward_to_group")

        try:
            config_manager.save()
        except Exception as e:
            raise HTTPException(500, f"配置保存失败: {e}")

        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "save_settings",
                            f"section={section}", "", f"fields={updated}")
        if section == "human_escalation" and telegram_client:
            h = getattr(telegram_client, "_human_escalation", None)
            if h:
                try:
                    h.reload_config(config_manager.config or {})
                except Exception:
                    pass
        if section == "human_escalation":
            invalidate_schedule_status_cache()
        return {"ok": True, "updated": updated}

    # ── 回复逻辑管理 API ──────────────────────────────────────
    @app.get("/api/reply-logic")
    async def api_reply_logic_get(request: Request, _=Depends(_api_auth)):
        cfg = config_manager.config or {}
        tg = cfg.get("telegram", {})
        trigger_cfg = cfg.get("trigger", {})
        reply_logic = tg.get("reply_logic", {})
        group_reply = tg.get("group_reply", {})

        trigger_rules = {}
        try:
            tr_path = trigger_cfg.get("config_file", "config/trigger_rules.yaml")
            import yaml as _yaml
            full_path = Path(config_manager.config_path).parent.parent / tr_path \
                if hasattr(config_manager, "config_path") else Path(tr_path)
            if full_path.exists():
                with open(full_path, "r", encoding="utf-8") as f:
                    trigger_rules = _yaml.safe_load(f) or {}
        except Exception:
            pass

        l2_threshold = (trigger_rules.get("l2_semantic_trigger", {})
                        .get("confidence_thresholds", {})
                        .get("reply_threshold", 0.75))
        l3_cooldown = (trigger_rules.get("l3_context_filter", {})
                       .get("cooldown", {})
                       .get("default_cooldown", 90))

        return {
            "trigger_enabled": trigger_cfg.get("enabled", False),
            "group_reply_mode": group_reply.get("mode", "mention_or_keyword"),
            "reply_chain_enabled": reply_logic.get("reply_chain", {}).get("enabled", True),
            "follow_up_enabled": reply_logic.get("follow_up", {}).get("enabled", True),
            "follow_up_lookback": reply_logic.get("follow_up", {}).get("lookback_count", 10),
            "session_window_enabled": reply_logic.get("session_window", {}).get("enabled", True),
            "session_window_minutes": reply_logic.get("session_window", {}).get("reply_within_minutes", 45),
            "l2_fallback_enabled": reply_logic.get("l2_fallback", {}).get("enabled", True),
            "ai_context_reply_enabled": reply_logic.get("ai_context_reply", {}).get("enabled", True),
            "l2_reply_threshold": l2_threshold,
            "l3_cooldown_seconds": l3_cooldown,
            "group_reply_keywords": group_reply.get("keywords", []),
        }

    @app.post("/api/reply-logic")
    async def api_reply_logic_save(request: Request, _=Depends(_api_write("manage_settings"))):
        body = await request.json()
        cfg = config_manager.config
        if cfg is None:
            cfg = {}
            config_manager.config = cfg
        if "telegram" not in cfg:
            cfg["telegram"] = {}
        tg = cfg["telegram"]
        if "reply_logic" not in tg:
            tg["reply_logic"] = {}
        rl = tg["reply_logic"]
        if "group_reply" not in tg:
            tg["group_reply"] = {}
        gr = tg["group_reply"]
        if "trigger" not in cfg:
            cfg["trigger"] = {}

        updated = []

        if "trigger_enabled" in body:
            cfg["trigger"]["enabled"] = bool(body["trigger_enabled"])
            updated.append("trigger.enabled")

        if "group_reply_mode" in body:
            gr["mode"] = body["group_reply_mode"]
            updated.append("group_reply.mode")

        for sub_key, cfg_path in [
            ("reply_chain_enabled", ("reply_chain", "enabled")),
            ("follow_up_enabled", ("follow_up", "enabled")),
            ("session_window_enabled", ("session_window", "enabled")),
            ("l2_fallback_enabled", ("l2_fallback", "enabled")),
            ("ai_context_reply_enabled", ("ai_context_reply", "enabled")),
        ]:
            if sub_key in body:
                section_name, field = cfg_path
                if section_name not in rl:
                    rl[section_name] = {}
                rl[section_name][field] = bool(body[sub_key])
                updated.append(f"reply_logic.{section_name}.{field}")

        if "follow_up_lookback" in body:
            if "follow_up" not in rl:
                rl["follow_up"] = {}
            rl["follow_up"]["lookback_count"] = int(body["follow_up_lookback"])
            updated.append("reply_logic.follow_up.lookback_count")

        if "session_window_minutes" in body:
            if "session_window" not in rl:
                rl["session_window"] = {}
            rl["session_window"]["reply_within_minutes"] = int(body["session_window_minutes"])
            updated.append("reply_logic.session_window.reply_within_minutes")

        # trigger_rules.yaml 更新
        tr_updated = False
        if "l2_reply_threshold" in body or "l3_cooldown_seconds" in body:
            try:
                import yaml as _yaml
                tr_path_str = cfg.get("trigger", {}).get("config_file", "config/trigger_rules.yaml")
                full_path = Path(config_manager.config_path).parent.parent / tr_path_str \
                    if hasattr(config_manager, "config_path") else Path(tr_path_str)
                if full_path.exists():
                    with open(full_path, "r", encoding="utf-8") as f:
                        tr = _yaml.safe_load(f) or {}

                    if "l2_reply_threshold" in body:
                        if "l2_semantic_trigger" not in tr:
                            tr["l2_semantic_trigger"] = {}
                        if "confidence_thresholds" not in tr["l2_semantic_trigger"]:
                            tr["l2_semantic_trigger"]["confidence_thresholds"] = {}
                        tr["l2_semantic_trigger"]["confidence_thresholds"]["reply_threshold"] = \
                            round(float(body["l2_reply_threshold"]), 2)
                        updated.append("trigger_rules.l2_reply_threshold")
                        tr_updated = True

                    if "l3_cooldown_seconds" in body:
                        if "l3_context_filter" not in tr:
                            tr["l3_context_filter"] = {}
                        if "cooldown" not in tr["l3_context_filter"]:
                            tr["l3_context_filter"]["cooldown"] = {}
                        tr["l3_context_filter"]["cooldown"]["default_cooldown"] = \
                            int(body["l3_cooldown_seconds"])
                        updated.append("trigger_rules.l3_cooldown")
                        tr_updated = True

                    if tr_updated:
                        with open(full_path, "w", encoding="utf-8") as f:
                            _yaml.dump(tr, f, allow_unicode=True, default_flow_style=False,
                                       sort_keys=False)
            except Exception as e:
                logger.warning("更新 trigger_rules.yaml 失败: %s", e)

        # reload four_layer_trigger if threshold/cooldown changed
        if tr_updated and telegram_client:
            flt = getattr(telegram_client, "four_layer_trigger", None)
            if flt and hasattr(flt, "reload_config"):
                try:
                    flt.reload_config()
                except Exception:
                    pass

        try:
            config_manager.save()
        except Exception as e:
            raise HTTPException(500, f"配置保存失败: {e}")

        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "save_reply_logic", "", "", f"fields={updated}")
        return {"ok": True, "updated": updated}

    @app.get("/api/human-escalation/shift")
    async def api_human_escalation_shift_get(request: Request):
        _api_auth(request)
        st = getattr(telegram_client, "_human_escalation_store", None) if telegram_client else None
        if not st:
            return {"ok": False, "on_duty": False, "msg": "store_unavailable"}
        return {"ok": True, "on_duty": bool(st.get_shift_on_duty())}

    @app.post("/api/human-escalation/shift")
    async def api_human_escalation_shift_set(
        request: Request, _=Depends(_api_write("manage_settings")),
    ):
        body = await request.json()
        on = bool(body.get("on_duty"))
        st = getattr(telegram_client, "_human_escalation_store", None) if telegram_client else None
        if not st:
            raise HTTPException(503, "人工转接存储未初始化")
        st.set_shift_on_duty(on)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "human_escalation_shift", f"on_duty={on}", "", "")
        return {"ok": True, "on_duty": on}

    @app.get("/api/human-escalation/schedule-status")
    async def api_human_escalation_schedule_status(request: Request):
        """排班自检：当前是否在周模板/例外窗口内、手动值班、综合 duty 是否放行、粗估下一开/关窗。"""
        _api_auth(request)
        cfg = config_manager.config or {}
        he = cfg.get("human_escalation") or {}
        if not isinstance(he, dict):
            he = {}
        tz = (he.get("timezone") or "UTC").strip() or "UTC"
        wh = he.get("work_hours") if isinstance(he.get("work_hours"), dict) else {}
        wex = he.get("work_exceptions") if isinstance(he.get("work_exceptions"), dict) else {}
        now = datetime.now(timezone.utc)
        st = getattr(telegram_client, "_human_escalation_store", None) if telegram_client else None

        from src.utils.work_schedule import (
            estimate_minutes_until_next_close,
            estimate_minutes_until_next_open,
            is_within_work_hours,
        )
        from src.utils.human_escalation import (
            _resolve_duty_mode,
            active_teams_status,
            duty_allows,
        )

        class _DummyShift:
            def get_shift_on_duty(self):
                return False

        try:
            step = int(he.get("schedule_estimate_step_minutes", 15) or 15)
        except (TypeError, ValueError):
            step = 15
        step = max(1, min(step, 60))
        try:
            fh = int(he.get("schedule_estimate_fine_horizon_hours", 24) or 24)
        except (TypeError, ValueError):
            fh = 24
        fh = max(0, min(fh, 168))
        try:
            ttl = float(he.get("schedule_status_cache_ttl_sec", 30) or 0)
        except (TypeError, ValueError):
            ttl = 30.0
        ttl = max(0.0, min(ttl, 300.0))

        minute_bucket = int(now.timestamp() // 60)
        cache_key = (_human_escalation_cfg_hash(he), minute_bucket)
        partial = _schedule_status_cache_get(cache_key, ttl)
        estimates_cached = partial is not None
        if partial is None:
            try:
                in_sched = is_within_work_hours(now, tz, wh, wex)
            except Exception:
                in_sched = False
            try:
                min_open = estimate_minutes_until_next_open(
                    now,
                    tz,
                    wh,
                    wex,
                    step_minutes=step,
                    fine_horizon_hours=fh,
                )
            except Exception:
                min_open = None
            try:
                min_close = estimate_minutes_until_next_close(
                    now,
                    tz,
                    wh,
                    wex,
                    step_minutes=step,
                    fine_horizon_hours=fh,
                )
            except Exception:
                min_close = None
            try:
                teams = active_teams_status(he)
            except Exception:
                teams = []
            partial = {
                "in_schedule": in_sched,
                "minutes_until_next_open": min_open,
                "minutes_until_next_close": min_close,
                "active_teams": teams,
            }
            _schedule_status_cache_set(cache_key, partial, ttl)

        manual = bool(st.get_shift_on_duty()) if st else False
        dm = _resolve_duty_mode(he)
        duty_eff = duty_allows(he, st or _DummyShift())
        try:
            from zoneinfo import ZoneInfo

            local = now.astimezone(ZoneInfo(tz))
            local_iso = local.isoformat()
        except Exception:
            local_iso = ""

        return {
            "ok": True,
            "enabled": bool(he.get("enabled")),
            "timezone": tz,
            "duty_mode": dm,
            "local_time": local_iso,
            "in_schedule": partial["in_schedule"],
            "manual_shift": manual,
            "duty_effective": duty_eff,
            "minutes_until_next_open": partial["minutes_until_next_open"],
            "minutes_until_next_close": partial["minutes_until_next_close"],
            "estimate_step_minutes": step,
            "team_fallback_to_global": bool(he.get("team_fallback_to_global", True)),
            "team_pick_mode": (he.get("team_pick_mode") or "union"),
            "mention_round_robin_scope": (he.get("mention_round_robin_scope") or "global"),
            "schedule_estimate_step_minutes": step,
            "schedule_estimate_fine_horizon_hours": fh,
            "schedule_status_cache_ttl_sec": ttl,
            "schedule_estimates_cached": estimates_cached,
            "active_teams": partial["active_teams"],
        }

    @app.get("/api/human-escalation/mention-round-robin")
    async def api_human_escalation_mention_round_robin(request: Request):
        """运维：全局 / 按群轮询计数快照（只读）。"""
        _api_auth(request)
        st = getattr(telegram_client, "_human_escalation_store", None) if telegram_client else None
        if not st:
            return {"ok": False, "msg": "store_unavailable", "global_idx": 0, "per_chat": []}
        gidx, rows = st.get_round_robin_snapshot(50)
        return {
            "ok": True,
            "global_idx": gidx,
            "per_chat": [
                {"chat_id": c, "idx": i, "updated_at": ts}
                for c, i, ts in rows
            ],
        }

    @app.get("/api/human-escalation/verify")
    async def api_human_escalation_verify(request: Request):
        """
        确认当前进程内 `human_escalation` 是否与 Web 保存后的内存配置一致，
        以及 Helper / SQLite 存储是否已挂载（与 Bot 运行时读取同一 `config_manager.config`）。
        """
        _api_auth(request)
        cfg = config_manager.config or {}
        he = cfg.get("human_escalation") or {}
        if not isinstance(he, dict):
            he = {}
        st = getattr(telegram_client, "_human_escalation_store", None) if telegram_client else None
        h = getattr(telegram_client, "_human_escalation", None) if telegram_client else None
        agents = he.get("agents")
        n_agents = len(agents) if isinstance(agents, list) else 0
        teams = he.get("agent_teams")
        n_teams = len(teams) if isinstance(teams, list) else 0
        from src.utils.human_escalation import _resolve_duty_mode

        dm = _resolve_duty_mode(he)
        try:
            rt = int(he.get("repeat_threshold", 3) or 3)
        except (TypeError, ValueError):
            rt = 3
        try:
            uid_fb = int(he.get("human_user_id") or 0)
        except (TypeError, ValueError):
            uid_fb = 0
        return {
            "ok": True,
            "helper_loaded": h is not None,
            "store_loaded": st is not None,
            "config_path": str(getattr(config_manager, "config_path", "") or ""),
            "effective": {
                "enabled": bool(he.get("enabled")),
                "repeat_threshold": max(2, rt),
                "duty_mode": dm,
                "timezone": (he.get("timezone") or "UTC").strip() or "UTC",
                "escalation_cooldown_scope": (
                    (he.get("escalation_cooldown_scope") or "per_normalized_question")
                    .strip()
                    or "per_normalized_question"
                ),
                "agents_count": n_agents,
                "agent_teams_count": n_teams,
                "single_fallback_username": bool(str(he.get("human_username") or "").strip()),
                "single_fallback_user_id": uid_fb,
            },
            "note": "来源：config_manager.config（Web 保存后立即写入同一 dict）；"
            "Bot 处理消息时会对 HumanEscalationHelper.reload_config(同一 dict)。",
        }

    # ── 意图关键词管理（M3 热更新） ──────────────────────────
    @app.get("/api/settings/intent-keywords")
    async def api_get_intent_keywords(request: Request):
        """获取所有意图关键词配置"""
        _api_auth(request)
        cfg = config_manager.config or {}
        intent_cfg = cfg.get("intent", {})
        return {
            "keywords": intent_cfg.get("keywords", {}),
            "patterns": intent_cfg.get("patterns", {}),
        }

    @app.put("/api/settings/intent-keywords")
    async def api_update_intent_keywords(request: Request):
        """更新意图关键词配置 + 热更新 SkillManager"""
        _api_auth(request)
        body = await request.json()
        new_kw = body.get("keywords")
        if not isinstance(new_kw, dict):
            raise HTTPException(400, "keywords 必须是 {intent: [kw1, kw2, ...]} 格式")

        cfg = config_manager.config
        if "intent" not in cfg:
            cfg["intent"] = {}
        cfg["intent"]["keywords"] = new_kw
        try:
            config_manager.save()
        except Exception as e:
            raise HTTPException(500, f"保存失败: {e}")

        # 热更新 SkillManager
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                sm.intent_keywords = new_kw
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "update_intent_keywords", "",
                            "", f"intents={list(new_kw.keys())}")
        return {"ok": True, "intents": list(new_kw.keys())}

    @app.post("/api/settings/test-intent")
    async def api_test_intent(request: Request):
        """测试意图路由：输入消息，返回识别结果"""
        _api_auth(request)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text 不能为空")
        result = {"text": text, "intent": "direct_chat", "matched_by": "none"}
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm and hasattr(sm, "_recognize_intent"):
                intent = sm._recognize_intent(text)
                result["intent"] = intent
                result["matched_by"] = "skill_manager"
        else:
            cfg = config_manager.config or {}
            kw = cfg.get("intent", {}).get("keywords", {})
            text_lower = text.lower()
            for intent_name, keywords in kw.items():
                for k in (keywords or []):
                    if k.lower() in text_lower:
                        result["intent"] = intent_name
                        result["matched_by"] = f"keyword:{k}"
                        break
                if result["matched_by"] != "none":
                    break
        return result

    @app.get("/api/settings/test-webhook")
    async def api_settings_test_webhook(request: Request):
        """发送一条测试 Webhook 通知"""
        _api_auth(request)
        cfg = config_manager.config or {}
        url = cfg.get("notifications", {}).get("webhook_url", "")
        if not url:
            raise HTTPException(400, "未配置 Webhook URL")
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=10) as c:
                r = await c.post(url, json={
                    "type": "test",
                    "message": "AI 助手系统 Webhook 测试消息",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
            return {"ok": r.status_code < 300, "status": r.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 知识库分析报告 ────────────────────────────────────────

    @app.get("/api/kb/report")
    async def api_kb_report(request: Request, fmt: str = "html"):
        """生成知识库分析报告（自包含 HTML，可打印为 PDF）"""
        _api_auth(request)
        from fastapi.responses import Response as _Response
        from src.web.kb_report import build_kb_report
        html_content = build_kb_report(_kb_store, audit_store)
        ts = time.strftime("%Y%m%d_%H%M%S")
        return _Response(
            html_content.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="kb_report_{ts}.html"'},
        )

    # ── 知识库图片静态文件 ────────────────────────────────────

    @app.get("/kb-images/{filename}")
    async def kb_image_serve(filename: str, request: Request):
        """静态服务知识库图片文件"""
        _api_auth(request)
        from pathlib import Path as _P
        import mimetypes as _mt
        img_dir = _P(config_manager.config_path).parent / "kb_images"
        filepath = img_dir / filename
        # 防路径穿越
        if not filepath.resolve().is_relative_to(img_dir.resolve()):
            raise HTTPException(403, "访问被拒绝")
        if not filepath.exists():
            raise HTTPException(404, "图片不存在")
        from fastapi.responses import Response as _Response
        mime = _mt.guess_type(str(filepath))[0] or "image/jpeg"
        return _Response(
            filepath.read_bytes(),
            media_type=mime,
            headers={"Cache-Control": "public, max-age=86400"},
        )

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

    @app.get("/strategies", response_class=HTMLResponse)
    async def strategies_page(request: Request, _=Depends(_page_auth)):
        rs = config_manager.get_strategies_config()
        strategies = rs.get("strategies", {})
        intent_map = rs.get("intent_strategy_map", {})
        return templates.TemplateResponse(request, "strategies.html", {
            "strategies": strategies, "intent_map": intent_map,
            "intent_display_names": _get_intent_display_names(),
        })

    @app.put("/api/strategies/{strategy_id}")
    async def api_update_strategy(strategy_id: str, request: Request, _=Depends(_api_write("edit_strategy"))):
        body = await request.json()
        rs = config_manager.get_strategies_config()
        strategies = rs.get("strategies", {})
        if strategy_id not in strategies:
            raise HTTPException(404, f"Strategy '{strategy_id}' not found")
        snap_content = yaml.dump(rs, allow_unicode=True, default_flow_style=False)
        for key in ("temperature", "max_tokens", "context_rounds",
                    "reply_probability", "enabled", "skip_ai"):
            if key in body:
                strategies[strategy_id][key] = body[key]
        rs["strategies"] = strategies
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        actor = request.session.get("username", "api")
        _auto_snapshot("reply_strategies", snap_content, actor)
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm and hasattr(sm, "_refresh_strategies"):
                sm._refresh_strategies()
        if audit_store:
            audit_store.log(actor, "update_strategy", strategy_id, "", str(body)[:100])
        return {"ok": True, "strategy_id": strategy_id}

    @app.put("/api/strategies/mapping")
    async def api_update_mapping(request: Request, _=Depends(_api_write("edit_strategy"))):
        body = await request.json()
        intent = body.get("intent")
        sid = body.get("strategy_id")
        if not intent or not sid:
            raise HTTPException(400, "Missing intent or strategy_id")
        rs = config_manager.get_strategies_config()
        strategies = rs.get("strategies", {})
        if sid not in strategies:
            raise HTTPException(404, f"Strategy '{sid}' not found")
        rs.setdefault("intent_strategy_map", {})[intent] = sid
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm and hasattr(sm, "_refresh_strategies"):
                sm._refresh_strategies()
        if audit_store:
            audit_store.log("web_admin", "update_mapping", intent, "", sid)
        return {"ok": True, "intent": intent, "strategy_id": sid}

    @app.get("/api/strategies")
    async def api_get_strategies(request: Request, _=Depends(_api_auth)):
        rs = config_manager.get_strategies_config()
        return {
            "strategies": rs.get("strategies", {}),
            "intent_strategy_map": rs.get("intent_strategy_map", {}),
        }

    # ── 策略 A/B 分析 ──────────────────────────────────

    def _get_strategy_tracker():
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                return getattr(sm, "strategy_tracker", None)
        return None

    @app.get("/strategy-analytics", response_class=HTMLResponse)
    async def strategy_analytics_page(request: Request, _=Depends(_page_auth),
                                      hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        summary = tracker.strategy_summary(hours) if tracker else []
        matrix = tracker.intent_strategy_matrix(hours) if tracker else []
        total = tracker.total_events(hours) if tracker else 0
        if tracker:
            tracker.mark_no_follow_up()

        rs = config_manager.get_strategies_config()
        strategies_cfg = rs.get("strategies", {})

        from src.utils.strategy_advisor import analyze, suggest_param_adjustments, compute_quality_score_breakdown
        advisor = analyze(summary, strategies_cfg) if summary else {
            "scores": {}, "advisories": [], "best": None, "worst": None}
        for s in summary:
            s["quality_score"] = advisor["scores"].get(s["strategy_id"], 0)
            s["score_breakdown"] = compute_quality_score_breakdown(s)
            s["model_id"] = strategies_cfg.get(s["strategy_id"], {}).get("model", "")

        param_suggestions = suggest_param_adjustments(summary, strategies_cfg) if summary else []
        ab_tests = rs.get("ab_tests", {})
        autopilot = rs.get("autopilot", {})
        session_stats = tracker.session_stats(hours) if tracker else {}
        model_summary = tracker.model_summary(hours) if tracker else []
        model_matrix = tracker.model_strategy_matrix(hours) if tracker else []
        user_segments = tracker.user_segment_analysis(hours) if tracker else {}

        return templates.TemplateResponse(request, "strategy_analytics.html", {
            "summary": summary, "matrix": matrix,
            "total": total, "hours": hours,
            "advisor": advisor, "ab_tests": ab_tests,
            "param_suggestions": param_suggestions,
            "autopilot": autopilot, "session_stats": session_stats,
            "model_summary": model_summary, "model_matrix": model_matrix,
            "user_segments": user_segments,
        })

    @app.get("/api/strategy-analytics")
    async def api_strategy_analytics(request: Request, _=Depends(_api_auth),
                                     hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"summary": [], "matrix": [], "total": 0}
        tracker.mark_no_follow_up()
        summary = tracker.strategy_summary(hours)
        rs = config_manager.get_strategies_config()
        strategies_cfg = rs.get("strategies", {})
        from src.utils.strategy_advisor import analyze, suggest_param_adjustments
        advisor = analyze(summary, strategies_cfg)
        return {
            "summary": summary,
            "matrix": tracker.intent_strategy_matrix(hours),
            "total": tracker.total_events(hours),
            "hours": hours,
            "advisor": advisor,
            "param_suggestions": suggest_param_adjustments(summary, strategies_cfg),
            "session_stats": tracker.session_stats(hours),
            "model_summary": tracker.model_summary(hours),
            "model_matrix": tracker.model_strategy_matrix(hours),
            "user_segments": tracker.user_segment_analysis(hours),
        }

    @app.get("/api/strategy-analytics/compare")
    async def api_strategy_compare(request: Request, _=Depends(_api_auth),
                                   hours: int = Query(24, ge=1, le=720)):
        """B2: Compare current period vs previous period of same length."""
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"current": [], "previous": [], "changes": {}}
        tracker.mark_no_follow_up()
        from src.utils.strategy_advisor import compute_quality_score_breakdown
        current = tracker.strategy_summary(hours, offset_hours=0)
        previous = tracker.strategy_summary(hours, offset_hours=hours)
        # compute changes per strategy
        prev_map = {s["strategy_id"]: s for s in previous}
        changes = {}
        for s in current:
            sid = s["strategy_id"]
            s["score_breakdown"] = compute_quality_score_breakdown(s)
            s["quality_score"] = s["score_breakdown"]["total"]
            p = prev_map.get(sid)
            if p:
                p["score_breakdown"] = compute_quality_score_breakdown(p)
                p["quality_score"] = p["score_breakdown"]["total"]
                changes[sid] = {
                    "total_delta": s["total"] - p["total"],
                    "avg_ms_delta": s["avg_ms"] - p["avg_ms"],
                    "follow_up_delta": round(s["follow_up_rate"] - p["follow_up_rate"], 1),
                    "silence_delta": round(s["silence_rate"] - p["silence_rate"], 1),
                    "score_delta": round(s["quality_score"] - p["quality_score"], 1),
                }
            else:
                changes[sid] = {"total_delta": s["total"], "avg_ms_delta": 0,
                                "follow_up_delta": 0, "silence_delta": 0, "score_delta": 0}
        return {"current": current, "previous": previous, "changes": changes, "hours": hours}

    @app.get("/api/strategy-analytics/{strategy_id}/hourly")
    async def api_strategy_hourly(strategy_id: str, request: Request,
                                  _=Depends(_api_auth),
                                  hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"data": []}
        return {"data": tracker.strategy_hourly(strategy_id, hours)}

    @app.get("/api/model-summary")
    async def api_model_summary(request: Request, _=Depends(_api_auth),
                                hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"model_summary": [], "model_matrix": []}
        return {
            "model_summary": tracker.model_summary(hours),
            "model_matrix": tracker.model_strategy_matrix(hours),
        }

    @app.get("/api/user-segments")
    async def api_user_segments(request: Request, _=Depends(_api_auth),
                                hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"segments": {}}
        return {"segments": tracker.user_segment_analysis(hours)}

    @app.put("/api/ab-tests/{intent}")
    async def api_update_ab_test(intent: str, request: Request,
                                 _=Depends(_api_write("edit_strategy"))):
        """创建/更新/关闭某个意图的 A/B 灰度测试"""
        body = await request.json()
        rs = config_manager.get_strategies_config()
        ab = rs.setdefault("ab_tests", {})
        if body.get("delete"):
            ab.pop(intent, None)
        else:
            ab[intent] = {
                "enabled": body.get("enabled", True),
                "variants": body.get("variants", []),
            }
        rs["ab_tests"] = ab
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm and hasattr(sm, "_refresh_strategies"):
                sm._refresh_strategies()
        if audit_store:
            audit_store.log(request.session.get("username", "web_admin"),
                            "update_ab_test", intent, "", str(body)[:200])
        return {"ok": True, "intent": intent}

    @app.get("/api/ab-tests/evaluate")
    async def api_ab_evaluate(request: Request, hours: int = 24):
        """L3: 评估所有活跃 A/B 测试，返回结论（胜者/继续/数据不足）"""
        _api_auth(request)
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"results": [], "error": "策略追踪器未就绪"}
        tracker.mark_no_follow_up()
        summary = tracker.strategy_summary(min(hours, 168))
        rs = config_manager.get_strategies_config()
        ab_tests = rs.get("ab_tests", {})
        strategies_cfg = rs.get("strategies", {})
        from src.utils.strategy_advisor import evaluate_ab_tests
        results = evaluate_ab_tests(ab_tests, summary, strategies_cfg)
        return {"results": results, "ab_tests": ab_tests}

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

    @app.get("/api/strategy-history/{strategy_id}")
    async def api_strategy_history(strategy_id: str, request: Request, _=Depends(_page_auth),
                                   limit: int = 20):
        """返回某个策略最近 N 次参数变更记录（来自 audit_store）"""
        if not audit_store:
            return {"history": [], "strategy_id": strategy_id}
        all_entries = audit_store.query(limit=2000)
        # 筛选与该策略相关的操作记录
        history = [
            e for e in all_entries
            if strategy_id in str(e.get("target", "")) or strategy_id in str(e.get("new_val", ""))
            if "strategy" in str(e.get("action", "")).lower()
        ]
        return {"history": history[:limit], "strategy_id": strategy_id}

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
    @app.get("/knowledge", response_class=HTMLResponse)
    async def knowledge_page(request: Request):
        _require_auth(request)
        stats = _kb_store.stats()
        return templates.TemplateResponse(request, "knowledge.html", {
            "categories": KB_CATEGORIES,
            "stats": stats,
        })

    # ---------- 知识条目 ----------
    @app.get("/api/kb/entries")
    async def api_kb_list(
        request: Request,
        category: str = "",
        search: str = "",
        enabled_only: bool = False,
    ):
        _api_auth(request)
        entries = _kb_store.list_entries(category=category, enabled_only=enabled_only, search=search)
        for e in entries:
            try:
                e["triggers"] = json.loads(e.get("triggers", "[]"))
            except Exception:
                e["triggers"] = []
        return {"entries": entries, "total": len(entries)}

    @app.get("/api/kb/entries/{entry_id}")
    async def api_kb_get_entry(request: Request, entry_id: str):
        _api_auth(request)
        entry = _kb_store.get_entry(entry_id)
        if not entry:
            raise HTTPException(status_code=404)
        try:
            entry["triggers"] = json.loads(entry.get("triggers", "[]"))
        except Exception:
            entry["triggers"] = []
        try:
            entry["negative_triggers"] = json.loads(entry.get("negative_triggers", "[]"))
        except Exception:
            entry["negative_triggers"] = []
        return entry

    def _run_kb_conflict_checkers(data: dict) -> list:
        """Run all registered KB conflict checkers from domain packs."""
        warnings = []
        for checker in getattr(app.state, "kb_conflict_checkers", []):
            try:
                result = checker(data)
                if result:
                    warnings.extend(result)
            except Exception:
                pass
        return warnings

    def _format_trigger_overlap_messages(overlaps: list) -> list:
        """将 find_trigger_overlaps 结果格式化为可读中文列表。"""
        lines = []
        for o in overlaps or []:
            st = "【已启用】" if o.get("other_enabled") else "【已停用】"
            cat = o.get("other_category") or ""
            cat_s = f"「{cat}」" if cat else ""
            if o.get("kind") == "exact":
                lines.append(
                    f"{st} 触发词「{o.get('my_trigger', '')}」与条目 {cat_s}"
                    f"《{o.get('other_title', '')}》（id={o.get('other_id', '')}）"
                    f"中的「{o.get('other_trigger', '')}」完全相同"
                )
            else:
                lines.append(
                    f"{st} 触发词「{o.get('my_trigger', '')}」与条目 {cat_s}"
                    f"《{o.get('other_title', '')}》（id={o.get('other_id', '')}）"
                    f"的「{o.get('other_trigger', '')}」存在包含关系，可能影响命中排序"
                )
        return lines

    @app.post("/api/kb/check-conflict")
    async def api_kb_check_conflict(request: Request):
        """前端实时检测 KB 条目是否与域包数据冲突（由域包注册检测器）"""
        _api_auth(request)
        data = await request.json()
        warnings = _run_kb_conflict_checkers(data)
        return {"has_conflict": bool(warnings), "warnings": warnings}

    @app.post("/api/kb/check-trigger-overlaps")
    async def api_kb_check_trigger_overlaps(request: Request):
        """检测触发词与其他条目的重复 / 包含关系（保存前或编辑时调用）。"""
        _api_auth(request)
        data = await request.json()
        eid = (data.get("entry_id") or data.get("id") or "").strip() or None
        overlaps = _kb_store.find_trigger_overlaps(eid, data.get("triggers", []))
        msgs = _format_trigger_overlap_messages(overlaps)
        return {
            "has_overlap": bool(overlaps),
            "overlaps": overlaps,
            "overlap_messages": msgs,
        }

    @app.post("/api/kb/entries")
    async def api_kb_add_entry(request: Request):
        _api_auth(request)
        data = await request.json()
        overlaps = _kb_store.find_trigger_overlaps(None, data.get("triggers", []))
        if overlaps and not data.get("_force_save_triggers"):
            return {
                "ok": False,
                "trigger_overlap": True,
                "overlaps": overlaps,
                "overlap_messages": _format_trigger_overlap_messages(overlaps),
            }
        conflict_warnings = _run_kb_conflict_checkers(data)
        if conflict_warnings and not data.get("_force_save"):
            return {"ok": False, "conflict": True, "warnings": conflict_warnings}
        entry_id = _kb_store.add_entry(data)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_add_entry", entry_id)
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop().create_task(_fire_webhook(
                "kb_change", actor, data.get("title", entry_id),
                f"新增知识条目: {data.get('title', entry_id)}"
            ))
        except RuntimeError:
            pass
        return {"id": entry_id, "ok": True,
                "warnings": conflict_warnings if conflict_warnings else None}

    @app.put("/api/kb/entries/{entry_id}")
    async def api_kb_update_entry(request: Request, entry_id: str):
        _api_auth(request)
        data = await request.json()
        if "triggers" in data:
            overlaps = _kb_store.find_trigger_overlaps(entry_id, data.get("triggers", []))
            if overlaps and not data.get("_force_save_triggers"):
                return {
                    "ok": False,
                    "trigger_overlap": True,
                    "overlaps": overlaps,
                    "overlap_messages": _format_trigger_overlap_messages(overlaps),
                }
        conflict_warnings = _run_kb_conflict_checkers(data)
        if conflict_warnings and not data.get("_force_save"):
            return {"ok": False, "conflict": True, "warnings": conflict_warnings}
        actor = request.session.get("username", "web_admin")
        _kb_store.save_version(entry_id, editor=actor)
        ok = _kb_store.update_entry(entry_id, data)
        if audit_store:
            audit_store.log(actor, "kb_update_entry", entry_id)
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop().create_task(_fire_webhook(
                "kb_change", actor, data.get("title", entry_id),
                f"更新知识条目: {data.get('title', entry_id)}"
            ))
        except RuntimeError:
            pass
        return {"ok": ok,
                "warnings": conflict_warnings if conflict_warnings else None}

    # ---------- 版本历史 ----------
    @app.get("/api/kb/entries/{entry_id}/versions")
    async def api_kb_entry_versions(request: Request, entry_id: str):
        _api_auth(request)
        return {"versions": _kb_store.list_versions(entry_id)}

    @app.get("/api/kb/versions/{version_id}")
    async def api_kb_get_version(request: Request, version_id: str):
        _api_auth(request)
        ver = _kb_store.get_version(version_id)
        if not ver:
            raise HTTPException(status_code=404)
        return ver

    @app.post("/api/kb/versions/{version_id}/restore")
    async def api_kb_restore_version(request: Request, version_id: str):
        _api_auth(request)
        actor = request.session.get("username", "web_admin")
        ok = _kb_store.restore_version(version_id, editor=actor)
        if not ok:
            raise HTTPException(status_code=404)
        if audit_store:
            audit_store.log(actor, "kb_restore_version", version_id)
        return {"ok": True}

    @app.delete("/api/kb/entries/{entry_id}")
    async def api_kb_delete_entry(request: Request, entry_id: str):
        _api_auth(request)
        # 获取标题再删除，用于通知
        entry_before = _kb_store.get_entry(entry_id)
        title_before = (entry_before or {}).get("title", entry_id)
        _kb_store.delete_entry(entry_id)
        # 同步删除该条目关联的图片文件
        from pathlib import Path as _P
        img_names = _kb_store.delete_all_entry_images(entry_id)
        img_dir = _P(config_manager.config_path).parent / "kb_images"
        for fname in img_names:
            try:
                (img_dir / fname).unlink(missing_ok=True)
            except Exception:
                pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_delete_entry", entry_id)
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop().create_task(_fire_webhook(
                "kb_change", actor, title_before,
                f"删除知识条目: {title_before}"
            ))
        except RuntimeError:
            pass
        return {"ok": True}

    # ---------- 错误码 ----------
    @app.get("/api/kb/error-codes")
    async def api_kb_list_ec(request: Request):
        _api_auth(request)
        return {"error_codes": _kb_store.list_error_codes()}

    @app.post("/api/kb/error-codes")
    async def api_kb_add_ec(request: Request):
        _api_auth(request)
        data = await request.json()
        ec_id = _kb_store.add_error_code(data)
        return {"id": ec_id, "ok": True}

    @app.put("/api/kb/error-codes/{ec_id}")
    async def api_kb_update_ec(request: Request, ec_id: str):
        _api_auth(request)
        data = await request.json()
        ok = _kb_store.update_error_code(ec_id, data)
        return {"ok": ok}

    @app.delete("/api/kb/error-codes/{ec_id}")
    async def api_kb_delete_ec(request: Request, ec_id: str):
        _api_auth(request)
        _kb_store.delete_error_code(ec_id)
        return {"ok": True}

    # ---------- 对话示例 ----------
    @app.get("/api/kb/examples")
    async def api_kb_list_examples(request: Request, category: str = "", language: str = ""):
        _api_auth(request)
        return {"examples": _kb_store.list_examples(category=category, language=language)}

    @app.post("/api/kb/examples")
    async def api_kb_add_example(request: Request):
        _api_auth(request)
        data = await request.json()
        ex_id = _kb_store.add_example(data)
        return {"id": ex_id, "ok": True}

    @app.delete("/api/kb/examples/{ex_id}")
    async def api_kb_delete_example(request: Request, ex_id: str):
        _api_auth(request)
        _kb_store.delete_example(ex_id)
        return {"ok": True}

    # ---------- 硬规则 ----------
    @app.get("/api/kb/rules")
    async def api_kb_list_rules(request: Request):
        _api_auth(request)
        return {"rules": _kb_store.get_rules(enabled_only=False)}

    @app.post("/api/kb/rules")
    async def api_kb_add_rule(request: Request):
        _api_auth(request)
        data = await request.json()
        rule_id = _kb_store.add_rule(data)
        return {"id": rule_id, "ok": True}

    @app.delete("/api/kb/rules/{rule_id}")
    async def api_kb_delete_rule(request: Request, rule_id: str):
        _api_auth(request)
        _kb_store.delete_rule(rule_id)
        return {"ok": True}

    # ---------- 反馈 ----------
    @app.get("/api/kb/feedback")
    async def api_kb_list_feedback(request: Request, limit: int = 50):
        _api_auth(request)
        return {"feedback": _kb_store.list_feedback(limit=limit)}

    @app.post("/api/kb/feedback")
    async def api_kb_add_feedback(request: Request):
        # 不要求登录，bot 进程可直接调用
        data = await request.json()
        fb_id = _kb_store.add_feedback(data)
        return {"id": fb_id, "ok": True}

    @app.post("/api/kb/feedback/{fb_id}/promote")
    async def api_kb_promote_feedback(request: Request, fb_id: str):
        _api_auth(request)
        ok = _kb_store.promote_feedback_to_example(fb_id)
        return {"ok": ok}

    # ---------- 沙盒测试 ----------
    @app.post("/api/kb/sandbox")
    async def api_kb_sandbox(request: Request):
        _api_auth(request)
        data = await request.json()
        query = data.get("query", "")
        lang = data.get("lang", "zh")
        t0 = time.time()
        result = _kb_store.search(query, top_k=5, lang=lang)
        ai_context = _kb_store.build_ai_context_from_result(result, lang=lang)
        elapsed_ms = int((time.time() - t0) * 1000)
        for e in result.get("entries", []):
            try:
                e["triggers"] = json.loads(e.get("triggers", "[]"))
            except Exception:
                e["triggers"] = []
        return {
            "search_result": result,
            "ai_context": ai_context,
            "elapsed_ms": elapsed_ms,
            "search_mode": result.get("search_mode", "bm25"),
        }

    @app.post("/api/kb/sandbox/save-example")
    async def api_kb_sandbox_save_example(request: Request):
        """将沙盒对话另存为 KB 对话示例（高质量示例反哺知识库）"""
        _api_auth(request)
        data = await request.json()
        user_msg = (data.get("user_message") or "").strip()
        ai_reply  = (data.get("ai_reply") or "").strip()
        category  = data.get("category", "其他")
        lang      = data.get("lang", "zh")
        if not user_msg or not ai_reply:
            raise HTTPException(400, "user_message 和 ai_reply 不能为空")
        ex_id = _kb_store.add_example({
            "category": category,
            "user_message": user_msg,
            "correct_reply": ai_reply,
            "language": lang,
            "quality": 1,
            "source": "sandbox",
        })
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_sandbox_save_example", ex_id, user_msg[:80], ai_reply[:80])
        return {"ok": True, "id": ex_id}

    @app.get("/api/kb/category-stats")
    async def api_kb_category_stats(request: Request):
        """分类使用统计：每个分类的条目数、总使用次数、零使用条目数"""
        _api_auth(request)
        with _kb_store._conn() as c:
            rows = c.execute(
                "SELECT category, COUNT(*) as cnt, "
                "SUM(use_count) as total_use, "
                "SUM(CASE WHEN use_count=0 AND enabled=1 THEN 1 ELSE 0 END) as zero_use "
                "FROM kb_entries WHERE enabled=1 "
                "GROUP BY category ORDER BY total_use DESC"
            ).fetchall()
        return {"categories": [dict(r) for r in rows]}

    # ── 知识条目 AI 自动生成 ──────────────────────────────────

    async def _auto_fill_entry(entry_id: str, title: str, category: str,
                               source_query: str = ""):
        """
        后台自动填充新建 KB 条目的内容（fire-and-forget）。
        用 AI 生成 scenario/steps/principles/example_reply_zh，
        然后 UPDATE 到已有条目。
        """
        import httpx as _httpx, re as _re
        ai_cfg = config_manager.config.get("ai", {})
        api_key = ai_cfg.get("api_key", "")
        base_url = (ai_cfg.get("base_url", "")).rstrip("/")
        model = ai_cfg.get("model", "gemini-2.5-flash")
        if not api_key or not base_url:
            return

        hint = f"用户原始问题: 「{source_query}」" if source_query else ""
        sys_prompt = (
            "你是一位资深客服话术专家，专注于支付/金融领域。"
            "请根据标题和用户问题，生成完整的客服知识条目。"
            "严格只返回纯 JSON，不要代码块标记或额外说明。"
        )
        user_prompt = (
            f"标题：{title}\n分类：{category}\n{hint}\n\n"
            "返回 JSON：\n"
            '{"triggers":["关键词1","关键词2","关键词3"],'
            '"scenario":"什么场景下用户会问",'
            '"steps":"1. 步骤1\\n2. 步骤2\\n3. 步骤3",'
            '"principles":"处理原则",'
            '"example_reply_zh":"客服标准回复(100字内,友好专业)",'
            '"forbidden":"不能做的事"}'
        )
        try:
            async with _httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [
                              {"role": "system", "content": sys_prompt},
                              {"role": "user", "content": user_prompt},
                          ],
                          "max_tokens": 800, "temperature": 0.6,
                          "response_format": {"type": "json_object"}},
                )
            raw = resp.json()["choices"][0]["message"]["content"]
            generated = json.loads(raw)
        except json.JSONDecodeError:
            m = _re.search(r'\{[\s\S]+\}', raw)
            if not m:
                return
            try:
                generated = json.loads(m.group())
            except Exception:
                return
        except Exception:
            return

        update_fields = {}
        for field in ("scenario", "steps", "principles", "example_reply_zh", "forbidden"):
            val = (generated.get(field) or "").strip()
            if val:
                update_fields[field] = val
        new_triggers = generated.get("triggers", [])
        if isinstance(new_triggers, list) and new_triggers:
            update_fields["triggers"] = new_triggers

        if update_fields:
            _kb_store.update_entry(entry_id, update_fields)
            logger.info("L1 自动填充完成: entry=%s fields=%s",
                        entry_id, list(update_fields.keys()))

    @app.post("/api/kb/ai-generate")
    async def api_kb_ai_generate(request: Request):
        """
        根据给定主题，用 AI 自动补全知识条目的所有字段。
        输入: {topic, category, hint, lang}
        输出: {ok, entry: {triggers, scenario, steps, principles, example_reply_zh, forbidden}}
        """
        _api_auth(request)
        import httpx as _httpx, re as _re
        data = await request.json()
        topic    = (data.get("topic") or "").strip()
        category = data.get("category", "其他")
        hint     = data.get("hint", "")
        if not topic:
            raise HTTPException(400, "topic 不能为空")

        ai_cfg   = config_manager.config.get("ai", {})
        api_key  = ai_cfg.get("api_key", "")
        base_url = (ai_cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
        model    = ai_cfg.get("model", "deepseek-chat")
        if not api_key:
            raise HTTPException(400, "AI 未配置，请先在设置页面填入 API Key")

        sys_prompt = (
            "你是一位资深客服话术专家，专注于电商、SaaS、金融支付领域的客户服务。"
            "请根据给定主题，生成一条完整的知识库条目，严格以 JSON 格式返回，不要任何解释和代码块标记。"
        )
        hint_part = f"\n额外提示：{hint}" if hint else ""
        user_prompt = f"""主题/标题：{topic}
分类：{category}{hint_part}

请返回以下 JSON（字段名必须完全匹配，值用中文）：
{{
  "triggers": ["关键词1","关键词2","关键词3","关键词4","关键词5"],
  "scenario": "1-2句话：描述什么情况下用户会发这类消息",
  "steps": "1. 第一步\\n2. 第二步\\n3. 第三步（处理这类问题的标准步骤）",
  "principles": "处理此类问题的核心原则（1-2句话）",
  "example_reply_zh": "客服标准回复示例（100字以内，语气友好专业，可加适当emoji）",
  "forbidden": "绝对不能说或不能做的事情（1-2条）"
}}"""

        try:
            async with _httpx.AsyncClient(timeout=40) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [
                              {"role": "system", "content": sys_prompt},
                              {"role": "user",   "content": user_prompt},
                          ],
                          "max_tokens": 800, "temperature": 0.7,
                          "response_format": {"type": "json_object"}},
                )
            result = resp.json()
            raw    = result["choices"][0]["message"]["content"]
        except Exception as _e:
            return {"ok": False, "error": f"AI 调用失败: {_e}"}

        # 解析 JSON（尝试直接解析，失败则提取代码块中的 JSON）
        entry = {}
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            m = _re.search(r'\{[\s\S]+\}', raw)
            if m:
                try:
                    entry = json.loads(m.group())
                except Exception:
                    return {"ok": False, "error": "AI 返回了无法解析的格式", "raw": raw[:500]}

        # 规范化字段
        if "triggers" in entry and isinstance(entry["triggers"], list):
            entry["triggers"] = [str(t) for t in entry["triggers"] if str(t).strip()]
        for field in ("scenario", "steps", "principles", "example_reply_zh", "forbidden"):
            if field not in entry:
                entry[field] = ""

        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_ai_generate", topic)
        return {"ok": True, "entry": entry, "topic": topic, "category": category}

    # ── 知识库 Markdown 全文导出 ──────────────────────────────

    @app.get("/api/kb/export-markdown")
    async def api_kb_export_markdown(request: Request):
        """生成可读的 Markdown 格式知识库文档（按分类组织）"""
        _api_auth(request)
        from fastapi.responses import Response as _Response
        import re as _re

        with _kb_store._conn() as c:
            entries = c.execute(
                "SELECT category, title, triggers, scenario, steps, principles, "
                "example_reply_zh, forbidden, use_count "
                "FROM kb_entries WHERE enabled=1 "
                "ORDER BY category, use_count DESC, title"
            ).fetchall()

        lines = [
            "# 知识库文档",
            f"\n> 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')} · 共 {len(entries)} 条条目\n",
        ]

        current_cat = None
        for row in entries:
            e = dict(row)
            if e["category"] != current_cat:
                current_cat = e["category"]
                lines.append(f"\n## {current_cat}\n")

            # 解析触发词
            try:
                triggers = json.loads(e["triggers"] or "[]")
            except Exception:
                triggers = []
            trigger_str = " / ".join(f"`{t}`" for t in triggers[:6]) if triggers else "—"

            lines.append(f"### {e['title']}")
            lines.append(f"\n**触发词**：{trigger_str}")
            if e.get("scenario"):
                lines.append(f"\n**使用场景**：{e['scenario']}")
            if e.get("steps"):
                lines.append(f"\n**处理步骤**：\n\n{e['steps']}")
            if e.get("principles"):
                lines.append(f"\n**注意原则**：{e['principles']}")
            if e.get("example_reply_zh"):
                lines.append(f"\n**标准回复**：\n\n> {e['example_reply_zh']}")
            if e.get("forbidden"):
                lines.append(f"\n**禁止事项**：{e['forbidden']}")
            lines.append(f"\n_使用次数：{e.get('use_count', 0)}_\n")
            lines.append("---")

        md = "\n".join(lines)
        ts = time.strftime("%Y%m%d_%H%M%S")
        return _Response(
            md.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="kb_{ts}.md"'},
        )

    # ---------- 统计 ----------
    @app.get("/api/kb/stats")
    async def api_kb_stats(request: Request):
        _api_auth(request)
        return _kb_store.stats()

    # ---------- 翻译 ----------
    @app.post("/api/kb/entries/{entry_id}/translate")
    async def api_kb_translate(request: Request, entry_id: str):
        _api_auth(request)
        data = await request.json()
        lang = data.get("lang", "en")
        fields = {k: v for k, v in data.items() if k != "lang"}
        trans_id = _kb_store.upsert_translation(entry_id, lang, fields, auto=False)
        return {"id": trans_id, "ok": True}


    # ═══════════════════════════════════════════════════════════════════
    # 知识库 — 智能体翻译 / 健康度 / 沙盒 AI 回复 / Miss 日志
    # ═══════════════════════════════════════════════════════════════════

    async def _ai_translate_entry(entry: dict, langs: list) -> dict:
        """
        一次 API 调用同时翻译到所有目标语言（批量 4 语言 1 次调用优化）。
        返回 {"en": {...fields...}, "ur": {...}, "pt": {...}, "ar": {...}}
        """
        import httpx as _httpx, re as _re
        ai_cfg = config_manager.config.get("ai", {})
        api_key = ai_cfg.get("api_key", "")
        base_url = (ai_cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
        model = ai_cfg.get("model", "deepseek-chat")
        if not api_key:
            return {}

        fields_to_translate = {
            k: v for k, v in {
                "title":        entry.get("title", ""),
                "scenario":     entry.get("scenario", ""),
                "steps":        entry.get("steps", ""),
                "principles":   entry.get("principles", ""),
                "example_reply": entry.get("example_reply_zh", ""),
                "forbidden":    entry.get("forbidden", ""),
            }.items() if v
        }
        if not fields_to_translate:
            return {}

        _LANG_NAMES = {
            "en": "English",
            "ur": "Urdu (اردو)",
            "pt": "Portuguese (Brazilian)",
            "ar": "Arabic (عربي)",
        }
        target_desc = "; ".join(f'key="{l}" → {_LANG_NAMES.get(l,l)}' for l in langs)
        prompt = (
            "你是客服知识库专业翻译。请将下列中文字段同时翻译成多种语言。\n"
            "要求：保持客服专业语气；金融/支付术语使用目标语言的行业标准用词；"
            "EP/JC/Pay in 等系统专有名词保持原样不翻译。\n"
            "严格只返回纯 JSON，不得有任何额外说明。\n"
            f"目标语言：{target_desc}\n"
            f"JSON 结构：{{{', '.join(repr(l)+': {{...字段...}}' for l in langs)}}}\n"
            "源字段（中文）：\n"
            + json.dumps(fields_to_translate, ensure_ascii=False, indent=2)
        )
        try:
            async with _httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 2500,
                        "temperature": 0.2,
                    },
                )
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                match = _re.search(r"\{[\s\S]*\}", content)
                if match:
                    parsed = json.loads(match.group())
                    return {k: v for k, v in parsed.items() if k in langs}
        except Exception as _e:
            logger.warning("KB 自动翻译失败: %s", _e)
        return {}

    @app.post("/api/kb/entries/{entry_id}/auto-translate")
    async def api_kb_auto_translate(request: Request, entry_id: str):
        _api_auth(request)
        entry = _kb_store.get_entry(entry_id)
        if not entry:
            raise HTTPException(status_code=404)
        data = await request.json()
        langs = data.get("langs", ["en", "ur", "pt", "ar"])
        results = await _ai_translate_entry(entry, langs)
        saved = []
        for lang, fields in results.items():
            if isinstance(fields, dict) and fields:
                _kb_store.upsert_translation(entry_id, lang, fields, auto=True)
                saved.append(lang)
        return {"ok": True, "translated_to": saved, "results": results}

    @app.get("/api/kb/translation-gaps")
    async def api_kb_translation_gaps(request: Request):
        """分析翻译缺口：按使用频率排序，优先翻译高频条目"""
        _api_auth(request)
        target_langs = ["en", "ur", "pt", "ar"]
        entries = _kb_store.list_entries(enabled_only=True)
        gaps = []
        for e in entries:
            full = _kb_store.get_entry(e["id"])
            trans = (full or {}).get("translations", {})
            missing = [l for l in target_langs if l not in trans]
            if missing:
                gaps.append({
                    "entry_id":   e["id"],
                    "title":      e.get("title", ""),
                    "category":   e.get("category", ""),
                    "use_count":  e.get("use_count", 0),
                    "missing":    missing,
                    "has":        [l for l in target_langs if l in trans],
                })
        gaps.sort(key=lambda x: -x["use_count"])
        total = len(entries)
        fully_translated = total - len(gaps)
        return {
            "total_entries":      total,
            "fully_translated":   fully_translated,
            "coverage_pct":       round(fully_translated / total * 100) if total else 0,
            "gaps":               gaps[:20],
            "gap_count":          len(gaps),
        }

    # K2: 自动翻译扫描 — 处理 [TRANSLATE:lang:entry_id] miss_log 条目
    _translate_sweep_lock = asyncio.Lock()

    async def _run_translate_sweep(max_items: int = 5) -> dict:
        """后台扫描并处理翻译请求（去重+限速）"""
        async with _translate_sweep_lock:
            pending = _kb_store.get_pending_translate_requests(limit=max_items)
            if not pending:
                return {"processed": 0, "success": 0, "failed": 0}
            success = 0
            failed = 0
            for req in pending:
                entry = _kb_store.get_entry(req["entry_id"])
                if not entry:
                    _kb_store.delete_miss_entry(req["query"])
                    continue
                try:
                    results = await _ai_translate_entry(entry, [req["lang"]])
                    if results.get(req["lang"]):
                        _kb_store.upsert_translation(
                            req["entry_id"], req["lang"],
                            results[req["lang"]], auto=True
                        )
                        success += 1
                    else:
                        failed += 1
                except Exception as _te:
                    logger.warning("K2 自动翻译失败 entry=%s lang=%s: %s",
                                   req["entry_id"], req["lang"], _te)
                    failed += 1
                _kb_store.delete_miss_entry(req["query"])
            if success:
                logger.info("K2 自动翻译完成: %d 成功, %d 失败", success, failed)
            return {"processed": len(pending), "success": success, "failed": failed}

    @app.post("/api/kb/translate-sweep")
    async def api_kb_translate_sweep(request: Request):
        """手动触发翻译扫描"""
        _api_auth(request)
        result = await _run_translate_sweep(max_items=10)
        return {"ok": True, **result}

    # K2: 后台定时翻译扫描（每 10 分钟执行一次）
    async def _translate_sweep_loop():
        await asyncio.sleep(60)  # 启动后等 60 秒再开始
        while True:
            try:
                await _run_translate_sweep(max_items=3)
            except Exception as _e:
                logger.debug("K2 翻译扫描循环异常: %s", _e)
            await asyncio.sleep(600)  # 每 10 分钟

    # J2: 知识库自动演化 — 从 top_misses 自动创建草稿条目
    _kb_evolve_lock = asyncio.Lock()

    async def _run_kb_evolve(max_items: int = 3) -> dict:
        """扫描高频 miss，创建禁用状态的草稿条目 + AI 自动填充"""
        async with _kb_evolve_lock:
            misses = _kb_store.get_miss_stats(top_k=20)
            # 过滤掉翻译请求和低频 miss
            candidates = [
                m for m in misses
                if not m["query"].startswith("[TRANSLATE:")
                and m["cnt"] >= 3
            ][:max_items]
            if not candidates:
                return {"processed": 0, "created": 0}
            created = 0
            for m in candidates:
                query = m["query"]
                # 检查是否已有相似条目（避免重复创建）
                existing = _kb_store.search(query, top_k=1)
                if existing.get("entries") and existing["entries"][0].get("score", 0) > 0.5:
                    continue
                cat = _kb_store._guess_category(query) if hasattr(_kb_store, "_guess_category") else "其他"
                title = query[:50]
                entry_id = _kb_store.add_entry({
                    "category": cat,
                    "title": title,
                    "triggers": [query],
                    "scenario": f"用户高频询问: {query}",
                    "steps": "",
                    "principles": "",
                    "example_reply_zh": "",
                    "enabled": 0,  # 草稿状态，需运营审核
                })
                _kb_store.delete_miss_entry(query)
                # L1: 触发 AI 自动填充
                try:
                    asyncio.create_task(
                        _auto_fill_entry(entry_id, title, cat, source_query=query))
                except Exception:
                    pass
                created += 1
                logger.info("J2 自动创建草稿条目: query='%s' entry=%s", query[:50], entry_id)
            return {"processed": len(candidates), "created": created}

    @app.post("/api/kb/evolve-sweep")
    async def api_kb_evolve_sweep(request: Request):
        """手动触发知识库自动演化"""
        _api_auth(request)
        result = await _run_kb_evolve(max_items=10)
        return {"ok": True, **result}

    # J2: 后台定时知识库演化（每 6 小时执行一次）
    async def _kb_evolve_loop():
        await asyncio.sleep(300)  # 启动后等 5 分钟
        while True:
            try:
                await _run_kb_evolve(max_items=5)
            except Exception as _e:
                logger.debug("J2 知识库演化循环异常: %s", _e)
            await asyncio.sleep(21600)  # 每 6 小时

    # H2: 知识库自愈 API
    @app.post("/api/kb/self-heal")
    async def api_kb_self_heal(request: Request):
        """手动触发知识库自愈巡检"""
        _api_auth(request)
        result = _kb_store.run_self_heal(stale_days=14)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_self_heal", "",
                            "", f"expanded={result['triggers_expanded']} "
                                f"archived={result['entries_archived']}")
        return {"ok": True, **result}

    # H2: 后台定时自愈（每 12 小时执行一次）
    async def _kb_self_heal_loop():
        await asyncio.sleep(600)  # 启动后等 10 分钟
        while True:
            try:
                result = _kb_store.run_self_heal(stale_days=14)
                if result["triggers_expanded"] or result["entries_archived"]:
                    logger.info("H2 自愈完成: expanded=%d archived=%d overloaded=%d",
                                result["triggers_expanded"],
                                result["entries_archived"],
                                result["overloaded_flagged"])
            except Exception as _e:
                logger.debug("H2 自愈循环异常: %s", _e)
            await asyncio.sleep(43200)  # 每 12 小时

    @app.on_event("startup")
    async def _start_background_tasks():
        asyncio.create_task(_translate_sweep_loop())
        asyncio.create_task(_kb_evolve_loop())
        asyncio.create_task(_kb_self_heal_loop())
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

    @app.post("/api/kb/translate-all")
    async def api_kb_translate_all(request: Request):
        _api_auth(request)
        data = await request.json()
        langs = data.get("langs", ["en", "ur", "pt", "ar"])
        force = data.get("force", False)
        entries = _kb_store.list_entries(enabled_only=True)
        summary = {"total": len(entries), "translated": 0, "skipped": 0, "failed": 0}
        for entry in entries:
            if not force:
                full = _kb_store.get_entry(entry["id"])
                existing = set((full or {}).get("translations", {}).keys())
                target_langs = [l for l in langs if l not in existing]
                if not target_langs:
                    summary["skipped"] += 1
                    continue
            else:
                target_langs = langs
            try:
                trans = await _ai_translate_entry(entry, target_langs)
                for lang, fields in trans.items():
                    if isinstance(fields, dict) and fields:
                        _kb_store.upsert_translation(entry["id"], lang, fields, auto=True)
                summary["translated"] += 1
            except Exception:
                summary["failed"] += 1
        return summary

    @app.get("/api/kb/translate-progress")
    async def api_kb_translate_progress(request: Request, force: int = 0):
        """SSE 流式批量翻译进度（GET，通过 session cookie 鉴权）"""
        _api_auth(request)
        langs = ["en", "ur", "pt", "ar"]
        entries = _kb_store.list_entries(enabled_only=True)
        total = len(entries)

        async def _stream():
            yield f"data: {json.dumps({'type':'start','total':total})}\n\n"
            translated = skipped = failed = 0
            for i, entry in enumerate(entries):
                title_short = (entry.get("title") or "")[:24]
                if not force:
                    full = _kb_store.get_entry(entry["id"])
                    existing = set((full or {}).get("translations", {}).keys())
                    target_langs = [l for l in langs if l not in existing]
                    if not target_langs:
                        skipped += 1
                        yield f"data: {json.dumps({'type':'progress','i':i+1,'total':total,'translated':translated,'skipped':skipped,'failed':failed,'title':title_short,'action':'skip'})}\n\n"
                        continue
                else:
                    target_langs = langs
                try:
                    trans = await _ai_translate_entry(entry, target_langs)
                    for lang, fields in trans.items():
                        if isinstance(fields, dict) and fields:
                            _kb_store.upsert_translation(entry["id"], lang, fields, auto=True)
                    translated += 1
                    yield f"data: {json.dumps({'type':'progress','i':i+1,'total':total,'translated':translated,'skipped':skipped,'failed':failed,'title':title_short,'action':'done'})}\n\n"
                except Exception as _te:
                    failed += 1
                    yield f"data: {json.dumps({'type':'progress','i':i+1,'total':total,'translated':translated,'skipped':skipped,'failed':failed,'title':title_short,'action':'fail','error':str(_te)[:80]})}\n\n"
            yield f"data: {json.dumps({'type':'done','total':total,'translated':translated,'skipped':skipped,'failed':failed})}\n\n"

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )

    # ---------- 沙盒：智能体真实回复模拟 ----------
    @app.post("/api/kb/sandbox/ai-reply")
    async def api_kb_sandbox_ai_reply(request: Request):
        _api_auth(request)
        import httpx as _httpx
        data = await request.json()
        query = data.get("query", "")
        kb_context = data.get("kb_context", "")
        lang = data.get("lang", "zh")
        ai_cfg = config_manager.config.get("ai", {})
        api_key = ai_cfg.get("api_key", "")
        base_url = (ai_cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
        model = ai_cfg.get("model", "deepseek-chat")
        if not api_key:
            return {"reply": "AI 未配置，无法模拟回复", "ok": False}
        sys_prompt = (
            ai_cfg.get("system_prompt")
            or config_manager.config.get("system_prompt", "")
            or "你是一位专业的 AI 助手，回复简洁准确。"
        )
        messages = [{"role": "system", "content": sys_prompt}]
        if kb_context:
            messages.append({"role": "system",
                              "content": f"【知识库参考材料（请优先参考）】:\n{kb_context}"})
        messages.append({"role": "user", "content": query})
        try:
            async with _httpx.AsyncClient(timeout=35) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model, "messages": messages,
                          "max_tokens": 500, "temperature": 0.7},
                )
                result = resp.json()
                reply = result["choices"][0]["message"]["content"]
                return {"reply": reply.strip(), "ok": True}
        except Exception as _e:
            return {"reply": f"AI 调用失败: {_e}", "ok": False}

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
    @app.get("/api/cases/active")
    async def api_cases_active(request: Request):
        """返回所有有 _case_id 的活跃用户 case"""
        _api_auth(request)
        ctx_store, sm = _copilot_get_ctx_store()
        if not ctx_store:
            return {"cases": [], "count": 0}

        cases = []
        for uid, ctx in ctx_store._cache.items():
            case_id = ctx.get("_case_id")
            if not case_id:
                continue
            chain = ctx.get("_intent_chain", [])
            pattern = ctx.get("_chain_pattern", {})
            profile = ctx.get("_user_profile", {})
            cases.append({
                "case_id": case_id,
                "user_id": uid,
                "chat_id": ctx.get("chat_id", ""),
                "chat_title": ctx.get("chat_title", ""),
                "intent_chain": chain[-8:],
                "pattern": pattern.get("pattern", "") if isinstance(pattern, dict) else "",
                "pattern_desc": pattern.get("desc", "") if isinstance(pattern, dict) else "",
                "satisfaction": profile.get("satisfaction", 80) if isinstance(profile, dict) else 80,
                "at_risk": profile.get("at_risk", False) if isinstance(profile, dict) else False,
                "consecutive_same": ctx.get("_consecutive_same_intent", 0),
                "last_message": (ctx.get("last_message") or "")[:100],
                "last_reply": (ctx.get("last_reply") or "")[:100],
                "last_active": ctx.get("last_reply_time", 0),
                "escalation": bool(ctx.get("_escalation_ts")),
                "closed": ctx.get("_case_closed", False),
                "note": ctx.get("_case_note", ""),
            })
        cases.sort(key=lambda x: (x["closed"], not x["at_risk"], -x["consecutive_same"]))
        return {"cases": cases[:100], "count": len(cases)}

    @app.post("/api/cases/{case_id}/note")
    async def api_case_note(request: Request, case_id: str):
        """运营人员为 case 添加备注"""
        _api_auth(request)
        data = await request.json()
        note = (data.get("note") or "").strip()
        ctx_store, _ = _copilot_get_ctx_store()
        if not ctx_store:
            raise HTTPException(404, "上下文存储不可用")
        for uid, ctx in ctx_store._cache.items():
            if ctx.get("_case_id") == case_id:
                ctx["_case_note"] = note[:500]
                actor = request.session.get("username", "web_admin")
                if audit_store:
                    audit_store.log(actor, "case_note", case_id, uid, note[:80])
                return {"ok": True, "case_id": case_id}
        raise HTTPException(404, f"Case {case_id} 不存在")

    @app.post("/api/cases/{case_id}/close")
    async def api_case_close(request: Request, case_id: str):
        """运营人员结案"""
        _api_auth(request)
        data = await request.json()
        resolution = (data.get("resolution") or "").strip()
        ctx_store, _ = _copilot_get_ctx_store()
        if not ctx_store:
            raise HTTPException(404, "上下文存储不可用")
        for uid, ctx in ctx_store._cache.items():
            if ctx.get("_case_id") == case_id:
                ctx["_case_closed"] = True
                ctx["_case_resolution"] = resolution[:500]
                ctx["_case_closed_at"] = time.time()
                actor = request.session.get("username", "web_admin")
                if audit_store:
                    audit_store.log(actor, "case_close", case_id, uid, resolution[:80])
                return {"ok": True, "case_id": case_id}
        raise HTTPException(404, f"Case {case_id} 不存在")

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
    @app.get("/api/kb/health-stats")
    async def api_kb_health_stats(request: Request):
        _api_auth(request)
        stats = _kb_store.stats()
        with _kb_store._conn() as c:
            top_used = c.execute(
                "SELECT title, category, use_count FROM kb_entries "
                "WHERE use_count>0 ORDER BY use_count DESC LIMIT 5"
            ).fetchall()
            never_used = c.execute(
                "SELECT COUNT(*) FROM kb_entries WHERE use_count=0 AND enabled=1"
            ).fetchone()[0]
            recent_fb = c.execute(
                "SELECT COUNT(*) FROM kb_feedback "
                "WHERE created_at > datetime('now','-7 days')"
            ).fetchone()[0]
            recent_good = c.execute(
                "SELECT COUNT(*) FROM kb_feedback WHERE score=1 "
                "AND created_at > datetime('now','-7 days')"
            ).fetchone()[0]
            trans_coverage: dict = {}
            for lang in ["en", "ur", "pt", "ar"]:
                cnt = c.execute(
                    "SELECT COUNT(DISTINCT entry_id) FROM kb_translations WHERE lang=?",
                    (lang,)
                ).fetchone()[0]
                total = stats["total_entries"] or 1
                trans_coverage[lang] = round(cnt / total * 100, 1)
            miss_rows = c.execute(
                "SELECT query, cnt FROM kb_miss_log ORDER BY cnt DESC LIMIT 8"
            ).fetchall() if _miss_table_exists(c) else []
        return {
            "stats": stats,
            "top_used": [dict(r) for r in top_used],
            "never_used": never_used,
            "recent_feedback_7d": recent_fb,
            "recent_good_7d": recent_good,
            "recent_satisfaction": round(recent_good / recent_fb * 100, 1) if recent_fb else 0,
            "translation_coverage": trans_coverage,
            "miss_queries": [dict(r) for r in miss_rows],
        }

    def _miss_table_exists(conn) -> bool:
        try:
            conn.execute("SELECT 1 FROM kb_miss_log LIMIT 1")
            return True
        except Exception:
            return False

    # ---------- Miss 日志写入（供 bot 内部调用） ----------
    @app.post("/api/kb/miss-log")
    async def api_kb_miss_log(request: Request):
        data = await request.json()
        query = (data.get("query") or "").strip()[:200]
        if not query:
            return {"ok": False}
        _kb_store.log_miss(query)
        return {"ok": True}

    # ═══════════════════════════════════════════════════════════════════
    # Phase 4: 翻译审核 / Miss→Entry / Stale 检测 / 使用统计
    # ═══════════════════════════════════════════════════════════════════

    # ---------- 翻译审核 ----------
    @app.get("/api/kb/translations/pending")
    async def api_kb_trans_pending(request: Request, limit: int = 100):
        _api_auth(request)
        return {"pending": _kb_store.get_pending_translations(limit=limit)}

    @app.post("/api/kb/translations/{trans_id}/confirm")
    async def api_kb_trans_confirm(request: Request, trans_id: str):
        _api_auth(request)
        ok = _kb_store.confirm_translation(trans_id)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_confirm_translation", trans_id)
        return {"ok": ok}

    @app.post("/api/kb/translations/{trans_id}/retranslate")
    async def api_kb_trans_retranslate(request: Request, trans_id: str):
        _api_auth(request)
        with _kb_store._conn() as c:
            row = c.execute(
                "SELECT entry_id, lang FROM kb_translations WHERE id=?", (trans_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404)
        entry = _kb_store.get_entry(row["entry_id"])
        if not entry:
            raise HTTPException(status_code=404)
        results = await _ai_translate_entry(entry, [row["lang"]])
        if results.get(row["lang"]):
            _kb_store.upsert_translation(row["entry_id"], row["lang"],
                                         results[row["lang"]], auto=True)
            return {"ok": True, "result": results[row["lang"]]}
        return {"ok": False, "msg": "翻译API无返回"}

    @app.put("/api/kb/translations/{trans_id}")
    async def api_kb_trans_update(request: Request, trans_id: str):
        """手动修改翻译内容并标记为已审核"""
        _api_auth(request)
        data = await request.json()
        allowed = ("title", "scenario", "steps", "principles", "example_reply", "forbidden")
        sets = ", ".join(f"{k}=?" for k in allowed if k in data)
        vals = [data[k] for k in allowed if k in data]
        if not sets:
            return {"ok": False}
        import time as _time
        now = _time.strftime("%Y-%m-%dT%H:%M:%S")
        vals += [now, trans_id]
        with _kb_store._conn() as c:
            c.execute(
                f"UPDATE kb_translations SET {sets}, auto_translated=0, updated_at=? WHERE id=?",
                vals
            )
        return {"ok": True}

    # ---------- Miss Log → 创建 KB 条目 ----------
    @app.post("/api/kb/miss-to-entry")
    async def api_kb_miss_to_entry(request: Request):
        _api_auth(request)
        data = await request.json()
        query = (data.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query 不能为空")
        _title = data.get("title", query[:50])
        _cat = data.get("category", "其他")
        entry_id = _kb_store.add_entry({
            "category":         _cat,
            "title":            _title,
            "triggers":         [query],
            "scenario":         f"用户发送了: {query}",
            "steps":            data.get("steps", ""),
            "principles":       data.get("principles", ""),
            "example_reply_zh": data.get("example_reply_zh", ""),
        })
        _kb_store.delete_miss_entry(query)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_miss_to_entry", entry_id)
        # L1: 后台自动用 AI 填充条目内容
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop().create_task(
                _auto_fill_entry(entry_id, _title, _cat, source_query=query))
        except Exception:
            pass
        return {"id": entry_id, "ok": True}

    @app.delete("/api/kb/miss-log")
    async def api_kb_delete_miss(request: Request):
        _api_auth(request)
        data = await request.json()
        query = (data.get("query") or "").strip()
        if query:
            _kb_store.delete_miss_entry(query)
        return {"ok": True}

    # ---------- 过期/未用条目检测 ----------
    @app.get("/api/kb/stale")
    async def api_kb_stale(request: Request, days: int = 7):
        _api_auth(request)
        return {"stale": _kb_store.get_stale_entries(days=days), "days": days}

    @app.post("/api/kb/entries/bulk-disable")
    async def api_kb_bulk_disable(request: Request):
        _api_auth(request)
        data = await request.json()
        ids = data.get("ids", [])
        count = _kb_store.bulk_disable(ids)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_bulk_disable", f"{count} entries")
        return {"ok": True, "count": count}

    @app.post("/api/kb/entries/batch-update")
    async def api_kb_batch_update(request: Request):
        """
        批量更新条目属性。
        Body: {ids: [...], enabled: 0|1, category: "..."}
        只更新传入的字段，ids 为必填。
        """
        _api_auth(request)
        data   = await request.json()
        ids    = data.get("ids", [])
        if not ids:
            raise HTTPException(status_code=400, detail="ids 不能为空")
        updates: dict = {}
        if "enabled" in data:
            updates["enabled"] = int(bool(data["enabled"]))
        if "category" in data and data["category"]:
            updates["category"] = str(data["category"])
        if not updates:
            raise HTTPException(status_code=400, detail="未提供可更新的字段")
        count = 0
        with _kb_store._conn() as c:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            for eid in ids:
                c.execute(
                    f"UPDATE kb_entries SET {set_clause} WHERE id=?",
                    list(updates.values()) + [eid],
                )
                count += 1
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_batch_update",
                            f"ids={len(ids)},fields={list(updates.keys())}")
        return {"ok": True, "count": count}

    # ---------- 使用率排行 ----------
    @app.get("/api/kb/usage-ranking")
    async def api_kb_usage_ranking(request: Request, limit: int = 20):
        _api_auth(request)
        with _kb_store._conn() as c:
            top = c.execute(
                "SELECT id, title, category, use_count, enabled, rating "
                "FROM kb_entries ORDER BY use_count DESC LIMIT ?",
                (limit,)
            ).fetchall()
            zero = c.execute(
                "SELECT COUNT(*) FROM kb_entries WHERE use_count=0 AND enabled=1"
            ).fetchone()[0]
        return {"ranking": [dict(r) for r in top], "never_used": zero}

    # ═══════════════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════
    # Phase 8: 导出 / 导入 / 维护建议
    # ═══════════════════════════════════════════════════════════════════

    from fastapi.responses import Response as _Response

    @app.get("/api/kb/export")
    async def api_kb_export(request: Request, fmt: str = "json"):
        """导出启用的知识库为 JSON（fmt=json）或 YAML（fmt=yaml，需 PyYAML）"""
        _api_auth(request)
        data = _kb_store.export_all()
        ts = time.strftime("%Y%m%d_%H%M%S")
        if fmt == "yaml":
            try:
                import yaml as _yaml
                content = _yaml.dump(data, allow_unicode=True,
                                     default_flow_style=False, sort_keys=False)
                return _Response(
                    content, media_type="text/yaml",
                    headers={"Content-Disposition":
                             f'attachment; filename="kb_export_{ts}.yaml"'},
                )
            except ImportError:
                pass  # 降级为 JSON
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return _Response(
            content, media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="kb_export_{ts}.json"'},
        )

    @app.post("/api/kb/import")
    async def api_kb_import(request: Request):
        """
        批量导入知识库。
        Body: {data: <export dict>, mode: "skip"|"update"}
        """
        _api_auth(request)
        body = await request.json()
        data = body.get("data") or body   # 支持直接发 export dict 或包装格式
        mode = body.get("mode", "skip")
        # 安全检查：必须含 entries/error_codes/rules 键之一
        if not any(k in data for k in ("entries", "error_codes", "rules", "version")):
            raise HTTPException(status_code=400, detail="无效的导入格式")
        result = _kb_store.import_from_data(data, mode=mode)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_import",
                            f"added={result['added']},updated={result['updated']}")
        return result

    @app.get("/api/kb/export-csv")
    async def api_kb_export_csv(request: Request):
        """导出知识条目为 CSV（含 BOM，Excel 可直接打开）"""
        _api_auth(request)
        ts = time.strftime("%Y%m%d_%H%M%S")
        content = _kb_store.export_csv()
        return _Response(
            content.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="kb_entries_{ts}.csv"'},
        )

    @app.post("/api/kb/import-csv")
    async def api_kb_import_csv(request: Request):
        """
        从 CSV 文本导入知识条目。
        Body: {csv: "<csv text>", mode: "skip"|"update"}
        """
        _api_auth(request)
        body = await request.json()
        csv_text = body.get("csv", "")
        mode     = body.get("mode", "skip")
        if not csv_text:
            raise HTTPException(status_code=400, detail="CSV 内容为空")
        result = _kb_store.import_from_csv(csv_text, mode=mode)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_import_csv",
                            f"added={result['added']},updated={result['updated']}")
        return result

    # ── 知识条目图片附件 ──────────────────────────────────────

    @app.post("/api/kb/entries/{entry_id}/images")
    async def api_kb_upload_image(entry_id: str, request: Request,
                                  file: UploadFile = File(...),
                                  caption: str = Form("")):
        """上传图片并关联到指定知识条目（JPEG/PNG/GIF/WEBP，≤5 MB）"""
        _api_auth(request)
        allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        if file.content_type not in allowed_types:
            raise HTTPException(400, "仅支持 JPEG/PNG/GIF/WEBP 图片")
        data = await file.read()
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(400, "图片大小不能超过 5 MB")
        # 确保存储目录存在
        from pathlib import Path as _P
        img_dir = _P(config_manager.config_path).parent / "kb_images"
        img_dir.mkdir(exist_ok=True)
        # 生成唯一文件名（保留原始扩展名）
        import uuid as _uuid
        ext = (file.filename or "img.jpg").rsplit(".", 1)[-1].lower()
        filename = f"{_uuid.uuid4().hex[:16]}.{ext}"
        (img_dir / filename).write_bytes(data)
        img_id = _kb_store.add_entry_image(entry_id, filename, caption, len(data))
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_upload_image", entry_id, "", filename)
        return {"ok": True, "id": img_id, "filename": filename, "url": f"/kb-images/{filename}"}

    @app.get("/api/kb/entries/{entry_id}/images")
    async def api_kb_get_images(entry_id: str, request: Request):
        """获取条目的图片列表"""
        _api_auth(request)
        imgs = _kb_store.get_entry_images(entry_id)
        for img in imgs:
            img["url"] = f"/kb-images/{img['filename']}"
        return {"images": imgs}

    @app.delete("/api/kb/images/{img_id}")
    async def api_kb_delete_image(img_id: str, request: Request):
        """删除图片记录及物理文件"""
        _api_auth(request)
        filename = _kb_store.delete_entry_image(img_id)
        if not filename:
            raise HTTPException(404, "图片不存在")
        from pathlib import Path as _P
        img_file = _P(config_manager.config_path).parent / "kb_images" / filename
        try:
            img_file.unlink(missing_ok=True)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_delete_image", img_id, "", filename)
        return {"ok": True}

    # ── 种子数据导入 ──────────────────────────────────────────

    @app.post("/api/kb/seed")
    async def api_kb_seed(request: Request):
        """导入内置示例知识条目（电商/SaaS场景）"""
        _api_auth(request)
        body = await request.json()
        category = body.get("category", "all")
        from src.utils.kb_store import seed_kb_examples
        result = seed_kb_examples(_kb_store, category=category)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_seed", category,
                            "", f"added={result['added']}")
        if result["added"] > 0:
            _kb_store._rebuild_index()
        return result

    @app.get("/api/kb/maintenance-advice")
    async def api_kb_maintenance_advice(request: Request):
        """返回知识库健康诊断报告（健康分 + 可操作建议列表）"""
        _api_auth(request)
        return _kb_store.get_maintenance_advice()

    # K4: 运营日报自动生成
    # ═══════════════════════════════════════════════════════════════════

    @app.get("/api/report/daily")
    async def api_daily_report(request: Request, hours: int = 24):
        """
        K4: 生成运营日报 — 聚合所有核心指标 + 智能异常识别 + 趋势对比。
        返回结构化 JSON + 人类可读的 text 摘要。
        """
        _api_auth(request)
        report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "hours": hours}
        alerts = []

        # 1. KB 命中率
        try:
            qa = _kb_store.get_query_analytics(hours=hours)
            hit_pct = qa.get("totals", {}).get("hit_pct", 0)
            total_queries = qa.get("totals", {}).get("total", 0)
            weak_pct = qa.get("totals", {}).get("weak_pct", 0)
            report["kb"] = {
                "total_queries": total_queries,
                "hit_pct": hit_pct,
                "weak_pct": weak_pct,
                "avg_score": qa.get("totals", {}).get("avg_score", 0),
            }
            if hit_pct < 70:
                alerts.append(f"知识库命中率仅 {hit_pct}%，建议补充知识条目")
            if weak_pct > 30:
                alerts.append(f"弱命中占比 {weak_pct}%，建议优化触发词或拆分条目")
        except Exception:
            report["kb"] = {}

        # 2. 回复质量
        try:
            rq = _kb_store.get_reply_quality_stats(days=1)
            sat_rate = rq.get("satisfaction_rate", 0)
            report["quality"] = {
                "satisfaction_rate": sat_rate,
                "positive": rq.get("positive", 0),
                "negative": rq.get("negative", 0),
            }
            if sat_rate < 60:
                alerts.append(f"回复满意度仅 {sat_rate}%，需要优化回复策略")
        except Exception:
            report["quality"] = {}

        # 3. 策略效果
        tracker = _get_strategy_tracker()
        if tracker:
            try:
                tracker.mark_no_follow_up()
                summary = tracker.strategy_summary(hours)
                total_msgs = tracker.total_events(hours)
                from src.utils.strategy_advisor import analyze
                rs = config_manager.get_strategies_config()
                advisor = analyze(summary, rs.get("strategies", {}))
                report["strategy"] = {
                    "total_messages": total_msgs,
                    "best_strategy": advisor.get("best"),
                    "worst_strategy": advisor.get("worst"),
                    "scores": advisor.get("scores", {}),
                }
                worst = advisor.get("worst")
                if worst and advisor["scores"].get(worst, 100) < 40:
                    alerts.append(f"策略 {worst} 质量评分低于 40，建议调整")
            except Exception:
                report["strategy"] = {}

        # 4. A/B 测试状态
        try:
            rs = config_manager.get_strategies_config()
            ab_tests = rs.get("ab_tests", {})
            active = sum(1 for ab in ab_tests.values()
                         if isinstance(ab, dict) and ab.get("enabled"))
            concluded = sum(1 for ab in ab_tests.values()
                           if isinstance(ab, dict) and ab.get("concluded"))
            report["ab_tests"] = {
                "active": active, "concluded": concluded, "total": len(ab_tests),
            }
        except Exception:
            report["ab_tests"] = {}

        # 5. 翻译覆盖
        try:
            entries = _kb_store.list_entries(enabled_only=True)
            total_entries = len(entries)
            target_langs = ["en", "ur", "pt", "ar"]
            gaps = 0
            for e in entries:
                full = _kb_store.get_entry(e["id"])
                trans = (full or {}).get("translations", {})
                if any(l not in trans for l in target_langs):
                    gaps += 1
            coverage = round((total_entries - gaps) / max(total_entries, 1) * 100)
            report["translation"] = {
                "total_entries": total_entries,
                "coverage_pct": coverage,
                "gap_count": gaps,
            }
            if coverage < 50:
                alerts.append(f"翻译覆盖率仅 {coverage}%，影响多语言用户体验")
        except Exception:
            report["translation"] = {}

        # 6. at_risk 用户
        try:
            ctx_store = None
            if telegram_client:
                sm = getattr(telegram_client, "skill_manager", None)
                if sm:
                    ctx_store = getattr(sm, "_context_store", None)
            if ctx_store:
                risk_count = sum(
                    1 for ctx in ctx_store._cache.values()
                    if isinstance(ctx.get("_user_profile"), dict)
                    and ctx["_user_profile"].get("at_risk")
                )
                report["user_risk"] = {"at_risk_count": risk_count}
                if risk_count > 5:
                    alerts.append(f"{risk_count} 个用户满意度极低，可能流失")
        except Exception:
            report["user_risk"] = {}

        # 7. 未命中热词
        try:
            misses = _kb_store.get_miss_stats(top_k=5)
            report["top_misses"] = [
                {"query": m["query"][:50], "count": m["cnt"]}
                for m in misses if not m["query"].startswith("[TRANSLATE:")
            ]
        except Exception:
            report["top_misses"] = []

        report["alerts"] = alerts

        # 生成人类可读摘要
        lines = [f"📊 运营日报（过去 {hours} 小时）", ""]
        kb = report.get("kb", {})
        if kb:
            lines.append(f"知识库: 查询 {kb.get('total_queries', 0)} 次, "
                         f"命中率 {kb.get('hit_pct', 0)}%, "
                         f"平均分 {kb.get('avg_score', 0):.2f}")
        quality = report.get("quality", {})
        if quality:
            lines.append(f"回复质量: 满意度 {quality.get('satisfaction_rate', 0)}% "
                         f"(+{quality.get('positive', 0)}/-{quality.get('negative', 0)})")
        strat = report.get("strategy", {})
        if strat:
            lines.append(f"策略: 共 {strat.get('total_messages', 0)} 条消息, "
                         f"最优={strat.get('best_strategy', '-')}")
        trans = report.get("translation", {})
        if trans:
            lines.append(f"翻译: 覆盖率 {trans.get('coverage_pct', 0)}% "
                         f"({trans.get('gap_count', 0)} 条待翻译)")
        risk = report.get("user_risk", {})
        if risk:
            lines.append(f"用户: {risk.get('at_risk_count', 0)} 人处于流失风险")
        if alerts:
            lines.append("")
            lines.append("⚠ 告警:")
            for a in alerts:
                lines.append(f"  • {a}")

        report["text_summary"] = "\n".join(lines)
        return report

    # F4: 运营周报
    @app.get("/api/report/weekly")
    async def api_weekly_report(request: Request):
        """F4: 7 天聚合周报 — 复用日报逻辑 + 环比趋势"""
        _api_auth(request)
        report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "type": "weekly"}

        # 本周数据（168h）
        try:
            with _kb_store._conn() as c:
                now_ts = time.time()
                this_week = now_ts - 168 * 3600
                last_week = this_week - 168 * 3600
                tw_total = c.execute(
                    "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ?", (this_week,)
                ).fetchone()[0]
                tw_hits = c.execute(
                    "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND hit=1", (this_week,)
                ).fetchone()[0]
                lw_total = c.execute(
                    "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND ts < ?",
                    (last_week, this_week)
                ).fetchone()[0]
                lw_hits = c.execute(
                    "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND ts < ? AND hit=1",
                    (last_week, this_week)
                ).fetchone()[0]
            tw_rate = round(tw_hits / max(tw_total, 1) * 100, 1)
            lw_rate = round(lw_hits / max(lw_total, 1) * 100, 1)
            report["kb"] = {
                "this_week": {"queries": tw_total, "hits": tw_hits, "hit_rate": tw_rate},
                "last_week": {"queries": lw_total, "hits": lw_hits, "hit_rate": lw_rate},
                "trend": round(tw_rate - lw_rate, 1),
            }
        except Exception:
            report["kb"] = {}

        # 反馈趋势
        try:
            with _kb_store._conn() as c:
                tw_pos = c.execute(
                    "SELECT COUNT(*) FROM kb_feedback WHERE score > 0 "
                    "AND created_at >= datetime(?, 'unixepoch')", (this_week,)
                ).fetchone()[0]
                tw_neg = c.execute(
                    "SELECT COUNT(*) FROM kb_feedback WHERE score < 0 "
                    "AND created_at >= datetime(?, 'unixepoch')", (this_week,)
                ).fetchone()[0]
            report["feedback"] = {
                "positive": tw_pos, "negative": tw_neg,
                "satisfaction": round(tw_pos / max(tw_pos + tw_neg, 1) * 100, 1),
            }
        except Exception:
            report["feedback"] = {}

        # 生成可读摘要
        lines = ["📊 运营周报", ""]
        kb = report.get("kb", {})
        if kb:
            tw = kb.get("this_week", {})
            lines.append(f"本周 KB: {tw.get('queries', 0)} 次查询, 命中率 {tw.get('hit_rate', 0)}%")
            trend = kb.get("trend", 0)
            lines.append(f"环比: {'📈 +' if trend > 0 else '📉 '}{trend}%")
        fb = report.get("feedback", {})
        if fb:
            lines.append(f"反馈: 好评 {fb.get('positive', 0)}, 差评 {fb.get('negative', 0)}, "
                         f"满意度 {fb.get('satisfaction', 0)}%")
        report["text_summary"] = "\n".join(lines)
        return report

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

    @app.get("/api/kb/auto-suggestions")
    async def api_kb_auto_suggestions(request: Request):
        """综合 miss + 弱命中 + 过载条目，返回自动建议列表"""
        _api_auth(request)
        return {
            "suggestions": _kb_store.get_auto_suggestions(),
            "weak_hits":   _kb_store.get_weak_hits(top_k=10),
            "overloaded":  _kb_store.get_overloaded_entries(),
        }

    @app.post("/api/kb/accept-suggestion")
    async def api_kb_accept_suggestion(request: Request):
        """一键采纳建议 → 创建新 KB 条目 + 后台 AI 自动填充"""
        _api_auth(request)
        data = await request.json()
        _title = data.get("title", "")
        _cat = data.get("category", "其他")
        entry_id = _kb_store.add_entry({
            "category":         _cat,
            "title":            _title,
            "triggers":         data.get("triggers", []),
            "scenario":         data.get("scenario", ""),
            "steps":            data.get("steps", ""),
            "principles":       data.get("principles", ""),
            "example_reply_zh": data.get("example_reply_zh", ""),
        })
        query = (data.get("source_query") or "").strip()
        if query:
            _kb_store.delete_miss_entry(query)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_accept_suggestion", entry_id)
        # L1: 后台自动用 AI 填充条目内容
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop().create_task(
                _auto_fill_entry(entry_id, _title, _cat, source_query=query))
        except Exception:
            pass
        return {"id": entry_id, "ok": True}

    @app.get("/api/kb/reply-quality")
    async def api_kb_reply_quality(request: Request, days: int = 7):
        """回复质量统计：满意度、负面信号趋势、重复提问频率"""
        _api_auth(request)
        return _kb_store.get_reply_quality_stats(days=min(days, 30))

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

    @app.get("/api/kb/query-analytics")
    async def api_kb_query_analytics(request: Request, hours: int = 24):
        """返回过去 N 小时的 KB 命中率统计（每小时分桶）"""
        _api_auth(request)
        return _kb_store.get_query_analytics(hours=min(hours, 168))  # 最多7天

    @app.get("/api/kb/today-hit-rate")
    async def api_kb_today_hit_rate(request: Request):
        """今日命中率摘要（供 dashboard 快速展示）"""
        _api_auth(request)
        return _kb_store.get_today_hit_rate()

    @app.get("/api/kb/embed-stats")
    async def api_kb_embed_stats(request: Request):
        """读取 skill_manager 模块级 Embedding API / 缓存命中统计"""
        _api_auth(request)
        try:
            from src.skills.skill_manager import _EMBED_STATS, _EMBED_CACHE, _EMBED_CACHE_MAX
            import time as _time
            uptime_s = int(_time.time() - _EMBED_STATS.get("session_start", _time.time()))
            kb_q = _EMBED_STATS.get("kb_queries", 0)
            kb_h = _EMBED_STATS.get("kb_hits", 0)
            api  = _EMBED_STATS.get("api_calls", 0)
            chit = _EMBED_STATS.get("cache_hits", 0)
            return {
                **_EMBED_STATS,
                "cache_size":    len(_EMBED_CACHE),
                "cache_max":     _EMBED_CACHE_MAX,
                "cache_hit_pct": round(chit / (api + chit) * 100) if (api + chit) else 0,
                "kb_hit_pct":    round(kb_h / kb_q * 100) if kb_q else 0,
                "uptime_s":      uptime_s,
            }
        except ImportError:
            return {"error": "skill_manager 未加载，请确认 bot 正在运行"}

    # ---------- 隐式反馈接收（bot 内部调用） ----------
    @app.post("/api/kb/implicit-feedback")
    async def api_kb_implicit_feedback(request: Request):
        """bot 检测到用户隐式情绪信号后调用，自动记录反馈"""
        data = await request.json()
        fb_id = _kb_store.add_feedback({
            "user_message":  data.get("user_message", ""),
            "ai_reply":      data.get("ai_reply", ""),
            "score":         int(data.get("score", 0)),
            "correction":    data.get("correction", ""),
            "operator":      data.get("operator", "auto_detection"),
        })
        return {"id": fb_id, "ok": True}


    # ═══════════════════════════════════════════════════════════════════
    # Phase 5: 向量化 / 查重 / 知识库备份管理
    # ═══════════════════════════════════════════════════════════════════

    _embed_progress: dict = {
        "running": False, "total": 0, "done": 0, "failed": 0, "msg": ""
    }
    _kb_backup_dir = cfg_dir / "kb_backups"

    async def _call_embed_api(texts: List[str]) -> List[List[float]]:
        """
        调用智能体 Embedding API，批量返回向量列表。
        模型优先从 ai.embedding_model 读取，默认 text-embedding-v2。
        """
        import httpx as _httpx
        ai_cfg = config_manager.config.get("ai", {})
        api_key = ai_cfg.get("api_key", "")
        base_url = (ai_cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
        model = ai_cfg.get("embedding_model", "text-embedding-v2")
        if not api_key or not texts:
            return []
        try:
            async with _httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{base_url}/embeddings",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model, "input": texts},
                )
                data = resp.json()
                items = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in items]
        except Exception as _e:
            logger.warning("Embedding API 调用失败: %s", _e)
            return []

    def _build_embed_text(entry: dict) -> str:
        """将条目字段拼接成用于向量化的文本（字段权重体现在顺序与重复）"""
        triggers = entry.get("triggers", "[]")
        if isinstance(triggers, str):
            try:
                triggers = " ".join(json.loads(triggers))
            except Exception:
                pass
        parts = [
            str(triggers) * 2,              # 触发词权重加倍
            entry.get("title", "") * 1,
            entry.get("scenario", ""),
            entry.get("steps", ""),
            entry.get("example_reply_zh", ""),
        ]
        return " ".join(p for p in parts if p).strip()[:800]  # 截断防超 token

    async def _run_embed_all():
        """后台任务：增量向量化所有未处理条目（批量 20 条/次）"""
        pending = _kb_store.get_entries_without_embedding()
        _embed_progress.update({
            "running": True, "total": len(pending),
            "done": 0, "failed": 0, "msg": f"开始向量化 {len(pending)} 条…"
        })
        batch_size = 20
        for i in range(0, len(pending), batch_size):
            batch = pending[i: i + batch_size]
            texts  = [_build_embed_text(e) for e in batch]
            vectors = await _call_embed_api(texts)
            if not vectors or len(vectors) != len(batch):
                _embed_progress["failed"] += len(batch)
                _embed_progress["msg"] = f"第 {i} 批 Embedding API 调用失败"
            else:
                for entry, vec in zip(batch, vectors):
                    _kb_store.set_single_embedding(entry["id"], vec)
                _embed_progress["done"] += len(batch)
                _embed_progress["msg"] = (
                    f"已完成 {_embed_progress['done']}/{_embed_progress['total']}"
                )
        _embed_progress["running"] = False
        _embed_progress["msg"] = (
            f"完成！成功 {_embed_progress['done']} 条，"
            f"失败 {_embed_progress['failed']} 条"
        )

    from fastapi import BackgroundTasks

    @app.post("/api/kb/embed-all")
    async def api_kb_embed_all(request: Request, background_tasks: BackgroundTasks):
        _api_auth(request)
        if _embed_progress.get("running"):
            return {"ok": False, "msg": "向量化任务正在运行中"}
        pending_cnt = len(_kb_store.get_entries_without_embedding())
        if not pending_cnt:
            return {"ok": False, "msg": "所有条目已完成向量化，无需重新处理"}
        background_tasks.add_task(_run_embed_all)
        return {"ok": True, "pending": pending_cnt}

    @app.get("/api/kb/embed-progress")
    async def api_kb_embed_progress(request: Request):
        _api_auth(request)
        cov = _kb_store.embedding_coverage()
        return {**_embed_progress, "coverage": cov}

    @app.post("/api/kb/entries/{entry_id}/embed")
    async def api_kb_embed_single(request: Request, entry_id: str):
        _api_auth(request)
        entry = _kb_store.get_entry(entry_id)
        if not entry:
            raise HTTPException(status_code=404)
        text = _build_embed_text(entry)
        vecs = await _call_embed_api([text])
        if not vecs:
            return {"ok": False, "msg": "Embedding API 调用失败"}
        _kb_store.set_single_embedding(entry_id, vecs[0])
        return {"ok": True}

    @app.get("/api/kb/embed-coverage")
    async def api_kb_embed_coverage(request: Request):
        _api_auth(request)
        return _kb_store.embedding_coverage()

    # ---------- 查重 ----------
    @app.get("/api/kb/duplicates")
    async def api_kb_duplicates(request: Request, threshold: float = 0.85):
        _api_auth(request)
        pairs = _kb_store.find_duplicates(threshold=threshold)
        return {"pairs": pairs, "count": len(pairs), "threshold": threshold}

    # ---------- 知识库备份 ----------
    @app.post("/api/kb/backup")
    async def api_kb_backup(request: Request):
        _api_auth(request)
        path = _kb_store.backup(_kb_backup_dir)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_backup", path)
        return {"ok": True, "path": path}

    @app.get("/api/kb/backups")
    async def api_kb_backups(request: Request):
        _api_auth(request)
        return {"backups": _kb_store.list_backups(_kb_backup_dir)}

    @app.post("/api/kb/restore/{filename}")
    async def api_kb_restore(request: Request, filename: str):
        _api_auth(request)
        # 仅允许在 backup_dir 内的文件
        backup_path = _kb_backup_dir / filename
        if not backup_path.exists() or backup_path.parent != _kb_backup_dir:
            raise HTTPException(status_code=404)
        _kb_store.restore(backup_path)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_restore", filename)
        return {"ok": True}

    # 向外暴露 kb_store 供 skill_manager 调用
    app.state.kb_store = _kb_store

    # ═══════════════════════════════════════════════════════════════════
    # 每日自动学习 ── /api/learner/*
    # ═══════════════════════════════════════════════════════════════════
    from src.utils.daily_learner import DailyLearner

    def _get_learner() -> Optional[DailyLearner]:
        if hasattr(app.state, "_daily_learner"):
            return app.state._daily_learner
        ai = getattr(telegram_client, "ai_client", None) if telegram_client else None
        if not ai:
            return None
        learner = DailyLearner(_kb_store, ai, db_path=_kb_db_path)
        app.state._daily_learner = learner
        return learner

    @app.get("/api/learner/stats")
    async def api_learner_stats(request: Request, _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            return {"error": "AI client not available"}
        return learner.stats()

    @app.post("/api/learner/run")
    async def api_learner_run(request: Request, _=Depends(_api_auth)):
        """手动触发一次学习"""
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "AI client not available")
        domain_ctx = ""
        cfg_obj = config_manager.config if hasattr(config_manager, 'config') else {}
        domain_name = effective_domain_name(cfg_obj if isinstance(cfg_obj, dict) else {})
        if domain_name:
            domain_ctx = f"当前行业: {domain_name}"
        result = await learner.run_daily_learn(domain_context=domain_ctx)
        actor = request.session.get("username", "system")
        if audit_store:
            audit_store.log(actor, "learner_run", json.dumps(result))
        return result

    @app.get("/api/learner/drafts")
    async def api_learner_drafts(request: Request, _=Depends(_api_auth),
                                 status: str = Query("pending"),
                                 sort: str = Query("priority")):
        learner = _get_learner()
        if not learner:
            return {"drafts": []}
        return {"drafts": learner.list_drafts(status=status, sort=sort)}

    @app.get("/api/learner/drafts/{draft_id}")
    async def api_learner_draft_detail(request: Request, draft_id: str,
                                       _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        draft = learner.get_draft(draft_id)
        if not draft:
            raise HTTPException(404, "draft not found")
        return draft

    @app.put("/api/learner/drafts/{draft_id}")
    async def api_learner_draft_update(request: Request, draft_id: str,
                                        _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        body = await request.json()
        learner.update_draft(draft_id, body)
        return {"ok": True}

    @app.post("/api/learner/drafts/{draft_id}/approve")
    async def api_learner_draft_approve(request: Request, draft_id: str,
                                         _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        actor = request.session.get("username", "web_admin")
        entry_id = learner.approve_draft(draft_id, operator=actor)
        if not entry_id:
            raise HTTPException(400, "draft cannot be approved")
        if audit_store:
            audit_store.log(actor, "learner_approve", f"{draft_id} -> {entry_id}")
        return {"ok": True, "entry_id": entry_id}

    @app.post("/api/learner/drafts/{draft_id}/reject")
    async def api_learner_draft_reject(request: Request, draft_id: str,
                                        _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        actor = request.session.get("username", "web_admin")
        learner.reject_draft(draft_id, operator=actor)
        if audit_store:
            audit_store.log(actor, "learner_reject", draft_id)
        return {"ok": True}

    @app.post("/api/learner/drafts/approve-all")
    async def api_learner_approve_all(request: Request, _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        actor = request.session.get("username", "web_admin")
        count = learner.approve_all_pending(operator=actor)
        if audit_store:
            audit_store.log(actor, "learner_approve_all", str(count))
        return {"ok": True, "approved": count}

    @app.post("/api/learner/drafts/batch-action")
    async def api_learner_batch_action(request: Request, _=Depends(_api_auth)):
        """A2: Batch approve/reject selected drafts."""
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        body = await request.json()
        ids = body.get("ids", [])
        action = body.get("action", "")
        if not ids or action not in ("approve", "reject"):
            raise HTTPException(400, "ids[] and action (approve|reject) required")
        actor = request.session.get("username", "web_admin")
        result = learner.batch_action(ids, action, operator=actor)
        if audit_store:
            audit_store.log(actor, f"learner_batch_{action}",
                            f"{len(ids)} ids -> {result}")
        return {"ok": True, **result}

    # ── A3: Duplicate recheck ─────────────────────────────────
    @app.post("/api/learner/drafts/{draft_id}/recheck-dup")
    async def api_learner_recheck_dup(request: Request, draft_id: str,
                                       _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        dup = learner.recheck_duplicate(draft_id)
        return {"ok": True, "dup": dup}

    # ── Persona Management API ──────────────────────────────

    @app.get("/api/persona")
    async def api_persona_get(request: Request, chat_id: str = "",
                               _=Depends(_api_auth)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona(chat_id)
        return {
            "persona": persona,
            "chat_id": chat_id,
            "is_default": chat_id == "" or str(chat_id) not in pm._chat_personas,
        }

    @app.get("/api/persona/bindings")
    async def api_persona_bindings(request: Request, _=Depends(_api_auth)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        return {"bindings": pm.get_all_chat_bindings()}

    @app.post("/api/persona/bind")
    async def api_persona_bind(request: Request, _=Depends(_api_auth)):
        data = await request.json()
        chat_id = data.get("chat_id")
        persona_data = data.get("persona")
        if not chat_id or not persona_data:
            raise HTTPException(400, "chat_id and persona required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.bind_chat_persona(str(chat_id), persona_data)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "persona_bind", f"chat={chat_id} name={persona_data.get('name', '?')}")
        return {"ok": True}

    @app.post("/api/persona/unbind")
    async def api_persona_unbind(request: Request, _=Depends(_api_auth)):
        data = await request.json()
        chat_id = data.get("chat_id")
        if not chat_id:
            raise HTTPException(400, "chat_id required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.unbind_chat_persona(str(chat_id))
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "persona_unbind", f"chat={chat_id}")
        return {"ok": True}

    @app.post("/api/persona/update-default")
    async def api_persona_update_default(request: Request, _=Depends(_api_auth)):
        data = await request.json()
        persona_data = data.get("persona")
        if not persona_data:
            raise HTTPException(400, "persona data required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(persona_data)
        try:
            cm = getattr(request.app.state, "config_manager", None)
            if cm:
                pm.persist_default_persona(persona_data, cm)
        except Exception as _pe:
            logger.warning("persona persist: %s", _pe)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "persona_update_default", f"name={persona_data.get('name', '?')}")
        return {"ok": True}

    @app.get("/api/persona/preview-prompt")
    async def api_persona_preview_prompt(request: Request, chat_id: str = "",
                                          _=Depends(_api_auth)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        prompt = pm.build_system_prompt(chat_id=chat_id)
        return {"prompt": prompt, "chat_id": chat_id}

    # ── KB Import API ────────────────────────────────────────

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

    @app.get("/api/episodic-memory")
    async def api_episodic_memory_list(request: Request, prefix: str = "", limit: int = 100):
        """情景记忆条目列表（memory_key = 私聊用户 id 或 群id_用户id）。"""
        _api_auth(request)
        if not telegram_client or not getattr(telegram_client, "skill_manager", None):
            raise HTTPException(status_code=503, detail="Bot 未就绪或未注入 SkillManager")
        sm = telegram_client.skill_manager
        lim = max(1, min(int(limit or 100), 500))
        rows = sm.episodic_list_for_admin(prefix=prefix[:120], limit=lim)
        return {"ok": True, "items": rows, "count": len(rows)}

    @app.delete("/api/episodic-memory/{row_id}")
    async def api_episodic_memory_delete(request: Request, row_id: int):
        _api_write("episodic_memory")(request)
        if not telegram_client or not getattr(telegram_client, "skill_manager", None):
            raise HTTPException(status_code=503, detail="Bot 未就绪")
        ok = telegram_client.skill_manager.episodic_delete_for_admin(int(row_id))
        if not ok:
            raise HTTPException(status_code=404, detail="记录不存在或记忆未启用")
        return {"ok": True, "deleted": int(row_id)}

    @app.post("/api/episodic-memory/backfill")
    async def api_episodic_memory_backfill(
        request: Request, limit: int = 20, prefix: str = ""
    ):
        """为缺失向量的情景记忆行补全 embedding（限流：单次最多 100 条；可选 prefix 筛选 memory_key）。"""
        _api_write("episodic_memory")(request)
        if not telegram_client or not getattr(telegram_client, "skill_manager", None):
            raise HTTPException(status_code=503, detail="Bot 未就绪")
        sm = telegram_client.skill_manager
        lim = max(1, min(int(limit or 20), 100))
        pre = (prefix or "")[:120]
        out = await sm.episodic_backfill_embeddings(lim, memory_key_prefix=pre)
        if out.get("ok") is False:
            err = str(out.get("error") or "")
            if err == "vector_disabled":
                raise HTTPException(
                    status_code=400, detail="情景记忆向量功能未启用（memory.vector.enabled）"
                )
            if err == "daily_embed_budget_exceeded":
                raise HTTPException(
                    status_code=429,
                    detail="本日情景记忆补全嵌入预算已用尽（memory.vector.daily_embed_budget）",
                )
            if err == "no_store":
                raise HTTPException(
                    status_code=503, detail="情景记忆或 AI 客户端不可用"
                )
            raise HTTPException(status_code=400, detail=err or "backfill_failed")
        return out

    # ── S5: CrossPlatformIdentity API ─────────────────────────────────────────

    def _get_cpi():
        """Return CPI instance from SkillManager or None."""
        sm = getattr(telegram_client, "skill_manager", None) if telegram_client else None
        return getattr(sm, "_cpi", None) if sm else None

    @app.get("/api/identity")
    async def api_identity_list(request: Request, limit: int = 200):
        """List all (platform, platform_uid, canonical_id) rows."""
        _api_auth(request)
        cpi = _get_cpi()
        if not cpi:
            raise HTTPException(status_code=503, detail="CrossPlatformIdentity 未就绪")
        rows = cpi.list_all(limit=min(int(limit), 500))
        return {"ok": True, "items": [
            {"platform": r[0], "platform_uid": r[1], "canonical_id": r[2], "created_at": r[3]}
            for r in rows
        ]}

    @app.post("/api/identity/link")
    async def api_identity_link(request: Request):
        """Link two platform UIDs to share the same episodic memory.
        Body: {platform_a, uid_a, platform_b, uid_b}"""
        _api_write("identity")(request)
        cpi = _get_cpi()
        if not cpi:
            raise HTTPException(status_code=503, detail="CrossPlatformIdentity 未就绪")
        body = await request.json()
        pa, ua = str(body.get("platform_a", "")), str(body.get("uid_a", ""))
        pb, ub = str(body.get("platform_b", "")), str(body.get("uid_b", ""))
        if not all([pa, ua, pb, ub]):
            raise HTTPException(status_code=400, detail="需要 platform_a/uid_a/platform_b/uid_b")
        canon = cpi.link(pa, ua, pb, ub)
        return {"ok": True, "canonical_id": canon}

    @app.post("/api/identity/unlink")
    async def api_identity_unlink(request: Request):
        """Detach a platform UID back to its own canonical_id.
        Body: {platform, uid}"""
        _api_write("identity")(request)
        cpi = _get_cpi()
        if not cpi:
            raise HTTPException(status_code=503, detail="CrossPlatformIdentity 未就绪")
        body = await request.json()
        plat, uid = str(body.get("platform", "")), str(body.get("uid", ""))
        if not plat or not uid:
            raise HTTPException(status_code=400, detail="需要 platform 和 uid")
        new_canon = cpi.unlink(plat, uid)
        return {"ok": True, "canonical_id": new_canon}

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


